"""MULTIUSER_PLAN MU-3: ``tenant.allocation`` — cross-account blind-slot allocation (§5.4).

Pure functions, no I/O. The candidate lists come from the REAL ``MangroveBayAdapter.
synthesize_blind_slots`` (pure date arithmetic over the widened grid), and the end-to-end
assertions push each allowlist back through ``set_blind_allowlist`` — so these tests pin the
allocator AND the E2 hook AND the E3 grid working together, exactly as the runner will use them.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from itertools import pairwise
from uuid import UUID, uuid4

import pytest

from teetime.core.models import (
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.courses.foreup.mangrove_bay import BLIND_POST_MORNING_GRID, MangroveBayAdapter
from teetime.tenant.allocation import BlindAllocation, allocate_blind_slots, draft_order
from teetime.tenant.models import RowId

SAT = date(2026, 5, 16)
OPERATOR_WINDOW = TimeWindow(earliest=time(8, 45), latest=time(10, 0))  # midpoint 09:22:30
EARLY_WINDOW = TimeWindow(earliest=time(7, 0), latest=time(8, 30))  # midpoint 07:45 (disjoint)

# Fixed ids so the sorted order is deterministic: A < B < C.
ROW_A = RowId(UUID("00000000-0000-0000-0000-00000000000a"))
ROW_B = RowId(UUID("00000000-0000-0000-0000-00000000000b"))
ROW_C = RowId(UUID("00000000-0000-0000-0000-00000000000c"))

BURST = 3  # blind_post_max_count in every shipped config (untouched by MU-3)


def _request(window: TimeWindow = OPERATOR_WINDOW) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(SAT,),
        time_windows=(window,),
        players=tuple(
            Player(first_name=f"P{i}", last_name="L", email=f"p{i}@x.test") for i in range(4)
        ),
        course_preferences=(CourseId("foreup:mangrove_bay"),),
        dry_run=False,
    )


def _grid_size() -> int:
    assert BLIND_POST_MORNING_GRID is not None
    return len(BLIND_POST_MORNING_GRID)


def _candidates(adapter: MangroveBayAdapter, window: TimeWindow) -> list[TeeTimeSlot]:
    """The UNFILTERED ranked in-window list (max_count = grid size), per §5.4 'Input'."""
    return adapter.synthesize_blind_slots(_request(window), SAT, max_count=_grid_size())


def _burst(
    adapter: MangroveBayAdapter, window: TimeWindow, allowlist: frozenset[SlotId]
) -> list[str]:
    """Apply an allowlist through the E2 hook and return the HH:MM the burst would fire."""
    adapter.set_blind_allowlist(allowlist)
    try:
        return [
            s.tee_time.strftime("%H:%M")
            for s in adapter.synthesize_blind_slots(_request(window), SAT, max_count=BURST)
        ]
    finally:
        adapter.set_blind_allowlist(None)


def _date_with_rotation(n: int, k: int) -> date:
    """A SATURDAY whose week index (``toordinal() // 7``) % n == k (so the draft rotation is
    known). Stepping by whole weeks mirrors real targets: one course's drops for one account
    recur on the same weekday, so rotation must advance per WEEK, not per day."""
    d = SAT
    while (d.toordinal() // 7) % n != k:
        d += timedelta(days=7)
    return d


# --- draft_order ---------------------------------------------------------------------------


def test_draft_order_sorts_by_id_and_rotates_by_date() -> None:
    rows = [ROW_C, ROW_A, ROW_B]  # deliberately unsorted input
    assert draft_order(rows, target_date=_date_with_rotation(3, 0)) == (ROW_A, ROW_B, ROW_C)
    assert draft_order(rows, target_date=_date_with_rotation(3, 1)) == (ROW_B, ROW_C, ROW_A)
    assert draft_order(rows, target_date=_date_with_rotation(3, 2)) == (ROW_C, ROW_A, ROW_B)
    assert draft_order([], target_date=SAT) == ()


def test_allocation_rotates_first_pick_by_date() -> None:
    """§5.4: over consecutive weeks first pick rotates, so the rank-0 slot (09:22 for the
    operator window) goes to a different account each week and every account holds first pick
    equally often over a season."""
    adapter = MangroveBayAdapter()
    ranked = {
        ROW_A: _candidates(adapter, OPERATOR_WINDOW),
        ROW_B: _candidates(adapter, OPERATOR_WINDOW),
    }
    rank0 = ranked[ROW_A][0].slot_id
    assert ranked[ROW_A][0].tee_time.strftime("%H:%M") == "09:22"
    holders: list[RowId] = []
    for day in (_date_with_rotation(2, 0), _date_with_rotation(2, 1)):
        order = draft_order([ROW_A, ROW_B], target_date=day)
        alloc = allocate_blind_slots(ranked, order=order, burst_size=BURST, max_blind_rows=8)
        assert alloc.order == order
        holders.append(next(r for r, ids in alloc.allowlists.items() if rank0 in ids))
    assert holders == [ROW_A, ROW_B]


def test_allocation_rotation_cycles_for_seven_accounts() -> None:
    """MU-3 review follow-up: rotating by the RAW ordinal (``toordinal() % N``) never rotates
    when N == 7, because one account's targets for a course recur WEEKLY (same weekday), so the
    ordinal advances by exactly 7 between drops and ``% 7`` is constant — the same account would
    hold first pick every week forever. Rotating by the WEEK index cycles all seven."""
    rows = [RowId(UUID(int=i + 1)) for i in range(7)]
    saturdays = [SAT + timedelta(weeks=w) for w in range(7)]
    first_picks = [draft_order(rows, target_date=d)[0] for d in saturdays]
    assert sorted(first_picks) == sorted(rows)  # each account first exactly once in 7 weeks
    # And consecutive same-weekday drops always hand first pick to a DIFFERENT account.
    assert all(a != b for a, b in pairwise(first_picks))


# --- allocate_blind_slots -----------------------------------------------------------------


def test_allocation_disjoint() -> None:
    """§5.4 guarantee (a): three accounts wanting the SAME window get pairwise-disjoint
    allowlists, each a full burst (the 11-slot operator grid holds 9 >= 3 x 3)."""
    adapter = MangroveBayAdapter()
    ranked = {r: _candidates(adapter, OPERATOR_WINDOW) for r in (ROW_A, ROW_B, ROW_C)}
    alloc = allocate_blind_slots(
        ranked, order=(ROW_A, ROW_B, ROW_C), burst_size=BURST, max_blind_rows=8
    )
    assert isinstance(alloc, BlindAllocation)
    lists = [alloc.allowlists[r] for r in (ROW_A, ROW_B, ROW_C)]
    assert all(len(ids) == BURST for ids in lists)
    assert (
        lists[0].isdisjoint(lists[1])
        and lists[0].isdisjoint(lists[2])
        and lists[1].isdisjoint(lists[2])
    )
    assert alloc.search_only == frozenset()
    # Every allocated id is a real grid slot for that account.
    for r, ids in alloc.allowlists.items():
        assert ids <= {s.slot_id for s in ranked[r]}


def test_allocation_distinct_rank0_when_grid_ge_n() -> None:
    """§5.4 guarantee (b): each account's first pick is its best STILL-AVAILABLE slot, so two
    accounts sharing a window get DISTINCT rank-0s. Snake draft over A,B: round 0 A=09:22,
    B=09:15; round 1 (reversed) B=09:30, A=09:37; round 2 A=09:07, B=09:00. Pushed through the
    E2 hook, each burst fires in that account's rank order with its allocated rank-0 FIRST."""
    adapter = MangroveBayAdapter()
    ranked = {
        ROW_A: _candidates(adapter, OPERATOR_WINDOW),
        ROW_B: _candidates(adapter, OPERATOR_WINDOW),
    }
    alloc = allocate_blind_slots(ranked, order=(ROW_A, ROW_B), burst_size=BURST, max_blind_rows=8)
    burst_a = _burst(adapter, OPERATOR_WINDOW, alloc.allowlists[ROW_A])
    burst_b = _burst(adapter, OPERATOR_WINDOW, alloc.allowlists[ROW_B])
    assert burst_a == ["09:22", "09:37", "09:07"]
    assert burst_b == ["09:15", "09:30", "09:00"]
    assert burst_a[0] != burst_b[0]


def test_allocation_disjoint_windows_unaffected() -> None:
    """§5.4 guarantee (c): the operator (08:45-10:00) and an early golfer (07:00-08:30) do not
    overlap, so each receives EXACTLY its own unallocated top-3 — today's burst, untouched."""
    adapter = MangroveBayAdapter()
    ranked = {
        ROW_A: _candidates(adapter, OPERATOR_WINDOW),
        ROW_B: _candidates(adapter, EARLY_WINDOW),
    }
    alloc = allocate_blind_slots(ranked, order=(ROW_B, ROW_A), burst_size=BURST, max_blind_rows=8)
    assert alloc.allowlists[ROW_A] == frozenset(s.slot_id for s in ranked[ROW_A][:BURST])
    assert alloc.allowlists[ROW_B] == frozenset(s.slot_id for s in ranked[ROW_B][:BURST])
    # And through the hook: byte-for-byte the unallocated burst.
    assert _burst(adapter, OPERATOR_WINDOW, alloc.allowlists[ROW_A]) == ["09:22", "09:15", "09:30"]
    assert _burst(adapter, EARLY_WINDOW, alloc.allowlists[ROW_B]) == ["07:45", "07:52", "07:37"]


def test_allocation_over_cap_rows_are_search_only() -> None:
    """§5.3: rows ranked beyond max_blind_rows get an EMPTY allowlist (search race path) and
    are reported in search_only; they take nothing from the draft."""
    adapter = MangroveBayAdapter()
    ranked = {r: _candidates(adapter, OPERATOR_WINDOW) for r in (ROW_A, ROW_B, ROW_C)}
    alloc = allocate_blind_slots(
        ranked, order=(ROW_C, ROW_A, ROW_B), burst_size=BURST, max_blind_rows=2
    )
    assert alloc.allowlists[ROW_B] == frozenset()
    assert alloc.search_only == frozenset({ROW_B})
    assert len(alloc.allowlists[ROW_C]) == BURST
    assert len(alloc.allowlists[ROW_A]) == BURST
    assert _burst(adapter, OPERATOR_WINDOW, alloc.allowlists[ROW_B]) == []


def test_allocation_exhausted_grid_gives_fewer_or_zero() -> None:
    """§5.4 (b), the 'if not' clause: a window holding only 2 grid slots (09:00-09:07) across
    3 accounts leaves the later accounts with fewer or zero — the zero one is search-only."""
    adapter = MangroveBayAdapter()
    narrow = TimeWindow(earliest=time(9, 0), latest=time(9, 7))
    ranked = {r: _candidates(adapter, narrow) for r in (ROW_A, ROW_B, ROW_C)}
    assert len(ranked[ROW_A]) == 2
    alloc = allocate_blind_slots(
        ranked, order=(ROW_A, ROW_B, ROW_C), burst_size=BURST, max_blind_rows=8
    )
    assert len(alloc.allowlists[ROW_A]) == 1
    assert len(alloc.allowlists[ROW_B]) == 1
    assert alloc.allowlists[ROW_C] == frozenset()
    assert alloc.search_only == frozenset({ROW_C})
    assert alloc.allowlists[ROW_A].isdisjoint(alloc.allowlists[ROW_B])


def test_allocation_rejects_inconsistent_inputs() -> None:
    adapter = MangroveBayAdapter()
    ranked = {ROW_A: _candidates(adapter, OPERATOR_WINDOW)}
    with pytest.raises(ValueError, match="order"):
        allocate_blind_slots(ranked, order=(ROW_A, ROW_B), burst_size=BURST, max_blind_rows=8)
    with pytest.raises(ValueError, match="order"):
        allocate_blind_slots(ranked, order=(ROW_A, ROW_A), burst_size=BURST, max_blind_rows=8)
    with pytest.raises(ValueError, match="burst_size"):
        allocate_blind_slots(ranked, order=(ROW_A,), burst_size=-1, max_blind_rows=8)
    with pytest.raises(ValueError, match="max_blind_rows"):
        allocate_blind_slots(ranked, order=(ROW_A,), burst_size=BURST, max_blind_rows=-1)
