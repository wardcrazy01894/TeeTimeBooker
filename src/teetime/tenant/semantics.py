"""The pure ``TenantStore`` rules shared by every implementation (MULTIUSER_PLAN §3.4/§3.5).

``InMemoryTenantStore`` (MU-5) and ``CosmosTenantStore`` (MU-8b) must behave identically, and the
conformance suite pins it. Everything here is a pure function of domain values (no storage, no
clock, no I/O), so both stores compute a write the same way and differ only in HOW they commit it:
a dict swap versus one Cosmos transactional batch with per-op IfMatch ETags.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time

from ..core.booking_cutoff import cutoff_instant
from ..core.config import BookingCutoffConfig
from ..core.models import CourseId
from .models import (
    ACTIVE_ROW_STATUSES,
    SYSTEM_WITHDRAW_REASONS,
    Actor,
    CourseAccount,
    OwnedBooking,
    RequestRow,
    RowFingerprint,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    StandingRule,
    TransitionRefusedError,
    check_transition,
    is_user_terminal,
    lease_held,
    row_is_frozen,
    row_request_id,
)
from .store import RowLeaseError, RowOutcome

# Consecutive soft login failures after which an account stops logging in (§7.5).
SOFT_AUTH_FAILURE_LIMIT = 3

# One message for "missing" and "not yours" alike, naming no id (IDOR defence, §9.1).
NOT_FOUND = "not found"

# Only these statuses are ever leased: nothing is booked, upgraded or cancelled from any other.
LEASABLE_STATUSES = frozenset({RowStatus.PENDING, RowStatus.BOOKED})

# The ONLY §3.4 edges the LEASED path (``record_outcomes``) may write, and by whom. Everything else
# (skip, withdraw, un-supersede, reactivate, ...) goes through the unleased paths, which carry the
# extra guards (user-terminal history, D2 restore, rule active). MU-5 review round 2, MF1.
LEASED_EDGES: dict[tuple[RowStatus, RowStatus], frozenset[Actor]] = {
    (RowStatus.PENDING, RowStatus.BOOKED): frozenset({Actor.BOOKING_RUNNER, Actor.WATCHER}),
    (RowStatus.BOOKED, RowStatus.BOOKED): frozenset({Actor.WATCHER}),  # upgrade
    (RowStatus.BOOKED, RowStatus.PENDING): frozenset({Actor.WATCHER}),  # + needs_reconcile
    # watcher: external (vanish); web: the §8.5 managed cancel (user / already_gone)
    (RowStatus.BOOKED, RowStatus.CANCELLED): frozenset({Actor.WATCHER, Actor.WEB}),
}

# One row write inside a batch: (the row as read, or None for a create; the row to write).
type RowWrite = tuple[RequestRow | None, RequestRow]


def becomes_bookable(old: RequestRow | None, new: RequestRow) -> bool:
    """True when a write puts a row into PENDING / SKIPPED from outside the active set
    (create, reactivate, restore, un-supersede) or unskips it. booked -> pending (the M2 edge)
    is excluded: it records what already happened and must never be refused."""
    if new.status not in (RowStatus.PENDING, RowStatus.SKIPPED):
        return False
    if old is None or old.status not in ACTIVE_ROW_STATUSES:
        return True
    return old.status is RowStatus.SKIPPED and new.status is RowStatus.PENDING


def fingerprint_matches(row: RequestRow, expected: RowFingerprint | None) -> bool:
    if expected is None:
        return True
    return (row.status, row.version, row.booked_raw_id) == (
        expected.status,
        expected.version,
        expected.booked_raw_id,
    )


def strip_ttb(code: str | None) -> str | None:
    if code is None:
        return None
    return code.removeprefix("TTB:")


def cutoff_at(*, timezone: str, day: date, cutoff: BookingCutoffConfig) -> datetime:
    return cutoff_instant(day, timezone=timezone, cutoff=cutoff).astimezone(UTC)


def new_row(
    *,
    row_id: RowId,
    account: CourseAccount,
    timezone: str,
    cutoff: BookingCutoffConfig,
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
        timezone=timezone,
        window_earliest=window[0],
        window_latest=window[1],
        party_size=party_size,
        status=status,
        source=source,
        cutoff_at=cutoff_at(timezone=timezone, day=target_date, cutoff=cutoff),
        request_id=row_request_id(row_id),
        version=1,
        rule_id=rule_id,
    )


def unleased_write(row: RequestRow, now: datetime) -> RequestRow:
    """An unleased (web / materializer / finalizer) write clears an EXPIRED lease, so a stale
    holder cannot come back and move a row someone else has since written (SF2)."""
    if row.lease_owner is not None and not lease_held(row, now=now):
        return replace(row, lease_owner=None, lease_expires_at=None)
    return row


def rule_covers_row(row: RequestRow, rule: StandingRule | None) -> bool:
    """THE coverage predicate (round-5 MF1/MF2), given the STORED rule (None when it does not
    exist): True for explicit rows, and for a rule row whose rule exists, is active, is on the
    row's weekday and belongs to the row's account. Every read path that offers rows for booking
    and every writer of an active status onto a rule row goes through it."""
    if row.rule_id is None:
        return True
    return (
        rule is not None
        and rule.active
        # Defensive: upsert_rule never moves a rule between accounts, so this leg cannot
        # fail today; kept so a future account move cannot silently cover foreign rows.
        and rule.course_account_id == row.course_account_id
        and rule.weekday == row.target_date.weekday()
    )


def uncovered_reason(row: RequestRow, rule: StandingRule | None) -> str:
    """The system withdraw reason for a rule row its (stored) rule no longer covers."""
    # The account leg is defensive (unreachable today, see rule_covers_row).
    if rule is None or rule.course_account_id != row.course_account_id:
        return "rule_deleted"
    if not rule.active:
        return "rule_deactivated"
    return "rule_weekday_changed"


def restorable_rule_row(
    *,
    history: Sequence[RequestRow],
    rules: Mapping[RuleId, StandingRule],
    now: datetime,
) -> RowWrite | None:
    """The rule row a withdrawn explicit row gives the date back to, restored to
    ``superseded_from or PENDING``, iff its rule is active and the date not frozen:
    - a SUPERSEDED row (§3.4, round-4 D2); else
    - a SYSTEM-WITHDRAWN row with no user-terminal row for the date (round-6: deactivate ->
      reactivate while the one-off held the slot -> withdraw one-off; reactivation was
      refused by the held slot, so nothing else would bring it back), refreshed from the rule.
    The restore writes that row too, so it must be unleased like every web write (M4).
    ``history`` is the withdrawn explicit row's (account, date) history sorted by row id;
    ``rules`` holds the STORED rule of every rule row in it (a missing key: no such rule)."""
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
        rule = rules.get(row.rule_id) if row.rule_id is not None else None
        if row.source is not RowSource.RULE or not rule_covers_row(row, rule):
            continue  # round-4 MF-A / round-5: only a row its (stored) rule still covers
        assert rule is not None  # covered implies the rule exists
        if row_is_frozen(row, now=now):
            continue
        if lease_held(row, now=now):
            raise RowLeaseError(f"booking in progress for {row.target_date}")
        target = row.superseded_from or RowStatus.PENDING
        restored = replace(
            unleased_write(row, now),
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


# --- record_outcomes -------------------------------------------------------------------------


def outcome_row(row: RequestRow, o: RowOutcome) -> RequestRow:
    """The row a ``record_outcomes`` outcome writes, or a refusal (``TransitionRefusedError`` /
    ``RowLeaseError``). Pure: the caller commits it IfMatch the row it read."""
    owns = row.lease_owner is not None and row.lease_owner == o.release_lease_owner
    holder = owns and lease_held(row, now=o.at)  # an expired lease is no lease (SF2)
    if o.to_status is not None:
        leased_by = LEASED_EDGES.get((row.status, o.to_status), frozenset())
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
            booking_fields(new, o),
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


def booking_fields(row: RequestRow, o: RowOutcome) -> RequestRow:
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
        raw_id = strip_ttb(o.result.confirmation_code)
    return replace(
        row,
        booked_raw_id=raw_id,
        booked_tee_time=tee,
        booked_confirmation=confirmation,
        booked_at=o.at,
    )


def ledger_entries(o: RowOutcome) -> list[OwnedBooking]:
    return [
        *([o.booking] if o.booking is not None else []),
        *o.held_extras,
        *o.cancelled_extras,
    ]


def validate_ledger(o: RowOutcome, *, course_id: CourseId | None) -> None:
    """Before ANY write: the row and its ledger are one unit (SF3)."""
    for entry in ledger_entries(o):
        if entry.course_account_id != o.course_account_id:
            raise ValueError(f"ledger entry {entry.id} is for another account")
        if entry.target_date != o.target_date:
            raise ValueError(f"ledger entry {entry.id} is for another date")
        if course_id is not None and entry.course_id != course_id:
            raise ValueError(f"ledger entry {entry.id} is for another course")


# --- rules -----------------------------------------------------------------------------------


def upserted_rule(rule: StandingRule, existing: StandingRule | None) -> StandingRule:
    """The rule ``upsert_rule`` stores: the caller's copy for a create; for a replace, a version
    bump with the watermark merged. A stored None is a RESET (§7.7) and wins over the caller's
    copy; otherwise the watermark never moves backwards; a weekday move or a re-activation
    clears it in the same write (round-5 SF-2)."""
    if existing is None:
        return rule
    through = existing.materialized_through
    if through is not None and rule.materialized_through is not None:
        through = max(through, rule.materialized_through)
    moved = rule.weekday != existing.weekday
    reactivated = rule.active and not existing.active
    if moved or reactivated:
        through = None
    return replace(rule, version=existing.version + 1, materialized_through=through)
