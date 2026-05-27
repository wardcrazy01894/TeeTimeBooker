"""Feature 3 — Prefer Earlier Times Within the Window (M-feature-3).

These are REGRESSION GUARD tests, not red-phase stubs. The ascending-time sort
is already implemented in Orchestrator._rank_slots (see orchestrator.py). These
tests verify the existing behaviour so any future change to _rank_slots breaks
loudly here rather than silently.

Coverage areas:
- Within-window earliest-wins sort (the headline behavior).
- Filter still applies (out-of-window slots are excluded).
- Tie on tee_time (same minute, different slots) — stable order.
- Single-slot case (degenerate, must still return that slot).
- Empty input returns empty list.
- Slots from multiple windows: all matching slots sorted ascending globally.
- Slots with insufficient available_spots are excluded.
- Slots exceeding max_price_per_player are excluded.

NOTE: The midpoint-distance sort from v0 was REPLACED by ascending-time sort.
See PLAN.md M-feature-3 for rationale.
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


def test_rank_slots_returns_earliest_first() -> None:
    """Multiple slots in window: earliest tee_time wins (ascending sort)."""
    orch = _make_orchestrator()
    req = _request()
    slots = [
        _slot(10, 30),  # latest in window
        _slot(9, 0),  # earliest in window
        _slot(9, 45),  # middle
    ]
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 3
    # Ascending by tee_time: 09:00 first, 09:45 second, 10:30 last.
    assert ranked[0].tee_time.hour == 9 and ranked[0].tee_time.minute == 0
    assert ranked[1].tee_time.hour == 9 and ranked[1].tee_time.minute == 45
    assert ranked[2].tee_time.hour == 10 and ranked[2].tee_time.minute == 30


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


def test_rank_slots_multiple_windows_sorted_globally() -> None:
    """With two time windows, all matching slots from both windows are sorted
    ascending by tee_time globally. Caller picks index 0 for best slot."""
    orch = _make_orchestrator()
    windows = [
        TimeWindow(earliest=time(9, 0), latest=time(9, 30)),
        TimeWindow(earliest=time(10, 0), latest=time(10, 30)),
    ]
    req = _request(windows=windows)
    slots = [
        _slot(10, 15),  # in second window
        _slot(9, 0),  # in first window — earliest overall
        _slot(9, 30),  # in first window
        _slot(10, 0),  # in second window
    ]
    ranked = orch._rank_slots(slots, req)
    assert len(ranked) == 4
    # All four should appear, globally sorted ascending by tee_time.
    assert ranked[0].tee_time.hour == 9 and ranked[0].tee_time.minute == 0
    assert ranked[1].tee_time.hour == 9 and ranked[1].tee_time.minute == 30
    assert ranked[2].tee_time.hour == 10 and ranked[2].tee_time.minute == 0
    assert ranked[3].tee_time.hour == 10 and ranked[3].tee_time.minute == 15


def test_rank_slots_tie_breaking_stable() -> None:
    """Two slots at identical tee_time: _rank_slots returns both (not just one),
    and the order is stable across repeated calls with the same input."""
    orch = _make_orchestrator()
    req = _request()
    tee_time = datetime(2026, 5, 16, 9, 0, tzinfo=UTC)
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
