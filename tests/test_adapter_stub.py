"""Reference test: how to test a CourseAdapter without hitting the network.

Pattern (from PLAN.md "Testing strategy"):
    - Build a fake adapter that satisfies the CourseAdapter Protocol.
    - Drive the orchestrator with FakeClock + in-memory store + NoopNotifier.
    - For real ForeUP adapter: vcrpy/respx cassettes recorded in Spike S1.
"""

from __future__ import annotations

from teetime.core.adapter import CourseAdapter
from teetime.core.models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
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

    async def aclose(self) -> None:
        return None


def test_fake_adapter_satisfies_protocol() -> None:
    """A class doesn't need to inherit from CourseAdapter — structural typing FTW."""
    fake = _FakeAdapter()
    assert isinstance(fake, CourseAdapter)


async def test_search_returns_list() -> None:
    # asyncio_mode=auto in pyproject.toml means no @pytest.mark.asyncio needed.
    fake = _FakeAdapter()
    creds = CourseCredentials(username="u", password="p")
    await fake.authenticate(creds)
    # We don't construct a real BookingRequest yet; that's M2.T2's job. This
    # test only proves the Protocol shape; real coverage lives alongside the
    # real implementation.
