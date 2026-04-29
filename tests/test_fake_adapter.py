"""Tests for the scriptable FakeAdapter (teetime.dev.fake_adapter).

Drives orchestrator tests in M2.T1 AND the CLI's --use-fake-adapter mode for
local dev runs without ForeUP. This file verifies the scripting surface.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core.adapter import (
    CourseAdapter,
    InventoryNotPublishedError,
    SlotGoneError,
)
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.dev.fake_adapter import FakeAdapter


def _request() -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 5, 13),),
        time_windows=(TimeWindow(earliest=time(7, 0), latest=time(9, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CourseId("fake:course"),),
    )


def _slot() -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=CourseId("fake:course"),
        slot_id=SlotId("slot-1"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def test_fake_adapter_satisfies_protocol() -> None:
    fa = FakeAdapter(course_id=CourseId("fake:course"))
    assert isinstance(fa, CourseAdapter)


async def test_default_search_returns_canned_slot_and_book_succeeds() -> None:
    """Out of the box, FakeAdapter is a happy-path booking — that's what the
    CLI's --use-fake-adapter mode demos. Tests can override either side."""
    fa = FakeAdapter(course_id=CourseId("fake:course"))
    await fa.authenticate(CourseCredentials(username="u", password="p"))
    slots = await fa.search(_request())
    assert len(slots) == 1
    res = await fa.book(slots[0], _request())
    assert res.outcome == BookingOutcome.BOOKED
    assert res.confirmation_code is not None


async def test_set_search_response_overrides_default() -> None:
    fa = FakeAdapter(course_id=CourseId("fake:course"))
    fa.set_search_response([_slot(), _slot()])
    assert len(await fa.search(_request())) == 2


async def test_set_search_to_raise_inventory_not_published() -> None:
    fa = FakeAdapter(course_id=CourseId("fake:course"))
    fa.set_search_to_raise(InventoryNotPublishedError("not yet"))
    with pytest.raises(InventoryNotPublishedError):
        await fa.search(_request())


async def test_set_book_to_raise_slot_gone() -> None:
    fa = FakeAdapter(course_id=CourseId("fake:course"))
    fa.set_book_to_raise(SlotGoneError("gone"))
    with pytest.raises(SlotGoneError):
        await fa.book(_slot(), _request())


async def test_recorded_book_calls() -> None:
    fa = FakeAdapter(course_id=CourseId("fake:course"))
    assert fa.book_call_count == 0
    await fa.book(_slot(), _request())
    assert fa.book_call_count == 1


async def test_set_existing_reservations_drives_list_reservations() -> None:
    """Layer 2 / 4 of §9 needs FakeAdapter to script list_reservations output."""
    fa = FakeAdapter(course_id=CourseId("fake:course"))
    res = ExistingReservation(
        course_id=CourseId("fake:course"),
        confirmation_code="X-1",
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
        party_size=1,
    )
    fa.set_existing_reservations([res])
    got = await fa.list_reservations()
    assert got == [res]
