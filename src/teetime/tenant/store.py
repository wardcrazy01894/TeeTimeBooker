"""TenantStore: the durable multi-user store (MULTIUSER_PLAN §3.7).

A SIBLING of ``persistence.store.BookingStore``, which is UNCHANGED and stays the engine's in-run
memory (``InMemoryStore``: terminals, attempt log, in-process ``request_lock``). This Protocol
carries durable intent + ownership: users, course accounts, standing rules, dated rows, the
ownership ledger, snapshots, login-probe counters and the audit log.

Implementations: ``tenant.in_memory_store.InMemoryTenantStore`` (MU-5, tests + the conformance
reference) and ``CosmosTenantStore`` (MU-8b, async ``azure-cosmos`` + MI auth, free-tier account
§10.2). Both must pass the same conformance suite, ``tests/tenant/conformance.py`` (the Cosmos leg
is ``integration``-marked against the real ``dev`` database; no emulator in CI). The suite, not this
docstring, is the executable contract.

Write paths and their lease rule (§3.4 "every web-initiated transition requires the row unleased",
M4; §3.5):

- ``transition_row`` / ``create_explicit_row`` / ``reactivate_rule_row`` are the UNLEASED paths
  (web + materializer). They refuse with ``RowLeaseError`` while ANY owner holds an unexpired
  lease on an affected row, so a booker claim can never be pulled out from under WRITE #2.
- ``record_outcomes`` is the LEASED path (booking runner, watcher, and the web's managed cancel,
  which first takes its own 60 s lease). A status change requires ``release_lease_owner`` to be
  the current lease holder.
- Lease writes (``claim_rows``, ``acquire_row_lease``, ``release_row_lease``,
  ``set_upgrade_marker``) change the doc ETag but NOT the domain ``version``, so the
  ``RowFingerprint`` a reader registered stays valid across its own lease acquire (M5). ``version``
  bumps on every status / booking / content change.

Cosmos mapping (§3.1/§3.2): partition key = ``course_account_id``, so a row, its date-slot pointer
doc (``slot|<date>``: the one-ACTIVE-row-per-(account, date) invariant), its ledger entries and the
account snapshot share ONE logical partition, and every state change is ONE transactional batch
with per-op IfMatch ETags. Uniqueness comes from deterministic document ids (unique per partition),
not unique-key policies (those cannot be filtered to "active" rows).
Every mutating method is atomic (one batch) and enforces the §3.4 state
machine via ``models.check_transition``.

Call-budget contract for the booking runner (§4.2, proven by
``test_runner_no_store_calls_inside_race_window``): exactly ``load_event_rows`` + ``claim_rows``
before T0 - lead, then NOTHING until T0 + ``post_burst_quiet_s`` (10 s), after which
``record_outcomes`` is called once PER ROW as each account's result is ready (streamed, round-1
SF5/M4). No store call happens inside [T0 - lead - 1 s, T0 + 10 s].
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol, runtime_checkable

from ..core.models import BookingResult, CourseId, RequestId
from ..persistence.in_memory_store import InMemoryStore
from .models import (
    Actor,
    CourseAccount,
    CourseAccountId,
    EventRow,
    OwnedBooking,
    RequestRow,
    ReservationSnapshot,
    RowFingerprint,
    RowId,
    RowStatus,
    RuleId,
    StandingRule,
    User,
    UserId,
)

_MU9 = "MULTIUSER_PLAN.md MU-9c"


class RowLeaseError(RuntimeError):
    """A row lease is held by another owner (§3.5). The web surfaces it as
    "booking in progress"; ``LeasedBookingStore`` maps it to ``ConcurrentRunError``."""


class TenantNotFoundError(LookupError):
    """The row / account / rule does not exist OR is not the caller's (``user_id`` scoping). The
    two are deliberately indistinguishable (IDOR defence, §9.1); the web returns 404."""


class UniquenessConflictError(ValueError):
    """A cross-partition uniqueness claim is taken (§3.2): UNIQUE(course, username), UNIQUE
    (provider, subject), ``max_accounts_per_course``, or an account id that is not
    ``models.derive_account_id(user_id, course_id)``."""


@dataclass(frozen=True, slots=True)
class RowOutcome:
    """One row's post-burst / post-act result for ``record_outcomes`` (§4.2 WRITE #2).

    Keyed by ``row_id`` AND by (``course_account_id``, ``target_date``). If the row's transition is
    refused (it moved), the ledger entries are still written against (account, date), and the
    active row for that date gets ``needs_reconcile`` (M4).

    ``to_status`` is None when the row stays in its status (e.g. NO_INVENTORY leaves it
    PENDING). ``booking`` is the ownership record to insert (BOOKED by us), and
    ``cancelled_extras`` are the ledger entries of the surplus bookings ``_cancel_extras``
    cancelled (state cancelled_extra; full entries, not bare raw ids, so the ledger keeps the
    tee time the recorder saw). ``release_lease_owner`` names the writer's lease: a status change
    requires it to be the current holder, and the lease is released iff still held by it.
    ``status_reason`` is the new reason (e.g. ``external`` on booked -> cancelled).
    """

    row_id: RowId
    course_account_id: CourseAccountId
    target_date: date
    actor: Actor
    to_status: RowStatus | None
    last_outcome: str
    at: datetime
    result: BookingResult | None = None
    booking: OwnedBooking | None = None
    cancelled_extras: tuple[OwnedBooking, ...] = ()
    held_extras: tuple[OwnedBooking, ...] = ()  # _cancel_extras failures (M1)
    cancelled_upgrade_raw_id: str | None = None  # an upgrade's cancelled old reservation (M2)
    clear_upgrade_marker: bool = False
    needs_reconcile: bool = False
    status_reason: str | None = None
    release_lease_owner: str | None = None


@runtime_checkable
class TenantStore(Protocol):
    """Durable tenant persistence. See module docstring for the call-budget contract."""

    async def initialize(self) -> None:
        """Verify connectivity + schema version (migrations are run by the migrate job, not
        here, §10.1). Idempotent."""
        ...

    # --- booking runner (pre-T0 read + claim; post-burst write) ----------------------

    async def load_event_rows(
        self,
        *,
        targets: Mapping[CourseId, date],
        now: datetime,
    ) -> list[EventRow]:
        """READ #1 (§4.2): PENDING rows for each (course, target_date), ``cutoff_at > now``,
        joined to ACTIVE accounts. Ordered by row id (the allocator rotates from there)."""
        ...

    async def claim_rows(
        self,
        row_ids: Sequence[RowId],
        *,
        owner: str,
        until: datetime,
        now: datetime,
    ) -> frozenset[RowId]:
        """WRITE #1 (§4.2): conditionally lease each row (free or expired lease AND still
        PENDING). Returns the ids actually claimed."""
        ...

    async def record_outcomes(self, outcomes: Sequence[RowOutcome]) -> None:
        """WRITE #2 (§4.2) and watcher outcome writes. **One transaction PER outcome**, never
        one for the batch: a refused transition on one row must not roll back other accounts'
        outcomes (M4). Each applies the status transition (via ``check_transition``), ledger
        inserts, ``last_outcome``, ``needs_reconcile``, the upgrade marker, and lease release.
        Returns nothing; per-row failures are raised as an ``ExceptionGroup`` AFTER every row
        was attempted.

        A REFUSED row (it moved, or the writer no longer holds its lease) still gets its ledger
        entries, written against (account, date), and the ACTIVE row for that date (if any) gets
        ``needs_reconcile``; the refusal is then reported in the ``ExceptionGroup`` (M4)."""
        ...

    # --- leases (watcher + web, §3.5) ------------------------------------------------

    async def acquire_row_lease(
        self,
        row_id: RowId,
        *,
        owner: str,
        until: datetime,
        now: datetime,
        expected: RowFingerprint | None,
    ) -> bool:
        """Conditional lease acquire; True iff acquired. Cosmos: point read + IfMatch replace (a 412
        means not acquired). With ``expected`` set, status/version/booked_raw_id must also be
        unchanged in the doc read; the IfMatch makes check-and-set atomic (M5)."""
        ...

    async def release_row_lease(self, row_id: RowId, *, owner: str) -> None:
        """Release iff ``owner`` still holds it (no-op otherwise)."""
        ...

    # --- watcher (§7) ------------------------------------------------------------------

    async def load_watch_rows(
        self,
        *,
        horizons: Mapping[CourseId, tuple[date, date]],
        now: datetime,
    ) -> list[EventRow]:
        """The single watcher query (§7.1): PENDING (not frozen) + BOOKED rows within each
        course's ``[local_today, local_today + advance_days]`` horizon, joined to accounts."""
        ...

    async def finalize_lost(self, *, now: datetime) -> list[RequestRow]:
        """PENDING rows now frozen (``cutoff_at <= now`` or date passed) -> LOST; returns
        them so a ``lost`` email is sent exactly once (§3.4)."""
        ...

    async def get_snapshot(self, account_id: CourseAccountId) -> ReservationSnapshot | None: ...

    async def save_snapshot(self, snapshot: ReservationSnapshot) -> None:
        """Upsert the latest snapshot for the account (§7.4)."""
        ...

    async def list_owned_bookings(
        self, account_id: CourseAccountId, *, target_date: date
    ) -> list[OwnedBooking]: ...

    async def set_upgrade_marker(
        self, row_id: RowId, *, owner: str, at: datetime, expected: RowFingerprint
    ) -> bool:
        """Set ``upgrade_started_at`` iff ``owner`` holds the lease and the fingerprint matches
        (M2). The watcher calls it before running the engine on a BOOKED row."""
        ...

    async def record_soft_auth_failure(self, account_id: CourseAccountId) -> int:
        """Increment and return ``consecutive_soft_auth_failures``; at 3 the store flips the
        account to AUTH_FAILED (§7.5)."""
        ...

    # --- materializer (§7.7) -----------------------------------------------------------

    async def rules_needing_materialization(self, *, through: date) -> list[StandingRule]: ...

    async def rows_for_account_date(
        self, account_id: CourseAccountId, target_date: date
    ) -> list[RequestRow]:
        """Every row (any status) for (account, date): one single-partition query. It is the
        (account, date) history the materializer consults (round-2 M1, §7.7)."""
        ...

    async def insert_rule_row_if_absent(
        self, rule: StandingRule, target_date: date, *, now: datetime
    ) -> RequestRow | None:
        """Create ``row|rule|<rule_id>|<date>`` (+ its slot doc in the same batch if the slot is
        free; otherwise as SUPERSEDED). A create conflict returns None and means only that a row
        EXISTS, not that the date is handled: the caller must consult ``rows_for_account_date``
        and may call ``reactivate_rule_row`` (round-2 M1). Callers first skip dates with a
        user-terminal row (``models.USER_TERMINAL``)."""
        ...

    async def reactivate_rule_row(
        self, row: RequestRow, rule: StandingRule, *, now: datetime
    ) -> RequestRow:
        """System-withdrawn (``models.SYSTEM_WITHDRAW_REASONS``) rule row -> PENDING, with window
        and party refreshed from ``rule``: one batch of IfMatch replace + slot create. Refused
        (``TransitionRefusedError``) if the date has a user-terminal row, is frozen, or the slot is
        held."""
        ...

    async def set_materialized_through(self, rule_id: RuleId, through: date) -> None: ...

    # --- web (§8) ----------------------------------------------------------------------

    async def get_user_by_subject(self, provider: str, subject: str) -> User | None: ...

    async def upsert_user(self, user: User) -> None:
        """Create or replace a user (operator ``/admin/users`` invite/disable, §8.2). Enforces
        UNIQUE(provider, subject) for bound users (``UniquenessConflictError``)."""
        ...

    async def bind_invited_user(self, *, email: str, provider: str, subject: str) -> User | None:
        """First sign-in: bind an INVITED user's subject (matched by provider-verified email).
        Returns None if not invited (the web returns 403)."""
        ...

    async def list_rows_for_user(
        self, user_id: UserId, *, from_date: date, to_date: date
    ) -> list[RequestRow]:
        """Every query in the web is scoped by ``user_id`` (IDOR defence, §9.1)."""
        ...

    async def get_account(
        self, account_id: CourseAccountId, *, user_id: UserId
    ) -> CourseAccount | None: ...

    async def upsert_account(self, account: CourseAccount) -> None:
        """Enforces uniqueness of (user, course) (derived accountId), (course, username) (a
        claim doc, reclaimed if orphaned), and ``max_accounts_per_course`` (an IfMatch counter
        doc) (§3.2)."""
        ...

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
        """Supersedes a PENDING/SKIPPED rule row for the same (account, date) in the same
        transaction; refuses (``TransitionRefusedError``) if a BOOKED row or another explicit row
        holds the date, or the date is frozen; ``RowLeaseError`` if the rule row is leased (M4)."""
        ...

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
        """The UNLEASED guarded transition for the WEB (skip, unskip, withdraw, un-supersede) and
        the MATERIALIZER (system withdraw). ``user_id`` scopes web calls (required for WEB). Refuses
        with ``RowLeaseError`` while the row is leased (M4) and ``TransitionRefusedError`` per
        §3.4. Refused here: leased-path actors (runner, watcher), the supersede edge (only ever
        written by ``create_explicit_row``, in the same batch as the explicit row) and
        withdrawn -> pending (only ever written by ``reactivate_rule_row``, which re-checks the
        user-terminal history and refreshes the row from the rule).
        Withdrawing an explicit row restores the rule row it superseded to PENDING in the same
        batch when that rule is still active and the date is not frozen (§3.4)."""
        ...

    async def upsert_rule(self, rule: StandingRule, *, user_id: UserId) -> StandingRule:
        """Create or replace (version bump) a rule on one of ``user_id``'s accounts. Refuses a
        second ACTIVE rule on the same (account, weekday) with ``materialize.RuleConflictError``
        (round-3 SF1). Does not touch rows (the materializer does, §7.7)."""
        ...

    async def count_login_probes(
        self, *, user_id: UserId | None, username_hash: str | None, since: datetime
    ) -> int: ...

    async def record_login_probe(
        self, *, user_id: UserId, course_id: CourseId, username_hash: str, ok: bool, at: datetime
    ) -> None: ...

    async def append_audit(
        self,
        *,
        user_id: UserId | None,
        action: str,
        row_id: RowId | None,
        detail: Mapping[str, object],
        at: datetime,
    ) -> None:
        """Append-only; ``detail`` passes through ``core.redaction.redact_payload``."""
        ...


class LeasedBookingStore:
    """``BookingStore`` adapter for the WATCHER and WEB paths (§3.5).

    Delegates terminals/attempts/sessions to an ``InMemoryStore`` but maps
    ``request_lock(request_id)`` to the DURABLE row lease of the row registered for that
    RequestId, **matching the ``RowFingerprint`` the runner read** (M5). It raises
    ``ConcurrentRunError`` on contention OR if the row changed since it was read, so the
    engine's existing defer handling (Gate-3 / ``maybe_upgrade``) serializes against the
    booker and the web across PROCESSES. The booking runner does NOT use this: its lease is
    already held from ``claim_rows``, and its in-run lock stays a plain ``InMemoryStore`` (no
    DB near T0).

    STUB — implemented in MU-9c. Not yet a structural ``BookingStore`` (methods land with MU-9c).
    """

    def __init__(
        self,
        *,
        inner: InMemoryStore,
        tenant: TenantStore,
        owner: str,
        lease_seconds: float,
        row_for_request: Mapping[RequestId, tuple[RowId, RowFingerprint]],
    ) -> None:
        raise NotImplementedError(_MU9)

    def request_lock(self, request_id: RequestId) -> AbstractAsyncContextManager[None]:
        """Acquire the durable row lease for ``row_for_request[request_id]`` with its
        fingerprint; else raise ``ConcurrentRunError`` (contention or row changed)."""
        raise NotImplementedError(_MU9)
