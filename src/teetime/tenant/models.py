"""Tenant domain shapes (MULTIUSER_PLAN §3). Plain frozen dataclasses with no I/O.

Invariants live in the store (Cosmos: deterministic ids + date-slot pointer docs + transactional
batches, §3.2) AND in the state-machine
checker (``check_transition``, §3.4), and both are exercised by the store conformance suite (MU-5).
``frozen`` is DERIVED (``core.booking_cutoff.frozen_reason``), never a stored status.

STUB — implemented in MULTIUSER_PLAN MU-5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import NewType
from uuid import UUID

from ..core.models import CourseId, RequestId

_MU5 = "MULTIUSER_PLAN.md MU-5"

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


def row_request_id(row_id: RowId) -> RequestId:
    """``derive_request_id(f"tenant-row|{row_id}")`` (§3.3). NOT the TOML fingerprint: two
    accounts with identical window + synthesized guest names would otherwise share a RequestId
    and collide on ``request_lock`` inside one runner process."""
    raise NotImplementedError(_MU5)


def check_transition(
    row: RequestRow,
    to: RowStatus,
    *,
    actor: Actor,
    now: datetime,
) -> None:
    """Raise ``TransitionRefusedError`` unless the §3.4 table allows ``row.status -> to`` for
    ``actor`` at ``now`` (e.g. booked -> skipped is always refused; creation/unskip require not
    frozen). Pure; the store calls it inside the transaction that performs the write."""
    raise NotImplementedError(_MU5)


class TransitionRefusedError(ValueError):
    """A state-machine transition the §3.4 table forbids (surfaced to the web as a 409)."""
