"""Scriptable in-process CourseAdapter.

Two roles:
- Orchestrator unit tests drive specific paths (slot-gone, captcha, uncertain)
  by setting the response/exception before each call.
- The CLI's `--use-fake-adapter` flag wires this so a user can demonstrate
  the full booking flow on their laptop with no ForeUP credentials, no HTTP,
  and no risk of making a real reservation.

Default behavior is a happy-path booking (one slot, BOOKED) so the CLI demo
"just works" without configuration.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from ..core.adapter import AdapterError, CancelError
from ..core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    SlotId,
    TeeTimeSlot,
)


class FakeAdapter:
    """Structurally satisfies CourseAdapter; scripted via setter methods."""

    course_id: CourseId

    def __init__(self, *, course_id: CourseId) -> None:
        self.course_id = course_id
        self._search_response: list[TeeTimeSlot] | None = None
        self._search_exc: AdapterError | None = None
        self._book_outcome: BookingOutcome = BookingOutcome.BOOKED
        self._book_exc: AdapterError | None = None
        self._book_side_effects: list[BookingOutcome | AdapterError] = []
        self._existing: list[ExistingReservation] = []
        self._cancel_exc: CancelError | None = None
        self._cancel_should_succeed: bool = True
        self._prepare_book_exc: Exception | None = None
        self.authenticate_call_count: int = 0
        self.search_call_count: int = 0
        self.prepare_book_call_count: int = 0
        self.book_call_count: int = 0
        self.list_reservations_call_count: int = 0
        self.cancel_call_count: int = 0
        self.aclose_call_count: int = 0

    # --- scripting surface ----------------------------------------------

    def set_search_response(self, slots: list[TeeTimeSlot]) -> None:
        self._search_response = slots
        self._search_exc = None

    def set_search_to_raise(self, exc: AdapterError) -> None:
        self._search_exc = exc

    def set_book_to_raise(self, exc: AdapterError) -> None:
        self._book_exc = exc

    def set_book_side_effects(self, effects: list[BookingOutcome | AdapterError]) -> None:
        """Configure successive book() calls to yield outcomes or raise exceptions in order."""
        self._book_side_effects = list(effects)

    def set_existing_reservations(self, reservations: list[ExistingReservation]) -> None:
        self._existing = list(reservations)

    def set_cancel_to_raise(self, exc: CancelError) -> None:
        """Script cancel_reservation() to raise `exc` (simulates server refusal)."""
        self._cancel_exc = exc
        self._cancel_should_succeed = False

    def set_prepare_book_to_raise(self, exc: Exception) -> None:
        """Script prepare_book() to raise `exc` (simulates CAPTCHA service failure)."""
        self._prepare_book_exc = exc

    # --- CourseAdapter Protocol -----------------------------------------

    async def authenticate(self, creds: CourseCredentials) -> None:
        self.authenticate_call_count += 1

    async def prepare_book(self, slot: TeeTimeSlot | None, request: BookingRequest) -> None:
        self.prepare_book_call_count += 1
        if self._prepare_book_exc is not None:
            raise self._prepare_book_exc

    async def search(self, request: BookingRequest) -> list[TeeTimeSlot]:
        self.search_call_count += 1
        if self._search_exc is not None:
            raise self._search_exc
        if self._search_response is not None:
            return list(self._search_response)
        return [self._default_slot(request)]

    async def book(
        self,
        slot: TeeTimeSlot,
        request: BookingRequest,
    ) -> BookingResult:
        self.book_call_count += 1
        side_effects = self._book_side_effects
        if side_effects:
            effect = side_effects.pop(0)
            if isinstance(effect, AdapterError):
                raise effect
            outcome = effect
        elif self._book_exc is not None:
            raise self._book_exc
        else:
            outcome = self._book_outcome
        conf_code = (
            f"{MANAGED_BOOKING_TAG}FAKE-{slot.slot_id}"
            if outcome == BookingOutcome.BOOKED
            else None
        )
        result = BookingResult(
            request_id=request.request_id,
            outcome=outcome,
            course_id=self.course_id,
            slot=slot,
            confirmation_code=conf_code,
            booked_at=datetime.now(tz=UTC) if outcome == BookingOutcome.BOOKED else None,
            attempts=1,
        )
        # SF-2: reflect a successful book in _existing so list_reservations()
        # returns the new reservation in subsequent calls. This makes upgrade
        # tests accurate: after book() succeeds, list_reservations sees it.
        if outcome == BookingOutcome.BOOKED and conf_code is not None:
            self._existing.append(
                ExistingReservation(
                    course_id=self.course_id,
                    # Store without the TTB: prefix in ExistingReservation, mirroring
                    # real ForeUP behaviour (server does not echo the TTB: prefix back).
                    confirmation_code=f"FAKE-{slot.slot_id}",
                    tee_time=slot.tee_time,
                    party_size=len(request.players),
                )
            )
        return result

    async def list_reservations(self) -> list[ExistingReservation]:
        self.list_reservations_call_count += 1
        return list(self._existing)

    async def cancel_reservation(self, confirmation_code: str) -> None:
        """Simulate cancellation. If set_cancel_to_raise() was called, raises CancelError.
        Otherwise removes the matching reservation from _existing (idempotent on 404-style
        not-found: if no matching reservation exists, returns normally).

        Mirrors ForeUpAdapter.cancel_reservation(): strips the TTB: prefix from
        `confirmation_code` before matching, so callers may pass either the raw id
        or the TTB:-prefixed store value (as maybe_upgrade() will do).
        """
        self.cancel_call_count += 1
        if self._cancel_exc is not None:
            raise self._cancel_exc
        # Strip the TTB: prefix if present — _existing stores raw ids (no prefix),
        # mirroring real ForeUP behaviour. This matches what ForeUpAdapter does.
        raw_id = confirmation_code.removeprefix(MANAGED_BOOKING_TAG)
        # Remove matching reservation — simulates successful server-side cancel.
        self._existing = [r for r in self._existing if r.confirmation_code != raw_id]

    async def aclose(self) -> None:
        self.aclose_call_count += 1

    # --- helpers --------------------------------------------------------

    def _default_slot(self, request: BookingRequest) -> TeeTimeSlot:
        target_date = request.target_dates[0]
        window = request.time_windows[0]
        midpoint = _window_midpoint(window.earliest, window.latest)
        tee_time = datetime.combine(target_date, midpoint, tzinfo=UTC)
        return TeeTimeSlot(
            course_id=self.course_id,
            slot_id=SlotId(f"fake-slot-{target_date.isoformat()}"),
            tee_time=tee_time,
            holes=request.holes,
            available_spots=len(request.players),
            price_per_player=Decimal("45.00"),
            cart_included=True,
        )


def _window_midpoint(earliest: time, latest: time) -> time:
    e = timedelta(hours=earliest.hour, minutes=earliest.minute)
    last = timedelta(hours=latest.hour, minutes=latest.minute)
    mid = (e + last) / 2
    total_min = int(mid.total_seconds() // 60)
    return time(hour=total_min // 60, minute=total_min % 60)
