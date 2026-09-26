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
  which first takes its own 60 s lease). A status change requires ``release_lease_owner`` to hold
  an UNEXPIRED lease at ``RowOutcome.at``. Every unleased write clears an EXPIRED lease, so a
  stale holder can never reclaim a row the web touched after its lease ran out.
- The "one ACTIVE rule per (account, weekday)" invariant (round-3 SF1) is a deterministic
  in-partition pointer doc ``ruleday|<weekday>`` (``activeRuleId``), exactly like the date slot:
  activating a rule creates it (409 = the weekday is taken), moving or deactivating a rule
  deletes / re-points it, in the same batch as the rule replace. A scan-then-write cannot hold
  this invariant under concurrent web requests in Cosmos; the pointer can (§3.2, MU-8b).
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

import logging
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import UUID

from ..core.clock import Clock
from ..core.models import BookingResult, CourseId, RequestId
from ..persistence.in_memory_store import InMemoryStore
from ..persistence.store import ConcurrentRunError
from .models import (
    Actor,
    CourseAccount,
    CourseAccountId,
    EventRow,
    OwnedBooking,
    RankedWindow,
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

log = logging.getLogger(__name__)


class RowLeaseError(RuntimeError):
    """A row lease is held by another owner (§3.5). The web surfaces it as
    "booking in progress"; ``LeasedBookingStore`` maps it to ``ConcurrentRunError``."""


class TenantNotFoundError(LookupError):
    """The row / account / rule does not exist OR is not the caller's (``user_id`` scoping). The
    two are deliberately indistinguishable (IDOR defence, §9.1); the web returns 404."""


class VersionConflictError(ValueError):
    """An update was made from a stale read (the caller's ``version`` is not the stored one); the
    caller must re-read and retry (Cosmos: IfMatch 412). Used for rule edits (§3.4)."""


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
        joined to ACTIVE accounts, EXCLUDING rule rows their STORED rule no longer covers
        (missing, inactive, other weekday). Ordered by row id (the allocator rotates from
        there)."""
        ...

    async def rows_in_groups(self, keys: Collection[tuple[UUID, date]]) -> list[RequestRow]:
        """System read (§16.3/§16.4, MU-R2): every row of each ``(group_id, target_date)``, any
        status, across account partitions. The group floor and the collapse read it; the web
        never calls it."""
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
        Only the LEASED edges may be written here: pending -> booked (runner, watcher), booked ->
        booked (watcher upgrade), booked -> pending + needs_reconcile (watcher), booked ->
        cancelled (watcher ``external``; web ``user`` / ``already_gone`` for the §8.5 cancel).
        Every other edge is refused, because only the unleased paths carry its guards
        (user-terminal history, the D2 restore, rule active). Returns nothing; per-row failures
        are raised as an ``ExceptionGroup`` AFTER every row was attempted.

        A REFUSED row (it moved, or the writer no longer holds an unexpired lease) still gets its
        ledger entries, written against (account, date), and the ACTIVE row for that date (if any)
        gets ``needs_reconcile``; the refusal is then reported in the ``ExceptionGroup`` (M4).
        When the date has NO active row, a BOOKED outcome survives ONLY in the ledger and the
        ``ExceptionGroup``: callers (MU-9a/MU-10b) MUST treat an ``ExceptionGroup`` as a non-zero
        exit, and the §7.6 ownership report lists such ledger-only bookings as orphans.

        Ledger entries are validated (same account) BEFORE anything is written: an invalid entry
        writes neither the row nor the ledger (one unit). An outcome with no status change needs
        no lease, unless ANOTHER owner holds an unexpired one (then it is refused)."""
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
        unchanged in the doc read; the IfMatch makes check-and-set atomic (M5). Only PENDING and
        BOOKED rows are leasable (nothing is booked, upgraded or cancelled from any other status);
        any other status returns False."""
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
        course's ``[local_today, local_today + advance_days]`` horizon, joined to accounts. A
        PENDING rule row its stored rule no longer covers is excluded UNLESS it has
        ``needs_reconcile`` (round-6: a rebook that may have landed must still be adopted, §7.6);
        BOOKED rows are always included."""
        ...

    async def finalize_lost(self, *, now: datetime) -> list[RequestRow]:
        """PENDING rows now frozen (``cutoff_at <= now`` or date passed) -> LOST; returns
        them so a ``lost`` email is sent exactly once (§3.4). A frozen PENDING rule row its stored
        rule no longer covers is WITHDRAWN instead (``rule_deleted`` / ``rule_deactivated`` /
        ``rule_weekday_changed``), is not returned, and gets no email."""
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

    async def get_rule_unscoped(self, rule_id: RuleId) -> StandingRule | None:
        """SYSTEM read (the materializer tick, MU-6): the STORED rule by id, active or not, with
        NO ``user_id`` scoping. The tick's sweep names each straggler's withdraw reason from it
        (missing -> ``rule_deleted``, inactive -> ``rule_deactivated``, else
        ``rule_weekday_changed``, §7.7). Never called from a web request: the web reads rules
        user-scoped (IDOR, §9.1). Cosmos (MU-8b): a point read of the rule doc."""
        ...

    async def get_account_unscoped(self, account_id: CourseAccountId) -> CourseAccount | None:
        """SYSTEM read (the materializer tick, MU-6): the account by id with NO ``user_id``
        scoping, so a due rule (which carries only ``course_account_id``) can be mapped to its
        course and so to its ``ReleasePolicy`` (horizon + course-local today). Never called
        from a web request, which uses the user-scoped ``get_account`` (IDOR, §9.1). Cosmos
        (MU-8b): a point read of the partition's ``account`` doc."""
        ...

    async def get_user_unscoped(self, user_id: UserId) -> User | None:
        """SYSTEM read (the booking runner's post-race emails, MU-9b): the user by id with NO
        session scoping — a row reaches the runner carrying only its account's ``user_id``.
        Never called from a web request. Cosmos (MU-8b): a point read of the ``user:<id>``
        doc in ``global``."""
        ...

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
        user-terminal row (``models.USER_TERMINAL``). ``rule`` must be the STORED version
        (IfMatch; a stale copy is ``TransitionRefusedError``), so a row is never created under a
        rule that has since moved weekday. Cosmos (MU-8b): assert the rule doc's (or its
        ``ruleday|<weekday>`` pointer's) ETag in the same batch as the row create."""
        ...

    async def reactivate_rule_row(
        self, row: RequestRow, rule: StandingRule, *, now: datetime
    ) -> RequestRow:
        """System-withdrawn (``models.SYSTEM_WITHDRAW_REASONS``) rule row -> its pre-supersede
        status if it was superseded before the withdraw (``superseded_from``, round-5: SKIPPED
        stays SKIPPED), else PENDING, with window and party refreshed from ``rule``: one batch of
        IfMatch replace + slot create. Refused (``TransitionRefusedError``) if ``rule`` is not the
        STORED version (IfMatch, round-5), the stored rule no longer covers the row (inactive,
        other weekday or account), the date has a user-terminal row, is frozen, or the slot is
        held. Every writer of an active status onto a rule row applies the same coverage +
        user-terminal guard (round-5 MF1)."""
        ...

    async def rewrite_pending_rule_row(
        self,
        row_id: RowId,
        *,
        rule: StandingRule,
        expected_version: int,
        now: datetime,
    ) -> RequestRow:
        """The rule-edit writer (§3.4, MU-6 ``apply_rule_edit``): rewrite a PENDING rule row's
        window/party in place from ``rule``. Guards: the row is still ``expected_version``
        (IfMatch), is this rule's own PENDING row, unleased (``RowLeaseError``), not frozen,
        ``rule`` is the STORED version, and the stored rule still covers the row
        (``RuleNoLongerCoversError``). A pending -> pending write does not pass the batch's
        may-become-active guard, so this method calls the coverage guard itself."""
        ...

    async def set_materialized_through(self, rule_id: RuleId, through: date) -> None:
        """Advance the rule's materialization watermark; never moves it backwards."""
        ...

    async def reset_materialized_through(self, rule_id: RuleId) -> None:
        """Clear the watermark (the rule is due for the next tick). The web deactivation flow
        calls it FIRST and AGAIN after withdrawing rows (a concurrent tick may have re-advanced
        it), before writing the rule inactive; the reactivation flow calls it after its upsert. A
        crash part-way then leaves the tick work to do instead of a withdrawn row nothing
        revisits (§7.7). A stored None also wins over a caller's stale copy in ``upsert_rule``."""
        ...

    async def rows_no_longer_covered(self, *, now: datetime) -> list[RequestRow]:
        """Unleased PENDING / SUPERSEDED rule rows their STORED rule no longer covers (missing,
        inactive, or on another weekday / account): the stragglers a deactivation or weekday move
        had to skip because they were leased at the time (§3.4 "rule edits never touch leased
        rows"). The materializer tick withdraws them with the matching system reason
        (``rule_deleted`` / ``rule_deactivated`` / ``rule_weekday_changed``, §7.7). SKIPPED
        rows are deliberately excluded: they are never booked, and keeping them preserves the
        user's skip across a later reactivation (round-5)."""
        ...

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

    async def get_row(self, row_id: RowId, *, user_id: UserId) -> RequestRow | None:
        """The stored row iff it is on one of ``user_id``'s accounts, else None — a missing row
        and another user's row are deliberately indistinguishable (IDOR, §9.1; the web answers
        404 for both). Read-only (MU-13). Cosmos (MU-8b): the user's account ids, then a point
        read in that partition."""
        ...

    async def list_accounts_for_user(self, user_id: UserId) -> list[CourseAccount]:
        """Every course account of ``user_id`` (any status). Read-only, user-scoped (MU-13: the
        rules / dates forms pick an account from it)."""
        ...

    async def list_rules_for_user(self, user_id: UserId) -> list[StandingRule]:
        """Every standing rule (active AND inactive, stored versions) on ``user_id``'s accounts.
        Read-only, user-scoped (MU-13: the rules page lists, edits and reactivates them)."""
        ...

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
        options: tuple[RankedWindow, ...],
        party_size: int,
        now: datetime,
        max_price: Decimal | None = None,
        group_id: UUID | None = None,
        group_rank: int | None = None,
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
        §3.4. Unskip and un-supersede of a rule row carry the shared coverage + user-terminal
        guard: ``RuleNoLongerCoversError`` (a ``TransitionRefusedError``) when the stored rule no
        longer covers the date, which the web renders as "add it as a one-off instead".
        Refused here: leased-path actors (runner, watcher), booked -> cancelled (a leased
        edge, ``record_outcomes`` only), the supersede edge (only ever written by
        ``create_explicit_row``, in the same batch as the explicit row) and
        withdrawn -> pending (only ever written by ``reactivate_rule_row``, which re-checks the
        user-terminal history and refreshes the row from the rule).
        Withdrawing an explicit row restores, in the same batch, the date's rule row whose rule
        is ACTIVE and still covers it (same weekday + account, round-4 MF-A) and whose date is not
        frozen: a SUPERSEDED one (D2), or else a SYSTEM-WITHDRAWN one (round-6, the deactivate ->
        reactivate -> withdraw order, window/party refreshed from the rule). Either returns to
        ``superseded_from or PENDING``. Nothing is restored on a user-terminal date (round-7):
        withdrawing a re-request undoes the re-request, not the cancel."""
        ...

    async def upsert_rule(self, rule: StandingRule, *, user_id: UserId) -> StandingRule:
        """Create or replace (version bump) a rule on one of ``user_id``'s accounts. Refuses a
        second ACTIVE rule on the same (account, weekday) with ``materialize.RuleConflictError``
        (round-3 SF1, the ``ruleday|<weekday>`` pointer), and a replace whose ``rule.version`` is
        not the stored version with ``VersionConflictError`` (IfMatch). ``materialized_through``
        never moves backwards (a web edit from a copy read before the watcher tick keeps the
        tick's value), except that a stored None (a reset) wins, and a weekday change or a
        re-activation CLEARS it in the same write so the rule is due for the tick (round-5 SF-2).
        Does not touch rows (the materializer does, §7.7).
        A future rule DELETE must remove the rule doc and this pointer in the same batch."""
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

    SINGLE-READ FINGERPRINT (MU-9c review): the ``RowFingerprint`` is captured once per instance
    and never refreshed. If anything writes the row between two ``request_lock`` acquisitions
    for the same RequestId (e.g. a ``record_outcomes`` that sets ``needs_reconcile`` and bumps the
    version), every later acquisition on that row in the same run DEFERS as "moved". A caller
    that locks a row more than once per run with an intervening write (the watcher's
    reconcile-then-upgrade) must re-read and build a fresh instance, or refresh the map.

    Delegates terminals/attempts/sessions to an ``InMemoryStore`` but maps
    ``request_lock(request_id)`` to the DURABLE row lease of the row registered for that
    RequestId, **matching the ``RowFingerprint`` the runner read** (M5). It raises
    ``ConcurrentRunError`` on contention OR if the row changed since it was read, so the
    engine's existing defer handling (Gate-3 / ``maybe_upgrade``) serializes against the
    booker and the web across PROCESSES. The booking runner does NOT use this: its lease is
    already held from ``claim_rows``, and its in-run lock stays a plain ``InMemoryStore`` (no
    DB near T0).
    """

    def __init__(
        self,
        *,
        inner: InMemoryStore,
        tenant: TenantStore,
        owner: str,
        lease_seconds: float,
        clock: Clock,
        row_for_request: Mapping[RequestId, tuple[RowId, RowFingerprint]],
    ) -> None:
        self._inner = inner
        self._tenant = tenant
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._row_for_request = dict(row_for_request)

    async def initialize(self) -> None:
        await self._inner.initialize()

    async def get_terminal(
        self, request_id: RequestId, resolved_date: date
    ) -> BookingResult | None:
        return await self._inner.get_terminal(request_id, resolved_date)

    async def record_terminal(self, result: BookingResult, resolved_date: date) -> None:
        await self._inner.record_terminal(result, resolved_date)

    async def delete_terminal(self, request_id: RequestId, resolved_date: date) -> None:
        await self._inner.delete_terminal(request_id, resolved_date)

    async def append_attempt(
        self,
        request_id: RequestId,
        attempt: int,
        event: str,
        payload: dict[str, object],
        at: datetime,
    ) -> None:
        await self._inner.append_attempt(request_id, attempt, event, payload, at)

    async def cache_session(self, course_id: CourseId, blob: bytes, expires_at: datetime) -> None:
        await self._inner.cache_session(course_id, blob, expires_at)

    async def load_session(self, course_id: CourseId) -> bytes | None:
        return await self._inner.load_session(course_id)

    def request_lock(self, request_id: RequestId) -> AbstractAsyncContextManager[None]:
        """Acquire the durable row lease for ``row_for_request[request_id]`` with its
        fingerprint; else raise ``ConcurrentRunError`` (contention, row changed, or no row
        registered for ``request_id``). Not re-entrant, exactly like ``InMemoryStore``: the inner
        in-process lock is taken FIRST, so a nested acquire raises before touching the lease."""
        return self._lease(request_id)

    @asynccontextmanager
    async def _lease(self, request_id: RequestId) -> AsyncIterator[None]:
        async with self._inner.request_lock(request_id):
            registered = self._row_for_request.get(request_id)
            if registered is None:
                # A wiring bug, not contention: defer (never act without cross-process
                # exclusion), but loudly.
                log.error("leased store: no row registered for request %s; deferring", request_id)
                raise ConcurrentRunError(f"no row registered for request {request_id}")
            row_id, fingerprint = registered
            now = self._clock.now_utc()
            acquired = await self._tenant.acquire_row_lease(
                row_id,
                owner=self._owner,
                until=now + timedelta(seconds=self._lease_seconds),
                now=now,
                expected=fingerprint,
            )
            if not acquired:
                log.info("leased store: row %s leased or changed since the read; deferring", row_id)
                raise ConcurrentRunError(f"row {row_id} is leased or changed since it was read")
            try:
                yield
            finally:
                await self._release(row_id)

    async def _release(self, row_id: RowId) -> None:
        # Never raise from here: an exception in flight (or a cancellation) must not be masked by
        # a DB blip, and an unreleased lease simply expires after ``lease_seconds``.
        try:
            await self._tenant.release_row_lease(row_id, owner=self._owner)
        except Exception:
            log.warning(
                "leased store: release of row %s lease failed; it expires on its own",
                row_id,
                exc_info=True,
            )
