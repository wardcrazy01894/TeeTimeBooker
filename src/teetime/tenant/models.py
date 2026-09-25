"""Tenant domain shapes (MULTIUSER_PLAN §3). Plain frozen dataclasses with no I/O.

Invariants live in the store (Cosmos: deterministic ids + date-slot pointer docs + transactional
batches, §3.2) AND in the state-machine
checker (``check_transition``, §3.4), and both are exercised by the store conformance suite (MU-5).
``frozen`` is DERIVED, never a stored status: ``row_is_frozen`` reads the row's denormalized
``cutoff_at`` (= ``core.booking_cutoff.cutoff_instant``) plus "date passed in the course tz".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import NewType
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from ..core.models import CourseId, RequestId, derive_request_id

UserId = NewType("UserId", UUID)
CourseAccountId = NewType("CourseAccountId", UUID)
RuleId = NewType("RuleId", UUID)
RowId = NewType("RowId", UUID)
OwnedBookingId = NewType("OwnedBookingId", UUID)


class UserRole(StrEnum):
    OPERATOR = "operator"
    MEMBER = "member"


class UserStatus(StrEnum):
    INVITED = "invited"  # allowlisted by email; OAuth subject bound on first sign-in
    ACTIVE = "active"
    DISABLED = "disabled"


class AccountProvenance(StrEnum):
    """Who created the course login [D1]. v1 only writes USER_SUPPLIED; SYSTEM_PROVISIONED
    (shadow accounts) is reserved so that flow is a data change, not an engine change."""

    USER_SUPPLIED = "user_supplied"
    SYSTEM_PROVISIONED = "system_provisioned"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    # Hard AuthError, or 3 consecutive soft login failures (§7.5). No automatic logins until
    # the user re-verifies (PLAN §12: never hammer login on auth failure).
    AUTH_FAILED = "auth_failed"
    DISABLED = "disabled"


class RowStatus(StrEnum):
    """Stored row statuses (§3.4). ``frozen`` is intentionally absent (derived)."""

    PENDING = "pending"
    BOOKED = "booked"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"  # a rule row displaced by an explicit row for the same date
    WITHDRAWN = "withdrawn"  # a pending row retracted (explicit deleted / rule edited or off)
    CANCELLED = "cancelled"  # a BOOKED row whose reservation was cancelled (user or external)
    LOST = "lost"  # frozen (cutoff/date passed) without ever booking


# ``status_reason`` vocabulary that drives materializer resurrection (round-2 M1, §3.4/§7.7).
# SYSTEM withdrawals are undone automatically when a rule applies to the date again.
SYSTEM_WITHDRAW_REASONS: frozenset[str] = frozenset(
    {"rule_weekday_changed", "rule_deactivated", "rule_deleted"}
)
# USER-terminal (status, reason) pairs block EVERY materialization for that (account, date), even
# by a brand-new rule. Only the user's explicit "Re-request this date" reopens the date.
# Deliberately NOT here (round-3 M1): ``(WITHDRAWN, "user_withdrawn")``. Withdrawing an explicit
# row means "undo my one-off", not "never book this date" — SKIPPED is the "don't book" action.
# Including it silently poisoned the date for every rule (a withdrawn one-off followed by a rule
# for that weekday would never materialize → missed drop with exit 0).
USER_TERMINAL: frozenset[tuple[RowStatus, str]] = frozenset(
    {
        (RowStatus.CANCELLED, "user"),
        (RowStatus.CANCELLED, "external"),
        (RowStatus.CANCELLED, "already_gone"),
    }
)
# The only reason a WEB withdraw of an EXPLICIT row may carry ("undo my one-off"). NOT
# user-terminal (see above), and disjoint from the system reasons, which apply to RULE rows only.
USER_WITHDRAW_REASON = "user_withdrawn"
# booked -> cancelled must name one of these (user = web cancel, external = the watcher's vanish
# inference §7.5, already_gone = a web cancel that found the reservation already absent). Every
# cancelled row is user-terminal for its (account, date).
CANCEL_REASONS: frozenset[str] = frozenset(
    reason for status, reason in USER_TERMINAL if status is RowStatus.CANCELLED
)


# Statuses that occupy an (account, date): exactly these hold the ``slot|<date>`` pointer
# doc (§3.2).
ACTIVE_ROW_STATUSES: frozenset[RowStatus] = frozenset(
    {RowStatus.PENDING, RowStatus.BOOKED, RowStatus.SKIPPED}
)


class RowSource(StrEnum):
    EXPLICIT = "explicit"
    RULE = "rule"


class Actor(StrEnum):
    """Who is attempting a transition. Each §3.4 transition has exactly one owning actor set."""

    WEB = "web"
    MATERIALIZER = "materializer"
    BOOKING_RUNNER = "booking_runner"
    WATCHER = "watcher"


class BookingSource(StrEnum):
    BLIND = "blind"
    SEARCH = "search"
    WATCH = "watch"
    UPGRADE = "upgrade"
    ADOPTED_OWNED = "adopted_owned"  # tenant-seed: a TOML-era bot booking, operator-confirmed
    # Watcher adoption of a landed-but-UNCERTAIN POST: an exact tee-time match with a recorded
    # UNCERTAIN slot (§4.6).
    ADOPTED_RECONCILE = "adopted_reconcile"


class BookingState(StrEnum):
    HELD = "held"
    # A surplus the in-run _cancel_extras FAILED to cancel. It stays OWNED, so the watcher's
    # owned-only duplicate reconcile collapses it (M1, preserves today's crash-net).
    HELD_EXTRA = "held_extra"
    CANCELLED_EXTRA = "cancelled_extra"
    CANCELLED_USER = "cancelled_user"
    CANCELLED_UPGRADE = "cancelled_upgrade"
    VANISHED = "vanished"


@dataclass(frozen=True, slots=True)
class User:
    id: UserId
    oauth_provider: str
    oauth_subject: str | None  # None while INVITED (bound at first sign-in)
    email: str
    display_name: str
    role: UserRole
    status: UserStatus


@dataclass(frozen=True, slots=True)
class CourseAccount:
    """One user's login at one course [D1]. UNIQUE(user_id, course_id) and
    UNIQUE(course_id, username) (§3.1). ``password_ciphertext`` is the ``tenant.crypto`` blob
    (``v1:<kid>:<nonce>:<ct>``) with AAD bound to this account (§9.2). The plaintext never lives
    here. ``otp_mailbox`` is nullable and UNUSED in v1 (OTP enforcement is UI-only)."""

    id: CourseAccountId
    user_id: UserId
    course_id: CourseId
    provenance: AccountProvenance
    username: str
    password_ciphertext: str
    key_id: str
    status: AccountStatus
    otp_mailbox: str | None = None
    consecutive_soft_auth_failures: int = 0
    verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StandingRule:
    """A weekly want, materialized into dated rows 2-3 weeks ahead (§7.7)."""

    id: RuleId
    course_account_id: CourseAccountId
    weekday: int  # Python date.weekday(): Mon=0 .. Sun=6
    window_earliest: time
    window_latest: time
    party_size: int
    active: bool
    materialized_through: date | None
    version: int


@dataclass(frozen=True, slots=True)
class RequestRow:
    """One dated booking intent: at most one ACTIVE row per (account, date) (§3.2).

    ``timezone`` is the COURSE timezone (denormalized) and ``cutoff_at`` is the UTC instant from
    ``booking_cutoff.cutoff_instant``, denormalized for query filtering; Python re-checks
    ``frozen_reason``. ``request_id`` = ``row_request_id(id)`` (§3.3). The booked_* fields are
    set only in BOOKED (and kept as history after CANCELLED). ``lease_*`` is the cross-process
    row lease (§3.5). ``group_id``/``group_rank`` are the cross-course hook (§3.6, unused in v1).
    """

    id: RowId
    course_account_id: CourseAccountId
    course_id: CourseId
    target_date: date
    timezone: str
    window_earliest: time
    window_latest: time
    party_size: int
    status: RowStatus
    source: RowSource
    cutoff_at: datetime
    request_id: RequestId
    version: int
    rule_id: RuleId | None = None
    status_reason: str | None = None
    booked_tee_time: datetime | None = None
    booked_confirmation: str | None = None
    booked_raw_id: str | None = None
    booked_at: datetime | None = None
    needs_reconcile: bool = False
    # M2 intent marker: set under the lease BEFORE the watcher runs the engine on a BOOKED row
    # (an upgrade may cancel first); cleared by the outcome write. If still set on a later run,
    # a missing reservation is bot-caused (-> PENDING + needs_reconcile), never external.
    upgrade_started_at: datetime | None = None
    # Round-4 D2 (MU-5 review): the status (PENDING or SKIPPED) a rule row had when an explicit
    # row superseded it. Set while SUPERSEDED and KEPT through a system withdraw (round-5), so
    # both un-superseding and a later reactivation restore exactly this: a user's skip is
    # honoured when their one-off is withdrawn or the rule is deactivated and reactivated.
    superseded_from: RowStatus | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_outcome: str | None = None
    last_outcome_at: datetime | None = None
    group_id: UUID | None = None
    group_rank: int | None = None


@dataclass(frozen=True, slots=True)
class RowFingerprint:
    """What the reader saw. A lease acquire matches it in the same conditional UPDATE (M5):
    if the row changed since it was read (user skip, web cancel, rule edit; every web write
    bumps ``version``), the lease is NOT acquired and the engine defers."""

    status: RowStatus
    version: int
    booked_raw_id: str | None


@dataclass(frozen=True, slots=True)
class OwnedBooking:
    """Ownership ledger entry: a reservation THIS system created (§7.6). UNIQUE(course_id,
    raw_reservation_id). Restricts the duplicate reconcile and upgrade to bot-made bookings,
    which resolves the PLAN §12 single-user residual for the hosted path."""

    id: OwnedBookingId
    row_id: RowId
    course_account_id: CourseAccountId
    course_id: CourseId
    target_date: date  # ledger is keyed by (account, date) too, so it survives a row move (M4)
    raw_reservation_id: str
    tee_time: datetime
    party_size: int
    source: BookingSource
    state: BookingState


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    raw_id: str
    tee_time: datetime
    party_size: int


@dataclass(frozen=True, slots=True)
class ReservationSnapshot:
    """Latest live reservation list per account (§7.4). ``trusted`` is False when the login
    was soft-failed or returned a non-JSON body (§7.5): such a snapshot is shown but never
    used for vanish inference or adoption."""

    course_account_id: CourseAccountId
    observed_at: datetime
    source: str  # "watcher" | "refresh" | "seed"
    trusted: bool
    entries: tuple[SnapshotEntry, ...]


@dataclass(frozen=True, slots=True)
class EventRow:
    """A row joined with its account, as returned by the single pre-T0 read (§4.2) and the
    watcher query (§7.1). Carries the ciphertext, never plaintext."""

    row: RequestRow
    account: CourseAccount


# Namespace for the deterministic tenant ids (§3.1). Constant by design: changing it would
# re-key every account and rule row.
_TENANT_ID_NAMESPACE = UUID("5d0c8a3e-2b7f-4e61-9c1a-7f3e0b6d4a21")


def row_request_id(row_id: RowId) -> RequestId:
    """``derive_request_id(f"tenant-row|{row_id}")`` (§3.3). NOT the TOML fingerprint: two
    accounts with identical window + synthesized guest names would otherwise share a RequestId
    and collide on ``request_lock`` inside one runner process."""
    return derive_request_id(f"tenant-row|{row_id}")


def rule_row_id(rule_id: RuleId, target_date: date) -> RowId:
    """Deterministic id of the rule row for (rule, date): the UUID form of the Cosmos document id
    ``row|rule|<rule_id>|<date>`` (§3.1), so UNIQUE(rule_id, date) is the id's uniqueness."""
    return RowId(uuid5(_TENANT_ID_NAMESPACE, f"row|rule|{rule_id}|{target_date.isoformat()}"))


def derive_account_id(user_id: UserId, course_id: CourseId) -> CourseAccountId:
    """``accountId = uuid5(user_id, course_id)`` (§3.1): UNIQUE(user, course) by construction."""
    return CourseAccountId(uuid5(user_id, str(course_id)))


def is_user_terminal(row: RequestRow) -> bool:
    """True iff ``row`` blocks every materialization of its (account, date) (``USER_TERMINAL``).

    It also blocks RESTORING a rule row for that date (round-7): withdrawing a later re-request
    undoes the re-request, not the cancel, so a superseded or system-withdrawn rule row stays put.
    Only a new explicit row (the user's "Re-request this date") books the date again."""
    return row.status_reason is not None and (row.status, row.status_reason) in USER_TERMINAL


def row_is_frozen(row: RequestRow, *, now: datetime) -> bool:
    """Derived ``frozen`` (§3.4): ``now >= cutoff_at`` (inclusive, like ``frozen_reason``) or the
    target date has passed in the row's COURSE timezone. The skip leg is retired for tenant rows
    (a skip is the SKIPPED status)."""
    if now >= row.cutoff_at:
        return True
    return row.target_date < now.astimezone(ZoneInfo(row.timezone)).date()


def lease_held(row: RequestRow, *, now: datetime) -> bool:
    """True iff some owner holds an UNEXPIRED lease on ``row`` (§3.5)."""
    return (
        row.lease_owner is not None
        and row.lease_expires_at is not None
        and row.lease_expires_at > now
    )


class TransitionRefusedError(ValueError):
    """A state-machine transition the §3.4 table forbids (surfaced to the web as a 409)."""


class RuleNoLongerCoversError(TransitionRefusedError):
    """A rule row cannot become (or stay) active because its STORED rule no longer covers the
    date: the rule was deactivated, deleted, or moved to another weekday (round-5/6). Distinct so
    the web (MU-13) can render "This rule no longer covers <date>; add it as a one-off instead"."""


# The §3.4 table: (from, to) -> the ONLY actors that may write it. Any pair absent here is refused.
# Guards beyond actor ownership live in ``_GUARDS`` / ``check_transition``; lease guards need the
# writer's identity and live in the store.
_P, _B, _S = RowStatus.PENDING, RowStatus.BOOKED, RowStatus.SKIPPED
_SUP, _W = RowStatus.SUPERSEDED, RowStatus.WITHDRAWN
_TRANSITION_OWNERS: dict[tuple[RowStatus, RowStatus], frozenset[Actor]] = {
    (_P, _B): frozenset({Actor.BOOKING_RUNNER, Actor.WATCHER}),
    (_P, _S): frozenset({Actor.WEB}),
    (_S, _P): frozenset({Actor.WEB}),
    (_P, _SUP): frozenset({Actor.WEB}),
    (_S, _SUP): frozenset({Actor.WEB}),
    (_SUP, _P): frozenset({Actor.WEB}),
    (_SUP, _S): frozenset({Actor.WEB}),  # round-4 D2: restore a superseded-while-skipped row
    (_P, _W): frozenset({Actor.WEB, Actor.MATERIALIZER}),
    # Round-4 D1: rule deactivate/delete/weekday change withdraws superseded rows too.
    (_SUP, _W): frozenset({Actor.WEB, Actor.MATERIALIZER}),
    (_W, _P): frozenset({Actor.MATERIALIZER}),
    # Round-5: reactivating a row that was superseded-while-skipped restores SKIPPED.
    (_W, _S): frozenset({Actor.MATERIALIZER}),
    (_B, _B): frozenset({Actor.WATCHER}),  # upgrade, via UpgradeOrchestrator under the lease
    (_B, RowStatus.CANCELLED): frozenset({Actor.WEB, Actor.WATCHER}),
    (_B, _P): frozenset({Actor.WATCHER}),  # upgrade cancelled the old slot, rebook failed
    (_P, RowStatus.LOST): frozenset({Actor.WATCHER}),  # the finalizer
}
# Edges back INTO the active set (other than creation) that require the date not frozen.
_REQUIRES_NOT_FROZEN: frozenset[tuple[RowStatus, RowStatus]] = frozenset(
    {(_S, _P), (_SUP, _P), (_W, _P), (_W, _S)}
)
# Creation (∅ -> status): the owning actor per row source, and the allowed initial statuses
# (a rule row colliding with another active row is created SUPERSEDED, §7.7 step 3).
_CREATE_OWNER: dict[RowSource, Actor] = {
    RowSource.EXPLICIT: Actor.WEB,
    RowSource.RULE: Actor.MATERIALIZER,
}
_CREATE_STATUSES: dict[RowSource, frozenset[RowStatus]] = {
    RowSource.EXPLICIT: frozenset({_P}),
    RowSource.RULE: frozenset({_P, _SUP}),
}


def check_create(row: RequestRow, *, actor: Actor, now: datetime) -> None:
    """The ``∅ -> pending`` row of §3.4 (pure): explicit rows are created by the WEB, rule rows by
    the MATERIALIZER (PENDING, or SUPERSEDED on collision), never for a frozen/past date. The
    "no other active row" leg is the store's (the slot pointer, §3.2)."""
    owner = _CREATE_OWNER[row.source]
    if actor is not owner:
        raise TransitionRefusedError(f"{actor} may not create a {row.source} row (only {owner})")
    if row.status not in _CREATE_STATUSES[row.source]:
        raise TransitionRefusedError(f"a {row.source} row cannot be created {row.status}")
    if row_is_frozen(row, now=now):
        raise TransitionRefusedError(f"{row.target_date} is frozen (cutoff or date passed)")


def _guard_withdraw(row: RequestRow, actor: Actor, reason: str | None) -> None:
    # WEB: an explicit row carries USER_WITHDRAW_REASON; a rule row (rule deleted/edited from the
    # web) carries a SYSTEM reason. MATERIALIZER: system reasons on rule rows only.
    if row.source is RowSource.EXPLICIT:
        ok = actor is Actor.WEB and reason == USER_WITHDRAW_REASON
    else:
        ok = reason in SYSTEM_WITHDRAW_REASONS
    if not ok:
        raise TransitionRefusedError(
            f"withdraw reason {reason!r} is not valid for a {row.source} row by {actor}"
        )


def _guard_reactivate(row: RequestRow, actor: Actor, reason: str | None) -> None:
    if row.status_reason not in SYSTEM_WITHDRAW_REASONS:
        raise TransitionRefusedError(
            f"only a system-withdrawn row comes back (status_reason={row.status_reason!r})"
        )


def _guard_supersede(row: RequestRow, actor: Actor, reason: str | None) -> None:
    if row.source is not RowSource.RULE:
        raise TransitionRefusedError("only rule rows can be superseded")


# Who may write which cancel reason: only the watcher infers ``external`` (vanish, §7.5); the web
# cancels for the user (``user``) or finds the reservation already absent (``already_gone``, §8.5).
_CANCEL_REASONS_BY_ACTOR: dict[Actor, frozenset[str]] = {
    Actor.WEB: frozenset({"user", "already_gone"}),
    Actor.WATCHER: frozenset({"external"}),
}


def _guard_cancel(row: RequestRow, actor: Actor, reason: str | None) -> None:
    allowed = _CANCEL_REASONS_BY_ACTOR.get(actor, frozenset())
    if reason not in allowed:
        raise TransitionRefusedError(f"cancel reason {reason!r} not in {sorted(allowed)} ({actor})")


_GUARDS = {
    (_P, _W): _guard_withdraw,
    (_SUP, _W): _guard_withdraw,
    (_W, _P): _guard_reactivate,
    (_W, _S): _guard_reactivate,
    (_P, _SUP): _guard_supersede,
    (_S, _SUP): _guard_supersede,
    (_B, RowStatus.CANCELLED): _guard_cancel,
}


def check_transition(
    row: RequestRow,
    to: RowStatus,
    *,
    actor: Actor,
    now: datetime,
    reason: str | None = None,
    needs_reconcile: bool = False,
) -> None:
    """Raise ``TransitionRefusedError`` unless the §3.4 table allows ``row.status -> to`` for
    ``actor`` at ``now`` (e.g. booked -> skipped is always refused; unskip / un-supersede /
    reactivate require not frozen; pending -> lost requires frozen). ``reason`` is the new
    ``status_reason`` (withdraw and cancel validate it against their vocabularies).
    ``needs_reconcile`` is the value the write sets: booked -> pending (the M2 upgrade
    cancel-ok / rebook-failed edge) is refused unless it is True, because without the flag the
    §7.6 in-window adoption never applies and a landed rebook would be adopted as unowned.

    Pure. Lease guards ("row not leased" for web/materializer writes, "holds the lease" for the
    runner/watcher) need the writer's identity, so the STORE enforces them in the same atomic
    write; so is "no other active row" (the slot pointer)."""
    edge = (row.status, to)
    if edge == (_B, _S):
        raise TransitionRefusedError("a booked row cannot be skipped: use Cancel instead")
    owners = _TRANSITION_OWNERS.get(edge)
    if owners is None:
        raise TransitionRefusedError(f"no transition {row.status} -> {to}")
    if actor not in owners:
        raise TransitionRefusedError(f"{actor} may not write {row.status} -> {to}")
    frozen = row_is_frozen(row, now=now)
    if edge in _REQUIRES_NOT_FROZEN and frozen:
        raise TransitionRefusedError(f"{row.target_date} is frozen (cutoff or date passed)")
    if to is RowStatus.LOST and not frozen:
        raise TransitionRefusedError(f"{row.target_date} is not frozen yet; cannot mark lost")
    if row.status in (_SUP, _W) and to in (_P, _S):
        # Round-4 D2 / round-5: un-supersede AND reactivation restore the pre-supersede status.
        prior = row.superseded_from or _P
        if to is not prior:
            raise TransitionRefusedError(f"row was superseded from {prior}; it returns to {prior}")
    if edge == (_B, _P) and not needs_reconcile:
        raise TransitionRefusedError("booked -> pending must set needs_reconcile (M2)")
    guard = _GUARDS.get(edge)
    if guard is not None:
        guard(row, actor, reason)
