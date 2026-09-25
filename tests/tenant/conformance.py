"""TenantStore conformance suite (MULTIUSER_PLAN §3.7, MU-5): the executable store contract.

Every ``TenantStore`` implementation must pass this suite unchanged: ``InMemoryTenantStore`` in
CI (``tests/tenant/test_in_memory_store.py``) and, from MU-8b, ``CosmosTenantStore``
``integration``-marked against the real ``dev`` database. To run it against a store, subclass
``TenantStoreConformance`` in a ``test_*.py`` module and provide a ``harness`` fixture returning a
``StoreHarness`` whose store was built with ``COURSE_TIMEZONES``, ``CUTOFF`` and
``MAX_ACCOUNTS_PER_COURSE``.

``StoreHarness.slot_pointer`` / ``ruleday_pointer`` read the implementation's pointer docs
(``slot|<date>`` and ``ruleday|<weekday>``, §3.2) so the suite can pin that each pointer never
disagrees with the rows / rules. They are the ONLY implementation-specific hooks; everything else
goes through the Protocol.

Coverage map: the §12 MU-5 named tests, one ``test_transition_<from>_<to>`` per §3.4 table row
(refused rows included), the M4 lease rules, the M5 fingerprint check, and round-3 SF1
(one active rule per (account, weekday)).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest

from teetime.core.config import BookingCutoffConfig
from teetime.core.models import BookingOutcome, BookingResult, CourseId
from teetime.tenant.materialize import RuleConflictError
from teetime.tenant.models import (
    ACTIVE_ROW_STATUSES,
    USER_WITHDRAW_REASON,
    AccountProvenance,
    AccountStatus,
    Actor,
    BookingSource,
    BookingState,
    CourseAccount,
    CourseAccountId,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    ReservationSnapshot,
    RowFingerprint,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    SnapshotEntry,
    StandingRule,
    TransitionRefusedError,
    User,
    UserId,
    UserRole,
    UserStatus,
    derive_account_id,
    row_request_id,
    rule_row_id,
)
from teetime.tenant.store import (
    RowLeaseError,
    RowOutcome,
    TenantNotFoundError,
    TenantStore,
    UniquenessConflictError,
    VersionConflictError,
)

MB = CourseId("foreup:19671:2149")
OTHER_COURSE = CourseId("foreup:1:1")
TZ = "America/New_York"
COURSE_TIMEZONES = {MB: TZ, OTHER_COURSE: TZ}
CUTOFF = BookingCutoffConfig()  # 16:00 course-local, the day before
MAX_ACCOUNTS_PER_COURSE = 8

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)  # a Friday
TARGET = date(2026, 10, 3)  # a Saturday (weekday 5)
SAT = 5
TARGET_CUTOFF = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)  # 16:00 EDT on 10/2
FROZEN_NOW = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)  # TARGET frozen, TARGET + 7 not
BOOKER = "booker:evt-1"
BOOKER_UNTIL = NOW + timedelta(seconds=1200)
WATCHER = "watcher:run-1"
WEB = "web:req-1"


@dataclass(frozen=True)
class StoreHarness:
    store: TenantStore
    slot_pointer: Callable[[CourseAccountId, date], Awaitable[RowId | None]]
    # The ``ruleday|<weekday>`` pointer doc (§3.2): the active rule holding (account, weekday).
    ruleday_pointer: Callable[[CourseAccountId, int], Awaitable[RuleId | None]]


@dataclass(frozen=True)
class Tenant:
    user: User
    account: CourseAccount


# --- builders -----------------------------------------------------------------------------


async def _tenant(store: TenantStore, *, course: CourseId = MB, n: int = 0) -> Tenant:
    user = User(
        id=UserId(uuid4()),
        oauth_provider="github",
        oauth_subject=f"gh-{uuid4()}",
        email=f"user{n}-{uuid4().hex[:6]}@example.test",
        display_name=f"User {n}",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    await store.upsert_user(user)
    account = CourseAccount(
        id=derive_account_id(user.id, course),
        user_id=user.id,
        course_id=course,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=f"golfer-{uuid4().hex[:8]}",
        password_ciphertext="v1:k1:nonce:ct",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )
    await store.upsert_account(account)
    return Tenant(user=user, account=account)


def _rule(
    t: Tenant,
    *,
    weekday: int = SAT,
    active: bool = True,
    earliest: time = time(8, 0),
    latest: time = time(10, 0),
    party: int = 2,
    rule_id: RuleId | None = None,
) -> StandingRule:
    return StandingRule(
        id=rule_id or RuleId(uuid4()),
        course_account_id=t.account.id,
        weekday=weekday,
        window_earliest=earliest,
        window_latest=latest,
        party_size=party,
        active=active,
        materialized_through=None,
        version=1,
    )


async def _rule_row(
    store: TenantStore, t: Tenant, *, target: date = TARGET
) -> tuple[StandingRule, RequestRow]:
    rule = await store.upsert_rule(_rule(t, weekday=target.weekday()), user_id=t.user.id)
    row = await store.insert_rule_row_if_absent(rule, target, now=NOW)
    assert row is not None
    return rule, row


async def _explicit(
    store: TenantStore, t: Tenant, *, target: date = TARGET, now: datetime = NOW
) -> RequestRow:
    return await store.create_explicit_row(
        user_id=t.user.id,
        account_id=t.account.id,
        target_date=target,
        window_earliest=time(9, 0),
        window_latest=time(11, 0),
        party_size=2,
        now=now,
    )


async def _get(store: TenantStore, row: RequestRow) -> RequestRow:
    rows = await store.rows_for_account_date(row.course_account_id, row.target_date)
    matches = [r for r in rows if r.id == row.id]
    assert len(matches) == 1, f"row {row.id} not found exactly once"
    return matches[0]


def _owned(
    row: RequestRow,
    raw_id: str,
    *,
    state: BookingState = BookingState.HELD,
    source: BookingSource = BookingSource.BLIND,
    tee: time = time(9, 0),
) -> OwnedBooking:
    return OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=row.course_account_id,
        course_id=row.course_id,
        target_date=row.target_date,
        raw_reservation_id=raw_id,
        tee_time=datetime.combine(row.target_date, tee, tzinfo=UTC),
        party_size=row.party_size,
        source=source,
        state=state,
    )


def _outcome(row: RequestRow, **kw: object) -> RowOutcome:
    base: dict[str, object] = {
        "row_id": row.id,
        "course_account_id": row.course_account_id,
        "target_date": row.target_date,
        "actor": Actor.BOOKING_RUNNER,
        "to_status": None,
        "last_outcome": "no_inventory",
        "at": NOW,
    }
    base.update(kw)
    return RowOutcome(**base)  # type: ignore[arg-type]


def _result(row: RequestRow, raw_id: str) -> BookingResult:
    return BookingResult(
        request_id=row.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=row.course_id,
        slot=None,
        confirmation_code=f"TTB:{raw_id}",
        booked_at=NOW,
        attempts=1,
    )


async def _book(store: TenantStore, row: RequestRow, *, raw_id: str = "R1") -> RequestRow:
    claimed = await store.claim_rows([row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
    assert claimed == frozenset({row.id})
    await store.record_outcomes(
        [
            _outcome(
                row,
                to_status=RowStatus.BOOKED,
                last_outcome="booked",
                result=_result(row, raw_id),
                booking=_owned(row, raw_id),
                release_lease_owner=BOOKER,
            )
        ]
    )
    return await _get(store, row)


def _fp(row: RequestRow) -> RowFingerprint:
    return RowFingerprint(status=row.status, version=row.version, booked_raw_id=row.booked_raw_id)


async def _lease(store: TenantStore, row: RequestRow, *, owner: str = WATCHER) -> None:
    ok = await store.acquire_row_lease(
        row.id, owner=owner, until=NOW + timedelta(seconds=300), now=NOW, expected=None
    )
    assert ok


async def _assert_slot_consistent(h: StoreHarness, account: CourseAccountId, day: date) -> None:
    rows = await h.store.rows_for_account_date(account, day)
    active = [r for r in rows if r.status in ACTIVE_ROW_STATUSES]
    assert len(active) <= 1, f"{len(active)} active rows for {day}"
    pointer = await h.slot_pointer(account, day)
    assert pointer == (active[0].id if active else None)


class TenantStoreConformance:
    """Subclass in a ``test_*.py`` module and provide a ``harness`` fixture."""

    # --- structure --------------------------------------------------------------------

    async def test_store_satisfies_protocol(self, harness: StoreHarness) -> None:
        assert isinstance(harness.store, TenantStore)

    async def test_initialize_is_idempotent(self, harness: StoreHarness) -> None:
        await harness.store.initialize()
        await harness.store.initialize()

    # --- §3.2 uniqueness ----------------------------------------------------------------

    async def test_one_active_row_per_account_date(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        first = await _explicit(s, t)
        with pytest.raises(TransitionRefusedError):
            await _explicit(s, t)
        # A rule row for the same date is created SUPERSEDED (not active), never a 2nd active row.
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        rule_row = await s.insert_rule_row_if_absent(rule, TARGET, now=NOW)
        assert rule_row is not None
        assert rule_row.status is RowStatus.SUPERSEDED
        rows = await s.rows_for_account_date(t.account.id, TARGET)
        assert [r.id for r in rows if r.status in ACTIVE_ROW_STATUSES] == [first.id]
        assert await harness.slot_pointer(t.account.id, TARGET) == first.id
        # Other dates and other accounts are independent.
        await _explicit(s, t, target=TARGET + timedelta(days=7))
        other = await _tenant(s, n=1)
        await _explicit(s, other)

    async def test_slot_and_row_never_diverge(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        acc = t.account.id

        async def check() -> None:
            await _assert_slot_consistent(harness, acc, TARGET)

        _, rule_row = await _rule_row(s, t)
        await check()
        await s.transition_row(
            rule_row.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        await check()
        await s.transition_row(
            rule_row.id,
            user_id=t.user.id,
            to=RowStatus.PENDING,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        await check()
        explicit = await _explicit(s, t)
        await check()
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        await check()
        booked = await _book(s, await _get(s, rule_row))
        await check()
        await _lease(s, booked, owner=WEB)
        await s.record_outcomes(
            [
                _outcome(
                    booked,
                    actor=Actor.WEB,
                    to_status=RowStatus.CANCELLED,
                    status_reason="user",
                    last_outcome="cancelled",
                    release_lease_owner=WEB,
                )
            ]
        )
        await check()
        await _explicit(s, t)  # "Re-request this date"
        await check()

    async def test_rule_row_id_is_deterministic_and_insert_is_idempotent(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, row = await _rule_row(s, t)
        assert row.id == rule_row_id(rule.id, TARGET)
        assert row.request_id == row_request_id(row.id)
        assert await s.insert_rule_row_if_absent(rule, TARGET, now=NOW) is None
        assert len(await s.rows_for_account_date(t.account.id, TARGET)) == 1

    # --- §3.4 transitions: ∅ -> pending ---------------------------------------------------

    async def test_transition_none_pending(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        assert row.status is RowStatus.PENDING
        assert row.source is RowSource.EXPLICIT
        assert row.course_id == MB
        assert row.timezone == TZ
        assert row.cutoff_at == TARGET_CUTOFF
        assert row.request_id == row_request_id(row.id)
        assert row.rule_id is None
        assert row.version == 1
        assert await _get(s, row) == row
        _, rule_row = await _rule_row(s, t, target=TARGET + timedelta(days=7))
        assert rule_row.status is RowStatus.PENDING
        assert rule_row.source is RowSource.RULE

    async def test_transition_none_pending_refused_when_frozen_or_past(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        with pytest.raises(TransitionRefusedError, match="frozen"):
            await _explicit(s, t, now=FROZEN_NOW)
        with pytest.raises(TransitionRefusedError, match="frozen"):
            await _explicit(s, t, target=NOW.date() - timedelta(days=1))
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="frozen"):
            await s.insert_rule_row_if_absent(rule, TARGET, now=FROZEN_NOW)
        assert await s.rows_for_account_date(t.account.id, TARGET) == []

    async def test_create_explicit_row_scoped_to_user(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        mallory = await _tenant(s, n=1)
        with pytest.raises(TenantNotFoundError):
            await s.create_explicit_row(
                user_id=mallory.user.id,
                account_id=t.account.id,
                target_date=TARGET,
                window_earliest=time(9, 0),
                window_latest=time(11, 0),
                party_size=2,
                now=NOW,
            )

    # --- pending -> booked --------------------------------------------------------------

    async def test_transition_pending_booked(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        booked = await _book(s, row, raw_id="R1")
        assert booked.status is RowStatus.BOOKED
        assert booked.booked_raw_id == "R1"
        assert booked.booked_confirmation == "TTB:R1"
        assert booked.booked_tee_time == datetime.combine(TARGET, time(9, 0), tzinfo=UTC)
        assert booked.booked_at == NOW
        assert booked.last_outcome == "booked"
        assert booked.lease_owner is None
        assert booked.version == row.version + 1
        ledger = await s.list_owned_bookings(t.account.id, target_date=TARGET)
        assert [(b.raw_reservation_id, b.state) for b in ledger] == [("R1", BookingState.HELD)]
        assert await harness.slot_pointer(t.account.id, TARGET) == row.id

    async def test_transition_pending_booked_by_watcher(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await _lease(s, row, owner=WATCHER)
        await s.record_outcomes(
            [
                _outcome(
                    row,
                    actor=Actor.WATCHER,
                    to_status=RowStatus.BOOKED,
                    last_outcome="booked",
                    booking=_owned(row, "W1", source=BookingSource.WATCH),
                    release_lease_owner=WATCHER,
                )
            ]
        )
        assert (await _get(s, row)).status is RowStatus.BOOKED

    async def test_transition_pending_booked_refused_for_web(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await _lease(s, row, owner=WEB)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        row,
                        actor=Actor.WEB,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        release_lease_owner=WEB,
                    )
                ]
            )
        assert eg.group_contains(TransitionRefusedError)
        assert (await _get(s, row)).status is RowStatus.PENDING

    async def test_transition_pending_booked_requires_lease_holder(
        self, harness: StoreHarness
    ) -> None:
        """A writer that no longer holds the lease cannot move the row, but its ledger entry is
        still recorded against (account, date) and the active row is flagged (M4)."""
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await _lease(s, row, owner=WATCHER)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        row,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        booking=_owned(row, "R9"),
                        release_lease_owner=BOOKER,
                    )
                ]
            )
        assert eg.group_contains(RowLeaseError)
        after = await _get(s, row)
        assert after.status is RowStatus.PENDING
        assert after.needs_reconcile is True
        assert after.lease_owner == WATCHER  # the other writer's lease is untouched
        ledger = await s.list_owned_bookings(t.account.id, target_date=TARGET)
        assert [b.raw_reservation_id for b in ledger] == ["R9"]

    # --- pending <-> skipped, booked -> skipped ----------------------------------------------

    async def test_transition_pending_skipped(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        skipped = await s.transition_row(
            row.id, user_id=t.user.id, to=RowStatus.SKIPPED, actor=Actor.WEB, reason=None, now=NOW
        )
        assert skipped.status is RowStatus.SKIPPED
        assert skipped.version == row.version + 1
        assert await harness.slot_pointer(t.account.id, TARGET) == row.id  # still holds the date

    async def test_transition_pending_skipped_refused_for_non_web(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        with pytest.raises(TransitionRefusedError):
            await s.transition_row(
                row.id,
                user_id=None,
                to=RowStatus.SKIPPED,
                actor=Actor.MATERIALIZER,
                reason=None,
                now=NOW,
            )

    async def test_transition_skipped_pending(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        await s.transition_row(
            row.id, user_id=t.user.id, to=RowStatus.SKIPPED, actor=Actor.WEB, reason=None, now=NOW
        )
        with pytest.raises(TransitionRefusedError, match="frozen"):
            await s.transition_row(
                row.id,
                user_id=t.user.id,
                to=RowStatus.PENDING,
                actor=Actor.WEB,
                reason=None,
                now=FROZEN_NOW,
            )
        back = await s.transition_row(
            row.id, user_id=t.user.id, to=RowStatus.PENDING, actor=Actor.WEB, reason=None, now=NOW
        )
        assert back.status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) == row.id

    async def test_skip_booked_refused(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        booked = await _book(s, row)
        with pytest.raises(TransitionRefusedError, match="Cancel"):
            await s.transition_row(
                row.id,
                user_id=t.user.id,
                to=RowStatus.SKIPPED,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert await _get(s, row) == booked

    async def test_transition_booked_skipped_refused(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t))
        with pytest.raises(TransitionRefusedError):
            await s.transition_row(
                booked.id,
                user_id=t.user.id,
                to=RowStatus.SKIPPED,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert (await _get(s, booked)).status is RowStatus.BOOKED

    # --- pending/skipped(rule) -> superseded ---------------------------------------------

    async def test_explicit_supersedes_pending_rule_row(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        assert explicit.status is RowStatus.PENDING
        superseded = await _get(s, rule_row)
        assert superseded.status is RowStatus.SUPERSEDED
        assert superseded.version == rule_row.version + 1
        assert await harness.slot_pointer(t.account.id, TARGET) == explicit.id

    async def test_transition_pending_superseded(self, harness: StoreHarness) -> None:
        """Only ``create_explicit_row`` writes this edge (same batch as the explicit row)."""
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        with pytest.raises(TransitionRefusedError):
            await s.transition_row(
                rule_row.id,
                user_id=t.user.id,
                to=RowStatus.SUPERSEDED,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert (await _get(s, rule_row)).status is RowStatus.PENDING
        await _explicit(s, t)
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED

    async def test_transition_skipped_superseded(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        await s.transition_row(
            rule_row.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        explicit = await _explicit(s, t)
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED
        assert await harness.slot_pointer(t.account.id, TARGET) == explicit.id

    async def test_explicit_refused_over_booked_row(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        await _book(s, rule_row)
        with pytest.raises(TransitionRefusedError):
            await _explicit(s, t)
        assert len(await s.rows_for_account_date(t.account.id, TARGET)) == 1

    async def test_supersede_refused_while_leased(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        claimed = await s.claim_rows([rule_row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        assert claimed == frozenset({rule_row.id})
        with pytest.raises(RowLeaseError):
            await _explicit(s, t)
        rows = await s.rows_for_account_date(t.account.id, TARGET)
        assert [r.id for r in rows] == [rule_row.id]
        assert rows[0].status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id

    # --- superseded -> pending (withdraw explicit) ---------------------------------------

    async def test_withdraw_explicit_restores_superseded(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        withdrawn = await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert withdrawn.status is RowStatus.WITHDRAWN
        assert withdrawn.status_reason == USER_WITHDRAW_REASON
        restored = await _get(s, rule_row)
        assert restored.status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id

    async def test_withdraw_explicit_does_not_restore_inactive_rule(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED
        assert await harness.slot_pointer(t.account.id, TARGET) is None

    async def test_withdraw_explicit_does_not_restore_when_frozen(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=FROZEN_NOW,
        )
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED
        assert await harness.slot_pointer(t.account.id, TARGET) is None

    async def test_transition_superseded_pending(self, harness: StoreHarness) -> None:
        """Direct un-supersede needs the slot free: refused while the explicit row holds it."""
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        await _explicit(s, t)
        with pytest.raises(TransitionRefusedError):
            await s.transition_row(
                rule_row.id,
                user_id=t.user.id,
                to=RowStatus.PENDING,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED

    async def test_transition_superseded_pending_when_slot_free(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        # Withdraw the one-off while the rule is inactive: no auto-restore, slot freed.
        rule = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        # Guard "rule active": refused while the rule is off...
        with pytest.raises(TransitionRefusedError, match="not active"):
            await s.transition_row(
                rule_row.id,
                user_id=t.user.id,
                to=RowStatus.PENDING,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        # ...allowed once it is back on and the slot is free.
        await s.upsert_rule(replace(rule, active=True), user_id=t.user.id)
        back = await s.transition_row(
            rule_row.id,
            user_id=t.user.id,
            to=RowStatus.PENDING,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        assert back.status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id

    # --- pending -> withdrawn -----------------------------------------------------------

    async def test_transition_pending_withdrawn_explicit_by_web(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        out = await s.transition_row(
            row.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert out.status is RowStatus.WITHDRAWN
        assert await harness.slot_pointer(t.account.id, TARGET) is None

    async def test_transition_pending_withdrawn_rule_by_materializer(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        with pytest.raises(TransitionRefusedError, match="reason"):
            await s.transition_row(
                row.id,
                user_id=None,
                to=RowStatus.WITHDRAWN,
                actor=Actor.MATERIALIZER,
                reason=USER_WITHDRAW_REASON,
                now=NOW,
            )
        out = await s.transition_row(
            row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        assert out.status is RowStatus.WITHDRAWN
        assert out.status_reason == "rule_deactivated"
        assert await harness.slot_pointer(t.account.id, TARGET) is None

    async def test_web_transition_requires_user_id(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        mallory = await _tenant(s, n=1)
        row = await _explicit(s, t)
        for uid in (None, mallory.user.id):
            with pytest.raises(TenantNotFoundError):
                await s.transition_row(
                    row.id, user_id=uid, to=RowStatus.SKIPPED, actor=Actor.WEB, reason=None, now=NOW
                )
        assert (await _get(s, row)).status is RowStatus.PENDING

    # --- withdrawn(system) -> pending (materializer reactivation) --------------------------

    async def _system_withdrawn(self, s: TenantStore, t: Tenant) -> tuple[StandingRule, RequestRow]:
        rule, row = await _rule_row(s, t)
        row = await s.transition_row(
            row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_weekday_changed",
            now=NOW,
        )
        return rule, row

    async def test_transition_withdrawn_pending(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, row = await self._system_withdrawn(s, t)
        edited = await s.upsert_rule(
            replace(rule, window_earliest=time(7, 0), window_latest=time(9, 0), party_size=3),
            user_id=t.user.id,
        )
        back = await s.reactivate_rule_row(row, edited, now=NOW)
        assert back.status is RowStatus.PENDING
        assert back.status_reason is None
        assert (back.window_earliest, back.window_latest, back.party_size) == (
            time(7, 0),
            time(9, 0),
            3,
        )
        assert back.version == row.version + 1
        assert await _get(s, row) == back
        assert await harness.slot_pointer(t.account.id, TARGET) == row.id

    async def test_transition_withdrawn_pending_refused_when_frozen(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, row = await self._system_withdrawn(s, t)
        with pytest.raises(TransitionRefusedError, match="frozen"):
            await s.reactivate_rule_row(row, rule, now=FROZEN_NOW)

    async def test_transition_withdrawn_pending_refused_when_slot_held(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, row = await self._system_withdrawn(s, t)
        explicit = await _explicit(s, t)
        with pytest.raises(TransitionRefusedError):
            await s.reactivate_rule_row(row, rule, now=NOW)
        assert await harness.slot_pointer(t.account.id, TARGET) == explicit.id

    async def test_transition_withdrawn_pending_refused_on_user_terminal(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, row = await self._system_withdrawn(s, t)
        explicit = await _book(s, await _explicit(s, t))
        await _lease(s, explicit, owner=WATCHER)
        await s.record_outcomes(
            [
                _outcome(
                    explicit,
                    actor=Actor.WATCHER,
                    to_status=RowStatus.CANCELLED,
                    status_reason="external",
                    last_outcome="cancelled_external",
                    release_lease_owner=WATCHER,
                )
            ]
        )
        assert await harness.slot_pointer(t.account.id, TARGET) is None  # slot free, but...
        with pytest.raises(TransitionRefusedError, match="user-terminal"):
            await s.reactivate_rule_row(row, rule, now=NOW)
        # A brand-new rule for that weekday (the old one switched off first: one active rule
        # per weekday) is blocked too. It is passed as stored: rules are IfMatch'd (round 5).
        await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        fresh = await s.upsert_rule(_rule(t, rule_id=RuleId(uuid4())), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="user-terminal"):
            await s.insert_rule_row_if_absent(fresh, TARGET, now=NOW)

    async def test_transition_withdrawn_pending_refused_for_user_withdrawn(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        explicit = await _explicit(s, t)
        withdrawn = await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        with pytest.raises(TransitionRefusedError):
            await s.reactivate_rule_row(withdrawn, rule, now=NOW)

    async def test_reactivate_refuses_stale_row(self, harness: StoreHarness) -> None:
        """IfMatch semantics: the row the caller read must still be the stored one."""
        s = harness.store
        t = await _tenant(s)
        rule, row = await self._system_withdrawn(s, t)
        stale = replace(row, version=row.version - 1)
        with pytest.raises(TransitionRefusedError, match="changed"):
            await s.reactivate_rule_row(stale, rule, now=NOW)

    # --- booked -> booked / cancelled / pending -------------------------------------------

    async def test_transition_booked_booked(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t), raw_id="R1")
        await _lease(s, booked, owner=WATCHER)
        assert await s.set_upgrade_marker(booked.id, owner=WATCHER, at=NOW, expected=_fp(booked))
        assert (await _get(s, booked)).upgrade_started_at == NOW
        await s.record_outcomes(
            [
                _outcome(
                    booked,
                    actor=Actor.WATCHER,
                    to_status=RowStatus.BOOKED,
                    last_outcome="upgraded",
                    booking=_owned(booked, "R2", source=BookingSource.UPGRADE, tee=time(9, 30)),
                    cancelled_upgrade_raw_id="R1",
                    clear_upgrade_marker=True,
                    release_lease_owner=WATCHER,
                )
            ]
        )
        after = await _get(s, booked)
        assert after.status is RowStatus.BOOKED
        assert after.booked_raw_id == "R2"
        assert after.booked_tee_time == datetime.combine(TARGET, time(9, 30), tzinfo=UTC)
        assert after.upgrade_started_at is None
        assert after.lease_owner is None
        states = {
            b.raw_reservation_id: b.state
            for b in await s.list_owned_bookings(t.account.id, target_date=TARGET)
        }
        assert states == {"R1": BookingState.CANCELLED_UPGRADE, "R2": BookingState.HELD}

    async def test_transition_booked_booked_refused_for_runner(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t))
        await s.claim_rows([booked.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        await _lease(s, booked, owner=BOOKER)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        booked,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        release_lease_owner=BOOKER,
                    )
                ]
            )
        assert eg.group_contains(TransitionRefusedError)

    async def test_transition_booked_cancelled(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t), raw_id="R1")
        await _lease(s, booked, owner=WEB)
        await s.record_outcomes(
            [
                _outcome(
                    booked,
                    actor=Actor.WEB,
                    to_status=RowStatus.CANCELLED,
                    status_reason="user",
                    last_outcome="cancelled",
                    cancelled_upgrade_raw_id=None,
                    release_lease_owner=WEB,
                )
            ]
        )
        after = await _get(s, booked)
        assert after.status is RowStatus.CANCELLED
        assert after.status_reason == "user"
        assert after.booked_raw_id == "R1"  # kept as history
        assert after.lease_owner is None
        assert await harness.slot_pointer(t.account.id, TARGET) is None
        # The date is free again for an explicit "Re-request this date".
        again = await _explicit(s, t)
        assert again.status is RowStatus.PENDING

    async def test_transition_booked_cancelled_external_by_watcher(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t))
        await _lease(s, booked, owner=WATCHER)
        await s.record_outcomes(
            [
                _outcome(
                    booked,
                    actor=Actor.WATCHER,
                    to_status=RowStatus.CANCELLED,
                    status_reason="external",
                    last_outcome="cancelled_external",
                    release_lease_owner=WATCHER,
                )
            ]
        )
        after = await _get(s, booked)
        assert (after.status, after.status_reason) == (RowStatus.CANCELLED, "external")

    async def test_transition_booked_cancelled_requires_lease(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t))
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        booked,
                        actor=Actor.WEB,
                        to_status=RowStatus.CANCELLED,
                        status_reason="user",
                        last_outcome="cancelled",
                        release_lease_owner=WEB,
                    )
                ]
            )
        assert eg.group_contains(RowLeaseError)
        assert (await _get(s, booked)).status is RowStatus.BOOKED

    async def test_transition_booked_pending(self, harness: StoreHarness) -> None:
        """Upgrade cancelled the old slot, rebook failed (M2): pending + needs_reconcile."""
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t), raw_id="R1")
        await _lease(s, booked, owner=WATCHER)
        await s.record_outcomes(
            [
                _outcome(
                    booked,
                    actor=Actor.WATCHER,
                    to_status=RowStatus.PENDING,
                    last_outcome="upgrade_rebook_failed",
                    cancelled_upgrade_raw_id="R1",
                    needs_reconcile=True,
                    clear_upgrade_marker=True,
                    release_lease_owner=WATCHER,
                )
            ]
        )
        after = await _get(s, booked)
        assert after.status is RowStatus.PENDING
        assert after.needs_reconcile is True
        assert await harness.slot_pointer(t.account.id, TARGET) == booked.id
        states = {
            b.raw_reservation_id: b.state
            for b in await s.list_owned_bookings(t.account.id, target_date=TARGET)
        }
        assert states == {"R1": BookingState.CANCELLED_UPGRADE}

    # --- pending -> lost, booked frozen -----------------------------------------------------

    async def test_transition_pending_lost(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        later = await _explicit(s, t, target=TARGET + timedelta(days=7))
        assert await s.finalize_lost(now=NOW) == []
        lost = await s.finalize_lost(now=FROZEN_NOW)
        assert [r.id for r in lost] == [row.id]
        assert lost[0].status is RowStatus.LOST
        assert await _get(s, row) == lost[0]
        assert (await _get(s, later)).status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) is None
        assert await s.finalize_lost(now=FROZEN_NOW) == []  # the lost email goes out once

    async def test_transition_booked_frozen_stays_booked(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t))
        assert await s.finalize_lost(now=FROZEN_NOW) == []
        assert (await _get(s, booked)).status is RowStatus.BOOKED

    async def test_terminal_statuses_never_move(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        (lost,) = await s.finalize_lost(now=FROZEN_NOW)
        with pytest.raises(TransitionRefusedError, match="no transition"):
            await s.transition_row(
                lost.id,
                user_id=t.user.id,
                to=RowStatus.PENDING,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert (await _get(s, row)).status is RowStatus.LOST

    # --- leases (§3.5, M4, M5) ------------------------------------------------------------

    async def test_lease_conditional_acquire(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        until = NOW + timedelta(seconds=300)
        assert await s.acquire_row_lease(row.id, owner="A", until=until, now=NOW, expected=None)
        assert not await s.acquire_row_lease(row.id, owner="B", until=until, now=NOW, expected=None)
        assert await s.acquire_row_lease(row.id, owner="A", until=until, now=NOW, expected=None)
        await s.release_row_lease(row.id, owner="B")  # not the holder: no-op
        assert (await _get(s, row)).lease_owner == "A"
        await s.release_row_lease(row.id, owner="A")
        assert (await _get(s, row)).lease_owner is None
        assert await s.acquire_row_lease(row.id, owner="B", until=until, now=NOW, expected=None)

    async def test_lease_expiry_allows_takeover(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        assert await s.acquire_row_lease(
            row.id, owner="A", until=NOW + timedelta(seconds=60), now=NOW, expected=None
        )
        later = NOW + timedelta(seconds=61)
        assert await s.acquire_row_lease(
            row.id, owner="B", until=later + timedelta(seconds=60), now=later, expected=None
        )
        after = await _get(s, row)
        assert after.lease_owner == "B"
        assert after.lease_expires_at == later + timedelta(seconds=60)

    async def test_lease_acquire_fails_on_fingerprint_mismatch(self, harness: StoreHarness) -> None:
        """M5: the watcher read the row, then the user skipped (and unskipped) it; its acquire
        must fail. (A SKIPPED row is never leasable at all, round-5 nit 1, so the fresh acquire
        below is made after the unskip.)"""
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        seen = _fp(row)
        for to in (RowStatus.SKIPPED, RowStatus.PENDING):
            await s.transition_row(
                row.id, user_id=t.user.id, to=to, actor=Actor.WEB, reason=None, now=NOW
            )
        until = NOW + timedelta(seconds=300)
        assert not await s.acquire_row_lease(
            row.id, owner=WATCHER, until=until, now=NOW, expected=seen
        )
        assert (await _get(s, row)).lease_owner is None
        fresh = _fp(await _get(s, row))
        assert await s.acquire_row_lease(
            row.id, owner=WATCHER, until=until, now=NOW, expected=fresh
        )

    async def test_acquire_lease_rejects_changed_fingerprint(self, harness: StoreHarness) -> None:
        """Each fingerprint leg is compared: status, version, booked_raw_id (round-1 M5)."""
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t), raw_id="R1")
        fp = _fp(booked)
        until = NOW + timedelta(seconds=300)
        for wrong in (
            replace(fp, status=RowStatus.PENDING),
            replace(fp, version=fp.version + 1),
            replace(fp, booked_raw_id="R-other"),
        ):
            assert not await s.acquire_row_lease(
                booked.id, owner=WATCHER, until=until, now=NOW, expected=wrong
            )
        assert await s.acquire_row_lease(
            booked.id, owner=WATCHER, until=until, now=NOW, expected=fp
        )

    async def test_lease_writes_do_not_bump_version(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t))
        fp = _fp(booked)
        await _lease(s, booked, owner=WATCHER)
        assert await s.set_upgrade_marker(booked.id, owner=WATCHER, at=NOW, expected=fp)
        await s.release_row_lease(booked.id, owner=WATCHER)
        assert (await _get(s, booked)).version == booked.version

    async def test_set_upgrade_marker_requires_holder_and_fingerprint(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t))
        fp = _fp(booked)
        assert not await s.set_upgrade_marker(booked.id, owner=WATCHER, at=NOW, expected=fp)
        await _lease(s, booked, owner=WATCHER)
        assert not await s.set_upgrade_marker(booked.id, owner="someone", at=NOW, expected=fp)
        assert not await s.set_upgrade_marker(
            booked.id, owner=WATCHER, at=NOW, expected=replace(fp, version=99)
        )
        assert (await _get(s, booked)).upgrade_started_at is None
        assert await s.set_upgrade_marker(booked.id, owner=WATCHER, at=NOW, expected=fp)

    @pytest.mark.parametrize("action", ["skip", "withdraw", "supersede", "system_withdraw"])
    async def test_web_transitions_refused_while_leased(
        self, harness: StoreHarness, action: str
    ) -> None:
        """M4: EVERY web-initiated transition (and the materializer's withdraw) requires the row
        unleased, so a booker claim is never pulled out from under WRITE #2."""
        s = harness.store
        t = await _tenant(s)
        if action == "withdraw":
            row = await _explicit(s, t)
        else:
            _, row = await _rule_row(s, t)
        await _lease(s, row, owner=BOOKER)
        before = await _get(s, row)
        with pytest.raises(RowLeaseError):
            if action == "skip":
                await s.transition_row(
                    row.id,
                    user_id=t.user.id,
                    to=RowStatus.SKIPPED,
                    actor=Actor.WEB,
                    reason=None,
                    now=NOW,
                )
            elif action == "withdraw":
                await s.transition_row(
                    row.id,
                    user_id=t.user.id,
                    to=RowStatus.WITHDRAWN,
                    actor=Actor.WEB,
                    reason=USER_WITHDRAW_REASON,
                    now=NOW,
                )
            elif action == "supersede":
                await _explicit(s, t)
            else:
                await s.transition_row(
                    row.id,
                    user_id=None,
                    to=RowStatus.WITHDRAWN,
                    actor=Actor.MATERIALIZER,
                    reason="rule_deactivated",
                    now=NOW,
                )
        assert await _get(s, row) == before
        await _assert_slot_consistent(harness, t.account.id, TARGET)

    async def test_web_transition_allowed_after_lease_expires(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        await s.claim_rows([row.id], owner=BOOKER, until=NOW + timedelta(seconds=60), now=NOW)
        later = NOW + timedelta(seconds=61)
        out = await s.transition_row(
            row.id, user_id=t.user.id, to=RowStatus.SKIPPED, actor=Actor.WEB, reason=None, now=later
        )
        assert out.status is RowStatus.SKIPPED

    async def test_claim_rows_skips_leased_and_non_pending(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        free = await _explicit(s, t)
        leased = await _explicit(s, t, target=TARGET + timedelta(days=7))
        skipped = await _explicit(s, t, target=TARGET + timedelta(days=14))
        await _lease(s, leased, owner=WATCHER)
        await s.transition_row(
            skipped.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        claimed = await s.claim_rows(
            [free.id, leased.id, skipped.id, RowId(uuid4())],
            owner=BOOKER,
            until=BOOKER_UNTIL,
            now=NOW,
        )
        assert claimed == frozenset({free.id})
        after = await _get(s, free)
        assert (after.lease_owner, after.lease_expires_at) == (BOOKER, BOOKER_UNTIL)
        assert after.version == free.version

    # --- record_outcomes (§4.2 WRITE #2, M4) -------------------------------------------------

    async def test_record_outcomes_isolates_rows(self, harness: StoreHarness) -> None:
        """One transaction PER row: a refused row never rolls back another account's outcome."""
        s = harness.store
        a = await _tenant(s, n=1)
        b = await _tenant(s, n=2)
        row_a = await _explicit(s, a)
        row_b = await _explicit(s, b)
        await s.claim_rows([row_a.id, row_b.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        # The user withdrew nothing (they can't while leased), but the watcher took row_a over
        # after an expiry: the booker's write for row_a is refused, row_b's must still land.
        later = BOOKER_UNTIL + timedelta(seconds=1)
        await s.acquire_row_lease(
            row_a.id, owner=WATCHER, until=later + timedelta(seconds=300), now=later, expected=None
        )
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        row_a,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        booking=_owned(row_a, "A1"),
                        release_lease_owner=BOOKER,
                    ),
                    _outcome(
                        row_b,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        booking=_owned(row_b, "B1"),
                        release_lease_owner=BOOKER,
                    ),
                ]
            )
        assert len(eg.value.exceptions) == 1
        after_a = await _get(s, row_a)
        after_b = await _get(s, row_b)
        assert after_b.status is RowStatus.BOOKED
        assert after_b.booked_raw_id == "B1"
        assert after_a.status is RowStatus.PENDING
        assert after_a.needs_reconcile is True
        ledger_a = await s.list_owned_bookings(a.account.id, target_date=TARGET)
        assert [x.raw_reservation_id for x in ledger_a] == ["A1"]

    async def test_record_outcomes_refused_row_flags_active_row_for_date(
        self, harness: StoreHarness
    ) -> None:
        """The ledger + needs_reconcile follow (account, date), not the moved row (M4)."""
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        await s.claim_rows([rule_row.id], owner=BOOKER, until=NOW + timedelta(seconds=10), now=NOW)
        later = NOW + timedelta(seconds=11)
        # The claim expired; the user superseded the rule row with a one-off.
        explicit = await _explicit(s, t, now=later)
        with pytest.raises(ExceptionGroup):
            await s.record_outcomes(
                [
                    _outcome(
                        rule_row,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        booking=_owned(rule_row, "R1"),
                        release_lease_owner=BOOKER,
                        at=later,
                    )
                ]
            )
        assert (await _get(s, explicit)).needs_reconcile is True
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED
        ledger = await s.list_owned_bookings(t.account.id, target_date=TARGET)
        assert [x.raw_reservation_id for x in ledger] == ["R1"]

    async def test_record_outcomes_without_status_change(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await s.claim_rows([row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        await s.record_outcomes(
            [_outcome(row, last_outcome="no_inventory", release_lease_owner=BOOKER)]
        )
        after = await _get(s, row)
        assert after.status is RowStatus.PENDING
        assert (after.last_outcome, after.last_outcome_at) == ("no_inventory", NOW)
        assert after.lease_owner is None
        assert after.version == row.version

    async def test_record_outcomes_uncertain_sets_needs_reconcile(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await s.claim_rows([row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        await s.record_outcomes(
            [
                _outcome(
                    row, last_outcome="uncertain", needs_reconcile=True, release_lease_owner=BOOKER
                )
            ]
        )
        assert (await _get(s, row)).needs_reconcile is True

    async def test_record_outcomes_writes_extras_to_ledger(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await s.claim_rows([row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        await s.record_outcomes(
            [
                _outcome(
                    row,
                    to_status=RowStatus.BOOKED,
                    last_outcome="booked",
                    booking=_owned(row, "K1"),
                    cancelled_extras=(_owned(row, "X1", state=BookingState.CANCELLED_EXTRA),),
                    held_extras=(_owned(row, "X2", state=BookingState.HELD_EXTRA),),
                    release_lease_owner=BOOKER,
                )
            ]
        )
        states = {
            b.raw_reservation_id: b.state
            for b in await s.list_owned_bookings(t.account.id, target_date=TARGET)
        }
        assert states == {
            "K1": BookingState.HELD,
            "X1": BookingState.CANCELLED_EXTRA,
            "X2": BookingState.HELD_EXTRA,
        }

    # --- reads for the runner / watcher --------------------------------------------------------

    async def test_load_event_rows(self, harness: StoreHarness) -> None:
        s = harness.store
        a = await _tenant(s, n=1)
        b = await _tenant(s, n=2)
        c = await _tenant(s, n=3)
        pa = await _explicit(s, a)
        pb = await _explicit(s, b)
        await _explicit(s, a, target=TARGET + timedelta(days=7))  # wrong date
        await _book(s, await _explicit(s, c))  # not pending
        for _ in range(3):
            await s.record_soft_auth_failure(b.account.id)  # b -> AUTH_FAILED
        rows = await s.load_event_rows(targets={MB: TARGET}, now=NOW)
        assert [er.row.id for er in rows] == [pa.id]
        assert rows[0].account.id == a.account.id
        assert await s.load_event_rows(targets={MB: TARGET}, now=TARGET_CUTOFF) == []
        assert pb.id not in {er.row.id for er in rows}

    async def test_load_event_rows_ordered_by_row_id(self, harness: StoreHarness) -> None:
        s = harness.store
        ids = []
        for n in range(4):
            t = await _tenant(s, n=n)
            ids.append((await _explicit(s, t)).id)
        rows = await s.load_event_rows(targets={MB: TARGET}, now=NOW)
        assert [er.row.id for er in rows] == sorted(ids)

    async def test_load_watch_rows(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        pending = await _explicit(s, t)
        booked = await _book(s, await _explicit(s, t, target=TARGET + timedelta(days=7)))
        skipped = await _explicit(s, t, target=TARGET + timedelta(days=14))
        await s.transition_row(
            skipped.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        await _explicit(s, t, target=TARGET + timedelta(days=21))  # outside the horizon
        horizon = {MB: (NOW.date(), TARGET + timedelta(days=14))}
        rows = await s.load_watch_rows(horizons=horizon, now=NOW)
        assert {er.row.id for er in rows} == {pending.id, booked.id}
        # A frozen PENDING row drops out; a frozen BOOKED row stays (held bookings are final).
        frozen = await s.load_watch_rows(horizons=horizon, now=FROZEN_NOW)
        assert {er.row.id for er in frozen} == {booked.id}

    async def test_snapshot_roundtrip(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        assert await s.get_snapshot(t.account.id) is None
        snap = ReservationSnapshot(
            course_account_id=t.account.id,
            observed_at=NOW,
            source="watcher",
            trusted=True,
            entries=(SnapshotEntry(raw_id="R1", tee_time=NOW, party_size=2),),
        )
        await s.save_snapshot(snap)
        assert await s.get_snapshot(t.account.id) == snap
        newer = replace(snap, observed_at=NOW + timedelta(minutes=10), trusted=False, entries=())
        await s.save_snapshot(newer)
        assert await s.get_snapshot(t.account.id) == newer

    async def test_record_soft_auth_failure_flips_account_at_three(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        assert await s.record_soft_auth_failure(t.account.id) == 1
        assert await s.record_soft_auth_failure(t.account.id) == 2
        acc = await s.get_account(t.account.id, user_id=t.user.id)
        assert acc is not None
        assert acc.status is AccountStatus.ACTIVE
        assert await s.record_soft_auth_failure(t.account.id) == 3
        acc = await s.get_account(t.account.id, user_id=t.user.id)
        assert acc is not None
        assert acc.status is AccountStatus.AUTH_FAILED

    async def test_list_owned_bookings_scoped_by_date(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        await _book(s, await _explicit(s, t), raw_id="R1")
        await _book(s, await _explicit(s, t, target=TARGET + timedelta(days=7)), raw_id="R2")
        got = await s.list_owned_bookings(t.account.id, target_date=TARGET + timedelta(days=7))
        assert [b.raw_reservation_id for b in got] == ["R2"]

    # --- materializer support -------------------------------------------------------------

    async def test_rules_needing_materialization(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        fresh = await s.upsert_rule(_rule(t, weekday=5), user_id=t.user.id)
        done = await s.upsert_rule(_rule(t, weekday=6), user_id=t.user.id)
        await s.upsert_rule(_rule(t, weekday=0, active=False), user_id=t.user.id)
        through = NOW.date() + timedelta(days=21)
        await s.set_materialized_through(done.id, through)
        got = await s.rules_needing_materialization(through=through)
        assert [r.id for r in got] == [fresh.id]
        await s.set_materialized_through(fresh.id, through)
        assert await s.rules_needing_materialization(through=through) == []
        assert {
            r.id for r in await s.rules_needing_materialization(through=through + timedelta(1))
        } == {
            fresh.id,
            done.id,
        }

    async def test_insert_rule_row_superseded_when_slot_held(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        explicit = await _explicit(s, t)
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        row = await s.insert_rule_row_if_absent(rule, TARGET, now=NOW)
        assert row is not None
        assert row.status is RowStatus.SUPERSEDED
        assert row.rule_id == rule.id
        assert (row.window_earliest, row.window_latest, row.party_size) == (
            rule.window_earliest,
            rule.window_latest,
            rule.party_size,
        )
        assert await harness.slot_pointer(t.account.id, TARGET) == explicit.id

    async def test_insert_rule_row_rejects_wrong_weekday_or_inactive_rule(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        sunday_rule = await s.upsert_rule(_rule(t, weekday=6), user_id=t.user.id)
        with pytest.raises(ValueError, match="weekday"):
            await s.insert_rule_row_if_absent(sunday_rule, TARGET, now=NOW)
        off = await s.upsert_rule(_rule(t, active=False), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="inactive"):
            await s.insert_rule_row_if_absent(off, TARGET, now=NOW)

    async def test_rows_for_account_date_returns_full_history(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        first = await _explicit(s, t)
        await s.transition_row(
            first.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        second = await _explicit(s, t)
        rows = await s.rows_for_account_date(t.account.id, TARGET)
        assert {r.id: r.status for r in rows} == {
            first.id: RowStatus.WITHDRAWN,
            second.id: RowStatus.PENDING,
        }

    # --- rules: one active rule per (account, weekday) (round-3 SF1) ------------------------

    async def test_second_active_rule_same_weekday_refused(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        first = await s.upsert_rule(_rule(t, weekday=SAT), user_id=t.user.id)
        with pytest.raises(RuleConflictError):
            await s.upsert_rule(_rule(t, weekday=SAT), user_id=t.user.id)
        # Editing the SAME rule is fine (version bump), as are other weekdays / inactive rules.
        edited = await s.upsert_rule(replace(first, window_latest=time(11, 0)), user_id=t.user.id)
        assert edited.version == first.version + 1
        await s.upsert_rule(_rule(t, weekday=6), user_id=t.user.id)
        await s.upsert_rule(_rule(t, weekday=SAT, active=False), user_id=t.user.id)
        # Another account may have its own Saturday rule.
        other = await _tenant(s, n=1)
        await s.upsert_rule(_rule(other, weekday=SAT), user_id=other.user.id)
        # An edit that MOVES another rule onto an occupied weekday is refused too.
        sunday = await s.upsert_rule(_rule(t, weekday=0), user_id=t.user.id)
        with pytest.raises(RuleConflictError):
            await s.upsert_rule(replace(sunday, weekday=SAT), user_id=t.user.id)
        # ...and so is re-activating an inactive rule onto an occupied weekday.
        dormant = await s.upsert_rule(_rule(t, weekday=SAT, active=False), user_id=t.user.id)
        with pytest.raises(RuleConflictError):
            await s.upsert_rule(replace(dormant, active=True), user_id=t.user.id)

    async def test_upsert_rule_scoped_to_user(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        mallory = await _tenant(s, n=1)
        with pytest.raises(TenantNotFoundError):
            await s.upsert_rule(_rule(t), user_id=mallory.user.id)

    # --- users / accounts / web reads ------------------------------------------------------

    async def test_bind_invited_user(self, harness: StoreHarness) -> None:
        s = harness.store
        invited = User(
            id=UserId(uuid4()),
            oauth_provider="github",
            oauth_subject=None,
            email="Turk@Example.test",
            display_name="Turk",
            role=UserRole.MEMBER,
            status=UserStatus.INVITED,
        )
        await s.upsert_user(invited)
        assert (
            await s.bind_invited_user(email="nobody@x.test", provider="github", subject="1") is None
        )
        bound = await s.bind_invited_user(
            email="turk@example.test", provider="github", subject="42"
        )
        assert bound is not None
        assert (bound.id, bound.oauth_subject, bound.status) == (
            invited.id,
            "42",
            UserStatus.ACTIVE,
        )
        assert await s.get_user_by_subject("github", "42") == bound
        assert await s.get_user_by_subject("google", "42") is None
        # Idempotent on a repeat sign-in; the invite is not reusable by another subject.
        assert (
            await s.bind_invited_user(email="turk@example.test", provider="github", subject="42")
            == bound
        )
        assert (
            await s.bind_invited_user(email="turk@example.test", provider="github", subject="43")
            is None
        )

    async def test_upsert_user_subject_unique(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        clash = replace(t.user, id=UserId(uuid4()), email="other@example.test")
        with pytest.raises(UniquenessConflictError):
            await s.upsert_user(clash)

    async def test_get_account_scoped_by_user(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        mallory = await _tenant(s, n=1)
        assert await s.get_account(t.account.id, user_id=t.user.id) == t.account
        assert await s.get_account(t.account.id, user_id=mallory.user.id) is None

    async def test_upsert_account_requires_derived_id(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s, course=OTHER_COURSE)
        bad = replace(t.account, id=CourseAccountId(uuid4()), course_id=MB, username="fresh")
        with pytest.raises(UniquenessConflictError):
            await s.upsert_account(bad)

    async def test_upsert_account_username_unique_per_course(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        other = await _tenant(s, n=1, course=OTHER_COURSE)
        steal = replace(
            other.account,
            id=derive_account_id(other.user.id, MB),
            course_id=MB,
            username=t.account.username.upper(),
        )
        with pytest.raises(UniquenessConflictError):
            await s.upsert_account(steal)
        # Re-upserting one's own account (e.g. a password rotation) is fine.
        await s.upsert_account(replace(t.account, password_ciphertext="v1:k2:n:ct2", key_id="k2"))
        got = await s.get_account(t.account.id, user_id=t.user.id)
        assert got is not None
        assert got.key_id == "k2"

    async def test_upsert_account_enforces_per_course_cap(self, harness: StoreHarness) -> None:
        s = harness.store
        for n in range(MAX_ACCOUNTS_PER_COURSE):
            await _tenant(s, n=n)
        with pytest.raises(UniquenessConflictError, match="max_accounts_per_course"):
            await _tenant(s, n=99)
        await _tenant(s, n=100, course=OTHER_COURSE)  # the cap is per course

    async def test_list_rows_for_user_scoped(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        mallory = await _tenant(s, n=1)
        mine = await _explicit(s, t)
        later = await _explicit(s, t, target=TARGET + timedelta(days=7))
        await _explicit(s, mallory)
        got = await s.list_rows_for_user(t.user.id, from_date=TARGET, to_date=TARGET)
        assert [r.id for r in got] == [mine.id]
        got = await s.list_rows_for_user(
            t.user.id, from_date=TARGET, to_date=TARGET + timedelta(days=7)
        )
        assert [r.id for r in got] == [mine.id, later.id]

    async def test_login_probe_counts(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        other = await _tenant(s, n=1)
        for at, uid, uh in (
            (NOW - timedelta(hours=2), t.user.id, "h1"),  # outside the window
            (NOW - timedelta(minutes=5), t.user.id, "h1"),
            (NOW - timedelta(minutes=4), t.user.id, "h2"),
            (NOW - timedelta(minutes=3), other.user.id, "h1"),
        ):
            await s.record_login_probe(user_id=uid, course_id=MB, username_hash=uh, ok=False, at=at)
        since = NOW - timedelta(hours=1)
        assert await s.count_login_probes(user_id=t.user.id, username_hash=None, since=since) == 2
        assert await s.count_login_probes(user_id=None, username_hash="h1", since=since) == 2
        assert await s.count_login_probes(user_id=t.user.id, username_hash="h1", since=since) == 1
        assert await s.count_login_probes(user_id=None, username_hash=None, since=since) == 3

    async def test_append_audit_accepts_entries(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        await s.append_audit(
            user_id=t.user.id, action="row.skip", row_id=None, detail={"k": "v"}, at=NOW
        )

    # --- review round 1 (MU-5) ------------------------------------------------------------

    async def test_transition_row_refuses_reactivation(self, harness: StoreHarness) -> None:
        """withdrawn(system) -> pending is written ONLY by ``reactivate_rule_row`` (which checks
        user-terminal history, rule active and refreshes the window). Probe: rule row withdrawn,
        then a one-off booked + cancelled by the user (user-terminal) — the date must stay shut."""
        s = harness.store
        t = await _tenant(s)
        _, row = await _rule_row(s, t)
        await s.transition_row(
            row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        one_off = await _book(s, await _explicit(s, t))
        await _lease(s, one_off, owner=WEB)
        await s.record_outcomes(
            [
                _outcome(
                    one_off,
                    actor=Actor.WEB,
                    to_status=RowStatus.CANCELLED,
                    status_reason="user",
                    last_outcome="cancelled",
                    release_lease_owner=WEB,
                )
            ]
        )
        with pytest.raises(TransitionRefusedError, match="reactivate_rule_row"):
            await s.transition_row(
                row.id,
                user_id=None,
                to=RowStatus.PENDING,
                actor=Actor.MATERIALIZER,
                reason=None,
                now=NOW,
            )
        assert (await _get(s, row)).status is RowStatus.WITHDRAWN
        assert await harness.slot_pointer(t.account.id, TARGET) is None

    async def test_booked_to_pending_without_needs_reconcile_refused(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t), raw_id="R1")
        await _lease(s, booked, owner=WATCHER)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        booked,
                        actor=Actor.WATCHER,
                        to_status=RowStatus.PENDING,
                        last_outcome="upgrade_rebook_failed",
                        cancelled_upgrade_raw_id="R1",
                        needs_reconcile=False,
                        release_lease_owner=WATCHER,
                    )
                ]
            )
        assert eg.group_contains(TransitionRefusedError)
        after = await _get(s, booked)
        assert after.status is RowStatus.BOOKED
        assert after.booked_raw_id == "R1"

    async def test_rule_deactivate_withdraws_superseded_rows(self, harness: StoreHarness) -> None:
        """Round-4 D1: superseded rule rows are not immune to rule edits."""
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="reason"):
            await s.transition_row(
                rule_row.id,
                user_id=None,
                to=RowStatus.WITHDRAWN,
                actor=Actor.MATERIALIZER,
                reason=USER_WITHDRAW_REASON,
                now=NOW,
            )
        out = await s.transition_row(
            rule_row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        assert (out.status, out.status_reason) == (RowStatus.WITHDRAWN, "rule_deactivated")
        # Round-5: the pre-supersede status survives the withdraw (reactivation restores it).
        assert out.superseded_from is RowStatus.PENDING
        assert (await _get(s, explicit)).status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) == explicit.id

    async def test_deactivate_withdraw_reactivate_rematerializes_via_system_withdrawn(
        self, harness: StoreHarness
    ) -> None:
        """Round-4 D1 scenario: deactivate -> one-off withdrawn -> reactivate. The rule row is
        withdrawn(system), so the normal reactivate path (slot free) brings it back."""
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        rule = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        await s.transition_row(
            rule_row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert await harness.slot_pointer(t.account.id, TARGET) is None
        rule = await s.upsert_rule(replace(rule, active=True), user_id=t.user.id)
        back = await s.reactivate_rule_row(await _get(s, rule_row), rule, now=NOW)
        assert back.status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id

    async def test_withdraw_explicit_restores_skipped_rule_row_as_skipped(
        self, harness: StoreHarness
    ) -> None:
        """Round-4 D2: withdrawing a one-off honours the user's earlier skip."""
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        await s.transition_row(
            rule_row.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        explicit = await _explicit(s, t)
        assert (await _get(s, rule_row)).superseded_from is RowStatus.SKIPPED
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        restored = await _get(s, rule_row)
        assert restored.status is RowStatus.SKIPPED
        assert restored.superseded_from is None
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id

    async def test_supersede_records_pre_supersede_status(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        await _explicit(s, t)
        assert (await _get(s, rule_row)).superseded_from is RowStatus.PENDING

    async def test_withdraw_explicit_refused_while_superseded_row_leased(
        self, harness: StoreHarness
    ) -> None:
        """The restore writes the superseded row too, so it must be unleased (M4). Since round-5
        nit 1 a superseded row cannot be leased at all, so the precondition cannot arise."""
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        await _explicit(s, t)
        assert not await s.acquire_row_lease(
            rule_row.id, owner=WATCHER, until=NOW + timedelta(seconds=300), now=NOW, expected=None
        )

    # --- review round 1: leases, ledger atomicity, rule versions, not-found --------------------

    async def test_expired_lease_cleared_by_web_write_and_stale_outcome_refused(
        self, harness: StoreHarness
    ) -> None:
        """SF2: a booker whose claim expired cannot move the row after the web touched it."""
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await s.claim_rows([row.id], owner=BOOKER, until=NOW + timedelta(seconds=10), now=NOW)
        later = NOW + timedelta(seconds=11)
        for to in (RowStatus.SKIPPED, RowStatus.PENDING):
            await s.transition_row(
                row.id, user_id=t.user.id, to=to, actor=Actor.WEB, reason=None, now=later
            )
        after_web = await _get(s, row)
        assert (after_web.lease_owner, after_web.lease_expires_at) == (None, None)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        row,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        booking=_owned(row, "R1"),
                        release_lease_owner=BOOKER,
                        at=later,
                    )
                ]
            )
        assert eg.group_contains(RowLeaseError)
        after = await _get(s, row)
        assert after.status is RowStatus.PENDING
        assert after.needs_reconcile is True
        ledger = await s.list_owned_bookings(t.account.id, target_date=TARGET)
        assert [b.raw_reservation_id for b in ledger] == ["R1"]

    async def test_record_outcomes_status_change_requires_unexpired_lease(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await s.claim_rows([row.id], owner=BOOKER, until=NOW + timedelta(seconds=10), now=NOW)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        row,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        booking=_owned(row, "R1"),
                        release_lease_owner=BOOKER,
                        at=NOW + timedelta(seconds=11),
                    )
                ]
            )
        assert eg.group_contains(RowLeaseError)
        assert (await _get(s, row)).status is RowStatus.PENDING

    async def test_record_outcomes_invalid_ledger_entry_writes_nothing(
        self, harness: StoreHarness
    ) -> None:
        """SF3: the row and its ledger are one unit; a bad entry leaves neither written."""
        s = harness.store
        t = await _tenant(s)
        other = await _tenant(s, n=1)
        row = await _explicit(s, t)
        foreign = await _explicit(s, other)
        await s.claim_rows([row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        row,
                        to_status=RowStatus.BOOKED,
                        last_outcome="booked",
                        booking=_owned(row, "R1"),
                        held_extras=(_owned(foreign, "X1", state=BookingState.HELD_EXTRA),),
                        release_lease_owner=BOOKER,
                    )
                ]
            )
        assert eg.group_contains(ValueError)
        after = await _get(s, row)
        assert after.status is RowStatus.PENDING
        assert after.lease_owner == BOOKER
        assert await s.list_owned_bookings(t.account.id, target_date=TARGET) == []
        assert await s.list_owned_bookings(other.account.id, target_date=TARGET) == []

    async def test_upsert_rule_refuses_stale_version(self, harness: StoreHarness) -> None:
        """SF5: a rule edit made from a stale read is refused, not silently applied."""
        s = harness.store
        t = await _tenant(s)
        first = await s.upsert_rule(_rule(t), user_id=t.user.id)
        await s.upsert_rule(replace(first, window_latest=time(11, 0)), user_id=t.user.id)
        with pytest.raises(VersionConflictError):
            await s.upsert_rule(replace(first, party_size=4), user_id=t.user.id)
        (stored,) = await s.rules_needing_materialization(through=TARGET)
        assert (stored.window_latest, stored.party_size, stored.version) == (time(11, 0), 2, 2)

    async def test_upsert_rule_never_regresses_materialized_through(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        read = await s.upsert_rule(_rule(t), user_id=t.user.id)
        await s.set_materialized_through(read.id, TARGET)  # the watcher tick, after the read
        edited = await s.upsert_rule(replace(read, window_latest=time(11, 0)), user_id=t.user.id)
        assert edited.materialized_through == TARGET
        assert await s.rules_needing_materialization(through=TARGET) == []

    async def test_ruleday_pointer_tracks_the_active_rule(self, harness: StoreHarness) -> None:
        """SF6: one active rule per (account, weekday) is a pointer doc, not a scan."""
        s = harness.store
        t = await _tenant(s)
        acc = t.account.id
        rule = await s.upsert_rule(_rule(t, weekday=SAT), user_id=t.user.id)
        assert await harness.ruleday_pointer(acc, SAT) == rule.id
        await s.upsert_rule(_rule(t, weekday=0, active=False), user_id=t.user.id)
        assert await harness.ruleday_pointer(acc, 0) is None
        with pytest.raises(RuleConflictError):
            await s.upsert_rule(_rule(t, weekday=SAT), user_id=t.user.id)
        assert await harness.ruleday_pointer(acc, SAT) == rule.id
        rule = await s.upsert_rule(replace(rule, weekday=6), user_id=t.user.id)
        assert await harness.ruleday_pointer(acc, SAT) is None
        assert await harness.ruleday_pointer(acc, 6) == rule.id
        await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        assert await harness.ruleday_pointer(acc, 6) is None

    async def test_not_found_is_indistinguishable_from_not_yours(
        self, harness: StoreHarness
    ) -> None:
        """IDOR defence (§9.1): same message, and no other user's account id in it."""
        s = harness.store
        t = await _tenant(s)
        mallory = await _tenant(s, n=1)
        row = await _explicit(s, t)
        with pytest.raises(TenantNotFoundError) as foreign:
            await s.transition_row(
                row.id,
                user_id=mallory.user.id,
                to=RowStatus.SKIPPED,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        with pytest.raises(TenantNotFoundError) as missing:
            await s.transition_row(
                RowId(uuid4()),
                user_id=mallory.user.id,
                to=RowStatus.SKIPPED,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert str(foreign.value) == str(missing.value)
        assert str(t.account.id) not in str(foreign.value)
        assert str(row.id) not in str(foreign.value)

    # --- review round 2 --------------------------------------------------------------------

    async def _user_terminal_date_with_withdrawn_rule_row(
        self, s: TenantStore, t: Tenant
    ) -> RequestRow:
        _, row = await _rule_row(s, t)
        row = await s.transition_row(
            row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        one_off = await _book(s, await _explicit(s, t))
        await _lease(s, one_off, owner=WEB)
        await s.record_outcomes(
            [
                _outcome(
                    one_off,
                    actor=Actor.WEB,
                    to_status=RowStatus.CANCELLED,
                    status_reason="user",
                    last_outcome="cancelled",
                    release_lease_owner=WEB,
                )
            ]
        )
        return row

    async def test_record_outcomes_refuses_reactivation(self, harness: StoreHarness) -> None:
        """MF1 back door: withdrawn -> pending is never a leased (record_outcomes) edge."""
        s = harness.store
        t = await _tenant(s)
        row = await self._user_terminal_date_with_withdrawn_rule_row(s, t)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        row,
                        actor=Actor.MATERIALIZER,
                        to_status=RowStatus.PENDING,
                        last_outcome="reactivated",
                        release_lease_owner=WATCHER,
                    )
                ]
            )
        assert eg.group_contains(TransitionRefusedError)
        assert (await _get(s, row)).status is RowStatus.WITHDRAWN
        assert await harness.slot_pointer(t.account.id, TARGET) is None

    async def test_record_outcomes_refuses_withdraw_of_explicit_row(
        self, harness: StoreHarness
    ) -> None:
        """A withdraw via the leased path would skip the D2 restore and strand the rule row."""
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        await _lease(s, explicit, owner=WEB)
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        explicit,
                        actor=Actor.WEB,
                        to_status=RowStatus.WITHDRAWN,
                        status_reason=USER_WITHDRAW_REASON,
                        last_outcome="withdrawn",
                        release_lease_owner=WEB,
                    )
                ]
            )
        assert eg.group_contains(TransitionRefusedError)
        assert (await _get(s, explicit)).status is RowStatus.PENDING
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED

    async def test_record_outcomes_refuses_unsupersede(self, harness: StoreHarness) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        with pytest.raises(ExceptionGroup) as eg:
            await s.record_outcomes(
                [
                    _outcome(
                        rule_row,
                        actor=Actor.WEB,
                        to_status=RowStatus.PENDING,
                        last_outcome="unsuperseded",
                        release_lease_owner=WEB,
                    )
                ]
            )
        assert eg.group_contains(TransitionRefusedError)
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED

    async def test_skip_survives_supersede_deactivate_withdraw_reactivate(
        self, harness: StoreHarness
    ) -> None:
        """Round-5 decision: D1 must not undo D2. A skipped rule row that was superseded, then
        withdrawn by a deactivation, comes back SKIPPED on reactivation, never PENDING."""
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        await s.transition_row(
            rule_row.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        explicit = await _explicit(s, t)
        rule = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        withdrawn = await s.transition_row(
            rule_row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        assert withdrawn.superseded_from is RowStatus.SKIPPED
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        rule = await s.upsert_rule(replace(rule, active=True), user_id=t.user.id)
        back = await s.reactivate_rule_row(withdrawn, rule, now=NOW)
        assert back.status is RowStatus.SKIPPED
        assert back.superseded_from is None
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id
        assert await s.load_event_rows(targets={MB: TARGET}, now=NOW) == []

    async def test_load_event_rows_excludes_rows_of_inactive_rules(
        self, harness: StoreHarness
    ) -> None:
        """Belt and braces for a non-atomic deactivation (§7.7): the booker never books for an
        inactive rule, even if its pending rows were not withdrawn yet."""
        s = harness.store
        t = await _tenant(s)
        rule, row = await _rule_row(s, t)
        other = await _tenant(s, n=1)
        explicit = await _explicit(s, other)
        await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        rows = await s.load_event_rows(targets={MB: TARGET}, now=NOW)
        assert [er.row.id for er in rows] == [explicit.id]
        assert (await _get(s, row)).status is RowStatus.PENDING

    async def test_record_outcomes_ledger_must_match_row_date_and_course(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        row = await _explicit(s, t)
        await s.claim_rows([row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
        for bad in (
            replace(_owned(row, "D1"), target_date=TARGET + timedelta(days=7)),
            replace(_owned(row, "C1"), course_id=OTHER_COURSE),
        ):
            with pytest.raises(ExceptionGroup) as eg:
                await s.record_outcomes(
                    [
                        _outcome(
                            row,
                            to_status=RowStatus.BOOKED,
                            last_outcome="booked",
                            booking=bad,
                            release_lease_owner=BOOKER,
                        )
                    ]
                )
            assert eg.group_contains(ValueError)
        assert (await _get(s, row)).status is RowStatus.PENDING
        assert await s.list_owned_bookings(t.account.id, target_date=TARGET) == []

    async def test_set_materialized_through_never_moves_backwards(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        await s.set_materialized_through(rule.id, TARGET + timedelta(days=14))
        await s.set_materialized_through(rule.id, TARGET)
        assert await s.rules_needing_materialization(through=TARGET + timedelta(days=14)) == []

    # --- review round 3 --------------------------------------------------------------------

    async def _pending_row_of_deactivated_rule(
        self, s: TenantStore, t: Tenant
    ) -> tuple[StandingRule, RequestRow]:
        """A rule deactivated while its row was leased: the edit had to skip the row (§3.4)."""
        rule, row = await _rule_row(s, t)
        await _lease(s, row, owner=BOOKER)
        rule = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        await s.release_row_lease(row.id, owner=BOOKER)
        return rule, await _get(s, row)

    async def test_load_watch_rows_excludes_pending_rows_of_inactive_rules(
        self, harness: StoreHarness
    ) -> None:
        """Round-3 MF1 (i): the watcher never books for a rule the user switched off."""
        s = harness.store
        t = await _tenant(s)
        _, row = await self._pending_row_of_deactivated_rule(s, t)
        booked_rule, booked_row = await _rule_row(s, t, target=TARGET + timedelta(days=7))
        booked = await _book(s, booked_row)
        await s.upsert_rule(replace(booked_rule, active=False), user_id=t.user.id)
        horizon = {MB: (NOW.date(), TARGET + timedelta(days=14))}
        rows = await s.load_watch_rows(horizons=horizon, now=NOW)
        # A held booking of an inactive rule is still watched (vanish / cancel); the pending
        # row is not offered.
        assert {er.row.id for er in rows} == {booked.id}
        assert row.status is RowStatus.PENDING

    async def test_finalize_withdraws_pending_rows_of_inactive_rules(
        self, harness: StoreHarness
    ) -> None:
        """Round-3 MF1 (ii): no spurious "lost" email for a rule the user switched off."""
        s = harness.store
        t = await _tenant(s)
        _, row = await self._pending_row_of_deactivated_rule(s, t)
        assert await s.finalize_lost(now=FROZEN_NOW) == []
        after = await _get(s, row)
        assert (after.status, after.status_reason) == (RowStatus.WITHDRAWN, "rule_deactivated")
        assert await harness.slot_pointer(t.account.id, TARGET) is None

    async def test_rows_of_inactive_rules_lists_unleased_stragglers(
        self, harness: StoreHarness
    ) -> None:
        """Round-3 MF1 (iii): the materializer tick finds rows a deactivation had to skip."""
        s = harness.store
        t = await _tenant(s)
        _, straggler = await self._pending_row_of_deactivated_rule(s, t)
        active_rule, active_row = await _rule_row(s, t, target=TARGET + timedelta(days=1))
        leased_rule, leased_row = await _rule_row(s, t, target=TARGET + timedelta(days=2))
        await _lease(s, leased_row, owner=BOOKER)
        await s.upsert_rule(replace(leased_rule, active=False), user_id=t.user.id)
        got = await s.rows_no_longer_covered(now=NOW)
        assert [r.id for r in got] == [straggler.id]
        out = await s.transition_row(
            straggler.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        assert out.status is RowStatus.WITHDRAWN
        assert await s.rows_no_longer_covered(now=NOW) == []
        assert active_rule.active and (await _get(s, active_row)).status is RowStatus.PENDING

    async def test_withdraw_explicit_restores_system_withdrawn_row_of_active_rule(
        self, harness: StoreHarness
    ) -> None:
        """Round-6 decision: deactivate -> reactivate while the one-off holds the slot ->
        withdraw one-off. The rule row (system-withdrawn, reactivation refused by the held slot)
        is restored in the withdraw batch, with window/party refreshed from the rule."""
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        rule = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        withdrawn = await s.transition_row(
            rule_row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        rule = await s.upsert_rule(
            replace(rule, active=True, window_earliest=time(7, 0), party_size=3),
            user_id=t.user.id,
        )
        with pytest.raises(TransitionRefusedError):
            await s.reactivate_rule_row(withdrawn, rule, now=NOW)  # slot still held
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        restored = await _get(s, rule_row)
        assert restored.status is RowStatus.PENDING
        assert restored.status_reason is None
        assert (restored.window_earliest, restored.party_size) == (time(7, 0), 3)
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id

    async def test_withdraw_explicit_restores_system_withdrawn_skip_as_skipped(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        await s.transition_row(
            rule_row.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        explicit = await _explicit(s, t)
        rule = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        await s.transition_row(
            rule_row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        await s.upsert_rule(replace(rule, active=True), user_id=t.user.id)
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert (await _get(s, rule_row)).status is RowStatus.SKIPPED

    async def test_transition_row_refuses_cancel(self, harness: StoreHarness) -> None:
        """Round-3 SF1: booked -> cancelled is a LEASED edge (record_outcomes) only."""
        s = harness.store
        t = await _tenant(s)
        booked = await _book(s, await _explicit(s, t), raw_id="R1")
        with pytest.raises(TransitionRefusedError, match="record_outcomes"):
            await s.transition_row(
                booked.id,
                user_id=t.user.id,
                to=RowStatus.CANCELLED,
                actor=Actor.WEB,
                reason="user",
                now=NOW,
            )
        assert (await _get(s, booked)).status is RowStatus.BOOKED

    async def test_reset_materialized_through_puts_rule_back_on_the_tick(
        self, harness: StoreHarness
    ) -> None:
        """Round-3 SF2: the deactivation flow resets materialization FIRST, so a crash before
        the rule is written inactive leaves the tick work to do (it re-materializes)."""
        s = harness.store
        t = await _tenant(s)
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        through = NOW.date() + timedelta(days=21)
        await s.set_materialized_through(rule.id, through)
        assert await s.rules_needing_materialization(through=through) == []
        await s.reset_materialized_through(rule.id)
        assert [r.id for r in await s.rules_needing_materialization(through=through)] == [rule.id]

    # --- review round 4 --------------------------------------------------------------------

    async def _weekday_change_withdraws(
        self, s: TenantStore, t: Tenant, rule: StandingRule, row: RequestRow, weekday: int
    ) -> StandingRule:
        rule = await s.upsert_rule(replace(rule, weekday=weekday), user_id=t.user.id)
        current = await _get(s, row)
        if current.status is not RowStatus.WITHDRAWN:
            await s.transition_row(
                row.id,
                user_id=None,
                to=RowStatus.WITHDRAWN,
                actor=Actor.MATERIALIZER,
                reason="rule_weekday_changed",
                now=NOW,
            )
        return rule

    async def test_withdraw_explicit_does_not_restore_row_of_weekday_changed_rule(
        self, harness: StoreHarness
    ) -> None:
        """Round-4 MF-A: a Saturday row is never restored under a rule now on Sunday."""
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        await self._weekday_change_withdraws(s, t, rule, rule_row, weekday=6)
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert (await _get(s, rule_row)).status is RowStatus.WITHDRAWN
        assert await harness.slot_pointer(t.account.id, TARGET) is None
        assert await s.load_event_rows(targets={MB: TARGET}, now=NOW) == []

    async def test_withdraw_explicit_restores_row_when_weekday_flipped_back_while_slot_held(
        self, harness: StoreHarness
    ) -> None:
        """The guard is the rule's CURRENT weekday, not the withdraw reason: changed away and
        back while the one-off held the slot, the row restores when the one-off is withdrawn."""
        s = harness.store
        t = await _tenant(s)
        rule, rule_row = await _rule_row(s, t)
        explicit = await _explicit(s, t)
        rule = await self._weekday_change_withdraws(s, t, rule, rule_row, weekday=6)
        rule = await s.upsert_rule(replace(rule, weekday=SAT), user_id=t.user.id)
        await s.transition_row(
            explicit.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert (await _get(s, rule_row)).status is RowStatus.PENDING
        assert await harness.slot_pointer(t.account.id, TARGET) == rule_row.id

    async def test_withdraw_rerequest_after_user_cancel_keeps_date_blocked(
        self, harness: StoreHarness
    ) -> None:
        """Round-7 decision: withdrawing a re-request undoes the re-request, not the cancel; the
        superseded rule row stays SUPERSEDED (inert) on a user-terminal date."""
        s = harness.store
        t = await _tenant(s)
        _, rule_row = await _rule_row(s, t)
        e1 = await _book(s, await _explicit(s, t), raw_id="E1")
        await _lease(s, e1, owner=WEB)
        await s.record_outcomes(
            [
                _outcome(
                    e1,
                    actor=Actor.WEB,
                    to_status=RowStatus.CANCELLED,
                    status_reason="user",
                    last_outcome="cancelled",
                    release_lease_owner=WEB,
                )
            ]
        )
        e2 = await _explicit(s, t)  # "Re-request this date"
        await s.transition_row(
            e2.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED
        assert await harness.slot_pointer(t.account.id, TARGET) is None
        assert await s.load_event_rows(targets={MB: TARGET}, now=NOW) == []

    async def test_upsert_rule_honours_a_reset_watermark(self, harness: StoreHarness) -> None:
        """Round-4 SF-A: a reset wins over the caller's stale copy of the watermark."""
        s = harness.store
        t = await _tenant(s)
        read = await s.upsert_rule(_rule(t), user_id=t.user.id)
        through = NOW.date() + timedelta(days=21)
        await s.set_materialized_through(read.id, through)
        stale = replace(read, materialized_through=through)  # the web read it after the tick
        await s.reset_materialized_through(read.id)
        edited = await s.upsert_rule(replace(stale, window_latest=time(11, 0)), user_id=t.user.id)
        assert edited.materialized_through is None
        assert [r.id for r in await s.rules_needing_materialization(through=through)] == [read.id]

    async def test_reactivation_clears_watermark_in_upsert(self, harness: StoreHarness) -> None:
        """Round-5 SF-2 (coordinator decision): ``upsert_rule`` clears the watermark itself when
        a rule is re-activated, so the tick revisits withdrawn dates even if the web's
        synchronous materialize never runs (no separate reset step)."""
        s = harness.store
        t = await _tenant(s)
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        through = NOW.date() + timedelta(days=21)
        await s.set_materialized_through(rule.id, through)
        off = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        on = await s.upsert_rule(
            replace(off, active=True, materialized_through=through), user_id=t.user.id
        )
        assert on.materialized_through is None
        assert [r.id for r in await s.rules_needing_materialization(through=through)] == [rule.id]

    # --- review round 5 --------------------------------------------------------------------

    async def _user_cancelled_date_with_superseded_rule_row(
        self, s: TenantStore, t: Tenant
    ) -> RequestRow:
        _, rule_row = await _rule_row(s, t)
        e1 = await _book(s, await _explicit(s, t), raw_id="E1")
        await _lease(s, e1, owner=WEB)
        await s.record_outcomes(
            [
                _outcome(
                    e1,
                    actor=Actor.WEB,
                    to_status=RowStatus.CANCELLED,
                    status_reason="user",
                    last_outcome="cancelled",
                    release_lease_owner=WEB,
                )
            ]
        )
        return await _get(s, rule_row)

    async def test_unsupersede_refused_on_user_terminal_date(self, harness: StoreHarness) -> None:
        """Round-5 MF1: the generic web un-supersede carries the same guards as every other
        writer of an active status onto a rule row (round-7: the date stays blocked)."""
        s = harness.store
        t = await _tenant(s)
        rule_row = await self._user_cancelled_date_with_superseded_rule_row(s, t)
        assert rule_row.status is RowStatus.SUPERSEDED
        with pytest.raises(TransitionRefusedError, match="user-terminal"):
            await s.transition_row(
                rule_row.id,
                user_id=t.user.id,
                to=RowStatus.PENDING,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert (await _get(s, rule_row)).status is RowStatus.SUPERSEDED
        assert await s.load_event_rows(targets={MB: TARGET}, now=NOW) == []

    async def test_unskip_refused_after_weekday_change(self, harness: StoreHarness) -> None:
        """Round-5 SF1: unskip may not revive a row its rule no longer covers."""
        s = harness.store
        t = await _tenant(s)
        rule, row = await _rule_row(s, t)
        await s.transition_row(
            row.id, user_id=t.user.id, to=RowStatus.SKIPPED, actor=Actor.WEB, reason=None, now=NOW
        )
        await s.upsert_rule(replace(rule, weekday=6), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="no longer covers"):
            await s.transition_row(
                row.id,
                user_id=t.user.id,
                to=RowStatus.PENDING,
                actor=Actor.WEB,
                reason=None,
                now=NOW,
            )
        assert (await _get(s, row)).status is RowStatus.SKIPPED

    async def test_weekday_change_straggler_never_offered_and_swept(
        self, harness: StoreHarness
    ) -> None:
        """Round-5 MF2 (a): a leased Saturday row survives a move of its rule to Sunday (the
        withdraw had to skip it). It is never offered again and the sweep finds it."""
        s = harness.store
        t = await _tenant(s)
        rule, row = await _rule_row(s, t)
        await _lease(s, row, owner=WATCHER)
        await s.upsert_rule(replace(rule, weekday=6), user_id=t.user.id)
        with pytest.raises(RowLeaseError):
            await s.transition_row(
                row.id,
                user_id=None,
                to=RowStatus.WITHDRAWN,
                actor=Actor.MATERIALIZER,
                reason="rule_weekday_changed",
                now=NOW,
            )
        await s.release_row_lease(row.id, owner=WATCHER)
        assert await s.load_event_rows(targets={MB: TARGET}, now=NOW) == []
        horizon = {MB: (NOW.date(), TARGET + timedelta(days=14))}
        assert await s.load_watch_rows(horizons=horizon, now=NOW) == []
        assert [r.id for r in await s.rows_no_longer_covered(now=NOW)] == [row.id]

    async def test_finalize_withdraws_weekday_mismatch_as_rule_weekday_changed(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        rule, row = await _rule_row(s, t)
        await _lease(s, row, owner=WATCHER)
        await s.upsert_rule(replace(rule, weekday=6), user_id=t.user.id)
        await s.release_row_lease(row.id, owner=WATCHER)
        assert await s.finalize_lost(now=FROZEN_NOW) == []
        after = await _get(s, row)
        assert (after.status, after.status_reason) == (RowStatus.WITHDRAWN, "rule_weekday_changed")

    async def test_reactivate_refuses_stale_rule(self, harness: StoreHarness) -> None:
        """Round-5 MF2 (b): validated against the STORED rule (IfMatch on its version)."""
        s = harness.store
        t = await _tenant(s)
        saturday, row = await _rule_row(s, t)
        row = await s.transition_row(
            row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_deactivated",
            now=NOW,
        )
        await s.upsert_rule(replace(saturday, weekday=6), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="stale"):
            await s.reactivate_rule_row(row, saturday, now=NOW)
        assert (await _get(s, row)).status is RowStatus.WITHDRAWN

    async def test_reactivate_refuses_row_the_stored_rule_no_longer_covers(
        self, harness: StoreHarness
    ) -> None:
        s = harness.store
        t = await _tenant(s)
        saturday, row = await _rule_row(s, t)
        row = await s.transition_row(
            row.id,
            user_id=None,
            to=RowStatus.WITHDRAWN,
            actor=Actor.MATERIALIZER,
            reason="rule_weekday_changed",
            now=NOW,
        )
        sunday = await s.upsert_rule(replace(saturday, weekday=6), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="no longer covers"):
            await s.reactivate_rule_row(row, sunday, now=NOW)

    async def test_insert_rule_row_refuses_stale_rule(self, harness: StoreHarness) -> None:
        """Round-5 MF2 (c): a stale Saturday copy cannot create a row under a Sunday rule."""
        s = harness.store
        t = await _tenant(s)
        saturday = await s.upsert_rule(_rule(t), user_id=t.user.id)
        await s.upsert_rule(replace(saturday, weekday=6), user_id=t.user.id)
        with pytest.raises(TransitionRefusedError, match="stale"):
            await s.insert_rule_row_if_absent(saturday, TARGET, now=NOW)
        assert await s.rows_for_account_date(t.account.id, TARGET) == []

    async def test_upsert_rule_clears_watermark_on_weekday_change(
        self, harness: StoreHarness
    ) -> None:
        """Round-5 SF-2: a weekday change clears the watermark in the same write; a window edit
        and a deactivation keep it (deactivation keeps its reset -> withdraw -> reset order)."""
        s = harness.store
        t = await _tenant(s)
        rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
        through = NOW.date() + timedelta(days=21)
        await s.set_materialized_through(rule.id, through)
        rule = await s.upsert_rule(replace(rule, window_latest=time(11, 0)), user_id=t.user.id)
        assert rule.materialized_through == through
        rule = await s.upsert_rule(replace(rule, weekday=6), user_id=t.user.id)
        assert rule.materialized_through is None
        await s.set_materialized_through(rule.id, through)
        off = await s.upsert_rule(replace(rule, active=False), user_id=t.user.id)
        assert off.materialized_through == through

    async def test_acquire_lease_refused_on_non_bookable_status(
        self, harness: StoreHarness
    ) -> None:
        """Round-5 nit 1: only PENDING and BOOKED rows are ever leased."""
        s = harness.store
        t = await _tenant(s)
        until = NOW + timedelta(seconds=300)
        skipped = await _explicit(s, t)
        await s.transition_row(
            skipped.id,
            user_id=t.user.id,
            to=RowStatus.SKIPPED,
            actor=Actor.WEB,
            reason=None,
            now=NOW,
        )
        withdrawn = await _explicit(s, t, target=TARGET + timedelta(days=7))
        await s.transition_row(
            withdrawn.id,
            user_id=t.user.id,
            to=RowStatus.WITHDRAWN,
            actor=Actor.WEB,
            reason=USER_WITHDRAW_REASON,
            now=NOW,
        )
        for row in (skipped, withdrawn):
            assert not await s.acquire_row_lease(
                row.id, owner=WATCHER, until=until, now=NOW, expected=None
            )
            assert (await _get(s, row)).lease_owner is None
        booked = await _book(s, await _explicit(s, t, target=TARGET + timedelta(days=14)))
        assert await s.acquire_row_lease(
            booked.id, owner=WATCHER, until=until, now=NOW, expected=None
        )
