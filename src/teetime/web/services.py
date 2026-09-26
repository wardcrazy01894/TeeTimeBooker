"""Web service layer (MULTIUSER_PLAN §8.4-§8.6). Framework-free, so every behaviour is testable
with ``InMemoryTenantStore`` + ``FakeAdapter`` + ``FakeClock``. The web NEVER calls ``book()``
(booking is job-only, §9.5); it calls only ``authenticate``, ``list_reservations`` and
``cancel_reservation``.

MU-13 (implemented here): the dashboard read model, standing-rule create/edit/deactivate
(materialized SYNCHRONOUSLY through ``tenant.materialize``, §7.7) and the dated-row actions
(one-off create, skip, unskip, withdraw, re-request). Every call is scoped by the signed-in
``user_id``: a row, rule or account that is missing, malformed OR belongs to someone else raises
the same ``WebNotFoundError`` (the web's uniform 404, IDOR §9.1). Store refusals are translated
to ``ActionRefusedError`` carrying the user-facing §8.2 message (the web's 409); malformed form
input is ``InvalidInputError`` (400). The dashboard reads the persisted snapshot and NEVER logs
in to ForeUP (§8.6).

STUB — connect / refresh / cancel are MULTIUSER_PLAN MU-14.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ..core.adapter import (
    AuthStateReportable,
    CancelError,
    CourseAdapter,
    RateLimitError,
    ReservationSnapshotHealth,
)
from ..core.clock import Clock
from ..core.config import BookingCutoffConfig
from ..core.models import MANAGED_BOOKING_TAG, CourseCredentials, CourseId, ExistingReservation
from ..core.redaction import register_secret_literals
from ..core.release_policy import ReleasePolicy
from ..persistence.in_memory_store import InMemoryStore
from ..persistence.store import ConcurrentRunError
from ..tenant.crypto import (
    CredentialDecryptError,
    Keyring,
    credential_aad,
    decrypt_password,
    encrypt_password,
)
from ..tenant.materialize import (
    MIN_HORIZON_DAYS,
    MaterializeReport,
    RuleConflictError,
    apply_rule_edit,
    materialize_rule,
)
from ..tenant.models import (
    ACTIVE_ROW_STATUSES,
    USER_WITHDRAW_REASON,
    AccountProvenance,
    AccountStatus,
    Actor,
    BookingState,
    CourseAccount,
    CourseAccountId,
    OwnedBooking,
    RequestRow,
    ReservationSnapshot,
    RowFingerprint,
    RowId,
    RowStatus,
    RuleId,
    RuleNoLongerCoversError,
    SnapshotEntry,
    StandingRule,
    TransitionRefusedError,
    UserId,
    derive_account_id,
    row_is_frozen,
)
from ..tenant.notify import UserEvent, UserEventKind, UserNotifier
from ..tenant.runner import AdapterFactory
from ..tenant.store import (
    LeasedBookingStore,
    RowLeaseError,
    RowOutcome,
    TenantNotFoundError,
    TenantStore,
    UniquenessConflictError,
    VersionConflictError,
)

_MU14 = "MULTIUSER_PLAN.md MU-14"
log = logging.getLogger(__name__)

WEEKDAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
# §7.7 round-3 SF2: a rule edit made after the booker's ~05:51 claim does not reach the claimed
# row, so that morning books the OLD window/weekday. The rules page states it.
RULE_EDIT_HINT = (
    "Edits apply from the next drop: a date the bot is already booking this morning keeps its "
    "old window, and a booked tee time is never changed."
)
# Sanity bound on a requested party size. The course enforces its own limits at search time
# (Mangrove Bay allows 2-4).
MIN_PARTY, MAX_PARTY = 1, 4
# Snapshot ages below this read "N min ago"; older ones "N h ago".
_MINUTES_LABEL_LIMIT = 120
# Statuses the dashboard/dates pages list. SUPERSEDED and WITHDRAWN rows are history the user
# already acted on (a one-off replaced them, or they undid it) and would only duplicate a date.
_VISIBLE = frozenset(
    {RowStatus.PENDING, RowStatus.BOOKED, RowStatus.SKIPPED, RowStatus.CANCELLED, RowStatus.LOST}
)


class WebNotFoundError(LookupError):
    """A row / rule / account id that is missing, malformed, or not the caller's. The three are
    deliberately indistinguishable: the web renders ONE 404 for all of them (IDOR, §9.1)."""


class InvalidInputError(ValueError):
    """A form field is missing or malformed (the web's 400). The message is safe to show."""


@dataclass(frozen=True, slots=True)
class OneOffPrefill:
    """The "add it as a one-off instead" action offered with a ``RuleNoLongerCoversError``
    (§8.2, round-6): a POST /rows form prefilled from the refused row."""

    account_id: CourseAccountId
    target_date: date
    window_earliest: time
    window_latest: time
    party_size: int


class ActionRefusedError(Exception):
    """A store refusal translated for the user (the web's 409, §8.2). Nothing was written."""

    def __init__(self, message: str, *, one_off: OneOffPrefill | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.one_off = one_off


@dataclass(frozen=True, slots=True)
class RuleInput:
    weekday: int
    window_earliest: time
    window_latest: time
    party_size: int


@dataclass(frozen=True, slots=True)
class OneOffInput:
    account_id: CourseAccountId
    target_date: date
    window_earliest: time
    window_latest: time
    party_size: int


@dataclass(frozen=True, slots=True)
class DashboardRow:
    """What the dashboard renders per row: DB status + the account snapshot, with its age
    labelled, and a mismatch badge (§7.4)."""

    row: RequestRow
    snapshot_observed_at: datetime | None
    snapshot_trusted: bool
    mismatch: str | None  # "not_seen_at_course" | "manual_reservation" | None
    # Always False until the booking runner (MU-9a) persists which rows ran search-only (§5.3):
    # there is no stored signal to read yet.
    search_only_last_drop: bool
    snapshot_label: str  # "as of 07:53 (7 min ago)" | "not checked yet"
    booked_tee_time_local: str | None  # "09:30", course-local
    can_rerequest: bool  # a cancelled date with no active row: offer "Re-request this date"
    frozen: bool


@dataclass(frozen=True, slots=True)
class RuleView:
    rule: StandingRule
    account: CourseAccount

    @property
    def weekday_name(self) -> str:
        return WEEKDAY_NAMES[self.rule.weekday]


class CancelRefusedError(ActionRefusedError):
    """The cancel could not be performed safely (dry-run, booker lease held, untrusted
    snapshot, not owned without confirmation, the course refused). Nothing was cancelled; the
    web renders it as a 409 with the message."""


# --- form parsing (pure) ----------------------------------------------------------------------


def parse_id(raw: str) -> UUID:
    """A path/form id. Malformed is NOT-FOUND (the same 404), never a 422 that would tell a
    prober which ids are well-formed."""
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError) as e:
        raise WebNotFoundError from e


def _label(key: str) -> str:
    return key.replace("_", " ")


def _field(form: Mapping[str, str], key: str) -> str:
    value = form.get(key, "").strip()
    if not value:
        raise InvalidInputError(f"{_label(key)} is required")
    return value


def _hhmm(form: Mapping[str, str], key: str) -> time:
    try:
        parsed = time.fromisoformat(_field(form, key))
    except ValueError as e:
        raise InvalidInputError(f"{_label(key)} must be a time like 08:30") from e
    return parsed.replace(second=0, microsecond=0, tzinfo=None)


def _int_in(form: Mapping[str, str], key: str, lo: int, hi: int) -> int:
    try:
        value = int(_field(form, key))
    except ValueError as e:
        raise InvalidInputError(f"{_label(key)} must be a number") from e
    if not lo <= value <= hi:
        raise InvalidInputError(f"{_label(key)} must be between {lo} and {hi}")
    return value


def _window(form: Mapping[str, str]) -> tuple[time, time]:
    earliest, latest = _hhmm(form, "window_earliest"), _hhmm(form, "window_latest")
    if earliest >= latest:
        raise InvalidInputError("the window's earliest time must be before its latest time")
    return earliest, latest


def parse_rule_form(form: Mapping[str, str]) -> RuleInput:
    earliest, latest = _window(form)
    return RuleInput(
        weekday=_int_in(form, "weekday", 0, 6),
        window_earliest=earliest,
        window_latest=latest,
        party_size=_int_in(form, "party_size", MIN_PARTY, MAX_PARTY),
    )


def parse_one_off_form(form: Mapping[str, str]) -> OneOffInput:
    account_id = CourseAccountId(parse_id(form.get("account_id", "")))
    try:
        target_date = date.fromisoformat(_field(form, "target_date"))
    except ValueError as e:
        raise InvalidInputError("date must look like 2026-10-03") from e
    earliest, latest = _window(form)
    return OneOffInput(
        account_id=account_id,
        target_date=target_date,
        window_earliest=earliest,
        window_latest=latest,
        party_size=_int_in(form, "party_size", MIN_PARTY, MAX_PARTY),
    )


def parse_version(form: Mapping[str, str]) -> int:
    return _int_in(form, "version", 0, 2**31)


# --- error translation (§8.2) -----------------------------------------------------------------


def _refused(exc: Exception, *, row: RequestRow | None = None) -> ActionRefusedError:
    """Map a store refusal to its user-facing message. Order matters: ``RuleNoLongerCoversError``
    is a ``TransitionRefusedError``."""
    if isinstance(exc, RuleNoLongerCoversError) and row is not None:
        return ActionRefusedError(
            f"This rule no longer covers {row.target_date.isoformat()}; "
            "add it as a one-off instead",
            one_off=OneOffPrefill(
                account_id=row.course_account_id,
                target_date=row.target_date,
                window_earliest=row.window_earliest,
                window_latest=row.window_latest,
                party_size=row.party_size,
            ),
        )
    if isinstance(exc, RowLeaseError):
        when = f" for {row.target_date.isoformat()}" if row is not None else ""
        return ActionRefusedError(
            f"Booking in progress{when}: the bot is working on this date right now. "
            "Try again after the drop (about 20 minutes)."
        )
    if isinstance(exc, VersionConflictError):
        return ActionRefusedError(
            "This rule changed since you loaded the page. Reload and try again."
        )
    message = str(exc)
    return ActionRefusedError(message[:1].upper() + message[1:] if message else "Not allowed.")


# --- dashboard --------------------------------------------------------------------------------


def _age_label(observed_at: datetime, *, now: datetime, tz: ZoneInfo) -> str:
    local = observed_at.astimezone(tz)
    stamp = local.strftime("%H:%M")
    if local.date() != now.astimezone(tz).date():
        stamp = local.strftime("%b %d %H:%M")
    minutes = max(0, int((now - observed_at).total_seconds() // 60))
    age = f"{minutes} min ago" if minutes < _MINUTES_LABEL_LIMIT else f"{minutes // 60} h ago"
    return f"as of {stamp} ({age})"


def _snapshot_label(snap: ReservationSnapshot | None, *, now: datetime, tz: ZoneInfo) -> str:
    if snap is None:
        return "not checked yet"
    label = _age_label(snap.observed_at, now=now, tz=tz)
    return label if snap.trusted else f"{label}, unverified"


def _visible_rows(rows: list[RequestRow], *, now: datetime) -> list[RequestRow]:
    shown: list[RequestRow] = []
    for row in rows:
        local_today = now.astimezone(ZoneInfo(row.timezone)).date()
        last = local_today + timedelta(days=MIN_HORIZON_DAYS)
        if local_today <= row.target_date <= last and row.status in _VISIBLE:
            shown.append(row)
    return shown


class _DashboardReader:
    """Per-request memo of the snapshot / ledger reads, so each account's snapshot and each
    (account, date)'s ledger is read at most once."""

    def __init__(self, store: TenantStore) -> None:
        self._store = store
        self._snapshots: dict[CourseAccountId, ReservationSnapshot | None] = {}
        self._owned: dict[tuple[CourseAccountId, date], frozenset[str]] = {}

    async def snapshot(self, account_id: CourseAccountId) -> ReservationSnapshot | None:
        if account_id not in self._snapshots:
            self._snapshots[account_id] = await self._store.get_snapshot(account_id)
        return self._snapshots[account_id]

    async def owned(self, account_id: CourseAccountId, day: date) -> frozenset[str]:
        key = (account_id, day)
        if key not in self._owned:
            ledger = await self._store.list_owned_bookings(account_id, target_date=day)
            self._owned[key] = frozenset(b.raw_reservation_id for b in ledger)
        return self._owned[key]

    async def mismatch(self, row: RequestRow, snap: ReservationSnapshot | None) -> str | None:
        """§7.4 badges, from a TRUSTED snapshot only (an untrusted one is shown, never believed)."""
        if snap is None or not snap.trusted:
            return None
        if row.status is RowStatus.BOOKED and row.booked_raw_id not in {
            e.raw_id for e in snap.entries
        }:
            return "not_seen_at_course"
        tz = ZoneInfo(row.timezone)
        on_date = {
            e.raw_id for e in snap.entries if e.tee_time.astimezone(tz).date() == row.target_date
        }
        owned = await self.owned(row.course_account_id, row.target_date)
        if on_date - owned - {row.booked_raw_id}:
            return "manual_reservation"
        return None


async def dashboard(store: TenantStore, *, user_id: UserId, clock: Clock) -> list[DashboardRow]:
    """Rows for the next 21 days + snapshot age. NEVER logs in to ForeUP (§8.6).

    One ``list_rows_for_user`` read, then each account's persisted snapshot and, only for a
    TRUSTED snapshot, the ownership ledger of the dates shown (§7.4: a booked row absent from a
    trusted snapshot is "not seen at course"; a snapshot reservation on a shown date that the
    ledger does not own is a "manual reservation")."""
    now = clock.now_utc()
    rows = await store.list_rows_for_user(
        user_id,
        from_date=now.date() - timedelta(days=1),
        to_date=now.date() + timedelta(days=MIN_HORIZON_DAYS + 1),
    )
    occupied = {
        (r.course_account_id, r.target_date) for r in rows if r.status in ACTIVE_ROW_STATUSES
    }
    reader = _DashboardReader(store)
    out: list[DashboardRow] = []
    for row in _visible_rows(rows, now=now):
        snap = await reader.snapshot(row.course_account_id)
        tz = ZoneInfo(row.timezone)
        frozen = row_is_frozen(row, now=now)
        booked_local = None
        if row.status is RowStatus.BOOKED and row.booked_tee_time is not None:
            booked_local = row.booked_tee_time.astimezone(tz).strftime("%H:%M")
        out.append(
            DashboardRow(
                row=row,
                snapshot_observed_at=snap.observed_at if snap is not None else None,
                snapshot_trusted=snap.trusted if snap is not None else False,
                mismatch=await reader.mismatch(row, snap),
                search_only_last_drop=False,
                snapshot_label=_snapshot_label(snap, now=now, tz=tz),
                booked_tee_time_local=booked_local,
                can_rerequest=(
                    row.status is RowStatus.CANCELLED
                    and (row.course_account_id, row.target_date) not in occupied
                    and not frozen
                ),
                frozen=frozen,
            )
        )
    return out


async def list_accounts(store: TenantStore, *, user_id: UserId) -> list[CourseAccount]:
    return await store.list_accounts_for_user(user_id)


async def list_rules(store: TenantStore, *, user_id: UserId) -> list[RuleView]:
    accounts = {a.id: a for a in await store.list_accounts_for_user(user_id)}
    return [
        RuleView(rule=rule, account=accounts[rule.course_account_id])
        for rule in await store.list_rules_for_user(user_id)
        if rule.course_account_id in accounts
    ]


# --- rules ------------------------------------------------------------------------------------


def _policy_for(account: CourseAccount, policies: Mapping[str, ReleasePolicy]) -> ReleasePolicy:
    policy = policies.get(str(account.course_id))
    if policy is None:
        raise ActionRefusedError("Standing rules are not available for this course yet.")
    return policy


async def _own_account(
    store: TenantStore, *, user_id: UserId, account_id: CourseAccountId
) -> CourseAccount:
    account = await store.get_account(account_id, user_id=user_id)
    if account is None:
        raise WebNotFoundError
    return account


async def _own_rule(store: TenantStore, *, user_id: UserId, rule_id: RuleId) -> StandingRule:
    for rule in await store.list_rules_for_user(user_id):
        if rule.id == rule_id:
            return rule
    raise WebNotFoundError


def _conflict(rule: StandingRule) -> ActionRefusedError:
    return ActionRefusedError(
        f"You already have an active rule for {WEEKDAY_NAMES[rule.weekday]} at this course. "
        "Edit or deactivate that rule instead."
    )


async def create_rule(
    store: TenantStore,
    *,
    user_id: UserId,
    account_id: CourseAccountId,
    rule_input: RuleInput,
    policies: Mapping[str, ReleasePolicy],
    cutoff: BookingCutoffConfig,
    clock: Clock,
) -> MaterializeReport:
    """Create an ACTIVE rule, then materialize it synchronously (§7.7: the web runs it on rule
    create). A second active rule on the weekday is ``RuleConflictError`` -> 409 (round-3 SF1)."""
    account = await _own_account(store, user_id=user_id, account_id=account_id)
    policy = _policy_for(account, policies)
    rule = StandingRule(
        id=RuleId(uuid4()),
        course_account_id=account.id,
        weekday=rule_input.weekday,
        window_earliest=rule_input.window_earliest,
        window_latest=rule_input.window_latest,
        party_size=rule_input.party_size,
        active=True,
        materialized_through=None,
        version=1,
    )
    try:
        stored = await store.upsert_rule(rule, user_id=user_id)
    except RuleConflictError as e:
        raise _conflict(rule) from e
    except TenantNotFoundError as e:
        raise WebNotFoundError from e
    try:
        return await materialize_rule(
            stored, store=store, policy=policy, cutoff=cutoff, now=clock.now_utc()
        )
    except TransitionRefusedError as e:
        raise _refused(e) from e


async def _edit(
    store: TenantStore,
    *,
    user_id: UserId,
    old: StandingRule,
    new: StandingRule,
    policies: Mapping[str, ReleasePolicy],
    cutoff: BookingCutoffConfig,
    clock: Clock,
) -> MaterializeReport:
    account = await _own_account(store, user_id=user_id, account_id=old.course_account_id)
    policy = _policy_for(account, policies)
    try:
        return await apply_rule_edit(
            old,
            new,
            store=store,
            policy=policy,
            cutoff=cutoff,
            now=clock.now_utc(),
            user_id=user_id,
        )
    except RuleConflictError as e:
        raise _conflict(new) from e
    except TenantNotFoundError as e:
        raise WebNotFoundError from e
    except (VersionConflictError, TransitionRefusedError) as e:
        raise _refused(e) from e


async def edit_rule(
    store: TenantStore,
    *,
    user_id: UserId,
    rule_id: RuleId,
    rule_input: RuleInput,
    version: int,
    policies: Mapping[str, ReleasePolicy],
    cutoff: BookingCutoffConfig,
    clock: Clock,
) -> MaterializeReport:
    """Window / party / weekday edit via ``apply_rule_edit`` (synchronous materialize).
    ``version`` is the one the form was rendered from: a stale page is
    ``VersionConflictError`` -> 409."""
    old = await _own_rule(store, user_id=user_id, rule_id=rule_id)
    new = replace(
        old,
        weekday=rule_input.weekday,
        window_earliest=rule_input.window_earliest,
        window_latest=rule_input.window_latest,
        party_size=rule_input.party_size,
        version=version,
    )
    return await _edit(
        store, user_id=user_id, old=old, new=new, policies=policies, cutoff=cutoff, clock=clock
    )


async def set_rule_active(
    store: TenantStore,
    *,
    user_id: UserId,
    rule_id: RuleId,
    active: bool,
    version: int,
    policies: Mapping[str, ReleasePolicy],
    cutoff: BookingCutoffConfig,
    clock: Clock,
) -> MaterializeReport:
    """Deactivate (withdraw pending/superseded rows in the §7.7 reset order; booked and skipped
    rows are kept) or reactivate (re-materialize) a rule."""
    old = await _own_rule(store, user_id=user_id, rule_id=rule_id)
    new = replace(old, active=active, version=version)
    return await _edit(
        store, user_id=user_id, old=old, new=new, policies=policies, cutoff=cutoff, clock=clock
    )


# --- dated rows -------------------------------------------------------------------------------


async def create_one_off(
    store: TenantStore, *, user_id: UserId, one_off: OneOffInput, clock: Clock
) -> RequestRow:
    """An explicit dated row. Also the "Re-request this date" action after a cancel (the only
    thing that reopens a user-terminal date, §3.4) and the "add it as a one-off" follow-up."""
    try:
        return await store.create_explicit_row(
            user_id=user_id,
            account_id=one_off.account_id,
            target_date=one_off.target_date,
            window_earliest=one_off.window_earliest,
            window_latest=one_off.window_latest,
            party_size=one_off.party_size,
            now=clock.now_utc(),
        )
    except TenantNotFoundError as e:
        raise WebNotFoundError from e
    except (RowLeaseError, TransitionRefusedError) as e:
        raise _refused(e) from e


async def _transition(
    store: TenantStore,
    *,
    user_id: UserId,
    row_id: RowId,
    to: RowStatus,
    reason: str | None,
    clock: Clock,
) -> RequestRow:
    row = await store.get_row(row_id, user_id=user_id)
    if row is None:
        raise WebNotFoundError
    try:
        return await store.transition_row(
            row_id, user_id=user_id, to=to, actor=Actor.WEB, reason=reason, now=clock.now_utc()
        )
    except TenantNotFoundError as e:
        raise WebNotFoundError from e
    except (RowLeaseError, TransitionRefusedError) as e:
        raise _refused(e, row=row) from e


async def skip_row(
    store: TenantStore, *, user_id: UserId, row_id: RowId, clock: Clock
) -> RequestRow:
    """pending -> skipped. A BOOKED row is refused with the store's "use Cancel instead"."""
    return await _transition(
        store, user_id=user_id, row_id=row_id, to=RowStatus.SKIPPED, reason=None, clock=clock
    )


async def unskip_row(
    store: TenantStore, *, user_id: UserId, row_id: RowId, clock: Clock
) -> RequestRow:
    """skipped -> pending. A rule row whose rule moved away is ``RuleNoLongerCoversError`` ->
    409 with the "add it as a one-off" action (round-6)."""
    return await _transition(
        store, user_id=user_id, row_id=row_id, to=RowStatus.PENDING, reason=None, clock=clock
    )


async def withdraw_row(
    store: TenantStore, *, user_id: UserId, row_id: RowId, clock: Clock
) -> RequestRow:
    """Withdraw a PENDING one-off (``user_withdrawn``; restores a superseded rule row, D2)."""
    return await _transition(
        store,
        user_id=user_id,
        row_id=row_id,
        to=RowStatus.WITHDRAWN,
        reason=USER_WITHDRAW_REASON,
        clock=clock,
    )


# --- MU-14 --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeLimits:
    """The §8.4 login-probe limits, all DB-backed (``probe`` docs, per-item TTL 2 h).

    The lockout is the conservative form of "2 consecutive failures for a username -> 15 min
    lockout": ANY ``lockout_probes`` probes of the username inside ``lockout_window_s`` lock it,
    because ``count_login_probes`` counts probes and does not read their outcome. It can only
    refuse MORE often than the plan's rule (never less), which is the safe direction while the
    ForeUP lockout threshold is unknown (Spike S-M6). ``refreshes_per_account_per_hour`` is
    the §8.6 hard cap on live refreshes."""

    per_user_per_hour: int = 5
    per_username_per_hour: int = 3
    site_per_hour: int = 30
    lockout_probes: int = 2
    lockout_window_s: int = 15 * 60
    refreshes_per_account_per_hour: int = 6


class RateLimitedError(ActionRefusedError):
    """A login-probe or refresh limit is reached (the web's 429). No ForeUP call was made."""


_HOUR = timedelta(hours=1)
_LOGIN_FAILED = (
    "Login failed: the course did not accept that username and password. Nothing was saved."
)
_PROBE_THROTTLED = "Too many login attempts for now. Please try again later."


def probe_username_hash(course_id: CourseId, username: str) -> str:
    """The ``username_hash`` a probe is recorded under: SHA-256 of ``probe|<course>|<casefolded
    username>``. The username itself is PII and is never stored on a probe doc (§9.3)."""
    key = f"probe|{course_id}|{username.strip().casefold()}"
    return hashlib.sha256(key.encode()).hexdigest()


def refresh_probe_hash(account_id: CourseAccountId) -> str:
    """The probe key a live REFRESH is recorded under, so the §8.6 per-account cap is DB-backed
    through the existing probe docs (no new store method) while staying apart from every
    username's connect budget."""
    return hashlib.sha256(f"refresh|{account_id}".encode()).hexdigest()


async def _connect_probes_by_user(store: TenantStore, *, user_id: UserId, since: datetime) -> int:
    """The user's CONNECT probes: every probe of theirs minus their accounts' refreshes."""
    total = await store.count_login_probes(user_id=user_id, username_hash=None, since=since)
    for account in await store.list_accounts_for_user(user_id):
        total -= await store.count_login_probes(
            user_id=user_id, username_hash=refresh_probe_hash(account.id), since=since
        )
    return total


async def _check_probe_limits(
    store: TenantStore,
    *,
    user_id: UserId,
    username_hash: str,
    limits: ProbeLimits,
    now: datetime,
) -> None:
    """Rate-check FIRST (§8.4), before any adapter is built. Raises ``RateLimitedError``."""
    since = now - _HOUR
    lockout_since = now - timedelta(seconds=limits.lockout_window_s)
    by_user = await _connect_probes_by_user(store, user_id=user_id, since=since)
    by_username = await store.count_login_probes(
        user_id=None, username_hash=username_hash, since=since
    )
    recent = await store.count_login_probes(
        user_id=None, username_hash=username_hash, since=lockout_since
    )
    site = await store.count_login_probes(user_id=None, username_hash=None, since=since)
    if (
        by_user >= limits.per_user_per_hour
        or by_username >= limits.per_username_per_hour
        or recent >= limits.lockout_probes
        or site >= limits.site_per_hour
    ):
        log.info("web: login probe refused by rate limit (user %s)", user_id)
        raise RateLimitedError(_PROBE_THROTTLED)


def _register_password(password: str) -> None:
    """E7 (§9.4): mask the plaintext in every log line for the rest of the process."""
    if register_secret_literals([password]) == 0:
        log.warning("web: a submitted password is shorter than the log-mask floor")


def _login_established(adapter: CourseAdapter) -> bool:
    return adapter.is_authenticated if isinstance(adapter, AuthStateReportable) else True


def _snapshot_trusted(adapter: CourseAdapter) -> bool:
    return adapter.snapshot_trusted if isinstance(adapter, ReservationSnapshotHealth) else True


async def _close(adapter: CourseAdapter) -> None:
    try:
        await adapter.aclose()
    except Exception as exc:
        log.warning("web: adapter close failed (%s)", type(exc).__name__)


async def _probe_login(
    *,
    account: CourseAccount,
    password: str,
    adapter_factory: AdapterFactory,
) -> bool:
    """ONE live ``authenticate`` on a throwaway adapter (no shared CAPTCHA pool, ``dry_run``
    so it can never book). NEVER retried (PLAN §8.1/§12): any exception is a failed probe.
    Logs the exception CLASS only (a message could echo the login)."""
    adapter = adapter_factory(
        course_id=account.course_id, account=account, pool=None, lease_key=None, dry_run=True
    )
    try:
        await adapter.authenticate(CourseCredentials(username=account.username, password=password))
        return _login_established(adapter)
    except RateLimitError as exc:
        log.warning("web: login probe for account %s rate-limited by the course", account.id)
        raise RateLimitedError(
            "The course is limiting logins right now. Please try again later."
        ) from exc
    except Exception as exc:
        log.warning("web: login probe for account %s failed (%s)", account.id, type(exc).__name__)
        return False
    finally:
        await _close(adapter)


async def _audit(
    store: TenantStore,
    *,
    user_id: UserId,
    action: str,
    row_id: RowId | None,
    detail: Mapping[str, object],
    at: datetime,
) -> None:
    """Best-effort audit (the ``global`` partition can't join an account batch, §8.5 step 5):
    a failure is logged at ERROR and never undoes or blocks what was already committed."""
    try:
        await store.append_audit(
            user_id=user_id, action=action, row_id=row_id, detail=detail, at=at
        )
    except Exception as exc:
        log.error("web: audit %s not written (%s)", action, type(exc).__name__)


async def _probe_and_store(
    store: TenantStore,
    *,
    user_id: UserId,
    base: CourseAccount,
    password: str,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
    limits: ProbeLimits,
) -> CourseAccount:
    """Shared by connect and re-verify: rate-check -> one probe -> record it -> on success
    encrypt with AAD and upsert. The plaintext is registered (E7) before anything else."""
    _register_password(password)
    username_hash = probe_username_hash(base.course_id, base.username)
    await _check_probe_limits(
        store, user_id=user_id, username_hash=username_hash, limits=limits, now=clock.now_utc()
    )
    ok = False
    try:
        ok = await _probe_login(account=base, password=password, adapter_factory=adapter_factory)
    finally:
        await store.record_login_probe(
            user_id=user_id,
            course_id=base.course_id,
            username_hash=username_hash,
            ok=ok,
            at=clock.now_utc(),
        )
    if not ok:
        raise ActionRefusedError(_LOGIN_FAILED)
    now = clock.now_utc()
    verified = replace(
        base,
        key_id=keyring.active_kid,
        status=AccountStatus.ACTIVE,
        consecutive_soft_auth_failures=0,
        verified_at=now,
    )
    account = replace(
        verified,
        password_ciphertext=encrypt_password(keyring, password, aad=credential_aad(verified)),
    )
    try:
        await store.upsert_account(account)
    except UniquenessConflictError as e:
        raise ActionRefusedError(
            "That course login can't be connected here: it is already connected, or this "
            "course has no free account places."
        ) from e
    await _audit(
        store,
        user_id=user_id,
        action="account_verified",
        row_id=None,
        detail={"course_account_id": str(account.id), "course_id": str(account.course_id)},
        at=now,
    )
    return account


async def connect_account(
    store: TenantStore,
    *,
    user_id: UserId,
    course_id: CourseId,
    username: str,
    password: str,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
    limits: ProbeLimits,
) -> CourseAccount:
    """Rate-check -> live ``authenticate`` on a throwaway adapter -> require
    ``is_authenticated`` -> encrypt with AAD -> store (``provenance=user_supplied``). On failure
    nothing is stored and a generic error is shown (§8.4). The account id is derived from the
    SESSION user, so a connect can never write another user's account."""
    username = username.strip()
    if not username:
        raise InvalidInputError("username is required")
    if not password:
        raise InvalidInputError("password is required")
    account_id = derive_account_id(user_id, course_id)
    existing = await store.get_account(account_id, user_id=user_id)
    base = CourseAccount(
        id=account_id,
        user_id=user_id,
        course_id=course_id,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=username,
        password_ciphertext="",
        key_id=keyring.active_kid,
        status=AccountStatus.ACTIVE,
        otp_mailbox=existing.otp_mailbox if existing is not None else None,
    )
    return await _probe_and_store(
        store,
        user_id=user_id,
        base=base,
        password=password,
        keyring=keyring,
        adapter_factory=adapter_factory,
        clock=clock,
        limits=limits,
    )


async def reverify_account(
    store: TenantStore,
    *,
    user_id: UserId,
    account_id: CourseAccountId,
    password: str,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
    limits: ProbeLimits,
) -> CourseAccount:
    """Re-probe an existing account (typically after ``auth_failed``, §7.5) with a freshly
    typed password: the same limits and single probe as connect; on success the ciphertext is
    replaced and the account is ACTIVE again with its soft-failure count reset."""
    if not password:
        raise InvalidInputError("password is required")
    account = await _own_account(store, user_id=user_id, account_id=account_id)
    return await _probe_and_store(
        store,
        user_id=user_id,
        base=account,
        password=password,
        keyring=keyring,
        adapter_factory=adapter_factory,
        clock=clock,
        limits=limits,
    )


class RefreshCache:
    """The in-process §8.6 TTL cache of live-refresh snapshots, keyed by account id. Coherent
    because the web runs ONE replica (§8.1). Lookups happen only AFTER the user-scoped account
    read, so a cached snapshot is never served to anyone but the account's owner."""

    def __init__(self, *, ttl_s: float) -> None:
        self._ttl = timedelta(seconds=ttl_s)
        self._entries: dict[CourseAccountId, tuple[datetime, ReservationSnapshot]] = {}

    def get(self, account_id: CourseAccountId, *, now: datetime) -> ReservationSnapshot | None:
        entry = self._entries.get(account_id)
        if entry is None or now - entry[0] >= self._ttl:
            return None
        return entry[1]

    def put(self, snapshot: ReservationSnapshot, *, now: datetime) -> None:
        self._entries[snapshot.course_account_id] = (now, snapshot)

    def invalidate(self, account_id: CourseAccountId) -> None:
        self._entries.pop(account_id, None)


_REVERIFY_FIRST = (
    "The course rejected this account's saved login: re-verify it with your password first."
)
_SNAPSHOT_UNTRUSTED = (
    "We logged in but couldn't read your reservations from the course. Nothing was updated; "
    "try again in a few minutes."
)


def _usable_account(account: CourseAccount) -> None:
    """``auth_failed`` / ``disabled`` accounts never log in (PLAN §12: never hammer a login)."""
    if account.status is AccountStatus.AUTH_FAILED:
        raise ActionRefusedError(_REVERIFY_FIRST)
    if account.status is not AccountStatus.ACTIVE:
        raise ActionRefusedError("This course account is disabled.")


def _decrypt_for_request(account: CourseAccount, keyring: Keyring) -> str:
    """Decrypt in process and E7-register the plaintext before any use. A bad blob names the
    account id and the error class only."""
    try:
        password = decrypt_password(
            keyring, account.password_ciphertext, aad=credential_aad(account)
        )
    except CredentialDecryptError as exc:
        log.error("web: account %s: credential decrypt failed (%s)", account.id, type(exc).__name__)
        raise ActionRefusedError(
            "This account's saved login can't be read. Please re-connect it."
        ) from exc
    _register_password(password)
    return password


def _snapshot_from(
    account_id: CourseAccountId,
    reservations: list[ExistingReservation],
    *,
    trusted: bool,
    now: datetime,
) -> ReservationSnapshot:
    return ReservationSnapshot(
        course_account_id=account_id,
        observed_at=now,
        source="refresh",
        trusted=trusted,
        entries=tuple(
            SnapshotEntry(
                raw_id=r.confirmation_code.removeprefix(MANAGED_BOOKING_TAG),
                tee_time=r.tee_time,
                party_size=r.party_size,
            )
            for r in reservations
        ),
    )


async def _check_refresh_limits(
    store: TenantStore, *, account: CourseAccount, limits: ProbeLimits, now: datetime
) -> None:
    since = now - _HOUR
    mine = await store.count_login_probes(
        user_id=None, username_hash=refresh_probe_hash(account.id), since=since
    )
    site = await store.count_login_probes(user_id=None, username_hash=None, since=since)
    if mine >= limits.refreshes_per_account_per_hour or site >= limits.site_per_hour:
        log.info("web: refresh of account %s refused by rate limit", account.id)
        raise RateLimitedError(
            "This account was refreshed too often in the last hour. Please try again later."
        )


async def _live_login(
    store: TenantStore,
    *,
    account: CourseAccount,
    password: str,
    adapter_factory: AdapterFactory,
    clock: Clock,
) -> CourseAdapter:
    """ONE authenticate for an existing account, recorded as a refresh probe. Returns the
    (still open) adapter of an established session; a soft failure is
    counted (§7.5). The caller closes the adapter. Never retried."""
    adapter = adapter_factory(
        course_id=account.course_id, account=account, pool=None, lease_key=None, dry_run=True
    )
    ok = False
    try:
        await adapter.authenticate(CourseCredentials(username=account.username, password=password))
        ok = _login_established(adapter)
    except RateLimitError as exc:
        await _close(adapter)
        raise RateLimitedError(
            "The course is limiting logins right now. Please try again later."
        ) from exc
    except Exception as exc:
        await _close(adapter)
        log.warning("web: login for account %s failed (%s)", account.id, type(exc).__name__)
        raise ActionRefusedError("We couldn't log in to the course. Nothing was changed.") from exc
    finally:
        await store.record_login_probe(
            user_id=account.user_id,
            course_id=account.course_id,
            username_hash=refresh_probe_hash(account.id),
            ok=ok,
            at=clock.now_utc(),
        )
    if not ok:
        await _close(adapter)
        count = await store.record_soft_auth_failure(account.id)
        log.warning("web: account %s: soft login failure #%d", account.id, count)
        raise ActionRefusedError(
            "We couldn't log in to the course with the saved login. Nothing was changed."
        )
    return adapter


async def refresh_account(
    store: TenantStore,
    *,
    user_id: UserId,
    account_id: CourseAccountId,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
    cache: RefreshCache,
    limits: ProbeLimits,
) -> ReservationSnapshot:
    """Live login + list, persisted as a snapshot. Served from the in-process TTL cache inside
    ``refresh_ttl_s``; hard-capped per account per hour (§8.6). Honours ``snapshot_trusted``
    (§7.5): an untrusted list is neither persisted nor cached, so the dashboard keeps the last
    trusted snapshot instead of showing a stale or empty one as fact."""
    account = await _own_account(store, user_id=user_id, account_id=account_id)
    now = clock.now_utc()
    cached = cache.get(account.id, now=now)
    if cached is not None:
        return cached
    _usable_account(account)
    await _check_refresh_limits(store, account=account, limits=limits, now=now)
    password = _decrypt_for_request(account, keyring)
    adapter = await _live_login(
        store, account=account, password=password, adapter_factory=adapter_factory, clock=clock
    )
    try:
        reservations = await adapter.list_reservations()
        trusted = _snapshot_trusted(adapter)
    finally:
        await _close(adapter)
    if not trusted:
        log.warning("web: account %s: UNTRUSTED reservation snapshot; not persisted", account.id)
        raise ActionRefusedError(_SNAPSHOT_UNTRUSTED)
    snapshot = _snapshot_from(account.id, reservations, trusted=True, now=clock.now_utc())
    await store.save_snapshot(snapshot)
    cache.put(snapshot, now=clock.now_utc())
    return snapshot


# The web's row lease (§3.5): long enough for one login + list + cancel round-trip.
WEB_LEASE_SECONDS = 60.0
_COULDNT_VERIFY = "We couldn't verify your booking with the course, so nothing was cancelled."
_UNOWNED = (
    "This booking wasn't made by TeeTimeBooker. Tick the confirmation box to cancel it anyway."
)


def _booked_row(row: RequestRow) -> str:
    if row.status is not RowStatus.BOOKED:
        raise ActionRefusedError("Only a booked tee time can be cancelled.")
    if row.booked_raw_id is None:
        raise CancelRefusedError(
            "This booking has no course reservation id on record, so it can't be cancelled "
            "from here."
        )
    return row.booked_raw_id


async def _owned_entry(store: TenantStore, row: RequestRow, raw_id: str) -> OwnedBooking | None:
    for entry in await store.list_owned_bookings(
        row.course_account_id, target_date=row.target_date
    ):
        if entry.raw_reservation_id == raw_id and entry.course_id == row.course_id:
            return entry
    return None


async def _cancel_at_course(adapter: CourseAdapter, raw_id: str) -> None:
    """§8.5 step 4. A 404 / ForeUP's 400 "can't find that teetime" already return normally
    (adapter contract), so they count as success."""
    try:
        await adapter.cancel_reservation(raw_id)
    except RateLimitError as exc:
        raise RateLimitedError(
            "The course is limiting requests right now; nothing was cancelled. Try again later."
        ) from exc
    except CancelError as exc:
        raise CancelRefusedError(
            "The course refused the cancel; your tee time is unchanged."
        ) from exc
    except Exception as exc:
        log.warning("web: cancel at the course failed (%s)", type(exc).__name__)
        raise CancelRefusedError(
            "We couldn't confirm the cancel with the course; your tee time may be unchanged. "
            "Refresh in a few minutes to see."
        ) from exc


async def _after_commit(
    store: TenantStore,
    *,
    row: RequestRow,
    user_id: UserId,
    snapshot: ReservationSnapshot,
    reason: str,
    owned: bool,
    cache: RefreshCache | None,
    notifier: UserNotifier | None,
    now: datetime,
) -> None:
    """§8.5 steps 5b-6, all AFTER the batch committed and each best-effort: a failure is logged
    at ERROR and never undoes the cancel."""
    try:
        await store.save_snapshot(snapshot)
    except Exception as exc:
        log.error("web: post-cancel snapshot not saved (%s)", type(exc).__name__)
    if cache is not None:
        cache.put(snapshot, now=now)
    await _audit(
        store,
        user_id=user_id,
        action=f"cancel_{reason}",
        row_id=row.id,
        detail={"course_account_id": str(row.course_account_id), "owned": owned},
        at=now,
    )
    if notifier is None:
        return
    try:
        await notifier.send(
            UserEvent(
                kind=UserEventKind.CANCELLED,
                user_id=user_id,
                row_id=row.id,
                course_id=row.course_id,
                target_date=row.target_date,
                tee_time=row.booked_tee_time,
                confirmation=row.booked_confirmation,
                detail=(
                    "cancelled from the site"
                    if reason == "user"
                    else "it was already gone at the course"
                ),
                at=now,
            )
        )
    except Exception as exc:
        log.error("web: cancel email not sent (%s)", type(exc).__name__)


async def cancel_row(
    store: TenantStore,
    *,
    user_id: UserId,
    row_id: RowId,
    confirm_unowned: bool,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
    dry_run: bool,
    cache: RefreshCache | None = None,
    notifier: UserNotifier | None = None,
) -> RequestRow:
    """The managed-cancel path (§8.5): dry-run refusal (§7.8) -> row lease (60 s, through
    ``LeasedBookingStore`` with the fingerprint just read, M5) -> decrypt -> authenticate (must
    be authenticated + trusted snapshot) -> confirm the raw id is live (absent = CANCELLED
    ``already_gone``, no cancel call) -> ``cancel_reservation`` -> ONE ``record_outcomes`` batch
    (row CANCELLED(user) + slot release + ledger ``cancelled_user``) -> release. The snapshot,
    the ``global`` audit doc and the email follow best-effort (round-2 SF7). Raises
    ``CancelRefusedError`` if unsafe; never cancels an unowned reservation without
    ``confirm_unowned``. The web never calls ``book()`` (§9.5)."""
    row = await store.get_row(row_id, user_id=user_id)
    if row is None:
        raise WebNotFoundError
    if dry_run:
        raise CancelRefusedError(
            "Cancelling is switched off in this dry-run environment: nothing was cancelled."
        )
    raw_id = _booked_row(row)
    entry = await _owned_entry(store, row, raw_id)
    if entry is None and not confirm_unowned:
        raise CancelRefusedError(_UNOWNED)
    account = await _own_account(store, user_id=user_id, account_id=row.course_account_id)
    _usable_account(account)
    owner = f"web:{uuid4().hex}"
    leased = LeasedBookingStore(
        inner=InMemoryStore(),
        tenant=store,
        owner=owner,
        lease_seconds=WEB_LEASE_SECONDS,
        clock=clock,
        row_for_request={
            row.request_id: (
                row.id,
                RowFingerprint(status=row.status, version=row.version, booked_raw_id=raw_id),
            )
        },
    )
    try:
        async with leased.request_lock(row.request_id):
            return await _cancel_under_lease(
                store,
                row=row,
                raw_id=raw_id,
                entry=entry,
                account=account,
                owner=owner,
                keyring=keyring,
                adapter_factory=adapter_factory,
                clock=clock,
                cache=cache,
                notifier=notifier,
            )
    except ConcurrentRunError as exc:
        raise CancelRefusedError(
            f"Booking in progress for {row.target_date.isoformat()}: the bot is working on this "
            "date right now (or it just changed). Nothing was cancelled; try again after 06:20."
        ) from exc


async def _cancel_under_lease(
    store: TenantStore,
    *,
    row: RequestRow,
    raw_id: str,
    entry: OwnedBooking | None,
    account: CourseAccount,
    owner: str,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
    cache: RefreshCache | None,
    notifier: UserNotifier | None,
) -> RequestRow:
    password = _decrypt_for_request(account, keyring)
    try:
        adapter = await _live_login(
            store, account=account, password=password, adapter_factory=adapter_factory, clock=clock
        )
    except RateLimitedError:
        raise
    except ActionRefusedError as exc:
        raise CancelRefusedError(_COULDNT_VERIFY) from exc
    try:
        reservations = await adapter.list_reservations()
        if not _snapshot_trusted(adapter):
            log.warning("web: cancel of row %s: UNTRUSTED snapshot; refused", row.id)
            raise CancelRefusedError(_COULDNT_VERIFY)
        live = {r.confirmation_code.removeprefix(MANAGED_BOOKING_TAG) for r in reservations}
        reason = "user" if raw_id in live else "already_gone"
        if reason == "user":
            await _cancel_at_course(adapter, raw_id)
    finally:
        await _close(adapter)
    now = clock.now_utc()
    remaining = [
        r for r in reservations if r.confirmation_code.removeprefix(MANAGED_BOOKING_TAG) != raw_id
    ]
    state = BookingState.CANCELLED_USER if reason == "user" else BookingState.VANISHED
    outcome = RowOutcome(
        row_id=row.id,
        course_account_id=row.course_account_id,
        target_date=row.target_date,
        actor=Actor.WEB,
        to_status=RowStatus.CANCELLED,
        last_outcome=f"cancelled_{reason}",
        at=now,
        booking=replace(entry, state=state) if entry is not None else None,
        status_reason=reason,
        release_lease_owner=owner,
    )
    try:
        await store.record_outcomes([outcome])
    except Exception as exc:
        log.critical(
            "web: row %s: cancelled at the course (%s) but the row write FAILED (%s); the "
            "watcher's vanish inference will reconcile it",
            row.id,
            reason,
            type(exc).__name__,
        )
        raise ActionRefusedError(
            "The course cancelled your tee time, but saving that here failed. The dashboard "
            "will catch up after the next check."
        ) from exc
    snapshot = _snapshot_from(row.course_account_id, remaining, trusted=True, now=now)
    await _after_commit(
        store,
        row=row,
        user_id=account.user_id,
        snapshot=snapshot,
        reason=reason,
        owned=entry is not None,
        cache=cache,
        notifier=notifier,
        now=now,
    )
    cancelled = await store.get_row(row.id, user_id=account.user_id)
    return cancelled if cancelled is not None else row
