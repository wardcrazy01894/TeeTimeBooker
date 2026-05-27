"""Feature 2 — One Booking Policy / Upgrade Orchestrator (M-feature-2).

Covers the behavioral contracts of UpgradeOrchestrator.maybe_upgrade().

Coverage areas:
  "OUR" VS "MANUAL" BOOKING DETECTION
  PRIORITY (higher / equal / no-higher)
  BOOK-BEFORE-CANCEL ATOMICITY
  CANCEL-AFTER-REBOOK FAILURE
  IDEMPOTENCY KEY HANDLING (delete_terminal + record_terminal sequence)
  TIE-BREAKING (earliest tee_time within same priority window)
  NOTIFICATION
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from teetime.core.adapter import CancelError, SlotGoneError
from teetime.core.clock import FakeClock
from teetime.core.config import OneBookingPolicyConfig, PrioritySlotConfig, SchedulerConfig
from teetime.core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseId,
    ExistingReservation,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.core.upgrade_orchestrator import UpgradeOrchestrator
from teetime.dev.fake_adapter import FakeAdapter
from teetime.persistence.in_memory_store import InMemoryStore

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")

# Two courses: A is "good" (priority 1), B is "better" (priority 0).
COURSE_A = CourseId("fake:course_a")  # lower priority — current booking lives here
COURSE_B = CourseId("fake:course_b")  # higher priority — upgrade target

TARGET_DATE = date(2026, 6, 7)  # Saturday

# Time windows:
#   WINDOW_BETTER  = 09:00-09:30  (priority 0, COURSE_B only)
#   WINDOW_CURRENT = 09:30-10:30  (priority 1, COURSE_A only)
WINDOW_BETTER = TimeWindow(earliest=time(9, 0), latest=time(9, 30))
WINDOW_CURRENT = TimeWindow(earliest=time(9, 30), latest=time(10, 30))

PLAYERS = (Player(first_name="A", last_name="L", email="a@l.test"),) * 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CapturingNotifier:
    """Records every notify() call for assertion."""

    def __init__(self) -> None:
        self.calls: list[BookingResult] = []

    async def notify(self, result: BookingResult) -> None:
        self.calls.append(result)


def _make_request(*, course_prefs: tuple[CourseId, ...] = (COURSE_B, COURSE_A)) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=PLAYERS,
        course_preferences=course_prefs,
    )


def _make_slot(
    *,
    course_id: CourseId,
    tee_time: datetime,
    slot_id: str = "slot-1",
) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=course_id,
        slot_id=SlotId(slot_id),
        tee_time=tee_time,
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
    )


def _managed_booking(
    *,
    request: BookingRequest,
    course_id: CourseId = COURSE_A,
    tee_time: datetime | None = None,
    slot_id: str = "old-slot-1",
) -> BookingResult:
    if tee_time is None:
        tee_time = datetime(TARGET_DATE.year, TARGET_DATE.month, TARGET_DATE.day, 9, 45, tzinfo=ET)
    slot = _make_slot(course_id=course_id, tee_time=tee_time, slot_id=slot_id)
    return BookingResult(
        request_id=request.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=course_id,
        slot=slot,
        confirmation_code=f"{MANAGED_BOOKING_TAG}FAKE-{slot_id}",
        booked_at=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
        attempts=1,
    )


def _make_orchestrator(
    *,
    adapter_a: FakeAdapter | None = None,
    adapter_b: FakeAdapter | None = None,
    store: InMemoryStore | None = None,
    notifier: CapturingNotifier | None = None,
    policy: OneBookingPolicyConfig | None = None,
) -> tuple[UpgradeOrchestrator, FakeAdapter, FakeAdapter, InMemoryStore, CapturingNotifier]:
    fa = adapter_a or FakeAdapter(course_id=COURSE_A)
    fb = adapter_b or FakeAdapter(course_id=COURSE_B)
    st = store or InMemoryStore()
    nt = notifier or CapturingNotifier()
    clock = FakeClock(start=datetime(2026, 6, 7, 13, 0, 0, tzinfo=UTC))
    scheduler = SchedulerConfig()

    if policy is None:
        policy = OneBookingPolicyConfig(
            enabled=True,
            priority_slots=[
                PrioritySlotConfig(
                    priority=0,
                    course_id=str(COURSE_B),
                    time_window_earliest=time(9, 0),
                    time_window_latest=time(9, 30),
                ),
                PrioritySlotConfig(
                    priority=1,
                    course_id=str(COURSE_A),
                    time_window_earliest=time(9, 30),
                    time_window_latest=time(10, 30),
                ),
            ],
        )

    orc = UpgradeOrchestrator(
        adapters={COURSE_A: fa, COURSE_B: fb},
        store=st,
        notifier=nt,
        clock=clock,
        scheduler=scheduler,
        policy=policy,
    )
    return orc, fa, fb, st, nt


# ---------------------------------------------------------------------------
# "Our" vs "manual" booking detection
# ---------------------------------------------------------------------------


async def test_upgrade_skips_unmanaged_booking() -> None:
    """If current_booking (a BookingResult from the store) has a confirmation_code
    WITHOUT the TTB: prefix, maybe_upgrade() returns None immediately —
    unmanaged bookings are never touched."""
    orc, fa, fb, _st, _nt = _make_orchestrator()
    request = _make_request()

    unmanaged = BookingResult(
        request_id=request.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=COURSE_A,
        slot=_make_slot(
            course_id=COURSE_A,
            tee_time=datetime(2026, 6, 7, 9, 45, tzinfo=ET),
        ),
        confirmation_code="MANUAL-NO-TTB-PREFIX",  # no TTB: prefix
        booked_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        attempts=1,
    )

    result = await orc.maybe_upgrade(request, TARGET_DATE, unmanaged)

    assert result is None
    assert fb.book_call_count == 0  # never tried to book anything
    assert fa.cancel_call_count == 0  # never tried to cancel


async def test_upgrade_proceeds_for_managed_booking() -> None:
    """If current_booking has a TTB: prefix, maybe_upgrade() proceeds to check
    for higher-priority slots. When a better slot is available it returns a BOOKED result."""
    orc, fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)

    # Seed the store and the existing reservation on adapter_a
    await st.record_terminal(current, TARGET_DATE)
    fa.set_existing_reservations(
        [
            ExistingReservation(
                course_id=COURSE_A,
                confirmation_code="FAKE-old-slot-1",
                tee_time=datetime(2026, 6, 7, 9, 45, tzinfo=ET),
                party_size=4,
            )
        ]
    )

    # Set up a better slot on adapter_b
    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == COURSE_B


# ---------------------------------------------------------------------------
# Priority logic
# ---------------------------------------------------------------------------


async def test_upgrade_returns_none_when_no_higher_priority_slot_available() -> None:
    """If no higher-priority slots are available (search returns empty for all
    higher-priority windows), maybe_upgrade() returns None."""
    orc, fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    # No slots available in the better window
    fb.set_search_response([])

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    assert result is None
    assert fa.cancel_call_count == 0


async def test_upgrade_books_and_cancels_when_higher_priority_available() -> None:
    """Happy path: higher-priority slot found → book new → cancel old →
    delete_terminal → record_terminal with new result."""
    orc, fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)
    fa.set_existing_reservations(
        [
            ExistingReservation(
                course_id=COURSE_A,
                confirmation_code="FAKE-old-slot-1",
                tee_time=datetime(2026, 6, 7, 9, 45, tzinfo=ET),
                party_size=4,
            )
        ]
    )

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    # Booked the better slot
    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == COURSE_B

    # Old booking cancelled
    assert fa.cancel_call_count == 1

    # Store updated: old terminal gone, new one in place
    stored = await st.get_terminal(request.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.course_id == COURSE_B


async def test_upgrade_does_not_upgrade_to_equal_priority() -> None:
    """A slot at the SAME priority as the current booking does not trigger
    a cancel+rebook — only strictly higher priority (lower index) does."""
    # Policy with only one priority level: both courses share priority 0
    same_priority_policy = OneBookingPolicyConfig(
        enabled=True,
        priority_slots=[
            PrioritySlotConfig(
                priority=0,
                course_id=str(COURSE_A),
                time_window_earliest=time(9, 0),
                time_window_latest=time(10, 30),
            ),
        ],
    )
    orc, fa, _fb, st, _nt = _make_orchestrator(policy=same_priority_policy)
    request = _make_request(course_prefs=(COURSE_A,))
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    # Even though adapter_a has a different slot available, it's at the same priority
    alt_slot = _make_slot(
        course_id=COURSE_A,
        tee_time=datetime(2026, 6, 7, 9, 5, tzinfo=ET),
        slot_id="alt-slot",
    )
    fa.set_search_response([alt_slot])

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    assert result is None
    assert fa.cancel_call_count == 0


# ---------------------------------------------------------------------------
# Book-before-cancel atomicity
# ---------------------------------------------------------------------------


async def test_upgrade_books_before_cancelling() -> None:
    """Verify call order: adapter.book() is called BEFORE adapter.cancel_reservation().
    Verified by checking call counts at the point where cancel would be reached."""
    orc, fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])

    await orc.maybe_upgrade(request, TARGET_DATE, current)

    # Both were called; book happened first (implicit from the protocol)
    assert fb.book_call_count == 1
    assert fa.cancel_call_count == 1


async def test_upgrade_retains_original_if_rebook_fails() -> None:
    """If book() raises (slot gone, network error, etc.), the original booking is
    preserved: cancel_reservation is NOT called, store is unchanged."""

    orc, fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])
    fb.set_book_to_raise(SlotGoneError("slot gone"))  # book() fails

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    assert result is None
    assert fa.cancel_call_count == 0  # cancel was never called

    # Original booking still in store
    stored = await st.get_terminal(request.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.confirmation_code == current.confirmation_code


# ---------------------------------------------------------------------------
# Cancel-after-rebook failure
# ---------------------------------------------------------------------------


async def test_upgrade_persists_new_booking_if_cancel_fails() -> None:
    """If book() succeeds but cancel_reservation() raises CancelError, the new
    booking MUST still be persisted (we have a real confirmation code). The
    notifier is called with a dual-booking warning so the user can manually
    cancel the old booking."""
    orc, fa, fb, st, nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])
    fa.set_cancel_to_raise(CancelError("server refused cancel"))

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    # New booking is returned
    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == COURSE_B

    # New booking is persisted in the store
    stored = await st.get_terminal(request.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.course_id == COURSE_B

    # Notifier was called (with dual-booking warning)
    assert len(nt.calls) == 1


async def test_upgrade_sends_dual_booking_warning_on_cancel_failure() -> None:
    """Dual-booking scenario: notifier receives a result with a diagnostic
    field indicating the old booking must be manually cancelled."""
    orc, fa, fb, st, nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])
    fa.set_cancel_to_raise(CancelError("server refused cancel"))

    await orc.maybe_upgrade(request, TARGET_DATE, current)

    assert len(nt.calls) == 1
    notified = nt.calls[0]
    # The diagnostic must flag the dual-booking situation
    assert notified.diagnostics.get("dual_booking_warning") is True
    assert "old_confirmation_code" in notified.diagnostics


# ---------------------------------------------------------------------------
# Idempotency-key handling
# ---------------------------------------------------------------------------


async def test_upgrade_deletes_then_reinserts_terminal_under_lock() -> None:
    """The sequence must be: acquire lock → book new → delete_terminal →
    record_terminal (new). Verified by checking InMemoryStore state transitions."""
    orc, _fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])

    await orc.maybe_upgrade(request, TARGET_DATE, current)

    # After upgrade: only the NEW booking is in the store (old was deleted, new inserted)
    stored = await st.get_terminal(request.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.course_id == COURSE_B
    assert stored.confirmation_code is not None
    assert MANAGED_BOOKING_TAG in stored.confirmation_code


async def test_upgrade_does_not_delete_terminal_if_rebook_fails() -> None:
    """If book() fails, delete_terminal must NOT have been called —
    the old BOOKED terminal must still be in the store."""

    orc, _fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])
    fb.set_book_to_raise(SlotGoneError("slot gone"))

    await orc.maybe_upgrade(request, TARGET_DATE, current)

    # Original terminal still present
    stored = await st.get_terminal(request.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.course_id == COURSE_A
    assert stored.confirmation_code == current.confirmation_code


# ---------------------------------------------------------------------------
# Tie-breaking (Feature 3 integration)
# ---------------------------------------------------------------------------


async def test_upgrade_picks_earliest_slot_within_priority() -> None:
    """When multiple slots exist at a higher-priority window, the earliest
    tee_time slot is selected (ascending sort per Feature 3)."""
    orc, _fa, fb, st, _nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    # Two slots in the better window — later one first in the list to ensure
    # the orchestrator isn't just picking list[0].
    slot_late = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 25, tzinfo=ET),
        slot_id="late-slot",
    )
    slot_early = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 5, tzinfo=ET),
        slot_id="early-slot",
    )
    fb.set_search_response([slot_late, slot_early])  # late presented first

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    assert result is not None
    assert result.slot is not None
    assert result.slot.slot_id == SlotId("early-slot")


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


async def test_upgrade_notifies_on_successful_upgrade() -> None:
    """After a successful cancel+rebook, notifier.notify() is called exactly
    once with the new BOOKED BookingResult."""
    orc, _fa, fb, st, nt = _make_orchestrator()
    request = _make_request()
    current = _managed_booking(request=request)
    await st.record_terminal(current, TARGET_DATE)

    better_slot = _make_slot(
        course_id=COURSE_B,
        tee_time=datetime(2026, 6, 7, 9, 15, tzinfo=ET),
        slot_id="better-1",
    )
    fb.set_search_response([better_slot])

    result = await orc.maybe_upgrade(request, TARGET_DATE, current)

    assert result is not None
    assert len(nt.calls) == 1
    notified = nt.calls[0]
    assert notified.outcome == BookingOutcome.BOOKED
    assert notified.course_id == COURSE_B
    assert notified is result  # same object returned and notified
