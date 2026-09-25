"""InMemoryTenantStore: the reference ``TenantStore`` (MULTIUSER_PLAN §3.7, MU-5).

Used by tests and by every tenant component until ``CosmosTenantStore`` (MU-8b) lands, and it is
the reference implementation of the conformance suite (``tests/tenant/conformance.py``). It
reproduces the Cosmos semantics the plan relies on, so a behaviour that passes here is a behaviour
Cosmos must also provide:

- **Deterministic ids** (§3.1): rule rows are ``models.rule_row_id(rule_id, date)`` (UNIQUE(rule,
  date)); accounts are ``models.derive_account_id(user_id, course_id)`` (UNIQUE(user, course)).
- **Date-slot pointer** (§3.2): ``self._slots[(account, date)]`` is the ``slot|<date>`` doc. A row
  is "active" iff the slot points at it, and ``_commit`` derives every slot create / replace /
  delete from the status change in the SAME batch, so the slot and the row statuses can never
  disagree.
- **Transactional batch + IfMatch** (§3.2/§3.5): ``_commit`` validates every write (each row must
  still equal the version the writer read; a create's id must be free; a slot claim must find the
  slot free) BEFORE mutating anything, so a batch is all-or-nothing. There is no ``await`` inside a
  mutation, so a batch is also atomic with respect to other coroutines.
- **Leases** (§3.5): a lease is data on the row (``lease_owner`` / ``lease_expires_at``), taken by
  a conditional replace; nothing is held in process.

Not durable: state lives for the process. Production multi-user state is Cosmos (§10.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from uuid import uuid4

from ..core.booking_cutoff import cutoff_instant
from ..core.config import BookingCutoffConfig
from ..core.models import CourseId
from ..core.redaction import redact_payload
from .materialize import RuleConflictError
from .models import (
    ACTIVE_ROW_STATUSES,
    AccountStatus,
    Actor,
    BookingState,
    CourseAccount,
    CourseAccountId,
    EventRow,
    OwnedBooking,
    RequestRow,
    ReservationSnapshot,
    RowFingerprint,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    StandingRule,
    TransitionRefusedError,
    User,
    UserId,
    UserStatus,
    check_create,
    check_transition,
    derive_account_id,
    is_user_terminal,
    lease_held,
    row_is_frozen,
    row_request_id,
    rule_row_id,
)
from .store import RowLeaseError, RowOutcome, TenantNotFoundError, UniquenessConflictError

# Consecutive soft login failures after which an account stops logging in (§7.5).
SOFT_AUTH_FAILURE_LIMIT = 3

_Write = tuple[RequestRow | None, RequestRow]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    user_id: UserId | None
    action: str
    row_id: RowId | None
    detail: dict[str, object]
    at: datetime


@dataclass(frozen=True, slots=True)
class _Probe:
    user_id: UserId
    course_id: CourseId
    username_hash: str
    ok: bool
    at: datetime


def _fingerprint_matches(row: RequestRow, expected: RowFingerprint | None) -> bool:
    if expected is None:
        return True
    return (row.status, row.version, row.booked_raw_id) == (
        expected.status,
        expected.version,
        expected.booked_raw_id,
    )


def _strip_ttb(code: str | None) -> str | None:
    if code is None:
        return None
    return code.removeprefix("TTB:")


class InMemoryTenantStore:
    """Dict-backed ``TenantStore`` with Cosmos semantics (see module docstring)."""

    def __init__(
        self,
        *,
        course_timezones: Mapping[CourseId, str],
        cutoff: BookingCutoffConfig,
        max_accounts_per_course: int = 8,
    ) -> None:
        self._course_timezones = dict(course_timezones)
        self._cutoff = cutoff
        self._max_accounts_per_course = max_accounts_per_course
        self._users: dict[UserId, User] = {}
        self._accounts: dict[CourseAccountId, CourseAccount] = {}
        self._rules: dict[RuleId, StandingRule] = {}
        self._rows: dict[RowId, RequestRow] = {}
        self._slots: dict[tuple[CourseAccountId, date], RowId] = {}
        self._ledger: dict[tuple[CourseAccountId, CourseId, str], OwnedBooking] = {}
        self._snapshots: dict[CourseAccountId, ReservationSnapshot] = {}
        self._probes: list[_Probe] = []
        self.audit_log: list[AuditEntry] = []

    # --- introspection (tests / conformance hook) ------------------------------------------

    def slot_pointer(self, account_id: CourseAccountId, day: date) -> RowId | None:
        """The ``slot|<date>`` doc's ``activeRowId`` (conformance ``StoreHarness`` hook)."""
        return self._slots.get((account_id, day))

    def course_timezone(self, course_id: CourseId) -> str:
        """The course's timezone; an unconfigured course is a loud ``KeyError``."""
        return self._course_timezones[course_id]

    # --- the batch primitive -------------------------------------------------------------

    def _commit(self, writes: Sequence[_Write]) -> None:
        """Apply one transactional batch: validate everything, then mutate (all or nothing)."""
        for old, new in writes:
            stored = self._rows.get(new.id)
            if old is None and stored is not None:
                raise TransitionRefusedError(f"row {new.id} already exists")
            if old is not None and stored != old:
                raise TransitionRefusedError(f"row {new.id} changed since it was read")
        slots = dict(self._slots)
        for old, new in writes:  # releases first, so a supersede frees the slot it re-points
            key = (new.course_account_id, new.target_date)
            leaving = old is not None and old.status in ACTIVE_ROW_STATUSES
            if leaving and new.status not in ACTIVE_ROW_STATUSES and slots.get(key) == new.id:
                del slots[key]
        for old, new in writes:
            key = (new.course_account_id, new.target_date)
            entering = old is None or old.status not in ACTIVE_ROW_STATUSES
            if entering and new.status in ACTIVE_ROW_STATUSES:
                if slots.get(key, new.id) != new.id:
                    raise TransitionRefusedError(
                        f"{new.target_date} already has an active row for this account"
                    )
                slots[key] = new.id
        self._slots = slots
        for _, new in writes:
            self._rows[new.id] = new

    # --- helpers -------------------------------------------------------------------------

    def _row(self, row_id: RowId) -> RequestRow:
        row = self._rows.get(row_id)
        if row is None:
            raise TenantNotFoundError(f"row {row_id}")
        return row

    def _account_for_user(
        self, account_id: CourseAccountId, user_id: UserId | None
    ) -> CourseAccount:
        account = self._accounts.get(account_id)
        if account is None or (user_id is not None and account.user_id != user_id):
            raise TenantNotFoundError(f"account {account_id}")
        return account

    def _history(self, account_id: CourseAccountId, day: date) -> list[RequestRow]:
        rows = (
            r
            for r in self._rows.values()
            if r.course_account_id == account_id and r.target_date == day
        )
        return sorted(rows, key=lambda r: r.id)

    def _cutoff_at(self, course_id: CourseId, day: date) -> datetime:
        tz = self.course_timezone(course_id)
        return cutoff_instant(day, timezone=tz, cutoff=self._cutoff).astimezone(UTC)

    def _new_row(
        self,
        *,
        row_id: RowId,
        account: CourseAccount,
        target_date: date,
        window: tuple[time, time],
        party_size: int,
        status: RowStatus,
        source: RowSource,
        rule_id: RuleId | None,
    ) -> RequestRow:
        return RequestRow(
            id=row_id,
            course_account_id=account.id,
            course_id=account.course_id,
            target_date=target_date,
            timezone=self.course_timezone(account.course_id),
            window_earliest=window[0],
            window_latest=window[1],
            party_size=party_size,
            status=status,
            source=source,
            cutoff_at=self._cutoff_at(account.course_id, target_date),
            request_id=row_request_id(row_id),
            version=1,
            rule_id=rule_id,
        )

    def _refuse_if_user_terminal(self, account_id: CourseAccountId, day: date) -> None:
        if any(is_user_terminal(r) for r in self._history(account_id, day)):
            raise TransitionRefusedError(
                f"{day} has a user-terminal row; only an explicit re-request reopens it"
            )

    def _restorable_superseded(self, explicit: RequestRow, now: datetime) -> RequestRow | None:
        """The rule row a withdrawn explicit row superseded, iff its rule is still active and the
        date is not frozen (§3.4 superseded -> pending)."""
        for row in self._history(explicit.course_account_id, explicit.target_date):
            if row.source is not RowSource.RULE or row.status is not RowStatus.SUPERSEDED:
                continue
            rule = self._rules.get(row.rule_id) if row.rule_id is not None else None
            if rule is not None and rule.active and not row_is_frozen(row, now=now):
                return row
        return None

    # --- TenantStore ---------------------------------------------------------------------

    async def initialize(self) -> None:
        return None

    # booking runner ------------------------------------------------------------------

    async def load_event_rows(
        self,
        *,
        targets: Mapping[CourseId, date],
        now: datetime,
    ) -> list[EventRow]:
        out: list[EventRow] = []
        for row in sorted(self._rows.values(), key=lambda r: r.id):
            account = self._accounts.get(row.course_account_id)
            if (
                row.status is RowStatus.PENDING
                and targets.get(row.course_id) == row.target_date
                and row.cutoff_at > now
                and account is not None
                and account.status is AccountStatus.ACTIVE
            ):
                out.append(EventRow(row=row, account=account))
        return out

    async def claim_rows(
        self,
        row_ids: Sequence[RowId],
        *,
        owner: str,
        until: datetime,
        now: datetime,
    ) -> frozenset[RowId]:
        claimed: set[RowId] = set()
        for row_id in row_ids:
            row = self._rows.get(row_id)
            if row is None or row.status is not RowStatus.PENDING:
                continue
            if lease_held(row, now=now) and row.lease_owner != owner:
                continue
            self._commit([(row, replace(row, lease_owner=owner, lease_expires_at=until))])
            claimed.add(row_id)
        return frozenset(claimed)

    async def record_outcomes(self, outcomes: Sequence[RowOutcome]) -> None:
        errors: list[Exception] = []
        for outcome in outcomes:
            try:
                self._apply_outcome(outcome)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup(f"record_outcomes: {len(errors)} row(s) not applied", errors)

    def _apply_outcome(self, o: RowOutcome) -> None:
        row = self._rows.get(o.row_id)
        try:
            if row is None:
                raise TenantNotFoundError(f"row {o.row_id}")
            self._commit([(row, self._outcome_row(row, o))])
        except (TransitionRefusedError, RowLeaseError, TenantNotFoundError):
            # The row moved: keep what the bot did (ledger by account + date) and make the
            # date's active row reconcile it on the next watcher run (M4).
            self._write_ledger(o, course_id=row.course_id if row is not None else None)
            self._flag_active_row(o.course_account_id, o.target_date)
            raise
        self._write_ledger(o, course_id=row.course_id)

    def _outcome_row(self, row: RequestRow, o: RowOutcome) -> RequestRow:
        holder = row.lease_owner is not None and row.lease_owner == o.release_lease_owner
        if o.to_status is not None:
            check_transition(row, o.to_status, actor=o.actor, now=o.at, reason=o.status_reason)
            if not holder:
                raise RowLeaseError(f"row {row.id}: {o.release_lease_owner!r} does not hold it")
        elif lease_held(row, now=o.at) and not holder:
            raise RowLeaseError(f"row {row.id} is leased by {row.lease_owner!r}")
        new = replace(row, last_outcome=o.last_outcome, last_outcome_at=o.at)
        if o.to_status is not None:
            new = replace(
                self._booking_fields(new, o),
                status=o.to_status,
                status_reason=o.status_reason,
                needs_reconcile=o.needs_reconcile,
            )
        elif o.needs_reconcile:
            new = replace(new, needs_reconcile=True)
        if o.clear_upgrade_marker:
            new = replace(new, upgrade_started_at=None)
        if holder:
            new = replace(new, lease_owner=None, lease_expires_at=None)
        changed = (new.status, new.needs_reconcile, new.booked_raw_id) != (
            row.status,
            row.needs_reconcile,
            row.booked_raw_id,
        )
        return replace(new, version=row.version + 1) if changed else new

    @staticmethod
    def _booking_fields(row: RequestRow, o: RowOutcome) -> RequestRow:
        if o.to_status is RowStatus.PENDING:  # upgrade cancelled the old slot: no booking held
            return replace(
                row,
                booked_tee_time=None,
                booked_confirmation=None,
                booked_raw_id=None,
                booked_at=None,
            )
        if o.to_status is not RowStatus.BOOKED:  # CANCELLED keeps booked_* as history
            return row
        raw_id = o.booking.raw_reservation_id if o.booking is not None else None
        tee = o.booking.tee_time if o.booking is not None else None
        if tee is None and o.result is not None and o.result.slot is not None:
            tee = o.result.slot.tee_time
        confirmation = o.result.confirmation_code if o.result is not None else None
        if confirmation is None and raw_id is not None:
            confirmation = f"TTB:{raw_id}"
        if raw_id is None and o.booking is None and o.result is not None:
            raw_id = _strip_ttb(o.result.confirmation_code)
        return replace(
            row,
            booked_raw_id=raw_id,
            booked_tee_time=tee,
            booked_confirmation=confirmation,
            booked_at=o.at,
        )

    def _write_ledger(self, o: RowOutcome, *, course_id: CourseId | None) -> None:
        entries = [
            *([o.booking] if o.booking is not None else []),
            *o.held_extras,
            *o.cancelled_extras,
        ]
        for entry in entries:
            if entry.course_account_id != o.course_account_id:
                raise ValueError(f"ledger entry {entry.id} is for another account")
            key = (entry.course_account_id, entry.course_id, entry.raw_reservation_id)
            self._ledger[key] = entry
        if o.cancelled_upgrade_raw_id is not None:
            for key, entry in list(self._ledger.items()):
                if (
                    key[0] == o.course_account_id
                    and key[2] == o.cancelled_upgrade_raw_id
                    and (course_id is None or key[1] == course_id)
                ):
                    self._ledger[key] = replace(entry, state=BookingState.CANCELLED_UPGRADE)

    def _flag_active_row(self, account_id: CourseAccountId, day: date) -> None:
        active_id = self._slots.get((account_id, day))
        if active_id is None:
            return
        active = self._rows[active_id]
        if not active.needs_reconcile:
            self._commit(
                [(active, replace(active, needs_reconcile=True, version=active.version + 1))]
            )

    # leases ------------------------------------------------------------------------------

    async def acquire_row_lease(
        self,
        row_id: RowId,
        *,
        owner: str,
        until: datetime,
        now: datetime,
        expected: RowFingerprint | None,
    ) -> bool:
        row = self._row(row_id)
        if lease_held(row, now=now) and row.lease_owner != owner:
            return False
        if not _fingerprint_matches(row, expected):
            return False
        self._commit([(row, replace(row, lease_owner=owner, lease_expires_at=until))])
        return True

    async def release_row_lease(self, row_id: RowId, *, owner: str) -> None:
        row = self._row(row_id)
        if row.lease_owner == owner:
            self._commit([(row, replace(row, lease_owner=None, lease_expires_at=None))])

    # watcher -----------------------------------------------------------------------------

    async def load_watch_rows(
        self,
        *,
        horizons: Mapping[CourseId, tuple[date, date]],
        now: datetime,
    ) -> list[EventRow]:
        out: list[EventRow] = []
        for row in sorted(self._rows.values(), key=lambda r: r.id):
            horizon = horizons.get(row.course_id)
            account = self._accounts.get(row.course_account_id)
            if horizon is None or account is None or account.status is not AccountStatus.ACTIVE:
                continue
            if not horizon[0] <= row.target_date <= horizon[1]:
                continue
            live_pending = row.status is RowStatus.PENDING and not row_is_frozen(row, now=now)
            if live_pending or row.status is RowStatus.BOOKED:
                out.append(EventRow(row=row, account=account))
        return out

    async def finalize_lost(self, *, now: datetime) -> list[RequestRow]:
        lost: list[RequestRow] = []
        for row in sorted(self._rows.values(), key=lambda r: r.id):
            if row.status is not RowStatus.PENDING or not row_is_frozen(row, now=now):
                continue
            if lease_held(row, now=now):
                continue  # an actor is mid-act; the next finalizer pass gets it
            check_transition(row, RowStatus.LOST, actor=Actor.WATCHER, now=now)
            reason = "cutoff" if now >= row.cutoff_at else "date_passed"
            new = replace(row, status=RowStatus.LOST, status_reason=reason, version=row.version + 1)
            self._commit([(row, new)])
            lost.append(new)
        return lost

    async def get_snapshot(self, account_id: CourseAccountId) -> ReservationSnapshot | None:
        return self._snapshots.get(account_id)

    async def save_snapshot(self, snapshot: ReservationSnapshot) -> None:
        self._snapshots[snapshot.course_account_id] = snapshot

    async def list_owned_bookings(
        self, account_id: CourseAccountId, *, target_date: date
    ) -> list[OwnedBooking]:
        entries = [
            e
            for (acc, _, _), e in self._ledger.items()
            if acc == account_id and e.target_date == target_date
        ]
        return sorted(entries, key=lambda e: (e.tee_time, e.raw_reservation_id))

    async def set_upgrade_marker(
        self, row_id: RowId, *, owner: str, at: datetime, expected: RowFingerprint
    ) -> bool:
        row = self._row(row_id)
        if row.lease_owner != owner or not lease_held(row, now=at):
            return False
        if not _fingerprint_matches(row, expected):
            return False
        self._commit([(row, replace(row, upgrade_started_at=at))])
        return True

    async def record_soft_auth_failure(self, account_id: CourseAccountId) -> int:
        account = self._account_for_user(account_id, None)
        count = account.consecutive_soft_auth_failures + 1
        status = AccountStatus.AUTH_FAILED if count >= SOFT_AUTH_FAILURE_LIMIT else account.status
        self._accounts[account_id] = replace(
            account, consecutive_soft_auth_failures=count, status=status
        )
        return count

    # materializer ------------------------------------------------------------------------

    async def rules_needing_materialization(self, *, through: date) -> list[StandingRule]:
        rules = (
            r
            for r in self._rules.values()
            if r.active and (r.materialized_through is None or r.materialized_through < through)
        )
        return sorted(rules, key=lambda r: r.id)

    async def rows_for_account_date(
        self, account_id: CourseAccountId, target_date: date
    ) -> list[RequestRow]:
        return self._history(account_id, target_date)

    async def insert_rule_row_if_absent(
        self, rule: StandingRule, target_date: date, *, now: datetime
    ) -> RequestRow | None:
        account = self._account_for_user(rule.course_account_id, None)
        if target_date.weekday() != rule.weekday:
            raise ValueError(f"{target_date} is not on the rule's weekday ({rule.weekday})")
        if not rule.active:
            raise TransitionRefusedError(f"rule {rule.id} is inactive")
        row_id = rule_row_id(rule.id, target_date)
        if row_id in self._rows:
            return None  # a row EXISTS; the caller consults the history (round-2 M1)
        self._refuse_if_user_terminal(account.id, target_date)
        slot_held = (account.id, target_date) in self._slots
        row = self._new_row(
            row_id=row_id,
            account=account,
            target_date=target_date,
            window=(rule.window_earliest, rule.window_latest),
            party_size=rule.party_size,
            status=RowStatus.SUPERSEDED if slot_held else RowStatus.PENDING,
            source=RowSource.RULE,
            rule_id=rule.id,
        )
        check_create(row, actor=Actor.MATERIALIZER, now=now)
        self._commit([(None, row)])
        return row

    async def reactivate_rule_row(
        self, row: RequestRow, rule: StandingRule, *, now: datetime
    ) -> RequestRow:
        stored = self._row(row.id)
        check_transition(row, RowStatus.PENDING, actor=Actor.MATERIALIZER, now=now)
        if row.rule_id != rule.id:
            raise ValueError(f"row {row.id} does not belong to rule {rule.id}")
        if not rule.active:
            raise TransitionRefusedError(f"rule {rule.id} is inactive")
        self._refuse_if_user_terminal(row.course_account_id, row.target_date)
        if lease_held(stored, now=now):
            raise RowLeaseError(f"row {row.id} is leased by {stored.lease_owner!r}")
        new = replace(
            row,
            status=RowStatus.PENDING,
            status_reason=None,
            window_earliest=rule.window_earliest,
            window_latest=rule.window_latest,
            party_size=rule.party_size,
            version=row.version + 1,
        )
        self._commit([(row, new)])
        return new

    async def set_materialized_through(self, rule_id: RuleId, through: date) -> None:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise TenantNotFoundError(f"rule {rule_id}")
        self._rules[rule_id] = replace(rule, materialized_through=through)

    # web ---------------------------------------------------------------------------------

    async def get_user_by_subject(self, provider: str, subject: str) -> User | None:
        for user in self._users.values():
            if user.oauth_provider == provider and user.oauth_subject == subject:
                return user
        return None

    async def upsert_user(self, user: User) -> None:
        if user.oauth_subject is not None:
            bound = await self.get_user_by_subject(user.oauth_provider, user.oauth_subject)
            if bound is not None and bound.id != user.id:
                raise UniquenessConflictError("that sign-in identity is bound to another user")
        self._users[user.id] = user

    async def bind_invited_user(self, *, email: str, provider: str, subject: str) -> User | None:
        existing = await self.get_user_by_subject(provider, subject)
        if existing is not None:
            return existing
        wanted = email.casefold()
        for user in self._users.values():
            if user.status is UserStatus.INVITED and user.email.casefold() == wanted:
                bound = replace(
                    user, oauth_provider=provider, oauth_subject=subject, status=UserStatus.ACTIVE
                )
                self._users[user.id] = bound
                return bound
        return None

    async def list_rows_for_user(
        self, user_id: UserId, *, from_date: date, to_date: date
    ) -> list[RequestRow]:
        mine = {a.id for a in self._accounts.values() if a.user_id == user_id}
        rows = (
            r
            for r in self._rows.values()
            if r.course_account_id in mine and from_date <= r.target_date <= to_date
        )
        return sorted(rows, key=lambda r: (r.target_date, r.id))

    async def get_account(
        self, account_id: CourseAccountId, *, user_id: UserId
    ) -> CourseAccount | None:
        account = self._accounts.get(account_id)
        return account if account is not None and account.user_id == user_id else None

    async def upsert_account(self, account: CourseAccount) -> None:
        if account.id != derive_account_id(account.user_id, account.course_id):
            raise UniquenessConflictError("account id is not derive_account_id(user, course)")
        username = account.username.casefold()
        same_course = [a for a in self._accounts.values() if a.course_id == account.course_id]
        for other in same_course:
            if other.id != account.id and other.username.casefold() == username:
                raise UniquenessConflictError("that course login is already connected")
        if account.id not in self._accounts and len(same_course) >= self._max_accounts_per_course:
            raise UniquenessConflictError(
                f"max_accounts_per_course ({self._max_accounts_per_course}) reached"
            )
        self._accounts[account.id] = account

    async def create_explicit_row(
        self,
        *,
        user_id: UserId,
        account_id: CourseAccountId,
        target_date: date,
        window_earliest: time,
        window_latest: time,
        party_size: int,
        now: datetime,
    ) -> RequestRow:
        account = self._account_for_user(account_id, user_id)
        row = self._new_row(
            row_id=RowId(uuid4()),
            account=account,
            target_date=target_date,
            window=(window_earliest, window_latest),
            party_size=party_size,
            status=RowStatus.PENDING,
            source=RowSource.EXPLICIT,
            rule_id=None,
        )
        check_create(row, actor=Actor.WEB, now=now)
        writes: list[_Write] = [(None, row)]
        holder_id = self._slots.get((account_id, target_date))
        if holder_id is not None:
            holder = self._rows[holder_id]
            if holder.source is not RowSource.RULE or holder.status is RowStatus.BOOKED:
                raise TransitionRefusedError(
                    f"{target_date} already has a {holder.status} {holder.source} row"
                )
            check_transition(holder, RowStatus.SUPERSEDED, actor=Actor.WEB, now=now)
            if lease_held(holder, now=now):
                raise RowLeaseError(f"booking in progress for {target_date}")
            superseded = replace(holder, status=RowStatus.SUPERSEDED, version=holder.version + 1)
            writes.insert(0, (holder, superseded))
        self._commit(writes)
        return row

    async def transition_row(
        self,
        row_id: RowId,
        *,
        user_id: UserId | None,
        to: RowStatus,
        actor: Actor,
        reason: str | None,
        now: datetime,
    ) -> RequestRow:
        row = self._row(row_id)
        if actor is Actor.WEB and user_id is None:
            raise TenantNotFoundError(f"row {row_id}")
        self._account_for_user(row.course_account_id, user_id)
        if actor not in (Actor.WEB, Actor.MATERIALIZER):
            raise TransitionRefusedError(f"{actor} writes through record_outcomes (leased path)")
        if to is RowStatus.SUPERSEDED:
            raise TransitionRefusedError("supersede is written only by create_explicit_row")
        check_transition(row, to, actor=actor, now=now, reason=reason)
        if lease_held(row, now=now):
            raise RowLeaseError(f"booking in progress for {row.target_date}")
        if (row.status, to) == (RowStatus.SUPERSEDED, RowStatus.PENDING):
            rule = self._rules.get(row.rule_id) if row.rule_id is not None else None
            if rule is None or not rule.active:
                raise TransitionRefusedError("the superseded row's rule is not active")
        new = replace(row, status=to, status_reason=reason, version=row.version + 1)
        writes: list[_Write] = [(row, new)]
        if row.source is RowSource.EXPLICIT and to is RowStatus.WITHDRAWN:
            restore = self._restorable_superseded(row, now)
            if restore is not None:
                restored = replace(
                    restore,
                    status=RowStatus.PENDING,
                    status_reason=None,
                    version=restore.version + 1,
                )
                writes.append((restore, restored))
        self._commit(writes)
        return new

    async def upsert_rule(self, rule: StandingRule, *, user_id: UserId) -> StandingRule:
        self._account_for_user(rule.course_account_id, user_id)
        existing = self._rules.get(rule.id)
        if existing is not None and existing.course_account_id != rule.course_account_id:
            raise TenantNotFoundError(f"rule {rule.id}")
        if rule.active:
            for other in self._rules.values():
                if (
                    other.id != rule.id
                    and other.course_account_id == rule.course_account_id
                    and other.active
                    and other.weekday == rule.weekday
                ):
                    raise RuleConflictError(
                        f"account already has an active rule for weekday {rule.weekday}"
                    )
        stored = replace(rule, version=existing.version + 1) if existing is not None else rule
        self._rules[rule.id] = stored
        return stored

    async def count_login_probes(
        self, *, user_id: UserId | None, username_hash: str | None, since: datetime
    ) -> int:
        return sum(
            1
            for p in self._probes
            if p.at >= since
            and (user_id is None or p.user_id == user_id)
            and (username_hash is None or p.username_hash == username_hash)
        )

    async def record_login_probe(
        self, *, user_id: UserId, course_id: CourseId, username_hash: str, ok: bool, at: datetime
    ) -> None:
        self._probes.append(
            _Probe(user_id=user_id, course_id=course_id, username_hash=username_hash, ok=ok, at=at)
        )

    async def append_audit(
        self,
        *,
        user_id: UserId | None,
        action: str,
        row_id: RowId | None,
        detail: Mapping[str, object],
        at: datetime,
    ) -> None:
        self.audit_log.append(
            AuditEntry(
                user_id=user_id, action=action, row_id=row_id, detail=redact_payload(detail), at=at
            )
        )
