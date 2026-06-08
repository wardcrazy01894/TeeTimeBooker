"""Reference test: how to test a CourseAdapter without hitting the network.

Pattern (from PLAN.md "Testing strategy"):
    - Build a fake adapter that satisfies the CourseAdapter Protocol.
    - Drive the orchestrator with FakeClock + in-memory store + NoopNotifier.
    - For the real ForeUP adapter: respx mocks (+ the test_foreup_canary.py live-drift canary).
"""

from __future__ import annotations

from datetime import date, time
from uuid import uuid4

from teetime.core.adapter import CourseAdapter
from teetime.core.models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    Player,
    RequestId,
    TeeTimeSlot,
    TimeWindow,
)


class _FakeAdapter:
    """Smallest thing that structurally satisfies CourseAdapter."""

    course_id = CourseId("fake:course")

    async def authenticate(self, creds: CourseCredentials) -> None:
        return None

    async def search(self, request: BookingRequest) -> list[TeeTimeSlot]:
        return []

    async def book(
        self,
        slot: TeeTimeSlot,
        request: BookingRequest,
    ) -> BookingResult:
        raise NotImplementedError

    async def list_reservations(self) -> list[ExistingReservation]:
        return []

    async def prepare_book(
        self,
        slot: TeeTimeSlot,
        request: BookingRequest,
    ) -> None:
        """Minimal structural stub for Protocol satisfaction."""
        return None

    async def cancel_reservation(self, confirmation_code: str) -> None:
        """Minimal structural stub for Protocol satisfaction."""
        return None

    async def aclose(self) -> None:
        return None


def test_fake_adapter_satisfies_protocol() -> None:
    """A class doesn't need to inherit from CourseAdapter — structural typing FTW."""
    fake = _FakeAdapter()
    assert isinstance(fake, CourseAdapter)


async def test_search_returns_list() -> None:
    # asyncio_mode=auto in pyproject.toml means no @pytest.mark.asyncio needed.
    # Proves the Protocol shape end-to-end (authenticate + search); real
    # search coverage lives in test_foreup_adapter.py / test_teeitup_adapter.py.
    fake = _FakeAdapter()
    creds = CourseCredentials(username="u", password="p")
    await fake.authenticate(creds)
    request = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 6, 13),),
        time_windows=(TimeWindow(earliest=time(8, 45), latest=time(10, 0)),),
        players=(Player(first_name="A", last_name="B", email="a@b.test"),),
        course_preferences=(fake.course_id,),
    )
    slots = await fake.search(request)
    assert isinstance(slots, list)
