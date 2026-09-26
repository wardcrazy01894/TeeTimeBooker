"""Tenant job entry points: the per-release-event booking runner and the tenant watcher
(MULTIUSER_PLAN §4, §7).

Booking runner (``teetime tenant-run --event <key> --wait``), in exact order (§4.2):
DST gate (pure) -> READ #1 ``load_event_rows`` -> WRITE #1 ``claim_rows`` -> dispose DB engine ->
decrypt creds in process -> site-key pre-flight (once per course) -> build per-account adapters
sharing one ``SharedCaptchaPool`` per course -> ``allocate_blind_slots`` + ``set_blind_allowlist``
-> register pool demand -> ``asyncio.gather`` of one UNMODIFIED ``core.orchestrator.Orchestrator``
per account (``return_exceptions=True``) -> WRITE #2 ``record_outcomes`` -> emails -> exit code.
NO DB call and NO decrypt between T0 - lead and the last orchestrator's return
(``test_runner_no_store_calls_inside_race_window``).

STUB — booking runner in MULTIUSER_PLAN MU-9a (core) + MU-9b (exit contract, CLI), watcher wiring
in MU-10b.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from enum import IntEnum
from typing import Protocol

from ..core.adapter import CourseAdapter
from ..core.clock import Clock
from ..core.config import BookingCutoffConfig, OneBookingPolicyConfig, SchedulerConfig
from ..core.models import BookingOutcome, CourseCredentials, CourseId
from ..core.redaction import register_secret_literals
from ..core.release_policy import ReleasePolicy
from ..courses.foreup.token_pool import SharedCaptchaPool
from .crypto import Keyring, credential_aad, decrypt_password
from .models import CourseAccount, EventRow, RowId
from .notify import UserNotifier
from .store import TenantStore

_MU9 = "MULTIUSER_PLAN.md MU-9a"
_MU9B = "MULTIUSER_PLAN.md MU-9b"
_MU10 = "MULTIUSER_PLAN.md MU-10b"

log = logging.getLogger(__name__)

# The BlindPostCapable members the orchestrator calls through a bare ``cast`` (§4.6 SF1).
_BLIND_METHODS = ("synthesize_blind_slots", "captcha_pool_size")


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
    adapters of the same course receive the same ``pool``; ``None`` -> private pool (today)."""

    def __call__(
        self,
        *,
        course_id: CourseId,
        account: CourseAccount,
        pool: SharedCaptchaPool | None,
        dry_run: bool,
    ) -> CourseAdapter: ...


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


@dataclass(frozen=True, slots=True)
class WatchReport:
    rows_loaded: int
    searches: int
    logins: int
    booked: tuple[RowId, ...]
    upgraded: tuple[RowId, ...]
    lost: tuple[RowId, ...]
    rate_limited: bool
    systemic_error: str | None


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
) -> RunReport:
    """The tenant booking job (§4). ``scheduler`` supplies the race knobs
    (``early_arrival_ms``, ``blind_post_stagger_ms``, ``blind_post_max_count``, lead, ...). The
    runner derives per-account copies (reserve -> 0 because the pool holds it; overflow accounts
    -> ``blind_post_max_count=0``, still registered with the pool at k=0) and passes
    ``fire_time``/``timezone`` from ``event``. Each account's adapter is wrapped by
    ``tenant.recording.make_recording_adapter``. Outcomes are STREAMED: one TenantStore write per
    row, each in its own batch, starting at T0 + ``post_burst_quiet_s``. A self-deadline marks
    unfinished rows ``needs_reconcile`` before ``replicaTimeout`` (round-1 M4/SF5)."""
    raise NotImplementedError(_MU9)


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
    """The tenant watcher (§7.1): one query + finalizer + materializer tick; one shared search per
    (course, date, party_size); per-account login only when ``watcher.needs_login`` says so;
    snapshot persistence; adoption; then an UNMODIFIED ``WatchOrchestrator`` per acting row."""
    raise NotImplementedError(_MU10)
