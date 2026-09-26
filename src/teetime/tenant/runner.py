"""Tenant job entry points: the per-release-event booking runner and the tenant watcher
(MULTIUSER_PLAN §4, §7).

Booking runner (``teetime tenant-run --event <key> --wait``), in exact order (§4.2):
DST gate (pure) -> READ #1 ``load_event_rows`` -> WRITE #1 ``claim_rows`` -> dispose DB engine ->
decrypt creds in process -> site-key pre-flight (once per course) -> build per-account adapters
sharing one ``SharedCaptchaPool`` per course -> ``allocate_blind_slots`` + ``set_blind_allowlist``
-> register pool demand -> ``asyncio.gather`` of one UNMODIFIED ``core.orchestrator.Orchestrator``
per account (per-account exception isolation) -> WRITE #2 ``record_outcomes`` STREAMED one row at
a time from T0 + ``post_burst_quiet_s`` -> emails -> exit code. NO store call and NO decrypt in
[T0 - lead - 1 s, T0 + ``post_burst_quiet_s``) (``test_runner_no_store_calls_inside_race_window``);
a self-deadline (start + replicaTimeout - 90 s) writes still-running rows ``needs_reconcile``.

MU-9a (the runner core: ``run_release_event``, ``resolve_credentials``,
``assert_blind_methods_present``) is IMPLEMENTED and UNWIRED — nothing on the production path
calls it. Still stubs: ``exit_code_for`` + the CLI + emails (MU-9b). ``LeasedBookingStore``
(MU-9c, ``tenant.store``) is for the watcher/web only. The tenant watcher (MU-10b) lives in
``tenant.watch_runner``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import IntEnum
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..core.adapter import AuthError, CaptchaError, CourseAdapter
from ..core.clock import Clock
from ..core.config import SchedulerConfig
from ..core.dst_gate import should_proceed
from ..core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    Player,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from ..core.orchestrator import Orchestrator
from ..core.redaction import register_secret_literals
from ..core.release_policy import ReleasePolicy, target_date_for
from ..courses.foreup.token_pool import LeaseKey, SharedCaptchaPool
from ..notifications.notifier import NoopNotifier
from ..persistence.in_memory_store import InMemoryStore
from .allocation import allocate_blind_slots, draft_order
from .crypto import Keyring, credential_aad, decrypt_password
from .models import (
    Actor,
    BookingSource,
    BookingState,
    CourseAccount,
    CourseAccountId,
    EventRow,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    RowId,
    RowStatus,
    row_is_frozen,
)
from .notify import UserNotifier
from .recording import (
    BlindCapableRecordingAdapter,
    RecordedBook,
    RecordingAdapter,
    RecordingLog,
    make_recording_adapter,
)
from .store import RowOutcome, TenantStore

_MU9 = "MULTIUSER_PLAN.md MU-9a"
_MU9B = "MULTIUSER_PLAN.md MU-9b"

log = logging.getLogger(__name__)

# The BlindPostCapable members the orchestrator calls through a bare ``cast`` (§4.6 SF1).
_BLIND_METHODS = ("synthesize_blind_slots", "captcha_pool_size")

# bookingReplicaTimeout (compute.bicep): the booker's claim lease runs until T0 + this (§3.5).
BOOKING_REPLICA_TIMEOUT_S = 1200.0
# The self-deadline stops the run this long BEFORE the replica timeout, so unfinished rows are
# written needs_reconcile before ACA kills the replica (§4.2, SF5).
SELF_DEADLINE_MARGIN_S = 90.0


@dataclass(frozen=True, slots=True)
class ReleaseEvent:
    """One distinct (timezone, release_time) and the hosted courses that share it (§6.2).
    Mirrors one entry of ``infra/bicep/release_events.json``; parity-tested (MU-15a)."""

    key: str  # <= 10 chars so derived ACA job names stay <= 32 chars
    timezone: str
    release_time: time
    course_ids: tuple[CourseId, ...]


class AdapterFactory(Protocol):
    """Builds ONE adapter per account (own httpx client, cookies, login cache, JWT). ForeUP
    adapters of the same course receive the same ``pool``; ``None`` -> private pool (today).
    ``lease_key`` is the account's key in that pool (the booking runner passes the row id, for
    ``ForeUpAdapter(captcha_pool=pool, captcha_lease_key=lease_key)``); it is ``None`` exactly
    when ``pool`` is (a throwaway web probe, or a course with no shared pool)."""

    def __call__(
        self,
        *,
        course_id: CourseId,
        account: CourseAccount,
        pool: SharedCaptchaPool | None,
        lease_key: LeaseKey | None,
        dry_run: bool,
    ) -> CourseAdapter: ...


PoolFactory = Callable[[CourseId], SharedCaptchaPool | None]
"""Builds a course's shared CAPTCHA pool (MU-9b: after the once-per-course site-key pre-flight,
bound to that course's page URL + site key), or ``None`` for a course without one (fakes)."""


class ExitStatus(IntEnum):
    """Process exit codes (§4.5). Per-user outcomes (a miss, a user's bad password) are NOT
    failures of the job; systemic causes are."""

    OK = 0
    SYSTEMIC_FAILURE = 1


@dataclass(frozen=True, slots=True)
class AccountOutcome:
    """One row's result after the burst. ``error`` holds the class name of a captured
    exception (never its message, which could carry PII) when ``outcome`` is None."""

    row_id: RowId
    outcome: BookingOutcome | None
    error: str | None
    uncertain: bool
    decrypt_failed: bool
    search_only: bool
    # From the recording decorator (§4.6, M1): surplus reservations whose in-run cancel failed
    # (ledgered held_extra so the watcher collapses them), and a Captcha/OTP error the blind burst
    # swallowed (makes the exit non-zero).
    held_extra_raw_ids: tuple[str, ...] = ()
    swallowed_captcha_error: bool = False


@dataclass(frozen=True, slots=True)
class RunReport:
    event_key: str
    rows_loaded: int
    rows_claimed: int
    outcomes: tuple[AccountOutcome, ...]
    systemic_error: str | None
    summary_email_failed: bool = False  # SF6: a failed operator summary makes the exit non-zero
    self_deadline_hit: bool = False  # SF5
    # Rows whose WRITE #2 was refused or failed after retries (§4.5: non-zero exit, MU-9b). Their
    # outcome JSON was printed to stdout and the watcher reconciles them from live.
    outcome_write_failures: tuple[RowId, ...] = ()


@dataclass(frozen=True, slots=True)
class WatchReport:
    """One tenant-watcher run (§7.1, MU-10b ``tenant.watch_runner.run_tenant_watch``). The exit
    status is ``watch_runner.watch_exit_status`` (§7.9)."""

    rows_loaded: int
    searches: int
    logins: int
    booked: tuple[RowId, ...]
    upgraded: tuple[RowId, ...]
    lost: tuple[RowId, ...]
    rate_limited: bool
    systemic_error: str | None
    adopted: tuple[RowId, ...] = ()
    cancelled_external: tuple[RowId, ...] = ()
    # BOOKED -> PENDING + needs_reconcile (a bot-caused loss: failed upgrade rebook / vanish M2).
    reconcile_flagged: tuple[RowId, ...] = ()
    uncertain: tuple[RowId, ...] = ()  # a book() whose POST may have landed (needs_reconcile)
    skipped_leased: tuple[RowId, ...] = ()  # another writer's lease, or the row moved (M5)
    auth_failed_accounts: tuple[CourseAccountId, ...] = ()
    decrypt_failures: tuple[CourseAccountId, ...] = ()
    captcha_error: bool = False
    outcome_write_failures: tuple[RowId, ...] = ()
    # Owned (held / held_extra) ledger ids on a watched date whose row is not BOOKED (§7.6).
    orphans: tuple[str, ...] = ()


def exit_code_for(report: RunReport | WatchReport) -> ExitStatus:
    """Map a report to the §4.5 / §7.9 exit contract: non-zero only for systemic causes (DB,
    keyring, any decrypt failure, any CaptchaError/OtpChallengeError, any UNCERTAIN, a failed
    outcome write). A missed drop and a per-account AuthError exit 0."""
    raise NotImplementedError(_MU9B)


async def run_release_event(
    *,
    event: ReleaseEvent,
    policies: Mapping[CourseId, ReleasePolicy],
    store: TenantStore,
    clock: Clock,
    scheduler: SchedulerConfig,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    notifier: UserNotifier,
    dry_run: bool,
    wait: bool,
    post_burst_quiet_s: float = 10.0,
    pool_factory: PoolFactory | None = None,
    replica_timeout_s: float = BOOKING_REPLICA_TIMEOUT_S,
    attempt_id: str | None = None,
) -> RunReport:
    """The tenant booking job (§4). ``scheduler`` supplies the race knobs
    (``early_arrival_ms``, ``blind_post_stagger_ms``, ``blind_post_max_count``, lead, ...). The
    runner derives per-account copies (reserve -> 0 because the pool holds it; overflow accounts
    -> ``blind_post_max_count=0``, still registered with the pool at k=0) and passes
    ``fire_time``/``timezone`` from ``event``. Each account's adapter is wrapped by
    ``tenant.recording.make_recording_adapter``. Outcomes are STREAMED: one TenantStore write per
    row, each in its own batch, starting at T0 + ``post_burst_quiet_s``. A self-deadline marks
    unfinished rows ``needs_reconcile`` before ``replicaTimeout`` (round-1 M4/SF5).

    ``wait=True`` is the cron path: the DST gate runs first (a wrong-season cron returns an
    empty report before ANY store call) and a row leased by another writer is re-claimed every
    15 s until the give-up point. ``wait=False`` (manual) skips both; the orchestrators still
    busy-wait to today's release instant (firing at once if it has passed).

    Not done here (MU-9b): the exit-code mapping (``exit_code_for``), per-user / operator emails
    through ``notifier``, the account ``auth_failed`` flip, and the CLI (which measures the NTP
    offset, builds the pools after the site-key pre-flight, and closes the Cosmos client after
    the claim). ``LeasedBookingStore`` is MU-9c; the booker never needs it (its lease is held
    from the claim)."""
    started = clock.now_utc()
    if wait and not should_proceed(clock, timezone=event.timezone, fire_time=event.release_time):
        log.info(
            "tenant-run %s: wrong-season cron (DST gate) — exiting before any store call",
            event.key,
        )
        return _report(event)
    t0 = _event_t0(event, started)
    owner = attempt_id or f"booker:{event.key}:{uuid4().hex[:12]}"
    phase = await _read_and_claim(
        event,
        policies=policies,
        store=store,
        clock=clock,
        started=started,
        t0=t0,
        owner=owner,
        until=t0 + timedelta(seconds=replica_timeout_s),
        give_up_at=_claim_give_up_at(t0, scheduler) if wait else started,
    )
    if isinstance(phase, RunReport):
        return phase
    loaded, claimed, claimed_rows = phase
    creds, decrypt_failed = resolve_credentials(claimed_rows, keyring=keyring)
    runnable = [r for r in claimed_rows if r.row.id in creds]
    pools: dict[CourseId, SharedCaptchaPool | None] = {}
    try:
        for cid in sorted({r.row.course_id for r in runnable}):
            pools[cid] = pool_factory(cid) if pool_factory is not None else None
        accounts = await _prepare_accounts(
            runnable,
            creds,
            event=event,
            scheduler=scheduler,
            clock=clock,
            adapter_factory=adapter_factory,
            pools=pools,
            dry_run=dry_run,
            t0=t0,
        )
    except Exception as exc:
        # A systemic pre-T0 failure (e.g. round-2 SF1): nothing raced, so hand the rows back.
        await _release_leases(store, claimed, owner=owner)
        await _close_pools(pools)
        return _systemic(event, "prepare", exc, rows_loaded=loaded, rows_claimed=len(claimed))
    finished, write_failures, deadline_hit = await _race(
        accounts,
        [r for r in claimed_rows if r.row.id in decrypt_failed],
        store=store,
        clock=clock,
        write_from=t0 + timedelta(seconds=post_burst_quiet_s),
        deadline=started + timedelta(seconds=replica_timeout_s - SELF_DEADLINE_MARGIN_S),
        owner=owner,
    )
    await _close_adapters(accounts)
    await _close_pools(pools)
    return _report(
        event,
        rows_loaded=loaded,
        rows_claimed=len(claimed),
        outcomes=tuple(f.account for f in finished),
        self_deadline_hit=deadline_hit,
        outcome_write_failures=tuple(write_failures),
    )


def assert_blind_methods_present(adapters: Sequence[CourseAdapter]) -> None:
    """Pre-T0 guard (round-2 SF1): every adapter with ``capabilities.blind_post`` must expose
    ``synthesize_blind_slots`` and ``captcha_pool_size`` via ``inspect.getattr_static`` (the
    orchestrator only ``cast``s). Raises ``TypeError`` -> systemic non-zero exit at ~05:51, never
    a silent T0 AttributeError."""
    for adapter in adapters:
        if not adapter.capabilities.blind_post:
            continue
        missing = [
            name for name in _BLIND_METHODS if inspect.getattr_static(adapter, name, None) is None
        ]
        if missing:
            raise TypeError(
                f"{type(adapter).__name__} reports capabilities.blind_post=True but lacks "
                f"{', '.join(missing)}: the orchestrator would fail silently at the pre-warm "
                "and fatally at T0"
            )


def resolve_credentials(
    rows: Sequence[EventRow], *, keyring: Keyring
) -> tuple[dict[RowId, CourseCredentials], frozenset[RowId]]:
    """Decrypt each account's password in process and register it with the log filter (E7,
    ``register_secret_literals``) BEFORE it is used anywhere. Returns (creds by row, rows whose
    decrypt failed). Never raises per row (§4.5): a failed row is logged by id and class name
    only (never the blob or any key material) and skipped."""
    creds: dict[RowId, CourseCredentials] = {}
    failed: set[RowId] = set()
    for event_row in rows:
        account = event_row.account
        try:
            password = decrypt_password(
                keyring, account.password_ciphertext, aad=credential_aad(account)
            )
        except Exception as exc:  # per-row isolation: one bad blob never blocks the others
            log.error(
                "tenant-run: row %s: credential decrypt failed (%s); row skipped",
                event_row.row.id,
                type(exc).__name__,
            )
            failed.add(event_row.row.id)
            continue
        if register_secret_literals([password]) == 0:
            log.warning(
                "tenant-run: row %s: decrypted password is shorter than the log-mask floor and "
                "will NOT be masked in logs",
                event_row.row.id,
            )
        creds[event_row.row.id] = CourseCredentials(username=account.username, password=password)
    return creds, frozenset(failed)


# --- booking runner internals (MU-9a) ------------------------------------------------------

# A row leased by another writer (a watcher mid-act) is re-claimed on this cadence until T0 -
# 150 s, then skipped with a WARNING (the watcher cannot book today+7 before the drop, §4.2).
_CLAIM_RETRY_S = 15.0
_CLAIM_GIVE_UP_BEFORE_T0_S = 150.0
# WRITE #2: each row's write retries for up to this long (§4.2).
_WRITE_RETRY_WINDOW_S = 60.0
_WRITE_RETRY_STEP_S = 5.0
# RequestRow has no holes field; every hosted course books 18 (the TOML default, AppConfig).
_TENANT_HOLES = 18
# "max_count = grid size" for the allocator's UNFILTERED candidate lists (§5.4 Input).
_UNFILTERED_MAX_COUNT = 10_000
# C when a course has no shared pool (fakes): the live-proven default (§5.3).
_DEFAULT_CAPTCHA_CONCURRENCY = 12
# Players are not stored (§3.1): ForeUP's POST sends only the count.
_GUEST = Player(first_name="Guest", last_name="Player", email="")
# Operator-action errors: loud (MU-9b exit contract) but NOT uncertain — no POST landed.
_CONTRACT_ERRORS: tuple[type[Exception], ...] = (AuthError, CaptchaError)
_BOOKED_OUTCOMES = frozenset({BookingOutcome.BOOKED, BookingOutcome.ALREADY_BOOKED})


class SelfDeadlineReachedError(Exception):
    """Marks an account the self-deadline stopped mid-run (never raised by an adapter)."""


@dataclass(slots=True)
class _Account:
    """Everything the runner holds for one claimed, decrypted row."""

    event_row: EventRow
    request: BookingRequest
    creds: CourseCredentials
    recorder: RecordingAdapter
    lease_key: LeaseKey
    pool: SharedCaptchaPool | None
    allowlist: frozenset[SlotId] | None = None
    search_only: bool = False
    orchestrator: Orchestrator | None = None

    @property
    def row(self) -> RequestRow:
        return self.event_row.row


@dataclass(frozen=True, slots=True)
class _Finished:
    account: AccountOutcome
    row: RowOutcome


def _report(
    event: ReleaseEvent,
    *,
    rows_loaded: int = 0,
    rows_claimed: int = 0,
    outcomes: tuple[AccountOutcome, ...] = (),
    systemic_error: str | None = None,
    self_deadline_hit: bool = False,
    outcome_write_failures: tuple[RowId, ...] = (),
) -> RunReport:
    return RunReport(
        event_key=event.key,
        rows_loaded=rows_loaded,
        rows_claimed=rows_claimed,
        outcomes=outcomes,
        systemic_error=systemic_error,
        self_deadline_hit=self_deadline_hit,
        outcome_write_failures=outcome_write_failures,
    )


def _systemic(
    event: ReleaseEvent,
    stage: str,
    exc: Exception,
    *,
    rows_loaded: int = 0,
    rows_claimed: int = 0,
) -> RunReport:
    # Class name only: a store/driver message can carry connection strings or row content.
    log.critical("tenant-run %s: systemic failure at %s (%s)", event.key, stage, type(exc).__name__)
    return _report(
        event,
        rows_loaded=rows_loaded,
        rows_claimed=rows_claimed,
        systemic_error=f"{stage}: {type(exc).__name__}",
    )


def _event_t0(event: ReleaseEvent, now: datetime) -> datetime:
    """Today's release instant in the event zone — the same T0 ``Orchestrator._compute_t0``
    derives from the per-account scheduler (fire_time/timezone copied from ``event``)."""
    zone = ZoneInfo(event.timezone)
    local = now.astimezone(zone)
    return datetime.combine(local.date(), event.release_time, tzinfo=zone).astimezone(UTC)


async def _read_and_claim(
    event: ReleaseEvent,
    *,
    policies: Mapping[CourseId, ReleasePolicy],
    store: TenantStore,
    clock: Clock,
    started: datetime,
    t0: datetime,
    owner: str,
    until: datetime,
    give_up_at: datetime,
) -> RunReport | tuple[int, frozenset[RowId], list[EventRow]]:
    """READ #1 + WRITE #1 (§4.2): the ONLY store calls before T0 + quiet. Returns a finished
    report (no rows / nothing claimed / a systemic store failure) or (rows loaded, claimed ids,
    the claimed rows)."""
    targets = {cid: target_date_for(policies[cid], started) for cid in event.course_ids}
    try:
        loaded = await store.load_event_rows(targets=targets, now=started)
    except Exception as exc:
        return _systemic(event, "load_event_rows", exc)
    # Python re-checks the derived freeze (§4.2): the query filtered cutoff_at > now already.
    rows = [r for r in loaded if not row_is_frozen(r.row, now=started)]
    if not rows:
        log.info("tenant-run %s: no pending rows for %s", event.key, targets)
        return _report(event, rows_loaded=len(loaded))
    try:
        claimed = await _claim(
            store,
            [r.row.id for r in rows],
            owner=owner,
            until=until,
            clock=clock,
            give_up_at=give_up_at,
        )
    except Exception as exc:
        return _systemic(event, "claim_rows", exc, rows_loaded=len(loaded))
    claimed_rows = [r for r in rows if r.row.id in claimed]
    if not claimed_rows:
        return _report(event, rows_loaded=len(loaded))
    log.info(
        "tenant-run %s: %d row(s) loaded, %d claimed as %s until %s",
        event.key,
        len(loaded),
        len(claimed),
        owner,
        until.isoformat(),
    )
    return len(loaded), claimed, claimed_rows


def _claim_give_up_at(t0: datetime, scheduler: SchedulerConfig) -> datetime:
    """Last instant a claim retry may run: T0 - 150 s, and never inside the race window."""
    return min(
        t0 - timedelta(seconds=_CLAIM_GIVE_UP_BEFORE_T0_S),
        t0 - timedelta(seconds=scheduler.captcha_prefetch_lead_s + 5),
    )


async def _claim(
    store: TenantStore,
    row_ids: Sequence[RowId],
    *,
    owner: str,
    until: datetime,
    clock: Clock,
    give_up_at: datetime,
) -> frozenset[RowId]:
    """WRITE #1 (§4.2), re-trying rows another writer holds until ``give_up_at``."""
    claimed = set(await store.claim_rows(row_ids, owner=owner, until=until, now=clock.now_utc()))
    while pending := [r for r in row_ids if r not in claimed]:
        if clock.now_utc() + timedelta(seconds=_CLAIM_RETRY_S) > give_up_at:
            log.warning(
                "tenant-run: %d row(s) still leased by another writer at the claim give-up; "
                "skipped this drop: %s",
                len(pending),
                [str(r) for r in pending],
            )
            break
        await clock.sleep(_CLAIM_RETRY_S)
        claimed |= await store.claim_rows(pending, owner=owner, until=until, now=clock.now_utc())
    return frozenset(claimed)


async def _release_leases(store: TenantStore, row_ids: frozenset[RowId], *, owner: str) -> None:
    for row_id in row_ids:
        try:
            await store.release_row_lease(row_id, owner=owner)
        except Exception as exc:  # best effort: the lease expires anyway
            log.warning("tenant-run: row %s: lease release failed (%s)", row_id, type(exc).__name__)


def _request_for(row: RequestRow, *, dry_run: bool) -> BookingRequest:
    return BookingRequest(
        request_id=row.request_id,
        target_dates=(row.target_date,),
        time_windows=(TimeWindow(earliest=row.window_earliest, latest=row.window_latest),),
        players=(_GUEST,) * row.party_size,
        course_preferences=(row.course_id,),
        holes=_TENANT_HOLES,
        dry_run=dry_run,
    )


async def _prepare_accounts(
    rows: Sequence[EventRow],
    creds: Mapping[RowId, CourseCredentials],
    *,
    event: ReleaseEvent,
    scheduler: SchedulerConfig,
    clock: Clock,
    adapter_factory: AdapterFactory,
    pools: Mapping[CourseId, SharedCaptchaPool | None],
    dry_run: bool,
    t0: datetime,
) -> list[_Account]:
    """Adapters (recorded), the SF1 guard, allocation, pool demand, and one UNMODIFIED
    ``Orchestrator`` per account — all pre-T0, all in memory. On any failure the adapters built
    so far are closed and the error propagates (systemic)."""
    accounts: list[_Account] = []
    try:
        for event_row in rows:
            row = event_row.row
            pool = pools.get(row.course_id)
            key = LeaseKey(str(row.id))
            inner = adapter_factory(
                course_id=row.course_id,
                account=event_row.account,
                pool=pool,
                lease_key=key if pool is not None else None,
                dry_run=dry_run,
            )
            accounts.append(
                _Account(
                    event_row=event_row,
                    request=_request_for(row, dry_run=dry_run),
                    creds=creds[row.id],
                    recorder=make_recording_adapter(inner, clock=clock),
                    lease_key=key,
                    pool=pool,
                )
            )
        assert_blind_methods_present([a.recorder for a in accounts])
        orders = _allocate(accounts, burst=scheduler.blind_post_max_count)
        _register_pools(
            accounts, orders, reserve=scheduler.blind_post_fallback_token_reserve, t0=t0
        )
    except BaseException:
        await _close_adapters(accounts)
        raise
    store = InMemoryStore()  # the engine's in-run memory, shared (distinct RequestIds per row)
    for account in accounts:
        account.orchestrator = Orchestrator(
            adapters={account.row.course_id: account.recorder},
            store=store,
            notifier=NoopNotifier(),  # nothing is sent during the race; results are returned
            clock=clock,
            scheduler=_account_scheduler(scheduler, event=event, account=account),
            creds={account.row.course_id: account.creds},
            prefetch_book=True,
        )
    return accounts


def _allocate(accounts: Sequence[_Account], *, burst: int) -> dict[CourseId, tuple[RowId, ...]]:
    """§5.4 per course: each blind account's UNFILTERED ranked candidates (its allowlist cleared
    first — MU-3 review follow-up), a snake draft over the week-rotated order, then
    ``set_blind_allowlist``. Accounts beyond ``C // burst`` (or drafting nothing) and
    non-blind accounts run the search race path. Returns each course's draft order."""
    orders: dict[CourseId, tuple[RowId, ...]] = {}
    by_course: dict[CourseId, list[_Account]] = {}
    for account in accounts:
        by_course.setdefault(account.row.course_id, []).append(account)
    for course_id, group in by_course.items():
        blind: dict[RowId, tuple[_Account, BlindCapableRecordingAdapter]] = {}
        for account in group:
            if isinstance(account.recorder, BlindCapableRecordingAdapter):
                blind[account.row.id] = (account, account.recorder)
            else:
                account.search_only = True
        if not blind:
            continue
        ranked: dict[RowId, list[TeeTimeSlot]] = {}
        for row_id, (account, recorder) in blind.items():
            recorder.set_blind_allowlist(None)
            ranked[row_id] = recorder.synthesize_blind_slots(
                account.request, account.row.target_date, max_count=_UNFILTERED_MAX_COUNT
            )
        first = next(iter(blind.values()))[0]
        order = draft_order(list(ranked), target_date=first.row.target_date)
        concurrency = (
            first.pool.max_concurrent_solves
            if first.pool is not None
            else _DEFAULT_CAPTCHA_CONCURRENCY
        )
        allocation = allocate_blind_slots(
            ranked,
            order=order,
            burst_size=burst,
            max_blind_rows=concurrency // burst if burst > 0 else 0,
        )
        for row_id, (account, recorder) in blind.items():
            account.allowlist = allocation.allowlists[row_id]
            account.search_only = row_id in allocation.search_only
            recorder.set_blind_allowlist(account.allowlist)
        orders[course_id] = allocation.order
        log.info(
            "tenant-run: course %s draft order %s; search-only this drop: %s",
            course_id,
            [str(r) for r in allocation.order],
            sorted(str(r) for r in allocation.search_only),
        )
    return orders


def _register_pools(
    accounts: Sequence[_Account],
    orders: Mapping[CourseId, tuple[RowId, ...]],
    *,
    reserve: int,
    t0: datetime,
) -> None:
    """Coordinated pool demand (§5.2/§5.3): EVERY account of a pooled course is registered, in
    draft order, with k = its allowlist size — k = 0 for over-cap / search-only / non-blind
    accounts, so their ``prepare_book(count=3)`` joins the one fill and solves nothing outside
    the C bound. Then the shared reserve R and ``arm(t0)``."""
    by_course: dict[CourseId, list[_Account]] = {}
    for account in accounts:
        if account.pool is not None:
            by_course.setdefault(account.row.course_id, []).append(account)
    for course_id, group in by_course.items():
        order = orders.get(course_id, ())
        rank = {row_id: i for i, row_id in enumerate(order)}
        group.sort(key=lambda a: (rank.get(a.row.id, len(order)), a.row.id))
        pool = group[0].pool
        assert pool is not None
        for account in group:
            demand = 0 if account.search_only or not account.allowlist else len(account.allowlist)
            pool.register(account.lease_key, demand)
        pool.set_reserve(reserve)
        pool.arm(t0=t0)


def _account_scheduler(
    scheduler: SchedulerConfig, *, event: ReleaseEvent, account: _Account
) -> SchedulerConfig:
    """S' (§4.2): the event's fire time + zone; reserve 0 when the pool holds R; burst 0 for a
    search-only account (the five-part blind gate then fails by construction, §5.3)."""
    update: dict[str, object] = {"timezone": event.timezone, "fire_time": event.release_time}
    if account.pool is not None:
        update["blind_post_fallback_token_reserve"] = 0
    if account.search_only:
        update["blind_post_max_count"] = 0
    return scheduler.model_copy(update=update)


async def _race(
    accounts: Sequence[_Account],
    decrypt_failed: Sequence[EventRow],
    *,
    store: TenantStore,
    clock: Clock,
    write_from: datetime,
    deadline: datetime,
    owner: str,
) -> tuple[list[_Finished], list[RowId], bool]:
    """Run every account concurrently, streaming each outcome to the writer as it returns."""
    queue: asyncio.Queue[_Finished | None] = asyncio.Queue()
    finished: list[_Finished] = []

    def emit(item: _Finished) -> None:
        finished.append(item)
        queue.put_nowait(item)

    for event_row in decrypt_failed:
        emit(_decrypt_failed(event_row, at=clock.now_utc(), owner=owner))
    writer = asyncio.create_task(
        _stream_outcomes(queue, store=store, clock=clock, start_at=write_from)
    )
    tasks = {
        a.row.id: asyncio.create_task(_run_account(a, emit=emit, clock=clock, owner=owner))
        for a in accounts
    }
    deadline_hit = await _await_accounts(list(tasks.values()), clock=clock, deadline=deadline)
    emitted = {item.account.row_id for item in finished}
    for account in accounts:
        if account.row.id in emitted:
            continue
        task = tasks[account.row.id]
        # Cancelled by the self-deadline, or died with a non-Exception BaseException.
        error = None if task.cancelled() else task.exception()
        emit(
            _finish(
                account,
                result=None,
                error=error or SelfDeadlineReachedError(),
                at=clock.now_utc(),
                owner=owner,
            )
        )
    queue.put_nowait(None)
    return finished, await writer, deadline_hit


async def _run_account(
    account: _Account, *, emit: Callable[[_Finished], None], clock: Clock, owner: str
) -> None:
    result: BookingResult | None = None
    error: Exception | None = None
    orchestrator = account.orchestrator
    assert orchestrator is not None
    try:
        result = await orchestrator.run(account.request)
    except Exception as exc:  # per-account isolation (§4.4 proof 4)
        error = exc
    finally:
        if account.pool is not None:
            account.pool.release(account.lease_key)  # leftovers serve other fallbacks
    emit(_finish(account, result=result, error=error, at=clock.now_utc(), owner=owner))


async def _await_accounts(
    tasks: Sequence[asyncio.Task[None]], *, clock: Clock, deadline: datetime
) -> bool:
    """Wait for every account or the self-deadline, whichever is first. On the deadline the
    unfinished accounts are cancelled; returns whether it was hit."""
    everything = asyncio.gather(*tasks, return_exceptions=True)
    delay = max((deadline - clock.now_utc()).total_seconds(), 0.0)
    timer = asyncio.ensure_future(clock.sleep(delay))
    waiters: list[asyncio.Future[Any]] = [everything, timer]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    if everything.done():
        return False
    unfinished = [t for t in tasks if not t.done()]
    log.critical(
        "tenant-run: self-deadline reached with %d account(s) still running; their rows are "
        "written needs_reconcile",
        len(unfinished),
    )
    for task in unfinished:
        task.cancel()
    await everything
    return True


async def _stream_outcomes(
    queue: asyncio.Queue[_Finished | None], *, store: TenantStore, clock: Clock, start_at: datetime
) -> list[RowId]:
    """WRITE #2 (§4.2): nothing before ``start_at`` (T0 + quiet), then one ``record_outcomes``
    call PER ROW, in completion order. Returns the rows whose write did not land."""
    delay = (start_at - clock.now_utc()).total_seconds()
    if delay > 0:
        await clock.sleep(delay)
    failures: list[RowId] = []
    while (item := await queue.get()) is not None:
        if not await _write_outcome(store, item.row, clock=clock):
            failures.append(item.row.row_id)
    return failures


async def _write_outcome(store: TenantStore, outcome: RowOutcome, *, clock: Clock) -> bool:
    give_up = clock.now_utc() + timedelta(seconds=_WRITE_RETRY_WINDOW_S)
    while True:
        try:
            await store.record_outcomes([outcome])
        except ExceptionGroup as group:
            # Refused (the row moved / lease lost): ledger entries were still written and the
            # date's active row flagged needs_reconcile (M4). Not retryable.
            log.critical(
                "tenant-run: row %s: outcome write REFUSED (%s)",
                outcome.row_id,
                [type(e).__name__ for e in group.exceptions],
            )
            _dump_outcome(outcome)
            return False
        except Exception as exc:
            if clock.now_utc() + timedelta(seconds=_WRITE_RETRY_STEP_S) > give_up:
                log.critical(
                    "tenant-run: row %s: outcome write FAILED after retries (%s)",
                    outcome.row_id,
                    type(exc).__name__,
                )
                _dump_outcome(outcome)
                return False
            log.warning(
                "tenant-run: row %s: outcome write failed (%s); retrying",
                outcome.row_id,
                type(exc).__name__,
            )
            await clock.sleep(_WRITE_RETRY_STEP_S)
        else:
            return True


def _dump_outcome(outcome: RowOutcome) -> None:
    """The outcome as one JSON line on stdout, so an unwritten result survives in the job log
    (ids, statuses, raw reservation ids and tee times only — no credentials, no PII)."""
    ledger = [*([outcome.booking] if outcome.booking else []), *outcome.held_extras]
    payload = {
        "tenant_run_unwritten_outcome": str(outcome.row_id),
        "course_account_id": str(outcome.course_account_id),
        "target_date": outcome.target_date.isoformat(),
        "to_status": outcome.to_status.value if outcome.to_status else None,
        "last_outcome": outcome.last_outcome,
        "needs_reconcile": outcome.needs_reconcile,
        "held": [[e.raw_reservation_id, e.state.value, e.tee_time.isoformat()] for e in ledger],
        "cancelled_extras": [e.raw_reservation_id for e in outcome.cancelled_extras],
    }
    print(json.dumps(payload), flush=True)


def _decrypt_failed(event_row: EventRow, *, at: datetime, owner: str) -> _Finished:
    row = event_row.row
    return _Finished(
        account=AccountOutcome(
            row_id=row.id,
            outcome=None,
            error="CredentialDecryptError",
            uncertain=False,
            decrypt_failed=True,
            search_only=False,
        ),
        row=RowOutcome(
            row_id=row.id,
            course_account_id=row.course_account_id,
            target_date=row.target_date,
            actor=Actor.BOOKING_RUNNER,
            to_status=None,
            last_outcome="decrypt_failed",
            at=at,
            release_lease_owner=owner,
        ),
    )


def _finish(
    account: _Account,
    *,
    result: BookingResult | None,
    error: BaseException | None,
    at: datetime,
    owner: str,
) -> _Finished:
    """Build the row's outcome from the returned result / captured exception PLUS the recording
    decorator's log (§4.6 "ownership derived at write time")."""
    recorded = account.recorder.log()
    uncertain = recorded.needs_reconcile() or (
        error is not None and not isinstance(error, _CONTRACT_ERRORS)
    )
    booked = result is not None and result.outcome in _BOOKED_OUTCOMES
    held, extras, cancelled = _ledger_for(account, result if booked else None, recorded)
    swallowed = bool(recorded.captcha_failures()) and not isinstance(error, CaptchaError)
    row = account.row
    return _Finished(
        account=AccountOutcome(
            row_id=row.id,
            outcome=result.outcome if result is not None else None,
            error=type(error).__name__ if error is not None else None,
            uncertain=uncertain,
            decrypt_failed=False,
            search_only=account.search_only,
            held_extra_raw_ids=tuple(e.raw_reservation_id for e in extras),
            swallowed_captcha_error=swallowed,
        ),
        row=RowOutcome(
            row_id=row.id,
            course_account_id=row.course_account_id,
            target_date=row.target_date,
            actor=Actor.BOOKING_RUNNER,
            to_status=RowStatus.BOOKED if booked else None,
            last_outcome=_last_outcome(result, error),
            at=at,
            result=result if booked else None,
            booking=held,
            cancelled_extras=cancelled,
            held_extras=extras,
            needs_reconcile=uncertain,
            release_lease_owner=owner,
        ),
    )


def _last_outcome(result: BookingResult | None, error: BaseException | None) -> str:
    if result is not None:
        return result.outcome.value
    if isinstance(error, SelfDeadlineReachedError):
        return "self_deadline"
    return f"error:{type(error).__name__}"


def _ledger_for(
    account: _Account, booked: BookingResult | None, recorded: RecordingLog
) -> tuple[OwnedBooking | None, tuple[OwnedBooking, ...], tuple[OwnedBooking, ...]]:
    """(held, held_extras, cancelled_extras) per the §4.6 ownership table. The kept booking is
    OWNED only if the recorder saw THIS run book its raw id: an ALREADY_BOOKED found by a guard
    (a manual booking, or a landed UNCERTAIN POST) is written unowned — the latter with
    ``needs_reconcile`` so the watcher can adopt it by exact slot. Every other owned id (booked
    here, not cancelled OK) is a live surplus: ``held_extra``, still owned (M1)."""
    books = {b.raw_id: b for b in recorded.books if b.raw_id is not None}
    owned = recorded.owned_raw_ids()
    best = None
    if booked is not None and booked.confirmation_code is not None:
        best = booked.confirmation_code.removeprefix(MANAGED_BOOKING_TAG)
    held = None
    if best is not None and best in owned:
        held = _owned(account, books[best], state=BookingState.HELD)
    extras = tuple(
        _owned(account, book, state=BookingState.HELD_EXTRA)
        for raw, book in books.items()
        if raw in owned and raw != best
    )
    cancelled = tuple(
        _owned(account, books[raw], state=BookingState.CANCELLED_EXTRA)
        for raw in recorded.cancelled_extras()
        if raw in books
    )
    return held, extras, cancelled


def _owned(account: _Account, book: RecordedBook, *, state: BookingState) -> OwnedBooking:
    assert book.raw_id is not None
    row = account.row
    blind = account.allowlist is not None and book.slot.slot_id in account.allowlist
    return OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=row.course_account_id,
        course_id=row.course_id,
        target_date=row.target_date,
        raw_reservation_id=book.raw_id,
        tee_time=book.slot.tee_time,
        party_size=row.party_size,
        source=BookingSource.BLIND if blind else BookingSource.SEARCH,
        state=state,
    )


async def _close_adapters(accounts: Sequence[_Account]) -> None:
    for account in accounts:
        try:
            await account.recorder.aclose()
        except Exception as exc:  # teardown must never mask the outcomes
            log.warning("tenant-run: adapter close failed (%s)", type(exc).__name__)


async def _close_pools(pools: Mapping[CourseId, SharedCaptchaPool | None]) -> None:
    for pool in pools.values():
        if pool is not None:
            await pool.aclose()
