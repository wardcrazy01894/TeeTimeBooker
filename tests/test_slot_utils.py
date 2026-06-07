"""PERDAY_WINDOWS_PLAN PR B: window-list order = priority in rank_slots_for_request.

When a request carries multiple time windows (a day with disjoint windows, e.g. morning +
afternoon), the EARLIER-listed window is preferred: a slot in window[0] outranks any slot in
window[1], regardless of midpoint distance. Within a single window, the existing
closest-to-midpoint sort (tee_time tiebreaker) applies.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from uuid import uuid4

from teetime.core.models import (
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.core.slot_utils import rank_slots_for_request

CID = CourseId("fake:course")
D = datetime(2026, 6, 14)  # any date


def _slot(hour: int, minute: int = 0) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId(f"slot-{hour:02d}{minute:02d}"),
        tee_time=datetime(D.year, D.month, D.day, hour, minute, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def _request(windows: list[TimeWindow]) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(D.date(),),
        time_windows=tuple(windows),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
    )


def test_window_list_order_is_priority() -> None:
    """morning (window[0]) 09:00-10:00 + afternoon (window[1]) 17:00-19:00. A 09:50 slot is
    OFF-center of its window (distance 20 from 09:30); an 18:00 slot is DEAD-center of its
    window (distance 0). The morning slot must still win — window order = priority."""
    morning = TimeWindow(earliest=time(9, 0), latest=time(10, 0))
    afternoon = TimeWindow(earliest=time(17, 0), latest=time(19, 0))
    req = _request([morning, afternoon])  # morning listed first = preferred

    ranked = rank_slots_for_request([_slot(18, 0), _slot(9, 50)], req)

    assert ranked[0].tee_time.hour == 9  # morning wins despite worse midpoint distance
    assert ranked[1].tee_time.hour == 18


def test_within_window_closest_to_midpoint_wins() -> None:
    win = TimeWindow(earliest=time(9, 0), latest=time(10, 0))  # midpoint 09:30
    req = _request([win])
    ranked = rank_slots_for_request([_slot(9, 22), _slot(9, 37)], req)
    assert ranked[0].tee_time.minute == 37  # 7 min from midpoint beats 8 min


def test_second_window_used_when_first_has_no_slots() -> None:
    morning = TimeWindow(earliest=time(9, 0), latest=time(10, 0))
    afternoon = TimeWindow(earliest=time(17, 0), latest=time(19, 0))
    req = _request([morning, afternoon])
    ranked = rank_slots_for_request([_slot(18, 0)], req)  # only an afternoon slot exists
    assert len(ranked) == 1
    assert ranked[0].tee_time.hour == 18  # falls through to window[1]
