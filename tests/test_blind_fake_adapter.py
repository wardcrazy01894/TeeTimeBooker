"""``dev.blind_fake_adapter.BlindFakeAdapter`` — the blind-capable FakeAdapter VARIANT with the
MU-3 allowlist hook (``set_blind_allowlist`` / ``blind_allowlist``) that Mangrove Bay exposes.

It exists so the recorder's blind-capable end-to-end test can drive a full race-path
``Orchestrator.run`` through every ``BlindPostCapable`` member the recorder must pass through,
without touching ``FakeAdapter``'s defaults (which stay ``blind_post=False``, no allowlist).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4

from teetime.core.adapter import CourseAdapter
from teetime.core.models import (
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.dev.blind_fake_adapter import BlindFakeAdapter
from teetime.dev.fake_adapter import FakeAdapter

CID = CourseId("fake:mb")
TARGET = date(2026, 5, 13)


def _request() -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET,),
        time_windows=(TimeWindow(earliest=time(7, 0), latest=time(9, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        dry_run=False,
    )


def _slot(hour: int, minute: int) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId(f"s-{hour:02d}{minute:02d}"),
        tee_time=datetime(2026, 5, 13, hour, minute, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def test_blind_fake_adapter_is_blind_capable_course_adapter() -> None:
    fa = BlindFakeAdapter(course_id=CID)
    assert isinstance(fa, CourseAdapter)
    assert fa.capabilities.blind_post is True
    assert fa.blind_allowlist is None


def test_fake_adapter_defaults_are_untouched() -> None:
    """The variant must not change FakeAdapter: still not blind-capable, no allowlist hook."""
    fa = FakeAdapter(course_id=CID)
    assert fa.capabilities.blind_post is False
    assert not hasattr(fa, "set_blind_allowlist")


def test_allowlist_filters_ranked_slots_before_truncation() -> None:
    """Mirrors ``MangroveBayAdapter.synthesize_blind_slots``: the allowlist is applied to the
    RANKED candidates BEFORE ``max_count`` truncation, so allocated non-top-N slots still fire
    a full burst of exactly those, in rank order. ``frozenset()`` = search-only; ``None`` =
    unfiltered."""
    fa = BlindFakeAdapter(course_id=CID)
    # Window 07:00-09:30 → midpoint 08:15; rank order is 08:15, 08:00/08:30, 07:30, 09:00.
    fa.set_blind_slots([_slot(9, 0), _slot(8, 15), _slot(7, 30), _slot(8, 0), _slot(8, 30)])
    req = _request()

    unfiltered = fa.synthesize_blind_slots(req, TARGET, max_count=2)
    assert [s.slot_id for s in unfiltered] == ["s-0815", "s-0800"]

    fa.set_blind_allowlist(frozenset({SlotId("s-0900"), SlotId("s-0730")}))
    assert fa.blind_allowlist == frozenset({SlotId("s-0900"), SlotId("s-0730")})
    allowed = fa.synthesize_blind_slots(req, TARGET, max_count=2)
    assert [s.slot_id for s in allowed] == ["s-0730", "s-0900"]  # rank order, not list order

    fa.set_blind_allowlist(frozenset())
    assert fa.synthesize_blind_slots(req, TARGET, max_count=2) == []

    fa.set_blind_allowlist(None)
    assert [s.slot_id for s in fa.synthesize_blind_slots(req, TARGET, max_count=2)] == [
        "s-0815",
        "s-0800",
    ]
