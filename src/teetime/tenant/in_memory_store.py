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
- **Rule-weekday pointer** (§3.2, round-3 SF1): ``self._ruledays[(account, weekday)]`` is the
  ``ruleday|<weekday>`` doc; ``upsert_rule`` claims / re-points / releases it in the same batch as
  the rule replace, so "one ACTIVE rule per (account, weekday)" never depends on a scan.
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
    SYSTEM_WITHDRAW_REASONS,
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
    RuleNoLongerCoversError,
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
from .store import (
    RowLeaseError,
    RowOutcome,
    TenantNotFoundError,
    UniquenessConflictError,
    VersionConflictError,
)

# Consecutive soft login failures after which an account stops logging in (§7.5).
SOFT_AUTH_FAILURE_LIMIT = 3

_Write = tuple[RequestRow | None, RequestRow]

# Only these statuses are ever leased: nothing is booked, upgraded or cancelled from any other.
_LEASABLE_STATUSES = frozenset({RowStatus.PENDING, RowStatus.BOOKED})


def _becomes_bookable(old: RequestRow | None, new: RequestRow) -> bool:
    """True when a write puts a row into PENDING / SKIPPED from outside the active set
    (create, reactivate, restore, un-supersede) or unskips it. booked -> pending (the M2 edge)
    is excluded: it records what already happened and must never be refused."""
    if new.status not in (RowStatus.PENDING, RowStatus.SKIPPED):
        return False
    if old is None or old.status not in ACTIVE_ROW_STATUSES:
        return True
    return old.status is RowStatus.SKIPPED and new.status is RowStatus.PENDING


# The ONLY §3.4 edges the LEASED path (``record_outcomes``) may write, and by whom. Everything else
# (skip, withdraw, un-supersede, reactivate, ...) goes through the unleased paths, which carry the
# extra guards (user-terminal history, D2 restore, rule active). MU-5 review round 2, MF1.
_LEASED_EDGES: dict[tuple[RowStatus, RowStatus], frozenset[Actor]] = {
    (RowStatus.PENDING, RowStatus.BOOKED): frozenset({Actor.BOOKING_RUNNER, Actor.WATCHER}),
    (RowStatus.BOOKED, RowStatus.BOOKED): frozenset({Actor.WATCHER}),  # upgrade
    (RowStatus.BOOKED, RowStatus.PENDING): frozenset({Actor.WATCHER}),  # + needs_reconcile
    # watcher: external (vanish); web: the §8.5 managed cancel (user / already_gone)
    (RowStatus.BOOKED, RowStatus.CANCELLED): frozenset({Actor.WATCHER, Actor.WEB}),
}

# One message for "missing" and "not yours" alike, naming no id (IDOR defence, §9.1).
_NOT_FOUND = "not found"


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
        # The ``ruleday|<weekday>`` pointer docs: one ACTIVE rule per (account, weekday).
        self._ruledays: dict[tuple[CourseAccountId, int], RuleId] = {}
        self._ledger: dict[tuple[CourseAccountId, CourseId, str], OwnedBooking] = {}
        self._snapshots: dict[CourseAccountId, ReservationSnapshot] = {}
        self._probes: list[_Probe] = []
        self.audit_log: list[AuditEntry] = []

    # --- introspection (tests / conformance hook) ------------------------------------------

    def slot_pointer(self, account_id: CourseAccountId, day: date) -> RowId | None:
        """The ``slot|<date>`` doc's ``activeRowId`` (conformance ``StoreHarness`` hook)."""
        return self._slots.get((account_id, day))

    def ruleday_pointer(self, account_id: CourseAccountId, weekday: int) -> RuleId | None:
        """The ``ruleday|<weekday>`` doc's ``activeRuleId`` (conformance ``StoreHarness`` hook)."""
        return self._ruledays.get((account_id, weekday))

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
        for old, new in writes:
            if new.source is RowSource.RULE and _becomes_bookable(old, new):
                self._guard_rule_row_may_become_active(new)
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
            raise TenantNotFoundError(_NOT_FOUND)
        return row

    def _account_for_user(
        self, account_id: CourseAccountId, user_id: UserId | None
    ) -> CourseAccount:
        account = self._accounts.get(account_id)
        if account is None or (user_id is not None and account.user_id != user_id):
            raise TenantNotFoundError(_NOT_FOUND)
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

    @staticmethod
    def _unleased_write(row: RequestRow, now: datetime) -> RequestRow:
        """An unleased (web / materializer / finalizer) write clears an EXPIRED lease, so a stale
        holder cannot come back and move a row someone else has since written (SF2)."""
        if row.lease_owner is not None and not lease_held(row, now=now):
            return replace(row, lease_owner=None, lease_expires_at=None)
        return row

    def _refuse_if_user_terminal(self, account_id: CourseAccountId, day: date) -> None:
        if any(is_user_terminal(r) for r in self._history(account_id, day)):
            raise TransitionRefusedError(
                f"{day} has a user-terminal row; only an explicit re-request reopens it"
            )

    def _rule_covers_row(self, row: RequestRow) -> bool:
        """THE coverage predicate (round-5 MF1/MF2), read from the STORED rule: True for explicit
        rows, and for a rule row whose rule exists, is active, is on the row's weekday and belongs
        to the row's account. Every read path that offers rows for booking and every writer of an
        active status onto a rule row goes through it."""
        if row.rule_id is None:
            return True
        rule = self._rules.get(row.rule_id)
        return (
            rule is not None
            and rule.active
            # Defensive: upsert_rule never moves a rule between accounts, so this leg cannot
            # fail today; kept so a future account move cannot silently cover foreign rows.
            and rule.course_account_id == row.course_account_id
            and rule.weekday == row.target_date.weekday()
        )

    def _uncovered_reason(self, row: RequestRow) -> str:
        """The system withdraw reason for a rule row its rule no longer covers."""
        rule = self._rules.get(row.rule_id) if row.rule_id is not None else None
        # The account leg is defensive (unreachable today, see _rule_covers_row).
        if rule is None or rule.course_account_id != row.course_account_id:
            return "rule_deleted"
        if not rule.active:
            return "rule_deactivated"
        return "rule_weekday_changed"

    def _guard_rule_row_may_become_active(self, row: RequestRow) -> None:
        """The shared "may become active" guard for a rule row (round-5 MF1), enforced inside the
        batch so NO writer can skip it: create, reactivate, un-supersede, unskip, the one-off
        withdraw restore. The rule must still cover the row, and the date must have no
        user-terminal row (round-7). Frozen and slot checks are the caller's check_transition /
        check_create and the slot pointer below."""
        if not self._rule_covers_row(row):
            raise RuleNoLongerCoversError(f"the rule no longer covers {row.target_date}")
        self._refuse_if_user_terminal(row.course_account_id, row.target_date)

    def _stored_rule_matching(self, rule: StandingRule) -> StandingRule:
        """IfMatch on the rule (round-5 MF2 b/c): the caller's copy must be the stored version.
        Cosmos (MU-8b): read the rule doc (or its ``ruleday|<weekday>`` pointer) and assert its
        ETag in the same batch as the row write."""
        stored = self._rules.get(rule.id)
        if stored is None or stored.version != rule.version:
            raise TransitionRefusedError(f"stale rule {rule.id}: re-read and retry")
        return stored

    def _restorable_rule_row(self, explicit: RequestRow, now: datetime) -> _Write | None:
        """The rule row a withdrawn explicit row gives the date back to, restored to
        ``superseded_from or PENDING``, iff its rule is active and the date not frozen:
        - a SUPERSEDED row (§3.4, round-4 D2); else
        - a SYSTEM-WITHDRAWN row with no user-terminal row for the date (round-6: deactivate ->
          reactivate while the one-off held the slot -> withdraw one-off; reactivation was
          refused by the held slot, so nothing else would bring it back), refreshed from the rule.
        The restore writes that row too, so it must be unleased like every web write (M4)."""
        history = self._history(explicit.course_account_id, explicit.target_date)
        superseded = [r for r in history if r.status is RowStatus.SUPERSEDED]
        withdrawn = [
            r
            for r in history
            if r.status is RowStatus.WITHDRAWN and r.status_reason in SYSTEM_WITHDRAW_REASONS
        ]
        if any(is_user_terminal(r) for r in history):
            # Round-7: a user-terminal date stays blocked for the rule. Withdrawing a re-request
            # undoes the re-request, not the cancel; a superseded rule row stays SUPERSEDED (inert).
            return None
        for row in [*superseded, *withdrawn]:
            if row.source is not RowSource.RULE or not self._rule_covers_row(row):
                continue  # round-4 MF-A / round-5: only a row its (stored) rule still covers
            rule = self._rules[row.rule_id] if row.rule_id is not None else None
            assert rule is not None  # covered implies the rule exists
            if row_is_frozen(row, now=now):
                continue
            if lease_held(row, now=now):
                raise RowLeaseError(f"booking in progress for {row.target_date}")
            target = row.superseded_from or RowStatus.PENDING
            restored = replace(
                self._unleased_write(row, now),
                status=target,
                status_reason=None,
                superseded_from=None,
                version=row.version + 1,
            )
            if row.status is RowStatus.WITHDRAWN:
                check_transition(row, target, actor=Actor.MATERIALIZER, now=now)
                restored = replace(
                    restored,
                    window_earliest=rule.window_earliest,
                    window_latest=rule.window_latest,
                    party_size=rule.party_size,
                )
            return (row, restored)
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
            # Belt and braces for a non-atomic deactivation (§7.7): never offer the booker a row
            # of an inactive rule, even before the materializer withdrew it.
            rule_ok = self._rule_covers_row(row)
            if (
                row.status is RowStatus.PENDING
                and targets.get(row.course_id) == row.target_date
                and row.cutoff_at > now
                and rule_ok
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
        # Before ANY write: the row and its ledger are one unit (SF3).
        self._validate_ledger(o, course_id=row.course_id if row is not None else None)
        try:
            if row is None:
                raise TenantNotFoundError(_NOT_FOUND)
            self._commit([(row, self._outcome_row(row, o))])
        except (TransitionRefusedError, RowLeaseError, TenantNotFoundError):
            # The row moved: keep what the bot did (ledger by account + date) and make the
            # date's active row reconcile it on the next watcher run (M4).
            self._write_ledger(o, course_id=row.course_id if row is not None else None)
            self._flag_active_row(o.course_account_id, o.target_date)
            raise
        self._write_ledger(o, course_id=row.course_id)

    def _outcome_row(self, row: RequestRow, o: RowOutcome) -> RequestRow:
        owns = row.lease_owner is not None and row.lease_owner == o.release_lease_owner
        holder = owns and lease_held(row, now=o.at)  # an expired lease is no lease (SF2)
        if o.to_status is not None:
            leased_by = _LEASED_EDGES.get((row.status, o.to_status), frozenset())
            if o.actor not in leased_by:
                raise TransitionRefusedError(
                    f"{row.status} -> {o.to_status} by {o.actor} is not a record_outcomes edge"
                )
            check_transition(
                row,
                o.to_status,
                actor=o.actor,
                now=o.at,
                reason=o.status_reason,
                needs_reconcile=o.needs_reconcile,
            )
            if not holder:
                raise RowLeaseError(f"row {row.id}: {o.release_lease_owner!r} holds no live lease")
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
        if owns:
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

    @staticmethod
    def _ledger_entries(o: RowOutcome) -> list[OwnedBooking]:
        return [
            *([o.booking] if o.booking is not None else []),
            *o.held_extras,
            *o.cancelled_extras,
        ]

    def _validate_ledger(self, o: RowOutcome, *, course_id: CourseId | None) -> None:
        for entry in self._ledger_entries(o):
            if entry.course_account_id != o.course_account_id:
                raise ValueError(f"ledger entry {entry.id} is for another account")
            if entry.target_date != o.target_date:
                raise ValueError(f"ledger entry {entry.id} is for another date")
            if course_id is not None and entry.course_id != course_id:
                raise ValueError(f"ledger entry {entry.id} is for another course")

    def _write_ledger(self, o: RowOutcome, *, course_id: CourseId | None) -> None:
        for entry in self._ledger_entries(o):
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
        if row.status not in _LEASABLE_STATUSES:
            return False  # nothing is ever booked, upgraded or cancelled from any other status
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
            # A pending row of an INACTIVE rule is never offered (round-3 MF1: a deactivation
            # that had to skip a leased row). Held bookings are still watched regardless.
            # needs_reconcile rows stay watched even when their rule no longer covers them
            # (round-6 SF2): a rebook that landed must still be adopted (§7.6), not lost.
            live_pending = (
                row.status is RowStatus.PENDING
                and not row_is_frozen(row, now=now)
                and (self._rule_covers_row(row) or row.needs_reconcile)
            )
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
            if not self._rule_covers_row(row):
                # The user switched the rule off, deleted it or moved its weekday: a system
                # withdraw, never a "lost" email.
                self._withdraw_uncovered(row, now)
                continue
            check_transition(row, RowStatus.LOST, actor=Actor.WATCHER, now=now)
            reason = "cutoff" if now >= row.cutoff_at else "date_passed"
            new = replace(
                self._unleased_write(row, now),
                status=RowStatus.LOST,
                status_reason=reason,
                version=row.version + 1,
            )
            self._commit([(row, new)])
            lost.append(new)
        return lost

    def _withdraw_uncovered(self, row: RequestRow, now: datetime) -> None:
        reason = self._uncovered_reason(row)
        check_transition(row, RowStatus.WITHDRAWN, actor=Actor.MATERIALIZER, now=now, reason=reason)
        new = replace(
            self._unleased_write(row, now),
            status=RowStatus.WITHDRAWN,
            status_reason=reason,
            version=row.version + 1,
        )
        self._commit([(row, new)])

    async def rows_no_longer_covered(self, *, now: datetime) -> list[RequestRow]:
        rows = (
            r
            for r in self._rows.values()
            if r.rule_id is not None
            and r.status in (RowStatus.PENDING, RowStatus.SUPERSEDED)
            and not self._rule_covers_row(r)
            and not r.needs_reconcile  # the watcher reconciles it first (round-6 SF2)
            and not lease_held(r, now=now)
        )
        return sorted(rows, key=lambda r: r.id)

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

    async def get_rule_unscoped(self, rule_id: RuleId) -> StandingRule | None:
        return self._rules.get(rule_id)

    async def get_account_unscoped(self, account_id: CourseAccountId) -> CourseAccount | None:
        return self._accounts.get(account_id)

    async def rows_for_account_date(
        self, account_id: CourseAccountId, target_date: date
    ) -> list[RequestRow]:
        return self._history(account_id, target_date)

    async def insert_rule_row_if_absent(
        self, rule: StandingRule, target_date: date, *, now: datetime
    ) -> RequestRow | None:
        account = self._account_for_user(rule.course_account_id, None)
        rule = self._stored_rule_matching(rule)
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
        # Round-5: back to the pre-supersede status if it was superseded before the withdraw.
        target = stored.superseded_from or RowStatus.PENDING
        check_transition(row, target, actor=Actor.MATERIALIZER, now=now)
        if row.rule_id != rule.id:
            raise ValueError(f"row {row.id} does not belong to rule {rule.id}")
        rule = self._stored_rule_matching(rule)
        if not rule.active:
            raise TransitionRefusedError(f"rule {rule.id} is inactive")
        self._refuse_if_user_terminal(row.course_account_id, row.target_date)
        if lease_held(stored, now=now):
            raise RowLeaseError(f"row {row.id} is leased by {stored.lease_owner!r}")
        new = replace(
            self._unleased_write(row, now),
            status=target,
            status_reason=None,
            superseded_from=None,
            window_earliest=rule.window_earliest,
            window_latest=rule.window_latest,
            party_size=rule.party_size,
            version=row.version + 1,
        )
        self._commit([(row, new)])
        return new

    async def reset_materialized_through(self, rule_id: RuleId) -> None:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise TenantNotFoundError(_NOT_FOUND)
        self._rules[rule_id] = replace(rule, materialized_through=None)

    async def rewrite_pending_rule_row(
        self,
        row_id: RowId,
        *,
        rule: StandingRule,
        expected_version: int,
        now: datetime,
    ) -> RequestRow:
        stored = self._row(row_id)
        if stored.version != expected_version:
            raise TransitionRefusedError(f"row {row_id} changed since it was read")
        if stored.source is not RowSource.RULE or stored.rule_id != rule.id:
            raise TransitionRefusedError("only this rule's own rule row can be rewritten")
        if stored.status is not RowStatus.PENDING:
            raise TransitionRefusedError(
                f"only a pending row is rewritten (row is {stored.status})"
            )
        if row_is_frozen(stored, now=now):
            raise TransitionRefusedError(f"{stored.target_date} is frozen (cutoff or date passed)")
        if lease_held(stored, now=now):
            raise RowLeaseError(f"booking in progress for {stored.target_date}")
        rule = self._stored_rule_matching(rule)
        # pending -> pending: _becomes_bookable is False, so the coverage guard is called here.
        self._guard_rule_row_may_become_active(stored)
        new = replace(
            self._unleased_write(stored, now),
            window_earliest=rule.window_earliest,
            window_latest=rule.window_latest,
            party_size=rule.party_size,
            version=stored.version + 1,
        )
        self._commit([(stored, new)])
        return new

    async def set_materialized_through(self, rule_id: RuleId, through: date) -> None:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise TenantNotFoundError(_NOT_FOUND)
        if rule.materialized_through is not None and rule.materialized_through >= through:
            return  # never moves backwards (§3.2)
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

    async def get_row(self, row_id: RowId, *, user_id: UserId) -> RequestRow | None:
        row = self._rows.get(row_id)
        if row is None:
            return None
        account = self._accounts.get(row.course_account_id)
        return row if account is not None and account.user_id == user_id else None

    async def list_accounts_for_user(self, user_id: UserId) -> list[CourseAccount]:
        mine = (a for a in self._accounts.values() if a.user_id == user_id)
        return sorted(mine, key=lambda a: str(a.course_id))

    async def list_rules_for_user(self, user_id: UserId) -> list[StandingRule]:
        mine = {a.id for a in self._accounts.values() if a.user_id == user_id}
        rules = (r for r in self._rules.values() if r.course_account_id in mine)
        return sorted(rules, key=lambda r: (r.weekday, str(r.id)))

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
            superseded = replace(
                self._unleased_write(holder, now),
                status=RowStatus.SUPERSEDED,
                superseded_from=holder.status,
                version=holder.version + 1,
            )
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
            raise TenantNotFoundError(_NOT_FOUND)
        self._account_for_user(row.course_account_id, user_id)
        if actor not in (Actor.WEB, Actor.MATERIALIZER):
            raise TransitionRefusedError(f"{actor} writes through record_outcomes (leased path)")
        if to is RowStatus.SUPERSEDED:
            raise TransitionRefusedError("supersede is written only by create_explicit_row")
        if to is RowStatus.CANCELLED:
            # The mirror of MF1: a leased edge (the §8.5 cancel runs under the web's own lease).
            raise TransitionRefusedError("booked -> cancelled is written only by record_outcomes")
        if row.status is RowStatus.WITHDRAWN and to in (RowStatus.PENDING, RowStatus.SKIPPED):
            # It must re-check the (account, date) history for a user-terminal row, the rule
            # being active, and refresh window/party from the rule (§3.4, operator decision c).
            raise TransitionRefusedError("reactivation is written only by reactivate_rule_row")
        check_transition(row, to, actor=actor, now=now, reason=reason)
        if lease_held(row, now=now):
            raise RowLeaseError(f"booking in progress for {row.target_date}")
        new = replace(
            self._unleased_write(row, now),
            status=to,
            status_reason=reason,
            # Round-5: a system withdraw KEEPS the pre-supersede status for reactivation.
            superseded_from=row.superseded_from if to is RowStatus.WITHDRAWN else None,
            version=row.version + 1,
        )
        writes: list[_Write] = [(row, new)]
        if row.source is RowSource.EXPLICIT and to is RowStatus.WITHDRAWN:
            restore = self._restorable_rule_row(row, now)
            if restore is not None:
                writes.append(restore)
        self._commit(writes)
        return new

    async def upsert_rule(self, rule: StandingRule, *, user_id: UserId) -> StandingRule:
        self._account_for_user(rule.course_account_id, user_id)
        existing = self._rules.get(rule.id)
        if existing is not None and existing.course_account_id != rule.course_account_id:
            raise TenantNotFoundError(_NOT_FOUND)
        if existing is not None and rule.version != existing.version:
            raise VersionConflictError(
                f"rule edited from version {rule.version}; stored is {existing.version}"
            )
        # One batch: the rule replace + its ruleday pointer ops (§3.2, round-3 SF1).
        ruledays = dict(self._ruledays)
        if existing is not None and existing.active:
            old_key = (existing.course_account_id, existing.weekday)
            if ruledays.get(old_key) == rule.id:
                del ruledays[old_key]
        if rule.active:
            key = (rule.course_account_id, rule.weekday)
            if ruledays.get(key, rule.id) != rule.id:
                raise RuleConflictError(
                    f"account already has an active rule for weekday {rule.weekday}"
                )
            ruledays[key] = rule.id
        stored = rule
        if existing is not None:
            # A stored None is a RESET (§7.7) and wins over the caller's copy; otherwise the
            # watermark never moves backwards.
            through = existing.materialized_through
            if through is not None and rule.materialized_through is not None:
                through = max(through, rule.materialized_through)
            moved = rule.weekday != existing.weekday
            reactivated = rule.active and not existing.active
            if moved or reactivated:
                # Round-5 SF-2 (coordinator decision): the rule is due for the tick in the same
                # write, so a crash before the web's synchronous materialize loses nothing.
                through = None
            stored = replace(rule, version=existing.version + 1, materialized_through=through)
        self._ruledays = ruledays
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
