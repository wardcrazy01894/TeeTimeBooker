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

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from ..core.adapter import AdapterCapabilities, AdapterError, CancelError
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

    def __init__(self, *, course_id: CourseId, supports_blind_post: bool = False) -> None:
        self.course_id = course_id
        # Blind-POST knob (ctor param `supports_blind_post`): feeds `self.capabilities`
        # below. Defaults False to mirror a bare ForeUP course; orchestrator tests flip it
        # True to exercise the blind path. The gate reads `capabilities.blind_post`, so no
        # separate `self.supports_blind_post` attribute is kept (it would be write-only).
        self._blind_slots: list[TeeTimeSlot] | None = None
        self._captcha_pool_size: int = 99
        self.synthesize_blind_slots_call_count: int = 0
        self._search_response: list[TeeTimeSlot] | None = None
        self._search_exc: AdapterError | None = None
        self._book_exc: AdapterError | None = None
        self._book_side_effects: list[BookingOutcome | AdapterError] = []
        self._existing: list[ExistingReservation] = []
        self._cancel_exc: CancelError | None = None
        self._prepare_book_exc: Exception | None = None
        self._authenticate_side_effects: list[Exception | None] = []
        # AuthStateReportable knobs (RACE_PREWARM_PLAN §3.1 SF#1). `_authenticated`
        # mirrors ForeUP's `_logged_in`: a non-raising authenticate() establishes a
        # session UNLESS `_auth_soft_fail` is set, which makes authenticate() RETURN
        # without establishing one (a 400/401/rejected-body soft failure).
        self._authenticated: bool = False
        self._auth_soft_fail: bool = False
        self.authenticate_call_count: int = 0
        self.search_call_count: int = 0
        self.last_search_skip_initial_spacing: bool | None = None
        # book_call_count observed at the START of each search() — lets a test prove the
        # blind-POST fresh-search fallback fired AFTER the whole blind burst (e.g. == [2]
        # means the single search saw both blind books). RESEARCH_FALLBACK_PLAN §5.5.
        self.search_book_counts: list[int] = []
        self.prepare_book_call_count: int = 0
        # Records the `count` passed to the most recent prepare_book() call, so
        # orchestrator tests can assert the race path requests N pooled tokens.
        self.last_prepare_count: int | None = None
        self.book_call_count: int = 0
        self.list_reservations_call_count: int = 0
        self.cancel_call_count: int = 0
        # Capability record mirroring a real adapter: blind_post reflects the ctor knob.
        # (FakeAdapter reports auth state via is_authenticated but is NOT
        # ReservationCacheRefreshable — it ships no refresh_reservations — matching how the
        # orchestrator gates each capability.)
        self.capabilities = AdapterCapabilities(blind_post=supports_blind_post)

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

    def set_prepare_book_to_raise(self, exc: Exception) -> None:
        """Script prepare_book() to raise `exc` (simulates CAPTCHA service failure)."""
        self._prepare_book_exc = exc

    def set_blind_slots(self, slots: list[TeeTimeSlot]) -> None:
        """Script synthesize_blind_slots() to return `slots` (truncated to max_count)."""
        self._blind_slots = list(slots)

    def set_captcha_pool_size(self, size: int) -> None:
        """Script captcha_pool_size() so orchestrator tests can bound the blind burst."""
        self._captcha_pool_size = size

    def set_authenticate_side_effects(self, effects: list[Exception | None]) -> None:
        """Configure successive authenticate() calls to raise or return in order.

        `None` = succeed; an Exception = raise it. Used by the race pre-warm tests:
        e.g. [RuntimeError(...), None] makes the pre-T0 prewarm login fail and the
        post-T0 inline retry succeed.
        """
        self._authenticate_side_effects = list(effects)

    def set_auth_soft_fail(self, value: bool = True) -> None:
        """Script authenticate() to RETURN without establishing a session (mirrors
        ForeUP's 400/401/rejected-body soft failure, which is logged + swallowed).
        `is_authenticated` then stays False even though authenticate() did not raise.
        """
        self._auth_soft_fail = value

    # --- CourseAdapter Protocol -----------------------------------------

    async def authenticate(self, creds: CourseCredentials) -> None:
        self.authenticate_call_count += 1
        if self._authenticate_side_effects:
            effect = self._authenticate_side_effects.pop(0)
            if effect is not None:
                raise effect
        # Reached only on a non-raising return: a real session is established unless a
        # soft login failure was scripted (mirrors ForeUP's 400/401 swallow).
        self._authenticated = not self._auth_soft_fail

    # --- AuthStateReportable Protocol -----------------------------------

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    async def prepare_book(
        self,
        slot: TeeTimeSlot | None,
        request: BookingRequest,
        *,
        count: int = 1,
    ) -> None:
        self.prepare_book_call_count += 1
        self.last_prepare_count = count
        if self._prepare_book_exc is not None:
            raise self._prepare_book_exc

    # --- BlindPostCapable Protocol --------------------------------------

    def captcha_pool_size(self) -> int:
        return self._captcha_pool_size

    def synthesize_blind_slots(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        max_count: int,
    ) -> list[TeeTimeSlot]:
        self.synthesize_blind_slots_call_count += 1
        slots = (
            list(self._blind_slots)
            if self._blind_slots is not None
            else [self._default_slot(request)]
        )
        return slots[:max_count]

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        self.search_call_count += 1
        self.search_book_counts.append(self.book_call_count)
        self.last_search_skip_initial_spacing = skip_initial_spacing
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
            # No side-effects queued and no book exception → a default success.
            # (Failure modes are driven by set_book_side_effects / _book_exc.)
            outcome = BookingOutcome.BOOKED
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
        # No real resources to release; present for CourseAdapter parity.
        return None

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
