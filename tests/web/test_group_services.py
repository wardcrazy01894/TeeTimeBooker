"""MU-R3 (MULTIUSER_PLAN §16.2): saving one ranked list as a GROUP — one explicit row (or one
rule) per course, sharing a ``group_id``, each carrying ITS course's options, price override and
best rank. Service level, against a real ``InMemoryTenantStore`` + ``FakeClock``.

Clock: T0 = Sat 2026-09-26 12:00 UTC (08:00 EDT); Sat 10/3 is bookable, today is frozen.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core.clock import FakeClock
from teetime.core.release_policy import ReleasePolicy
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    CourseAccount,
    RankedWindow,
    RowStatus,
    User,
    UserId,
    UserRole,
    UserStatus,
    derive_account_id,
)
from teetime.web import group_services as services
from teetime.web.booking_form import RankedChoice
from teetime.web.services import ActionRefusedError, InvalidInputError, WebNotFoundError

from ..tenant.conformance import CUTOFF, MB, OTHER_COURSE, TZ
from .conftest import T0, new_store

POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
POLICIES = {str(MB): POLICY, str(OTHER_COURSE): POLICY}
OCT3 = date(2026, 10, 3)
SAT = 5


async def _member(store: InMemoryTenantStore) -> tuple[UserId, CourseAccount, CourseAccount]:
    user = User(
        id=UserId(uuid4()),
        oauth_provider="google",
        oauth_subject=f"g-{uuid4().hex}",
        email=f"{uuid4().hex[:8]}@example.test",
        display_name="Turk",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    await store.upsert_user(user)
    accounts = []
    for course in (MB, OTHER_COURSE):
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
        accounts.append(account)
    return user.id, accounts[0], accounts[1]


def _choice(a: CourseAccount, b: CourseAccount) -> RankedChoice:
    """A 9-10 (1) > B 9-10 (2) > A 8-9 (3); a price override on B only."""
    return RankedChoice(
        party_size=4,
        per_account={
            a.id: (RankedWindow(1, time(9), time(10)), RankedWindow(3, time(8), time(9))),
            b.id: (RankedWindow(2, time(9), time(10)),),
        },
        prices={b.id: Decimal("85.00")},
    )


@pytest.fixture
def store() -> InMemoryTenantStore:
    return new_store()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


async def test_one_off_group_writes_one_row_per_course_sharing_a_group(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    user_id, a, b = await _member(store)
    report = await services.create_group_one_off(
        store, user_id=user_id, target_date=OCT3, choice=_choice(a, b), clock=clock
    )
    assert report.failures == ()
    rows = {r.course_account_id: r for r in report.rows}
    assert set(rows) == {a.id, b.id}
    assert rows[a.id].group_id is not None
    assert rows[a.id].group_id == rows[b.id].group_id
    assert rows[a.id].options == _choice(a, b).per_account[a.id]
    assert (rows[a.id].group_rank, rows[b.id].group_rank) == (1, 2)
    assert (rows[a.id].max_price, rows[b.id].max_price) == (None, Decimal("85.00"))
    assert all(r.party_size == 4 and r.target_date == OCT3 for r in report.rows)
    assert all(r.status is RowStatus.PENDING for r in report.rows)
    # Written in rank order: a partial write keeps the user's best options (§16.2).
    assert [r.course_account_id for r in report.rows] == [a.id, b.id]


async def test_one_off_group_partial_write_reports_the_failed_course(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    """B already has an explicit row on the date: A is saved, B is reported, nothing raised."""
    user_id, a, b = await _member(store)
    await store.create_explicit_row(
        user_id=user_id,
        account_id=b.id,
        target_date=OCT3,
        options=(RankedWindow(1, time(7), time(8)),),
        party_size=2,
        now=clock.now_utc(),
    )
    report = await services.create_group_one_off(
        store, user_id=user_id, target_date=OCT3, choice=_choice(a, b), clock=clock
    )
    assert [r.course_account_id for r in report.rows] == [a.id]
    assert [f.account_id for f in report.failures] == [b.id]
    assert report.failures[0].message


async def test_one_off_group_with_nothing_saved_is_refused(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    user_id, a, b = await _member(store)
    frozen = date(2026, 9, 26)  # today: past the day-before cutoff
    with pytest.raises(ActionRefusedError):
        await services.create_group_one_off(
            store, user_id=user_id, target_date=frozen, choice=_choice(a, b), clock=clock
        )


async def test_rule_group_writes_one_rule_per_course_and_materializes_them(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    user_id, a, b = await _member(store)
    report = await services.create_group_rule(
        store,
        user_id=user_id,
        weekday=SAT,
        choice=_choice(a, b),
        policies=POLICIES,
        cutoff=CUTOFF,
        clock=clock,
    )
    assert report.failures == ()
    rules = {r.course_account_id: r for r in report.rules}
    assert rules[a.id].group_id is not None
    assert rules[a.id].group_id == rules[b.id].group_id
    assert (rules[a.id].group_rank, rules[b.id].group_rank) == (1, 2)
    assert rules[b.id].max_price == Decimal("85.00")
    assert all(r.weekday == SAT and r.party_size == 4 and r.active for r in report.rules)
    rows = await store.rows_for_account_date(a.id, OCT3)
    assert [r.group_id for r in rows] == [rules[a.id].group_id]


async def test_rule_group_second_rule_on_the_weekday_is_reported_not_raised(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    user_id, a, b = await _member(store)
    only_b = RankedChoice(
        party_size=2, per_account={b.id: (RankedWindow(1, time(7), time(8)),)}, prices={}
    )
    await services.create_group_rule(
        store,
        user_id=user_id,
        weekday=SAT,
        choice=only_b,
        policies=POLICIES,
        cutoff=CUTOFF,
        clock=clock,
    )
    report = await services.create_group_rule(
        store,
        user_id=user_id,
        weekday=SAT,
        choice=_choice(a, b),
        policies=POLICIES,
        cutoff=CUTOFF,
        clock=clock,
    )
    assert [r.course_account_id for r in report.rules] == [a.id]
    assert [f.account_id for f in report.failures] == [b.id]
    assert "Saturday" in report.failures[0].message


async def test_set_default_price_updates_only_the_users_own_account(
    store: InMemoryTenantStore,
) -> None:
    user_id, a, _ = await _member(store)
    updated = await services.set_default_price(
        store, user_id=user_id, account_id=a.id, raw_price="72.5"
    )
    assert updated.default_max_price == Decimal("72.50")
    stored = await store.get_account(a.id, user_id=user_id)
    assert stored is not None
    assert stored.default_max_price == Decimal("72.50")
    assert stored.password_ciphertext == a.password_ciphertext


@pytest.mark.parametrize("raw", ["", "abc", "-1", "5000"])
async def test_set_default_price_refuses_a_bad_value(store: InMemoryTenantStore, raw: str) -> None:
    user_id, a, _ = await _member(store)
    with pytest.raises(InvalidInputError):
        await services.set_default_price(store, user_id=user_id, account_id=a.id, raw_price=raw)


async def test_set_default_price_on_a_foreign_account_is_not_found(
    store: InMemoryTenantStore,
) -> None:
    user_id, _, _ = await _member(store)
    _, stranger, _ = await _member(store)
    with pytest.raises(WebNotFoundError):
        await services.set_default_price(
            store, user_id=user_id, account_id=stranger.id, raw_price="50"
        )
