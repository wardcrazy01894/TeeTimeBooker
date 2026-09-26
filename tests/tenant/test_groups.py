"""MU-R2 pure group rules (MULTIUSER_PLAN §16.3/§16.4): the achieved rank of a booked row, the
group floor, and the collapse plan. No I/O."""

from __future__ import annotations

from dataclasses import replace
from datetime import time
from uuid import uuid4

from teetime.core.models import CourseId
from teetime.tenant.groups import booked_rank, group_floor, plan_collapse
from teetime.tenant.models import RankedWindow, RequestRow, RowStatus

from .watcher_builders import TARGET, account, row

COURSE_B = CourseId("foreup:1:2")
GROUP = uuid4()
# The operator's example: A 9-10 (rank 1) > B 9-10 (rank 2) > A 8-9 (rank 3).
A_OPTS = (RankedWindow(1, time(9), time(10)), RankedWindow(3, time(8), time(9)))
B_OPTS = (RankedWindow(2, time(9), time(10)),)


def _a(**kw: object) -> RequestRow:
    return replace(row(account(0), options=A_OPTS, **kw), group_id=GROUP)  # type: ignore[arg-type]


def _b(**kw: object) -> RequestRow:
    acct = replace(account(1), course_id=COURSE_B)
    return replace(row(acct, options=B_OPTS, **kw), group_id=GROUP, course_id=COURSE_B)  # type: ignore[arg-type]


def test_booked_rank_is_first_match_of_the_local_tee_time() -> None:
    assert booked_rank(_a(status=RowStatus.BOOKED, booked_tee=time(9, 30))) == 1
    assert booked_rank(_a(status=RowStatus.BOOKED, booked_tee=time(8, 30))) == 3
    assert booked_rank(_a()) is None  # pending


def test_group_floor_keeps_only_options_that_beat_the_best_sibling_booking() -> None:
    a_pending = _a()
    b_booked = _b(status=RowStatus.BOOKED, booked_tee=time(9, 15))  # rank 2
    assert group_floor(a_pending, [a_pending, b_booked]) == (A_OPTS[0],)  # only rank 1 < 2
    b_pending = _b()
    a_booked_1 = _a(status=RowStatus.BOOKED, booked_tee=time(9, 30))  # rank 1
    assert group_floor(b_pending, [a_booked_1, b_pending]) == ()  # nothing beats rank 1
    assert group_floor(a_pending, [a_pending, b_pending]) == A_OPTS  # nobody booked: all


def test_group_floor_ignores_other_groups_and_dates() -> None:
    a_pending = _a()
    stranger = replace(_b(status=RowStatus.BOOKED, booked_tee=time(9, 15)), group_id=uuid4())
    assert group_floor(a_pending, [a_pending, stranger]) == A_OPTS
    other_day = replace(
        _b(status=RowStatus.BOOKED, booked_tee=time(9, 15)),
        target_date=TARGET.replace(day=TARGET.day + 1),
    )
    assert group_floor(a_pending, [a_pending, other_day]) == A_OPTS


def test_plan_collapse_keeps_best_and_cancels_owned_worse() -> None:
    a3 = _a(status=RowStatus.BOOKED, booked_tee=time(8, 30), booked_raw_id="A3")  # rank 3
    b2 = _b(status=RowStatus.BOOKED, booked_tee=time(9, 15), booked_raw_id="B2")  # rank 2
    plan = plan_collapse([a3, b2], owned=lambda r: True)
    assert plan.keep == b2
    assert plan.cancel == (a3,)
    assert plan.leave_manual == ()


def test_plan_collapse_never_cancels_a_manual_booking() -> None:
    a3 = _a(status=RowStatus.BOOKED, booked_tee=time(8, 30), booked_raw_id="A3")
    b2 = _b(status=RowStatus.BOOKED, booked_tee=time(9, 15), booked_raw_id="B2")
    # better one manual: the bot's worse one is cancelled
    plan = plan_collapse([a3, b2], owned=lambda r: r.booked_raw_id == "A3")
    assert (plan.keep, plan.cancel, plan.leave_manual) == (b2, (a3,), ())
    # worse one manual: nothing cancelled, user told
    plan = plan_collapse([a3, b2], owned=lambda r: r.booked_raw_id == "B2")
    assert (plan.keep, plan.cancel, plan.leave_manual) == (b2, (), (a3,))


def test_plan_collapse_with_one_booking_does_nothing() -> None:
    b2 = _b(status=RowStatus.BOOKED, booked_tee=time(9, 15), booked_raw_id="B2")
    plan = plan_collapse([b2, _a()], owned=lambda r: True)
    assert (plan.keep, plan.cancel, plan.leave_manual) == (b2, (), ())
    empty = plan_collapse([_a()], owned=lambda r: True)
    assert empty.keep is None
