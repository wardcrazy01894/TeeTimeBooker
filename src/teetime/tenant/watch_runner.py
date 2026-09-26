"""The tenant watcher runner (MULTIUSER_PLAN §7, MU-10b): ``run_tenant_watch`` + its exit status.

One run, in order (§7.1):

1. **Materializer tick** (§7.7, the watcher owns it), then the **finalizer** (``finalize_lost``:
   frozen PENDING rows -> LOST, one ``lost`` email each, exactly once), then the ONE watch query
   (``load_watch_rows``), plus each watched account's latest snapshot and (account, date) ledger.
2. **Shared search**: ``group_rows_for_search`` -> ONE ``search()`` per (course, date, party)
   group on an unauthenticated per-course client, spaced >= 250 ms (PLAN §12).
3. **Per-row decision** (pure ``watcher.needs_login``). A row another writer holds an unexpired
   lease on is skipped before any login. An account logs in ONLY when one of its rows has a
   reason; accounts are processed sequentially, >= 250 ms apart.
4. Per account: decrypt (E7 registration via ``runner.resolve_credentials``), ``authenticate``,
   the soft-auth check (``is_authenticated``: a soft failure is counted, never persisted, and
   nothing acts), ``list_reservations``, then snapshot trust (E6 ``snapshot_trusted``): a TRUSTED
   snapshot is persisted; an UNTRUSTED one is neither persisted nor acted on (it would read as
   "everything vanished" and invite a double booking, §7.5).
5. Per row, under its OWN durable lease (``acquire_row_lease`` with the fingerprint read in step
   1, M5 — a row changed since the read is never acted on):
   - a BOOKED row whose reservation is missing from the trusted snapshot goes through
     ``classify_missing_booking`` (two trusted misses >= 10 min apart): NOT_YET -> nothing (the
     engine is NOT run, so a missing booking is never "upgraded" or re-booked); BOT_CAUSED ->
     PENDING + ``needs_reconcile``; ADOPT_REPLACEMENT -> the replacement is adopted;
     EXTERNAL_CANCEL -> CANCELLED(external) + an email, never re-booked (dry-run: logged only);
   - a PENDING row whose trusted snapshot shows a same-(date, party) reservation is ADOPTED
     (§7.6): owned iff ledgered (or an exact recorded-UNCERTAIN slot), else unowned;
   - otherwise, if the row had a login reason, the UNMODIFIED ``WatchOrchestrator`` runs for it
     over ``make_search_snapshot_adapter(make_recording_adapter(inner), slots)``: search is served
     from step 2, everything else is live. The outcome is derived from the RECORDER, not the
     engine's return (M2): old id cancelled + a new booking -> upgraded; old id cancelled and no
     new booking -> PENDING + ``needs_reconcile``; a new booking on a PENDING row -> BOOKED.

**The ownership gate on the upgrade (§7.6; E5 does NOT guard ``_try_upgrade``).** Two layers,
both in tenant code: (a) the engine's in-run store is PRE-SEEDED with the row's BOOKED terminal,
whose confirmation carries ``TTB:<raw>`` ONLY when ``watcher.upgrade_allowed`` says the booking
is owned (``seeded_terminal``) — so Gate 3 short-circuits every BOOKED row and
``maybe_upgrade``'s managed guard refuses a manual reservation; and (b) the upgrade policy is
handed to the engine ONLY for an owned BOOKED row (never for a PENDING row, whose
``_check_course`` would otherwise synthesize a ``TTB:`` booking from ANY live match and upgrade
it). ``reconcile_eligible`` is the same ownership predicate (E5). Before the engine runs on an
owned BOOKED row with the policy on, ``set_upgrade_marker`` (M2) is written under the lease, and
every outcome write clears it.

**Dry-run (§7.8, SF2).** Through ``watcher.dry_run_gate``: the upgrade policy is off,
``reconcile_eligible`` is ``lambda _: False``, a vanish is logged and NOT written
CANCELLED(external), and the engine's own ``request.dry_run`` suppresses the book POST. Logins,
snapshot persistence and DB-only adoption stay enabled.

**Exit contract (§7.9, ``watch_exit_status``).** ``RateLimitError`` anywhere aborts the run
cleanly with exit 0. ``AuthError`` (or the third soft-auth failure) is per-account: notified,
exit 0. ``CaptchaError``/``OtpChallengeError``, a DB failure, a credential decrypt failure and a
refused/failed outcome write exit non-zero. An UNCERTAIN book (the POST may have landed) sets
``needs_reconcile`` and is logged CRITICAL, but it is per-account, so the run continues and it
does not by itself fail the job (today's watcher also exits 0 on it; the next run reconciles).

**Deviations from the plan text, deliberate:** ``LeasedBookingStore`` (MU-9c) is not used — the
watcher takes the durable row lease itself BEFORE running the engine and holds it through the
outcome write, which gives the same cross-process exclusion (M5) with one fewer layer; the
engine's ``request_lock`` is the plain in-process ``InMemoryStore`` lock. The hard ``AuthError``
account flip to ``auth_failed`` needs a ``TenantStore`` write that does not exist yet (the same
gap MU-9b has); the soft-auth threshold flip is the store's.

UNWIRED: nothing on the production path calls this; ``teetime tenant-watch`` runs it over an
in-memory store until MU-16 (Cosmos) and MU-15a (infra).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ..core.adapter import (
    AuthError,
    AuthStateReportable,
    CaptchaError,
    CourseAdapter,
    InventoryNotPublishedError,
    NoInventoryError,
    RateLimitError,
    ReservationSnapshotHealth,
)
from ..core.clock import Clock
from ..core.config import BookingCutoffConfig, OneBookingPolicyConfig, SchedulerConfig
from ..core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    Player,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
    WatchConfig,
)
from ..core.release_policy import ReleasePolicy
from ..core.watch_orchestrator import WatchOrchestrator
from ..notifications.notifier import NoopNotifier
from ..persistence.in_memory_store import InMemoryStore
from .crypto import Keyring
from .groups import collapse_group, group_floor
from .materialize import materialize_tick
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
    ReservationSnapshot,
    RowFingerprint,
    RowId,
    RowStatus,
    SnapshotEntry,
    lease_held,
    options_time_windows,
    row_max_price,
)
from .notify import UserEvent, UserEventKind, UserNotifier
from .recording import RecordedBook, RecordingLog, make_recording_adapter
from .runner import AdapterFactory, ExitStatus, WatchReport, resolve_credentials
from .store import RowOutcome, TenantStore
from .watcher import (
    RECONCILE_EVERY_N_RUNS,
    LoginReason,
    MissingBookingVerdict,
    Ownership,
    SearchGroupKey,
    WatchAction,
    classify_missing_booking,
    dry_run_gate,
    group_rows_for_search,
    is_owned,
    make_search_snapshot_adapter,
    needs_login,
    ownership_of,
    upgrade_allowed,
)

log = logging.getLogger(__name__)

__all__ = [
    "RECONCILE_EVERY_N_RUNS",
    "WATCH_LEASE_S",
    "run_tenant_watch",
    "seeded_terminal",
    "watch_exit_status",
    "watch_run_index",
]

# The watch job's replicaTimeout (compute.bicep): a watcher lease never outlives its process.
WATCH_LEASE_S = 300.0
# PLAN §12 courtesy spacing between the run's searches and between account logins.
_SPACING_S = 0.25
# The store flips the account to AUTH_FAILED at this many consecutive soft failures (§7.5).
_SOFT_AUTH_FAILURE_LIMIT = 3
# RequestRow has no holes field; every hosted course books 18 (as the booking runner does).
_TENANT_HOLES = 18
# Players are not stored (§3.1): ForeUP's POST sends only the count.
_GUEST = Player(first_name="Guest", last_name="Player", email="")
_LIVE_LEDGER_STATES = frozenset({BookingState.HELD, BookingState.HELD_EXTRA})
_RUN_BUCKET_S = 600  # the watch cron cadence: one run_index per 10-minute bucket
_UPGRADED = "watch:upgraded"
_RECONCILED = "watch:reconciled"


def watch_run_index(now: datetime) -> int:
    """``floor(epoch_minutes / 10)`` (§7.1 step 3): the reconcile cadence's run counter,
    stateless and identical in every process."""
    return int(now.timestamp() // _RUN_BUCKET_S)


def watch_exit_status(report: WatchReport) -> ExitStatus:
    """§7.9: non-zero ONLY for systemic causes (a DB failure, a Captcha/OTP error, a credential
    decrypt failure, a refused/failed outcome write). A 429 abort, a per-account AuthError, a
    soft-auth failure and an UNCERTAIN book (flagged needs_reconcile) all exit 0."""
    systemic = (
        report.systemic_error is not None
        or report.captcha_error
        or bool(report.decrypt_failures)
        or bool(report.outcome_write_failures)
    )
    return ExitStatus.SYSTEMIC_FAILURE if systemic else ExitStatus.OK


def seeded_terminal(row: RequestRow, *, owned: bool) -> BookingResult:
    """The BOOKED terminal pre-seeded into the engine's in-run store for a BOOKED row (§7.1 step
    4, §7.6). Its confirmation is ``TTB:<raw>`` ONLY when ``owned``, so ``maybe_upgrade``'s managed
    guard refuses to upgrade (or cancel) an unowned, manual reservation. The slot's tee time is
    converted to the COURSE timezone: the upgrade's tier/midpoint maths reads ``.time()``."""
    if row.status is not RowStatus.BOOKED or row.booked_raw_id is None:
        raise ValueError("seeded_terminal needs a BOOKED row with a raw reservation id")
    raw = row.booked_raw_id
    return BookingResult(
        request_id=row.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=row.course_id,
        slot=_slot(row, raw, _held_tee_time(row)),
        confirmation_code=f"{MANAGED_BOOKING_TAG}{raw}" if owned else raw,
        booked_at=row.booked_at,
        attempts=0,
    )


async def run_tenant_watch(
    *,
    policies: Mapping[CourseId, ReleasePolicy],
    store: TenantStore,
    clock: Clock,
    scheduler: SchedulerConfig,
    booking_policy: OneBookingPolicyConfig,
    cutoff: BookingCutoffConfig,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    notifier: UserNotifier,
    dry_run: bool,
) -> WatchReport:
    """One tenant-watcher run (§7.1; the module docstring is the full contract). ``policies``
    names the hosted courses and their horizons; ``booking_policy`` is the one-booking (upgrade)
    policy, handed to the engine for OWNED booked rows only; ``cutoff`` is the hard booking
    cutoff (the engine's stop-acting gate and the materializer's)."""
    run = _Run(
        store=store,
        clock=clock,
        scheduler=scheduler,
        booking_policy=booking_policy,
        cutoff=cutoff,
        keyring=keyring,
        adapter_factory=adapter_factory,
        notifier=notifier,
        dry_run=dry_run,
        now=clock.now_utc(),
        owner=f"watcher:{uuid4().hex[:12]}",
    )
    try:
        rows = await run.read(policies)
        groups = group_rows_for_search(rows)
        slots = await run.search(groups)
        accounts = [w for w in await run.plan(groups, slots) if w.needs_login()]
        for i, work in enumerate(accounts):
            if i:
                await clock.sleep(_SPACING_S)
            await run.process(work)
        await run.collapse(rows)
    except _RateLimitedError:
        run.tally.rate_limited = True
    except _SystemicError as exc:
        log.critical("tenant-watch: systemic failure (%s)", exc)
        run.tally.systemic_error = str(exc)
    finally:
        await run.close()
    report = run.tally.report()
    log.info(
        "tenant-watch: %d row(s), %d search(es), %d login(s); booked=%d upgraded=%d adopted=%d "
        "cancelled_external=%d lost=%d rate_limited=%s",
        report.rows_loaded,
        report.searches,
        report.logins,
        len(report.booked),
        len(report.upgraded),
        len(report.adopted),
        len(report.cancelled_external),
        len(report.lost),
        report.rate_limited,
    )
    return report


# --- internals ---------------------------------------------------------------------------------


class _RateLimitedError(Exception):
    """Internal: a 429 was seen; unwind to the top and exit 0 (§7.9)."""


class _SystemicError(Exception):
    """Internal: a store failure; unwind to the top and exit non-zero (§7.9). Carries the stage
    and the exception CLASS name only (a driver message can carry connection strings)."""


class _Pre(Enum):
    """What the fresh trusted snapshot says to do with a row BEFORE the engine (§7.5/§7.6)."""

    ENGINE = "engine"  # nothing snapshot-derived: run the engine if the row had a login reason
    WAIT = "wait"  # a booked reservation missing from ONE trusted snapshot: do nothing yet
    ADOPT = "adopt"  # a PENDING row's (date, party) reservation exists: adopt it (§7.6)
    BOT_CAUSED = "bot_caused"
    ADOPT_REPLACEMENT = "adopt_replacement"
    EXTERNAL_CANCEL = "external_cancel"


_VERDICT_PRE: dict[MissingBookingVerdict, _Pre] = {
    MissingBookingVerdict.NOT_YET: _Pre.WAIT,
    MissingBookingVerdict.BOT_CAUSED: _Pre.BOT_CAUSED,
    MissingBookingVerdict.ADOPT_REPLACEMENT: _Pre.ADOPT_REPLACEMENT,
    MissingBookingVerdict.EXTERNAL_CANCEL: _Pre.EXTERNAL_CANCEL,
}


@dataclass
class _Tally:
    rows_loaded: int = 0
    searches: int = 0
    logins: int = 0
    booked: list[RowId] = field(default_factory=list)
    upgraded: list[RowId] = field(default_factory=list)
    lost: list[RowId] = field(default_factory=list)
    adopted: list[RowId] = field(default_factory=list)
    cancelled_external: list[RowId] = field(default_factory=list)
    reconcile_flagged: list[RowId] = field(default_factory=list)
    uncertain: list[RowId] = field(default_factory=list)
    skipped_leased: list[RowId] = field(default_factory=list)
    auth_failed: list[CourseAccountId] = field(default_factory=list)
    decrypt_failures: list[CourseAccountId] = field(default_factory=list)
    write_failures: list[RowId] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    captcha_error: bool = False
    rate_limited: bool = False
    systemic_error: str | None = None

    def report(self) -> WatchReport:
        return WatchReport(
            rows_loaded=self.rows_loaded,
            searches=self.searches,
            logins=self.logins,
            booked=tuple(self.booked),
            upgraded=tuple(self.upgraded),
            lost=tuple(self.lost),
            rate_limited=self.rate_limited,
            systemic_error=self.systemic_error,
            adopted=tuple(self.adopted),
            cancelled_external=tuple(self.cancelled_external),
            reconcile_flagged=tuple(self.reconcile_flagged),
            uncertain=tuple(self.uncertain),
            skipped_leased=tuple(self.skipped_leased),
            auth_failed_accounts=tuple(self.auth_failed),
            decrypt_failures=tuple(self.decrypt_failures),
            captcha_error=self.captcha_error,
            outcome_write_failures=tuple(self.write_failures),
            orphans=tuple(self.orphans),
        )


@dataclass
class _RowWork:
    event_row: EventRow
    reason: LoginReason | None
    owned: list[OwnedBooking]
    slots: list[TeeTimeSlot]

    @property
    def row(self) -> RequestRow:
        return self.event_row.row

    @property
    def account(self) -> CourseAccount:
        return self.event_row.account


@dataclass
class _AccountWork:
    account: CourseAccount
    previous: ReservationSnapshot | None  # the stored (last TRUSTED) snapshot, read in step 1
    rows: list[_RowWork] = field(default_factory=list)

    def needs_login(self) -> bool:
        return any(w.reason is not None for w in self.rows)


# A group needs this many BOOKED rows before there is anything to collapse (§16.4).
_DUPLICATE = 2


@dataclass
class _Run:
    """Everything one run shares; the methods are the §7.1 steps in order."""

    store: TenantStore
    clock: Clock
    scheduler: SchedulerConfig
    booking_policy: OneBookingPolicyConfig
    cutoff: BookingCutoffConfig
    keyring: Keyring
    adapter_factory: AdapterFactory
    notifier: UserNotifier
    dry_run: bool
    now: datetime
    owner: str
    tally: _Tally = field(default_factory=_Tally)
    adapters: list[CourseAdapter] = field(default_factory=list)
    # Logged-in sessions by account, kept for the end-of-run group collapse (§16.4).
    sessions: dict[CourseAccountId, CourseAdapter] = field(default_factory=dict)

    # --- step 1: the reads (tick -> finalizer -> the one watch query) ---------------------------

    async def read(self, policies: Mapping[CourseId, ReleasePolicy]) -> list[EventRow]:
        await self._store(
            "materialize_tick",
            materialize_tick(
                store=self.store,
                policies={str(cid): p for cid, p in policies.items()},
                cutoff=self.cutoff,
                now=self.now,
            ),
        )
        lost = await self._store("finalize_lost", self.store.finalize_lost(now=self.now))
        horizons = {cid: _horizon(p, self.now) for cid, p in policies.items()}
        rows = await self._store(
            "load_watch_rows", self.store.load_watch_rows(horizons=horizons, now=self.now)
        )
        self.tally.rows_loaded = len(rows)
        for row in lost:
            self.tally.lost.append(row.id)
            account = await self._store(
                "get_account_unscoped", self.store.get_account_unscoped(row.course_account_id)
            )
            await self._notify(UserEventKind.LOST, row, account=account, detail="frozen unbooked")
        return await self._group_floor(rows)

    async def _group_floor(self, rows: list[EventRow]) -> list[EventRow]:
        """§16.3: a PENDING grouped row is searched and booked only for its options ranked
        better than the best booking its group holds at another account, and dropped from this
        run if none is. BOOKED rows are untouched (the collapse needs them). Rows with no group
        cost no read; a failed group read is fail-open (the collapse keeps the best booking)."""
        keys = {(r.row.group_id, r.row.target_date) for r in rows if r.row.group_id is not None}
        if not keys:
            return rows
        try:
            siblings = await self.store.rows_in_groups(keys)
        except Exception as exc:
            log.warning(
                "tenant-watch: group read failed (%s); using every option", type(exc).__name__
            )
            return rows
        out: list[EventRow] = []
        for r in rows:
            if r.row.status is not RowStatus.PENDING or r.row.group_id is None:
                out.append(r)
                continue
            floor = group_floor(r.row, siblings)
            if not floor:
                log.info("tenant-watch: row %s skipped: its group holds a better booking", r.row.id)
                continue
            out.append(
                r
                if floor == r.row.options
                else EventRow(row=replace(r.row, options=floor), account=r.account)
            )
        return out

    # --- step 5: §16.4 group collapse, after every account was processed ----------------------

    async def collapse(self, rows: Sequence[EventRow]) -> None:
        """Collapse every group that holds two BOOKED rows (the backstop for the booker's pass,
        and the second half of a cross-course upgrade booked this run). Cancels go through the
        sessions this run opened; a losing account with no session is logged in here (only when
        a group actually needs collapsing). Dry-run cancels nothing (§7.8). Never raises."""
        keys = {(r.row.group_id, r.row.target_date) for r in rows if r.row.group_id is not None}
        if not keys:
            return
        try:
            members = await self.store.rows_in_groups(keys)
            groups: dict[tuple[UUID, date], list[RequestRow]] = {}
            for m in members:
                if m.group_id is not None:
                    groups.setdefault((m.group_id, m.target_date), []).append(m)
            for group in groups.values():
                booked = [m for m in group if m.status is RowStatus.BOOKED]
                if len(booked) < _DUPLICATE:
                    continue
                if not self.dry_run:
                    for m in booked:
                        await self._ensure_session(m)
                report = await collapse_group(
                    group,
                    store=self.store,
                    adapters=self.sessions,
                    actor=Actor.WATCHER,
                    owner=self.owner,
                    clock=self.clock,
                    dry_run=self.dry_run,
                )
                log.info("tenant-watch: group collapse %s", report)
        except Exception as exc:
            log.critical("tenant-watch: group collapse failed (%s)", type(exc).__name__)

    async def _ensure_session(self, row: RequestRow) -> None:
        if row.course_account_id in self.sessions:
            return
        account = await self.store.get_account_unscoped(row.course_account_id)
        if account is None:
            return
        probe = EventRow(row=row, account=account)
        resolved, failed = resolve_credentials([probe], keyring=self.keyring)
        if failed:
            self.tally.decrypt_failures.append(account.id)
            return
        inner = self._build(account.course_id, account)
        self.tally.logins += 1
        try:
            await inner.authenticate(resolved[probe.row.id])
        except Exception as exc:
            log.warning(
                "tenant-watch: collapse login failed for %s (%s)", account.id, type(exc).__name__
            )
            return
        if isinstance(inner, AuthStateReportable) and not inner.is_authenticated:
            return
        self.sessions[account.id] = inner

    # --- step 2: ONE search per (course, date, party) group --------------------------------------

    async def search(
        self, groups: Mapping[SearchGroupKey, Sequence[EventRow]]
    ) -> dict[SearchGroupKey, list[TeeTimeSlot]]:
        clients: dict[CourseId, CourseAdapter] = {}
        out: dict[SearchGroupKey, list[TeeTimeSlot]] = {}
        for i, (key, members) in enumerate(groups.items()):
            if i:
                await self.clock.sleep(_SPACING_S)
            client = clients.get(key.course_id)
            if client is None:
                # Never authenticated (search needs no login). Built from the group's first
                # account only because the factory is per-account.
                client = self._build(key.course_id, members[0].account)
                clients[key.course_id] = client
            self.tally.searches += 1
            out[key] = await self._search_one(client, key, members)
        return out

    async def _search_one(
        self, client: CourseAdapter, key: SearchGroupKey, members: Sequence[EventRow]
    ) -> list[TeeTimeSlot]:
        try:
            slots = await client.search(_group_request(key, members, dry_run=self.dry_run))
        except RateLimitError as exc:
            log.warning(
                "tenant-watch: rate-limited searching %s %s (retry_after=%ss); aborting the run",
                key.course_id,
                key.target_date,
                exc.retry_after_s,
            )
            raise _RateLimitedError from exc
        except (NoInventoryError, InventoryNotPublishedError):
            return []
        except Exception as exc:  # a transient blip: this group has no slots this run
            log.warning(
                "tenant-watch: search failed for %s %s party %d (%s)",
                key.course_id,
                key.target_date,
                key.party_size,
                type(exc).__name__,
            )
            return []
        zone = ZoneInfo(members[0].row.timezone)
        return [s for s in slots if s.tee_time.astimezone(zone).date() == key.target_date]

    # --- step 3: the pure per-row decision --------------------------------------------------------

    async def plan(
        self,
        groups: Mapping[SearchGroupKey, Sequence[EventRow]],
        slots: Mapping[SearchGroupKey, list[TeeTimeSlot]],
    ) -> list[_AccountWork]:
        run_index = watch_run_index(self.now)
        accounts: dict[CourseAccountId, _AccountWork] = {}
        for key, members in groups.items():
            for event_row in members:
                row, account = event_row.row, event_row.account
                work = accounts.get(account.id)
                if work is None:
                    previous = await self._store(
                        "get_snapshot", self.store.get_snapshot(account.id)
                    )
                    work = _AccountWork(account, previous)
                    accounts[account.id] = work
                owned = await self._store(
                    "list_owned_bookings",
                    self.store.list_owned_bookings(account.id, target_date=row.target_date),
                )
                self._report_orphans(row, owned)
                if lease_held(row, now=self.now) and row.lease_owner != self.owner:
                    log.info("tenant-watch: row %s leased by %s; skipped", row.id, row.lease_owner)
                    self.tally.skipped_leased.append(row.id)
                    continue
                reason = needs_login(
                    event_row,
                    group_slots=slots[key],
                    snapshot=work.previous,
                    run_index=run_index,
                    now=self.now,
                    owned=owned,
                )
                if reason is LoginReason.UPGRADE_CANDIDATE and not dry_run_gate(
                    dry_run=self.dry_run, action=WatchAction.UPGRADE
                ):
                    log.info("tenant-watch: DRY RUN — row %s would upgrade", row.id)
                work.rows.append(_RowWork(event_row, reason, owned, slots[key]))
        return sorted(accounts.values(), key=lambda w: w.account.id)

    def _report_orphans(self, row: RequestRow, owned: Sequence[OwnedBooking]) -> None:
        """§7.6 (MU-5 SF4): an owned (held / held_extra) ledger entry on a watched date whose row
        is not BOOKED survives only in the ledger. Reported for the operator, never acted on."""
        if row.status is RowStatus.BOOKED:
            return
        for entry in owned:
            if entry.state in _LIVE_LEDGER_STATES:
                log.warning(
                    "tenant-watch: ORPHAN owned reservation %s on %s (account %s) has no BOOKED "
                    "row; operator review",
                    entry.raw_reservation_id,
                    entry.target_date,
                    entry.course_account_id,
                )
                self.tally.orphans.append(entry.raw_reservation_id)

    # --- step 4: one account ----------------------------------------------------------------------

    async def process(self, work: _AccountWork) -> None:
        account = work.account
        first = work.rows[0]
        resolved, failed = resolve_credentials([first.event_row], keyring=self.keyring)
        if failed:
            self.tally.decrypt_failures.append(account.id)
            return
        creds = resolved[first.row.id]
        inner = self._build(account.course_id, account)
        self.tally.logins += 1
        snapshot = await self._login(work, inner, creds)
        if snapshot is None:
            return
        self.sessions[account.id] = inner
        for row_work in sorted(work.rows, key=lambda w: w.row.id):
            await self._act(row_work, inner, creds, snapshot=snapshot, previous=work.previous)

    async def _login(
        self, work: _AccountWork, inner: CourseAdapter, creds: CourseCredentials
    ) -> ReservationSnapshot | None:
        """authenticate -> soft-auth check -> list -> trust. Returns the TRUSTED snapshot (now
        persisted) or None (nothing acts for this account this run)."""
        account = work.account
        recorder = make_recording_adapter(inner, clock=self.clock)
        try:
            await recorder.authenticate(creds)
            if isinstance(recorder, AuthStateReportable) and not recorder.is_authenticated:
                await self._soft_auth_failure(work)
                return None
            reservations = await recorder.list_reservations()
        except RateLimitError as exc:
            log.warning("tenant-watch: account %s rate-limited at login; aborting", account.id)
            raise _RateLimitedError from exc
        except _SystemicError:
            raise
        except Exception as exc:
            await self._account_error(account, work.rows[0].row, exc, stage="login")
            return None
        trusted = (
            recorder.snapshot_trusted if isinstance(recorder, ReservationSnapshotHealth) else True
        )
        if not trusted:
            log.warning(
                "tenant-watch: account %s: UNTRUSTED reservation snapshot (§7.5); not persisted "
                "and nothing acts this run",
                account.id,
            )
            return None
        snapshot = ReservationSnapshot(
            course_account_id=account.id,
            observed_at=self.clock.now_utc(),
            source="watcher",
            trusted=True,
            entries=tuple(
                SnapshotEntry(
                    raw_id=r.confirmation_code.removeprefix(MANAGED_BOOKING_TAG),
                    tee_time=r.tee_time,
                    party_size=r.party_size,
                )
                for r in reservations
            ),
        )
        await self._store("save_snapshot", self.store.save_snapshot(snapshot))
        return snapshot

    async def _soft_auth_failure(self, work: _AccountWork) -> None:
        account = work.account
        count = await self._store(
            "record_soft_auth_failure", self.store.record_soft_auth_failure(account.id)
        )
        log.warning(
            "tenant-watch: account %s: soft login failure #%d; snapshot NOT persisted",
            account.id,
            count,
        )
        if count >= _SOFT_AUTH_FAILURE_LIMIT:
            self.tally.auth_failed.append(account.id)
            await self._notify(
                UserEventKind.AUTH_FAILED,
                work.rows[0].row,
                account=account,
                detail="repeated login failures",
            )

    async def _account_error(
        self, account: CourseAccount, row: RequestRow, exc: Exception, *, stage: str
    ) -> None:
        """AuthError: per-account (notify, exit 0). Captcha/OTP: systemic (exit non-zero).
        Anything else: transient, logged, the run continues. Class names only (PII)."""
        name = type(exc).__name__
        if isinstance(exc, CaptchaError):
            log.critical("tenant-watch: account %s: %s at %s", account.id, name, stage)
            self.tally.captcha_error = True
        elif isinstance(exc, AuthError):
            log.error("tenant-watch: account %s: %s at %s", account.id, name, stage)
            if account.id not in self.tally.auth_failed:
                self.tally.auth_failed.append(account.id)
                await self._notify(
                    UserEventKind.AUTH_FAILED, row, account=account, detail="login rejected"
                )
        else:
            log.warning(
                "tenant-watch: account %s: transient %s at %s; continuing", account.id, name, stage
            )

    # --- step 5: one row, under its own lease ------------------------------------------

    async def _act(
        self,
        work: _RowWork,
        inner: CourseAdapter,
        creds: CourseCredentials,
        *,
        snapshot: ReservationSnapshot,
        previous: ReservationSnapshot | None,
    ) -> None:
        row = work.row
        pre = self._pre_engine(work, snapshot=snapshot, previous=previous)
        if pre is _Pre.WAIT or (pre is _Pre.ENGINE and work.reason is None):
            return
        fingerprint = RowFingerprint(row.status, row.version, row.booked_raw_id)
        now = self.clock.now_utc()
        acquired = await self._store(
            "acquire_row_lease",
            self.store.acquire_row_lease(
                row.id,
                owner=self.owner,
                until=now + timedelta(seconds=WATCH_LEASE_S),
                now=now,
                expected=fingerprint,
            ),
        )
        if not acquired:
            log.info("tenant-watch: row %s leased or changed since the read; skipped (M5)", row.id)
            self.tally.skipped_leased.append(row.id)
            return
        try:
            if pre is _Pre.ENGINE:
                await self._engine(work, inner, creds, fingerprint=fingerprint, live=snapshot)
            else:
                await self._snapshot_outcome(work, pre, snapshot=snapshot)
        finally:
            # A no-op when the outcome write already released it (it releases iff still held).
            await self._store(
                "release_row_lease", self.store.release_row_lease(row.id, owner=self.owner)
            )

    def _pre_engine(
        self,
        work: _RowWork,
        *,
        snapshot: ReservationSnapshot,
        previous: ReservationSnapshot | None,
    ) -> _Pre:
        row = work.row
        if row.status is RowStatus.PENDING:
            return _Pre.ADOPT if _candidates(row, snapshot) else _Pre.ENGINE
        raw = row.booked_raw_id
        if raw is None:
            log.warning("tenant-watch: BOOKED row %s has no raw reservation id; left alone", row.id)
            return _Pre.WAIT
        if any(e.raw_id == raw for e in snapshot.entries):
            return _Pre.ENGINE
        verdict = classify_missing_booking(
            row, snapshots=[s for s in (previous, snapshot) if s is not None], owned=work.owned
        )
        if verdict is MissingBookingVerdict.NOT_YET:
            log.info(
                "tenant-watch: row %s: reservation %s missing from one trusted snapshot; waiting "
                "for a second one before inferring anything",
                row.id,
                raw,
            )
        return _VERDICT_PRE[verdict]

    async def _snapshot_outcome(
        self, work: _RowWork, pre: _Pre, *, snapshot: ReservationSnapshot
    ) -> None:
        row = work.row
        if pre in (_Pre.ADOPT, _Pre.ADOPT_REPLACEMENT):
            entry = _pick_adoption(row, snapshot, work.owned)
            ownership = ownership_of(
                _reservation(row, entry),
                row=row,
                owned=work.owned,
                uncertain_tee_times=_uncertain_times(row),
            )
            outcome = _adopt_outcome(row, entry, ownership, work.owned, base=self._base(row))
            if await self._write(outcome):
                log.info("tenant-watch: row %s adopted %s (%s)", row.id, entry.raw_id, ownership)
                self.tally.adopted.append(row.id)
        elif pre is _Pre.BOT_CAUSED:
            outcome = replace(
                self._base(row),
                to_status=RowStatus.PENDING,
                last_outcome="watch:vanished_bot_caused",
                needs_reconcile=True,
            )
            if await self._write(outcome):
                self.tally.reconcile_flagged.append(row.id)
        elif not dry_run_gate(dry_run=self.dry_run, action=WatchAction.MARK_CANCELLED_EXTERNAL):
            log.info(
                "tenant-watch: DRY RUN — row %s: reservation %s vanished (external cancel); "
                "NOT written",
                row.id,
                row.booked_raw_id,
            )
        else:
            outcome = replace(
                self._base(row),
                to_status=RowStatus.CANCELLED,
                status_reason="external",
                last_outcome="watch:cancelled_external",
            )
            if await self._write(outcome):
                self.tally.cancelled_external.append(row.id)
                await self._notify(
                    UserEventKind.CANCELLED_EXTERNAL,
                    row,
                    account=work.account,
                    detail="cancelled at the course; we will not re-book it",
                )

    async def _engine(
        self,
        work: _RowWork,
        inner: CourseAdapter,
        creds: CourseCredentials,
        *,
        fingerprint: RowFingerprint,
        live: ReservationSnapshot,
    ) -> None:
        """The UNMODIFIED ``WatchOrchestrator`` for one row, gated on ownership (§7.6)."""
        row = work.row
        booked = row.status is RowStatus.BOOKED
        uncertain_times = _uncertain_times(row)
        owned_booking = booked and upgrade_allowed(
            row, _held_reservation(row), owned=work.owned, uncertain_tee_times=uncertain_times
        )
        policy_on = (
            self.booking_policy.enabled
            and owned_booking
            and dry_run_gate(dry_run=self.dry_run, action=WatchAction.UPGRADE)
        )
        marker = False
        if policy_on:
            marker = await self._store(
                "set_upgrade_marker",
                self.store.set_upgrade_marker(
                    row.id, owner=self.owner, at=self.clock.now_utc(), expected=fingerprint
                ),
            )
            if not marker:
                log.info("tenant-watch: row %s changed before the upgrade marker; skipped", row.id)
                return
        memory = InMemoryStore()
        if booked:
            await memory.record_terminal(seeded_terminal(row, owned=owned_booking), row.target_date)
        recorder = make_recording_adapter(inner, clock=self.clock)
        engine = WatchOrchestrator(
            adapters={row.course_id: make_search_snapshot_adapter(recorder, slots=work.slots)},
            store=memory,
            notifier=NoopNotifier(),
            clock=self.clock,
            scheduler=self.scheduler.model_copy(update={"timezone": row.timezone}),
            watch_config=WatchConfig(),
            creds={row.course_id: creds},
            policy=self.booking_policy if policy_on else OneBookingPolicyConfig(enabled=False),
            booking_cutoff=self.cutoff,
            reconcile_eligible=self._eligibility(row, work.owned, uncertain_times),
        )
        result: BookingResult | None = None
        error: Exception | None = None
        try:
            result = await engine.check_once(
                _request_for(row, work.account, dry_run=self.dry_run), row.target_date
            )
        except (RateLimitError, CaptchaError, AuthError) as exc:  # check_once re-raises only these
            error = exc
        outcome = _engine_outcome(
            row,
            recorder.log(),
            result=result,
            owned=work.owned,
            live_ids=frozenset(e.raw_id for e in live.entries),
            base=self._base(row, clear_marker=marker),
        )
        if await self._write(outcome):
            await self._tally_engine(work, outcome, result)
        if isinstance(error, RateLimitError):
            raise _RateLimitedError from error
        if error is not None:
            await self._account_error(work.account, row, error, stage="act")

    def _eligibility(
        self, row: RequestRow, owned: Sequence[OwnedBooking], uncertain: Sequence[datetime]
    ) -> Callable[[ExistingReservation], bool]:
        """E5 ``reconcile_eligible``: ownership (§7.6), or ``lambda _: False`` in dry-run (§7.8)."""
        if not dry_run_gate(dry_run=self.dry_run, action=WatchAction.RECONCILE_CANCEL):
            return lambda _res: False
        return lambda res: is_owned(res, row=row, owned=owned, uncertain_tee_times=uncertain)

    async def _tally_engine(
        self, work: _RowWork, outcome: RowOutcome, result: BookingResult | None
    ) -> None:
        row = work.row
        if outcome.to_status is RowStatus.PENDING:
            log.critical(
                "tenant-watch: row %s: the upgrade cancelled %s but the rebook did not land; the "
                "row is pending + needs_reconcile",
                row.id,
                row.booked_raw_id,
            )
            self.tally.reconcile_flagged.append(row.id)
            return
        if outcome.needs_reconcile:
            log.critical(
                "tenant-watch: row %s: a book() is UNCERTAIN (the POST may have landed); "
                "needs_reconcile set",
                row.id,
            )
            self.tally.uncertain.append(row.id)
        if outcome.last_outcome == _RECONCILED:
            log.warning(
                "tenant-watch: row %s: the duplicate reconcile kept owned %s and cancelled the "
                "row's %s; the row follows the survivor",
                row.id,
                outcome.booking.raw_reservation_id if outcome.booking else None,
                row.booked_raw_id,
            )
        elif outcome.last_outcome == _UPGRADED:
            self.tally.upgraded.append(row.id)
            await self._notify(UserEventKind.UPGRADED, row, account=work.account, detail="watcher")
        elif outcome.to_status is RowStatus.BOOKED:
            self.tally.booked.append(row.id)
            await self._notify(UserEventKind.BOOKED, row, account=work.account, detail="watcher")
        elif result is not None and result.outcome is BookingOutcome.DRY_RUN:
            log.info("tenant-watch: DRY RUN — row %s would book %s", row.id, result.slot)

    # --- helpers ---------------------------------------------------------------------------

    def _base(self, row: RequestRow, *, clear_marker: bool = True) -> RowOutcome:
        """A no-change outcome for ``row`` under this run's lease; callers ``replace`` into it."""
        return RowOutcome(
            row_id=row.id,
            course_account_id=row.course_account_id,
            target_date=row.target_date,
            actor=Actor.WATCHER,
            to_status=None,
            last_outcome="watch:checked",
            at=self.clock.now_utc(),
            clear_upgrade_marker=clear_marker,
            release_lease_owner=self.owner,
        )

    async def _write(self, outcome: RowOutcome) -> bool:
        try:
            await self.store.record_outcomes([outcome])
        except ExceptionGroup as group:
            # Refused (the row moved / the lease was lost): the store still wrote the ledger and
            # flagged the date's active row needs_reconcile (M4). Non-zero exit.
            log.critical(
                "tenant-watch: row %s: outcome write REFUSED (%s)",
                outcome.row_id,
                [type(e).__name__ for e in group.exceptions],
            )
            self.tally.write_failures.append(outcome.row_id)
            return False
        except Exception as exc:
            raise _SystemicError(f"record_outcomes: {type(exc).__name__}") from exc
        return True

    async def _store[T](self, stage: str, call: Awaitable[T]) -> T:
        try:
            return await call
        except Exception as exc:
            raise _SystemicError(f"{stage}: {type(exc).__name__}") from exc

    def _build(self, course_id: CourseId, account: CourseAccount) -> CourseAdapter:
        adapter = self.adapter_factory(
            course_id=course_id, account=account, pool=None, lease_key=None, dry_run=self.dry_run
        )
        if all(adapter is not seen for seen in self.adapters):
            self.adapters.append(adapter)
        return adapter

    async def close(self) -> None:
        for adapter in self.adapters:
            try:
                await adapter.aclose()
            except Exception as exc:  # teardown never masks the outcomes
                log.warning("tenant-watch: adapter close failed (%s)", type(exc).__name__)

    async def _notify(
        self, kind: UserEventKind, row: RequestRow, *, account: CourseAccount | None, detail: str
    ) -> None:
        event = UserEvent(
            kind=kind,
            user_id=account.user_id if account is not None else None,
            row_id=row.id,
            course_id=row.course_id,
            target_date=row.target_date,
            tee_time=row.booked_tee_time,
            confirmation=None,
            detail=detail,
            at=self.clock.now_utc(),
        )
        try:
            await self.notifier.send(event)
        except Exception as exc:  # a notification failure never masks an outcome
            log.warning("tenant-watch: %s notification failed (%s)", kind, type(exc).__name__)


def _horizon(policy: ReleasePolicy, now: datetime) -> tuple[date, date]:
    """``[local_today, local_today + advance_days]`` in the COURSE timezone (§7.1 step 1)."""
    today = now.astimezone(ZoneInfo(policy.timezone)).date()
    return today, today + timedelta(days=policy.advance_days)


def _group_request(
    key: SearchGroupKey, members: Sequence[EventRow], *, dry_run: bool
) -> BookingRequest:
    """The group's ONE search request: the UNION of the members' windows (§7.2) and the HIGHEST
    member price cap (permissive, so no member loses a slot it could book); each row re-ranks
    with its own windows and cap afterwards."""
    windows: list[TimeWindow] = []
    for member in members:
        for window in options_time_windows(member.row.options):
            if window not in windows:
                windows.append(window)
    return BookingRequest(
        request_id=members[0].row.request_id,
        target_dates=(key.target_date,),
        time_windows=tuple(windows),
        players=(_GUEST,) * key.party_size,
        course_preferences=(key.course_id,),
        holes=_TENANT_HOLES,
        max_price_per_player=max(row_max_price(m.row, m.account) for m in members),
        dry_run=dry_run,
    )


def _request_for(row: RequestRow, account: CourseAccount, *, dry_run: bool) -> BookingRequest:
    return BookingRequest(
        request_id=row.request_id,
        target_dates=(row.target_date,),
        time_windows=options_time_windows(row.options),
        players=(_GUEST,) * row.party_size,
        course_preferences=(row.course_id,),
        holes=_TENANT_HOLES,
        max_price_per_player=row_max_price(row, account),
        dry_run=dry_run,
    )


def _slot(row: RequestRow, raw: str, tee: datetime) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=row.course_id,
        slot_id=SlotId(raw),
        tee_time=tee.astimezone(ZoneInfo(row.timezone)),
        holes=_TENANT_HOLES,
        available_spots=row.party_size,
        price_per_player=Decimal("0"),
        cart_included=False,
    )


def _held_tee_time(row: RequestRow) -> datetime:
    """The held tee time. A BOOKED row always carries one; the fallback (course-local midnight)
    only keeps a malformed row from crashing the run and can never match a real slot."""
    if row.booked_tee_time is not None:
        return row.booked_tee_time
    return datetime.combine(row.target_date, time(0), tzinfo=ZoneInfo(row.timezone))


def _held_reservation(row: RequestRow) -> ExistingReservation:
    return ExistingReservation(
        course_id=row.course_id,
        confirmation_code=row.booked_raw_id or "",
        tee_time=_held_tee_time(row),
        party_size=row.party_size,
    )


def _uncertain_times(row: RequestRow) -> tuple[datetime, ...]:
    """``row.booked_tee_time`` carries an UNCERTAIN slot across runs (§4.6) ONLY for a row that was
    BOOKED (the store nulls it on any transition to PENDING). KNOWN GAP: an uncertain book on a
    PENDING row leaves no durable slot, so a landed POST is later adopted UNOWNED (fail-safe; see
    BACKLOG "durable uncertain-slot carrier" and
    ``test_watch_uncertain_book_on_pending_row_leaves_no_durable_slot_known_gap``)."""
    return (row.booked_tee_time,) if row.booked_tee_time is not None else ()


def _candidates(row: RequestRow, snapshot: ReservationSnapshot) -> list[SnapshotEntry]:
    """Snapshot entries for the row's (date, party) other than its own held id, compared in the
    COURSE timezone (a UTC-stored instant must not slip a day)."""
    zone = ZoneInfo(row.timezone)
    return [
        e
        for e in snapshot.entries
        if e.raw_id != row.booked_raw_id
        and e.party_size == row.party_size
        and e.tee_time.astimezone(zone).date() == row.target_date
    ]


def _pick_adoption(
    row: RequestRow, snapshot: ReservationSnapshot, owned: Sequence[OwnedBooking]
) -> SnapshotEntry:
    """The reservation to adopt: a ledgered (owned) one first, then the earliest tee time."""
    live = {o.raw_reservation_id for o in owned if o.state in _LIVE_LEDGER_STATES}
    return min(_candidates(row, snapshot), key=lambda e: (e.raw_id not in live, e.tee_time))


def _reservation(row: RequestRow, entry: SnapshotEntry) -> ExistingReservation:
    return ExistingReservation(
        course_id=row.course_id,
        confirmation_code=entry.raw_id,
        tee_time=entry.tee_time,
        party_size=entry.party_size,
    )


def _new_booking(
    row: RequestRow, *, raw_id: str, tee: datetime, source: BookingSource
) -> OwnedBooking:
    return OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=row.course_account_id,
        course_id=row.course_id,
        target_date=row.target_date,
        raw_reservation_id=raw_id,
        tee_time=tee,
        party_size=row.party_size,
        source=source,
        state=BookingState.HELD,
    )


def _adopt_outcome(
    row: RequestRow,
    entry: SnapshotEntry,
    ownership: Ownership,
    owned: Sequence[OwnedBooking],
    *,
    base: RowOutcome,
) -> RowOutcome:
    """-> BOOKED on ``entry``. OWNED keeps its ledger entry; ADOPTED_RECONCILE ledgers it now;
    UNOWNED gets NO ledger entry and a RAW confirmation (no ``TTB:``), so the bot never upgrades
    or reconcile-cancels it (§7.6)."""
    booking: OwnedBooking | None = None
    if ownership is Ownership.OWNED:
        booking = next(
            (
                o
                for o in owned
                if o.raw_reservation_id == entry.raw_id and o.state in _LIVE_LEDGER_STATES
            ),
            None,
        )
    elif ownership is Ownership.ADOPTED_RECONCILE:
        booking = _new_booking(
            row, raw_id=entry.raw_id, tee=entry.tee_time, source=BookingSource.ADOPTED_RECONCILE
        )
    managed = ownership is not Ownership.UNOWNED
    result = BookingResult(
        request_id=row.request_id,
        outcome=BookingOutcome.ALREADY_BOOKED,
        course_id=row.course_id,
        slot=_slot(row, entry.raw_id, entry.tee_time),
        confirmation_code=f"{MANAGED_BOOKING_TAG}{entry.raw_id}" if managed else entry.raw_id,
        booked_at=None,
        attempts=0,
    )
    return replace(
        base,
        to_status=RowStatus.BOOKED,
        last_outcome=f"watch:adopted_{ownership.value}",
        result=result,
        booking=booking,
        needs_reconcile=False,
    )


def _booked_result(
    row: RequestRow, book: RecordedBook, result: BookingResult | None
) -> BookingResult:
    if result is not None and result.outcome is BookingOutcome.BOOKED:
        return result
    return BookingResult(
        request_id=row.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=row.course_id,
        slot=book.slot,
        confirmation_code=f"{MANAGED_BOOKING_TAG}{book.raw_id}",
        booked_at=book.at,
        attempts=1,
    )


def _engine_outcome(
    row: RequestRow,
    recorded: RecordingLog,
    *,
    result: BookingResult | None,
    owned: Sequence[OwnedBooking],
    live_ids: frozenset[str],
    base: RowOutcome,
) -> RowOutcome:
    """The row's outcome from the RECORDER (M2), never from the engine's return value: an
    upgrade that cancelled and then failed to rebook returns the OLD terminal.

    BOOKED row: old id cancelled OK + a new booking -> BOOKED (upgraded; old ledgered
    ``cancelled_upgrade``, new ``held``/upgrade); old id cancelled and nothing new -> PENDING +
    ``needs_reconcile`` (a bot-caused loss, re-book allowed) UNLESS an owned ledgered
    reservation still live in the trusted snapshot (``live_ids``) survived — the duplicate
    reconcile kept it over the row's own — in which case the row follows it (old id ledgered
    ``cancelled_extra``); otherwise unchanged. Owned extras
    the reconcile cancelled OK -> ``cancelled_extra``. PENDING row: a new booking -> BOOKED
    (``held``/watch). Any UNCERTAIN book (a non-SlotGone, non-captcha raise, or BOOKED with no
    confirmation) -> ``needs_reconcile``."""
    cancelled_ok = {c.raw_id for c in recorded.cancels if c.ok}
    books = [b for b in recorded.books if b.raw_id is not None and b.raw_id not in cancelled_ok]
    uncertain = any(not f.captcha for f in recorded.book_failures) or any(
        b.raw_id is None for b in recorded.books
    )
    if row.status is RowStatus.PENDING:
        if not books:
            last = result.outcome.value if result is not None else "watch:no_booking"
            return replace(base, last_outcome=last, needs_reconcile=uncertain)
        new = books[-1]
        assert new.raw_id is not None
        return replace(
            base,
            to_status=RowStatus.BOOKED,
            last_outcome="watch:booked",
            result=_booked_result(row, new, result),
            booking=_new_booking(
                row, raw_id=new.raw_id, tee=new.slot.tee_time, source=BookingSource.WATCH
            ),
            needs_reconcile=uncertain,
        )
    old = row.booked_raw_id
    extras = tuple(
        replace(o, state=BookingState.CANCELLED_EXTRA)
        for o in owned
        if o.raw_reservation_id in cancelled_ok
        and o.raw_reservation_id != old
        and o.state in _LIVE_LEDGER_STATES
    )
    if old not in cancelled_ok:
        return replace(
            base, last_outcome="watch:held", cancelled_extras=extras, needs_reconcile=uncertain
        )
    if not books:
        survivor = next(
            (
                o
                for o in owned
                if o.state in _LIVE_LEDGER_STATES
                and o.raw_reservation_id not in cancelled_ok
                and o.raw_reservation_id != old
                and o.raw_reservation_id in live_ids
            ),
            None,
        )
        if survivor is not None:
            return _follow_survivor(row, survivor, owned, extras=extras, base=base)
        return replace(
            base,
            to_status=RowStatus.PENDING,
            last_outcome="watch:upgrade_rebook_failed",
            cancelled_upgrade_raw_id=old,
            cancelled_extras=extras,
            needs_reconcile=True,
        )
    new = books[-1]
    assert new.raw_id is not None
    return replace(
        base,
        to_status=RowStatus.BOOKED,
        last_outcome=_UPGRADED,
        result=_booked_result(row, new, result),
        booking=_new_booking(
            row, raw_id=new.raw_id, tee=new.slot.tee_time, source=BookingSource.UPGRADE
        ),
        cancelled_upgrade_raw_id=old,
        cancelled_extras=extras,
        needs_reconcile=uncertain,
    )


def _follow_survivor(
    row: RequestRow,
    survivor: OwnedBooking,
    owned: Sequence[OwnedBooking],
    *,
    extras: tuple[OwnedBooking, ...],
    base: RowOutcome,
) -> RowOutcome:
    """BOOKED -> BOOKED on an owned reservation the duplicate reconcile kept over the row's own
    (keep-best ranks by midpoint distance, so it may keep a ``held_extra``). The survivor becomes
    ``held``; the row's cancelled reservation is ledgered ``cancelled_extra`` (ours, so a later
    vanish check reads it as bot-caused, never external)."""
    cancelled_own = tuple(
        replace(o, state=BookingState.CANCELLED_EXTRA)
        for o in owned
        if o.raw_reservation_id == row.booked_raw_id
    )
    result = BookingResult(
        request_id=row.request_id,
        outcome=BookingOutcome.ALREADY_BOOKED,
        course_id=row.course_id,
        slot=_slot(row, survivor.raw_reservation_id, survivor.tee_time),
        confirmation_code=f"{MANAGED_BOOKING_TAG}{survivor.raw_reservation_id}",
        booked_at=None,
        attempts=0,
    )
    return replace(
        base,
        to_status=RowStatus.BOOKED,
        last_outcome=_RECONCILED,
        result=result,
        booking=replace(survivor, state=BookingState.HELD),
        cancelled_extras=extras + cancelled_own,
        needs_reconcile=False,
    )
