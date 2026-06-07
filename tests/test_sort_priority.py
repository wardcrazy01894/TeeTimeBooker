"""Feature 3 — Prefer Slot Closest to Window Midpoint (M-feature-3).

Slots are ranked by their distance from the midpoint of the matching time
window, ascending. The slot whose tee_time is closest to the midpoint of the
window wins. For equidistant slots, ascending tee_time is the tiebreaker.

Example: window 09:00-10:00, midpoint 09:30.
  09:37 → distance 7 min  (WINNER over 09:22)
  09:22 → distance 8 min

The original ascending-time sort (earliest-first) was replaced with this
midpoint-distance sort. See PLAN.md M-feature-3 for rationale.

Coverage areas:
- Midpoint-wins sort (headline behaviour).
- Equidistant tie-breaking: ascending tee_time is the secondary key.
- Filter still applies (out-of-window slots are excluded).
- Single-slot case (degenerate, must still return that slot).
- Empty input returns empty list.
- Slots from multiple windows: each slot's distance measured against its own
  window's midpoint; globally sorted by that distance.
- Slots with insufficient available_spots are excluded.
- Slots exceeding max_price_per_player are excluded.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from teetime.core.config import SchedulerConfig
from teetime.core.models import (
    BookingRequest,
    CartPreference,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.core.orchestrator import Orchestrator

# --- Helpers ----------------------------------------------------------------


def _slot(hour: int, minute: int = 0, course_id: str = "fake:c") -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=CourseId(course_id),
        slot_id=SlotId(f"s-{hour:02d}{minute:02d}"),
        tee_time=datetime(2026, 5, 16, hour, minute, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def _request(
    windows: list[TimeWindow] | None = None,
    max_price: Decimal | None = None,
    n_players: int = 1,
) -> BookingRequest:
    players = tuple(
        Player(first_name=f"P{i}", last_name="L", email=f"p{i}@x.test") for i in range(n_players)
    )
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 5, 16),),
        time_windows=tuple(
            windows
            if windows is not None
            else [TimeWindow(earliest=time(9, 0), latest=time(10, 30))]
        ),
        players=players,
        course_preferences=(CourseId("fake:c"),),
        holes=18,
        max_price_per_player=max_price,
        cart=CartPreference.EITHER,
    )


def _make_orchestrator() -> Orchestrator:
    """Return an Orchestrator instance with minimal collaborators for unit testing
    _rank_slots. The adapters/store/notifier/clock/scheduler are never called by
    _rank_slots, so we pass the minimum required to construct the instance."""
    return Orchestrator(
        adapters={},
        store=MagicMock(),
        notifier=MagicMock(),
        clock=MagicMock(),
        scheduler=SchedulerConfig(),
    )


# --- Tests ------------------------------------------------------------------


def test_rank_slots_prefers_slot_closest_to_midpoint() -> None:
    """Slot closest to the window midpoint wins.

    Window 09:00-10:30 → midpoint 09:45.
      09:45 → distance 0   (WINNER)
      09:00 → distance 45 min (tied with 10:30)
      10:30 → distance 45 min (tied with 09:00; tiebreak: ascending tee_time → 09:00 before 10:30)
    """
    orch = _make_orchestrator()
    # Default window is 09:00-10:30, midpoint 09:45.
    req = _request()
    slots = [
        _slot(10, 30),  # distance 45 min
        _slot(9, 0),  # distance 45 min (ties 10:30; tiebreak → comes first)
        _slot(9, 45),  # distance  0 min → WINNER
    ]
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 3
    # Closest to midpoint first.
    assert ranked[0].tee_time.hour == 9 and ranked[0].tee_time.minute == 45
    # Tie at 45 min: ascending tee_time tiebreaker → 09:00 before 10:30.
    assert ranked[1].tee_time.hour == 9 and ranked[1].tee_time.minute == 0
    assert ranked[2].tee_time.hour == 10 and ranked[2].tee_time.minute == 30


def test_rank_slots_midpoint_example_from_spec() -> None:
    """The spec example: window 09:00-10:00 (midpoint 09:30).
    09:37 (distance 7) should rank before 09:22 (distance 8)."""
    orch = _make_orchestrator()
    req = _request(windows=[TimeWindow(earliest=time(9, 0), latest=time(10, 0))])
    slots = [_slot(9, 22), _slot(9, 37)]  # presented in reversed order
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 2
    # 09:37 is 7 min from midpoint (09:30); 09:22 is 8 min → 09:37 wins.
    assert ranked[0].tee_time.minute == 37
    assert ranked[1].tee_time.minute == 22


def test_rank_slots_equidistant_tiebreak_ascending_time() -> None:
    """Two slots equidistant from midpoint: ascending tee_time wins."""
    orch = _make_orchestrator()
    # Window 09:00-10:00, midpoint 09:30.
    req = _request(windows=[TimeWindow(earliest=time(9, 0), latest=time(10, 0))])
    # 09:15 and 09:45 are both 15 min from midpoint (09:30).
    slots = [_slot(9, 45), _slot(9, 15)]
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 2
    # Tiebreak: ascending tee_time → 09:15 before 09:45.
    assert ranked[0].tee_time.minute == 15
    assert ranked[1].tee_time.minute == 45


def test_rank_slots_single_slot_returned() -> None:
    """Degenerate case: one slot in window returns that slot."""
    orch = _make_orchestrator()
    req = _request()
    slots = [_slot(9, 15)]
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 1
    assert ranked[0].tee_time.hour == 9 and ranked[0].tee_time.minute == 15


def test_rank_slots_excludes_slots_outside_window() -> None:
    """Slots whose tee_time falls outside all time_windows are excluded."""
    orch = _make_orchestrator()
    # Window is 09:00-10:30
    req = _request()
    slots = [
        _slot(8, 59),  # just before window — excluded
        _slot(9, 0),  # exactly at earliest — included
        _slot(10, 30),  # exactly at latest — included
        _slot(10, 31),  # just after window — excluded
    ]
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 2
    times = [(s.tee_time.hour, s.tee_time.minute) for s in ranked]
    assert (9, 0) in times
    assert (10, 30) in times


def test_rank_slots_empty_input_returns_empty() -> None:
    """Empty slot list in -> empty list out."""
    orch = _make_orchestrator()
    req = _request()
    assert orch._rank_slots([], req) == []


def test_rank_slots_respects_available_spots() -> None:
    """Slots where available_spots < len(players) are excluded."""
    orch = _make_orchestrator()
    req = _request(n_players=4)
    # available_spots=3 < 4 players — excluded
    tight = TeeTimeSlot(
        course_id=CourseId("fake:c"),
        slot_id=SlotId("tight"),
        tee_time=datetime(2026, 5, 16, 9, 0, tzinfo=UTC),
        holes=18,
        available_spots=3,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )
    # available_spots=4 == 4 players — included
    ok = TeeTimeSlot(
        course_id=CourseId("fake:c"),
        slot_id=SlotId("ok"),
        tee_time=datetime(2026, 5, 16, 9, 30, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )
    ranked = orch._rank_slots([tight, ok], req)
    assert len(ranked) == 1
    assert ranked[0].slot_id == SlotId("ok")


def test_rank_slots_respects_max_price() -> None:
    """Slots where price_per_player > max_price_per_player are excluded."""
    orch = _make_orchestrator()
    req = _request(max_price=Decimal("50.00"))
    cheap = TeeTimeSlot(
        course_id=CourseId("fake:c"),
        slot_id=SlotId("cheap"),
        tee_time=datetime(2026, 5, 16, 9, 0, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),  # under cap — included
        cart_included=True,
    )
    expensive = TeeTimeSlot(
        course_id=CourseId("fake:c"),
        slot_id=SlotId("expensive"),
        tee_time=datetime(2026, 5, 16, 9, 30, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("55.00"),  # over cap — excluded
        cart_included=True,
    )
    ranked = orch._rank_slots([cheap, expensive], req)
    assert len(ranked) == 1
    assert ranked[0].slot_id == SlotId("cheap")


def test_rank_slots_multiple_windows_sorted_by_window_priority_then_midpoint() -> None:
    """With two time windows, WINDOW LIST ORDER is priority (PERDAY_WINDOWS_PLAN Q1): ALL
    window-0 slots outrank ALL window-1 slots, regardless of midpoint distance. Within a
    window, closest-to-midpoint wins (ascending tee_time tiebreaker).

    Window 0: 09:00-09:30 → midpoint 09:15
    Window 1: 10:00-10:30 → midpoint 10:15

    Expected order: 09:00, 09:30 (window 0, both dist 15, ascending tee_time), then
    10:15 (window 1, dist 0), then 10:00 (window 1, dist 15).
    """
    orch = _make_orchestrator()
    windows = [
        TimeWindow(earliest=time(9, 0), latest=time(9, 30)),
        TimeWindow(earliest=time(10, 0), latest=time(10, 30)),
    ]
    req = _request(windows=windows)
    slots = [
        _slot(10, 15),  # window 1, distance 0 — but window 1 is lower priority
        _slot(9, 0),  # window 0, distance 15
        _slot(9, 30),  # window 0, distance 15
        _slot(10, 0),  # window 1, distance 15
    ]
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 4
    # Window 0 first (priority), ascending tee_time within the same distance.
    assert ranked[0].tee_time.hour == 9 and ranked[0].tee_time.minute == 0
    assert ranked[1].tee_time.hour == 9 and ranked[1].tee_time.minute == 30
    # Then window 1, closest-to-midpoint first.
    assert ranked[2].tee_time.hour == 10 and ranked[2].tee_time.minute == 15
    assert ranked[3].tee_time.hour == 10 and ranked[3].tee_time.minute == 0


def test_rank_slots_tie_breaking_stable() -> None:
    """Two slots at identical tee_time (distance 0 from midpoint): _rank_slots
    returns both, and the order is stable across repeated calls."""
    orch = _make_orchestrator()
    # Window 09:00-09:30, midpoint 09:15.
    req = _request(windows=[TimeWindow(earliest=time(9, 0), latest=time(9, 30))])
    tee_time = datetime(2026, 5, 16, 9, 15, tzinfo=UTC)  # exactly at midpoint
    s1 = TeeTimeSlot(
        course_id=CourseId("fake:c"),
        slot_id=SlotId("alpha"),
        tee_time=tee_time,
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )
    s2 = TeeTimeSlot(
        course_id=CourseId("fake:c"),
        slot_id=SlotId("beta"),
        tee_time=tee_time,
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )
    ranked_1 = orch._rank_slots([s1, s2], req)
    ranked_2 = orch._rank_slots([s1, s2], req)
    # Both slots present.
    assert len(ranked_1) == 2
    # Order is stable across two identical calls.
    assert [s.slot_id for s in ranked_1] == [s.slot_id for s in ranked_2]
