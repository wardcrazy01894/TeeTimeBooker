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

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ..core.clock import Clock
from ..core.config import BookingCutoffConfig
from ..core.models import CourseId
from ..core.release_policy import ReleasePolicy
from ..tenant.crypto import Keyring
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
    Actor,
    CourseAccount,
    CourseAccountId,
    RequestRow,
    ReservationSnapshot,
    RowId,
    RowStatus,
    RuleId,
    RuleNoLongerCoversError,
    StandingRule,
    TransitionRefusedError,
    UserId,
    row_is_frozen,
)
from ..tenant.runner import AdapterFactory
from ..tenant.store import (
    RowLeaseError,
    TenantNotFoundError,
    TenantStore,
    VersionConflictError,
)

_MU14 = "MULTIUSER_PLAN.md MU-14"

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


class CancelRefusedError(RuntimeError):
    """The cancel could not be performed safely (booker lease held, untrusted snapshot, not
    owned without confirmation). Nothing was cancelled."""


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
) -> CourseAccount:
    """Rate-check -> live ``authenticate`` on a throwaway adapter -> require
    ``is_authenticated`` -> encrypt with AAD -> store (``provenance=user_supplied``). On failure
    nothing is stored and a generic error is shown (§8.4)."""
    raise NotImplementedError(_MU14)


async def refresh_account(
    store: TenantStore,
    *,
    user_id: UserId,
    account_id: CourseAccountId,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
) -> ReservationSnapshot:
    """Live login + list, persisted as a snapshot. Served from the in-process TTL cache inside
    ``refresh_ttl_s``; hard-capped per account per hour (§8.6)."""
    raise NotImplementedError(_MU14)


async def cancel_row(
    store: TenantStore,
    *,
    user_id: UserId,
    row_id: RowId,
    confirm_unowned: bool,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
) -> RequestRow:
    """The managed-cancel path (§8.5): row lease (60 s) -> decrypt -> authenticate (must be
    authenticated + trusted snapshot) -> confirm the raw id is live -> ``cancel_reservation`` ->
    ONE transaction (row CANCELLED(user), ledger cancelled_user, snapshot, audit) -> release ->
    email. Raises ``CancelRefusedError`` if unsafe; never cancels an unowned reservation without
    ``confirm_unowned``."""
    raise NotImplementedError(_MU14)
