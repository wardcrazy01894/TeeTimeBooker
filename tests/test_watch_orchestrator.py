"""Feature 1 — Cancellation Monitor / Watch Orchestrator (M-feature-1).

Tests for WatchOrchestrator.check_once().

TDD: these tests were written RED (against the NotImplementedError stubs),
then the implementation was written GREEN.  Do not weaken assertions to
match a wrong implementation — fix the implementation instead.

Coverage areas:
- Polls and books when a slot appears (happy path).
- Returns None (no booking) when no slot is available.
- Suppresses polling outside configured hours (polling_start_hour/end_hour gate).
- Stops and returns None when past the watch deadline (target_date has passed).
- Does NOT re-book if store already has a BOOKED terminal (idempotency guard).
- Does NOT re-book if list_reservations returns a matching reservation (§9 layer 2).
- Notifies on successful booking.
- Returns None on transient network failures (ACA cron retries on next interval).
- Re-raises CaptchaError and AuthError after notification.
- WatchConfig validation (already green — the dataclass __post_init__ guards).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

from teetime.core.adapter import AuthError, CaptchaError, NoInventoryError
from teetime.core.clock import FakeClock
from teetime.core.config import OneBookingPolicyConfig, PrioritySlotConfig, SchedulerConfig
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
    WatchConfig,
)
from teetime.core.watch_orchestrator import WatchOrchestrator
from teetime.dev.fake_adapter import FakeAdapter
from teetime.notifications.notifier import NoopNotifier, Notifier
from teetime.persistence.in_memory_store import InMemoryStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COURSE_ID = CourseId("fake:course")
# Target date: Saturday 2026-05-16 (7 days out from 2026-05-09).
TARGET_DATE = date(2026, 5, 16)
ET = ZoneInfo("America/New_York")

# During EDT (UTC-4):
#   10:00 ET  = 14:00 UTC  (inside polling window 07-22)
#    6:00 ET  = 10:00 UTC  (before polling_start_hour=7)
#   11:00 PM ET = 03:00 UTC next day (after polling_end_hour=22)
DURING_POLLING_UTC = datetime(2026, 5, 9, 14, 0, 0, tzinfo=UTC)  # 10 AM ET
BEFORE_POLLING_UTC = datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC)  # 6 AM ET
AFTER_POLLING_UTC = datetime(2026, 5, 10, 3, 0, 0, tzinfo=UTC)  # 11 PM ET (prev day)

# Past deadline: now is the day AFTER target_date.
PAST_DEADLINE_UTC = datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC)  # day after TARGET_DATE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(
    *,
    course_ids: tuple[CourseId, ...] = (COURSE_ID,),
    request_id: RequestId | None = None,
) -> BookingRequest:
    return BookingRequest(
        request_id=request_id or RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=course_ids,
    )


def _slot(*, hour: int = 9, minute: int = 0) -> TeeTimeSlot:
    """A slot at `hour:minute` ET on TARGET_DATE.

    Stored in course-local timezone (ET) as TeeTimeSlot.tee_time specifies:
    'tz-aware, course-local zone'. This ensures _matching_window comparisons
    work correctly since time_windows are also in course-local wall-clock time.
    """
    return TeeTimeSlot(
        course_id=COURSE_ID,
        slot_id=SlotId(f"slot-{hour:02d}{minute:02d}"),
        tee_time=datetime(
            TARGET_DATE.year,
            TARGET_DATE.month,
            TARGET_DATE.day,
            hour,
            minute,
            tzinfo=ET,
        ),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def _scheduler() -> SchedulerConfig:
    return SchedulerConfig(
        timezone="America/New_York",
        fire_time=time(6, 0, 0),
        early_arrival_ms=0,
        poll_interval_ms=10,
        max_poll_seconds=1,
    )


def _watch_config() -> WatchConfig:
    return WatchConfig(
        poll_interval_s=600,
        polling_start_hour=7,
        polling_end_hour=22,
    )


def _build(
    adapter: FakeAdapter,
    *,
    now_utc: datetime = DURING_POLLING_UTC,
    store: InMemoryStore | None = None,
    policy: OneBookingPolicyConfig | None = None,
) -> tuple[WatchOrchestrator, InMemoryStore, FakeClock]:
    store = store or InMemoryStore()
    clock = FakeClock(start=now_utc)
    creds = {COURSE_ID: CourseCredentials(username="u", password="p")}
    watch = WatchOrchestrator(
        adapters={COURSE_ID: adapter},
        store=store,
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=_scheduler(),
        watch_config=_watch_config(),
        creds=creds,
        policy=policy,
    )
    return watch, store, clock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_watch_check_once_books_when_slot_available() -> None:
    """When a slot is available and no existing booking exists, check_once()
    returns a BOOKED BookingResult and persists it to the store."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_slot(hour=9, minute=15)])
    watch, store, _ = _build(adapter)

    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    # Persisted so the next invocation is idempotent:
    stored = await store.get_terminal(result.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.outcome == BookingOutcome.BOOKED


async def test_watch_check_once_returns_none_when_no_slots() -> None:
    """When search() returns no slots (empty list), check_once() returns None."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([])
    watch, _, _ = _build(adapter)

    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is None
    assert adapter.search_call_count == 1


async def test_watch_check_once_raises_no_inventory_gracefully() -> None:
    """NoInventoryError from adapter is treated like an empty response — returns None."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_to_raise(NoInventoryError("none"))
    watch, _, _ = _build(adapter)

    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is None


async def test_watch_notifies_on_successful_booking() -> None:
    """check_once() calls notifier.notify() after a successful booking."""
    notified: list[BookingResult] = []

    class _CapturingNotifier:
        async def notify(self, result: BookingResult) -> None:
            notified.append(result)

    assert isinstance(_CapturingNotifier(), Notifier)

    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_slot()])
    store = InMemoryStore()
    clock = FakeClock(start=DURING_POLLING_UTC)
    creds = {COURSE_ID: CourseCredentials(username="u", password="p")}
    watch = WatchOrchestrator(
        adapters={COURSE_ID: adapter},
        store=store,
        notifier=_CapturingNotifier(),
        clock=clock,
        scheduler=_scheduler(),
        watch_config=_watch_config(),
        creds=creds,
    )

    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is not None
    assert len(notified) == 1
    assert notified[0].outcome == BookingOutcome.BOOKED


# ---------------------------------------------------------------------------
# Polling-hours gate
# ---------------------------------------------------------------------------


async def test_watch_suppressed_before_polling_start_hour() -> None:
    """check_once() returns None without calling search() when current time is
    before polling_start_hour. No HTTP calls should be made."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_slot()])  # slot available — but should never be searched
    watch, _, _ = _build(adapter, now_utc=BEFORE_POLLING_UTC)

    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is None
    assert adapter.search_call_count == 0


async def test_watch_suppressed_after_polling_end_hour() -> None:
    """check_once() returns None without calling search() when current time is
    past polling_end_hour."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_slot()])
    watch, _, _ = _build(adapter, now_utc=AFTER_POLLING_UTC)

    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is None
    assert adapter.search_call_count == 0


# ---------------------------------------------------------------------------
# Deadline gate
# ---------------------------------------------------------------------------


async def test_watch_stops_when_past_target_date() -> None:
    """check_once() returns None when now is on the day AFTER target_date.
    The round has passed; no point polling."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_slot()])
    watch, _, _ = _build(adapter, now_utc=PAST_DEADLINE_UTC)

    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is None
    assert adapter.search_call_count == 0


# ---------------------------------------------------------------------------
# Idempotency guards
# ---------------------------------------------------------------------------


async def test_watch_does_not_rebook_when_store_has_booked_terminal() -> None:
    """If store.get_terminal() returns a BOOKED result, check_once() returns
    that result immediately without touching the adapter."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    req = _request()
    store = InMemoryStore()

    # Pre-populate store with a BOOKED terminal (simulating the 6 AM job succeeded).
    prior = BookingResult(
        request_id=req.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=COURSE_ID,
        slot=_slot(),
        confirmation_code="TTB:prior-123",
        booked_at=DURING_POLLING_UTC,
        attempts=1,
    )
    await store.record_terminal(prior, TARGET_DATE)

    watch, _, _ = _build(adapter, store=store)
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code == "TTB:prior-123"
    # Adapter must NOT have been called.
    assert adapter.search_call_count == 0
    assert adapter.book_call_count == 0


async def test_watch_does_not_rebook_when_list_reservations_has_match() -> None:
    """If list_reservations() returns a matching reservation for the target date,
    check_once() returns None / ALREADY_BOOKED without POSTing again (§9 layer 2)."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    req = _request()

    # A reservation that matches target_date + party_size.
    adapter.set_existing_reservations(
        [
            ExistingReservation(
                course_id=COURSE_ID,
                confirmation_code="manual-abc",
                tee_time=datetime(
                    TARGET_DATE.year,
                    TARGET_DATE.month,
                    TARGET_DATE.day,
                    9,
                    30,
                    tzinfo=ET,
                ),
                party_size=1,  # matches len(request.players) == 1
            )
        ]
    )

    watch, _, _ = _build(adapter)
    await watch.check_once(req, TARGET_DATE)

    # No POST should be made.
    assert adapter.book_call_count == 0


async def test_watch_can_book_after_no_inventory_morning_run() -> None:
    """If the 6 AM run recorded NO_INVENTORY, the watch job can still book
    a newly released cancellation slot and overwrite the terminal."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    req = _request()
    store = InMemoryStore()

    # Simulate 6 AM run recording NO_INVENTORY.
    morning_result = BookingResult(
        request_id=req.request_id,
        outcome=BookingOutcome.NO_INVENTORY,
        course_id=None,
        slot=None,
        confirmation_code=None,
        booked_at=None,
        attempts=0,
    )
    await store.record_terminal(morning_result, TARGET_DATE)

    # Cancellation opens up.
    adapter.set_search_response([_slot(hour=9, minute=0)])
    watch, _, _ = _build(adapter, store=store)
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    # Store should now reflect BOOKED, not NO_INVENTORY.
    stored = await store.get_terminal(req.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.outcome == BookingOutcome.BOOKED


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_watch_returns_none_on_transient_network_error() -> None:
    """A transient httpx.RequestError in search() must NOT propagate — check_once()
    returns None so the ACA cron can retry on the next interval."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    # Patch search to raise a raw network error (not a typed AdapterError).
    adapter.search = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.RequestError("connection reset", request=None)
    )

    watch, _, _ = _build(adapter)
    result = await watch.check_once(_request(), TARGET_DATE)

    assert result is None


async def test_watch_reraises_captcha_error_after_notify() -> None:
    """CaptchaError from the adapter must be re-raised (after notifying) so the
    calling CLI/workflow can disable the watch job."""
    notified: list[BookingResult] = []

    class _CapturingNotifier:
        async def notify(self, result: BookingResult) -> None:
            notified.append(result)

    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_to_raise(CaptchaError("captcha required"))

    store = InMemoryStore()
    clock = FakeClock(start=DURING_POLLING_UTC)
    creds = {COURSE_ID: CourseCredentials(username="u", password="p")}
    watch = WatchOrchestrator(
        adapters={COURSE_ID: adapter},
        store=store,
        notifier=_CapturingNotifier(),
        clock=clock,
        scheduler=_scheduler(),
        watch_config=_watch_config(),
        creds=creds,
    )

    with pytest.raises(CaptchaError):
        await watch.check_once(_request(), TARGET_DATE)

    # Notifier must have been called before the re-raise.
    assert len(notified) == 1
    assert notified[0].outcome == BookingOutcome.CAPTCHA_BLOCKED


async def test_watch_reraises_auth_error_after_notify() -> None:
    """AuthError from authenticate() must be re-raised after notification."""
    notified: list[BookingResult] = []

    class _CapturingNotifier:
        async def notify(self, result: BookingResult) -> None:
            notified.append(result)

    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.authenticate = AsyncMock(side_effect=AuthError("bad creds"))  # type: ignore[method-assign]

    store = InMemoryStore()
    clock = FakeClock(start=DURING_POLLING_UTC)
    creds = {COURSE_ID: CourseCredentials(username="u", password="p")}
    watch = WatchOrchestrator(
        adapters={COURSE_ID: adapter},
        store=store,
        notifier=_CapturingNotifier(),
        clock=clock,
        scheduler=_scheduler(),
        watch_config=_watch_config(),
        creds=creds,
    )

    with pytest.raises(AuthError):
        await watch.check_once(_request(), TARGET_DATE)

    assert len(notified) == 1
    assert notified[0].outcome == BookingOutcome.AUTH_FAILED


# ---------------------------------------------------------------------------
# dry_run gate
# ---------------------------------------------------------------------------


async def test_watch_dry_run_returns_dry_run_outcome_without_booking() -> None:
    """check_once() with dry_run=True must return DRY_RUN — never POST a booking.

    Regression guard for the bug where _book_candidates had no dry_run gate.
    """
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_slot(hour=9, minute=15)])
    watch, store, _ = _build(adapter)

    req = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(COURSE_ID,),
        dry_run=True,
    )
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.outcome == BookingOutcome.DRY_RUN
    # No real booking POST.
    assert adapter.book_call_count == 0
    # DRY_RUN result is NOT persisted (we don't lock or write for dry runs).
    stored = await store.get_terminal(req.request_id, TARGET_DATE)
    assert stored is None


# ---------------------------------------------------------------------------
# Multi-course fallback on transient error
# ---------------------------------------------------------------------------

COURSE_ID_2 = CourseId("fake:course2")


async def test_watch_transient_error_on_first_course_still_tries_second() -> None:
    """When the first course raises a transient error, check_once() continues to
    the next course rather than returning None immediately.

    Regression guard for the bug where the transient-error handler did
    `return None` instead of `continue`.
    """
    adapter1 = FakeAdapter(course_id=COURSE_ID)
    adapter1.search = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.RequestError("connection reset", request=None)
    )

    adapter2 = FakeAdapter(course_id=COURSE_ID_2)
    adapter2.set_search_response(
        [
            TeeTimeSlot(
                course_id=COURSE_ID_2,
                slot_id=SlotId("slot-0930"),
                tee_time=datetime(
                    TARGET_DATE.year,
                    TARGET_DATE.month,
                    TARGET_DATE.day,
                    9,
                    30,
                    tzinfo=ET,
                ),
                holes=18,
                available_spots=4,
                price_per_player=Decimal("45.00"),
                cart_included=True,
            )
        ]
    )

    store = InMemoryStore()
    clock = FakeClock(start=DURING_POLLING_UTC)
    creds = {
        COURSE_ID: CourseCredentials(username="u", password="p"),
        COURSE_ID_2: CourseCredentials(username="u", password="p"),
    }
    watch = WatchOrchestrator(
        adapters={COURSE_ID: adapter1, COURSE_ID_2: adapter2},
        store=store,
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=_scheduler(),
        watch_config=_watch_config(),
        creds=creds,
    )

    req = _request(course_ids=(COURSE_ID, COURSE_ID_2))
    result = await watch.check_once(req, TARGET_DATE)

    # Should have fallen through to adapter2 and booked.
    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == COURSE_ID_2
    assert adapter2.book_call_count == 1


# ---------------------------------------------------------------------------
# WatchConfig validation (these were already green — kept as regression guard)
# ---------------------------------------------------------------------------


def test_watch_config_rejects_poll_interval_below_floor() -> None:
    """WatchConfig must raise ValueError if poll_interval_s < 300."""
    with pytest.raises(ValueError, match="300"):
        WatchConfig(poll_interval_s=299)


def test_watch_config_accepts_floor_value() -> None:
    """WatchConfig must accept poll_interval_s == 300 (the floor itself)."""
    cfg = WatchConfig(poll_interval_s=300)
    assert cfg.poll_interval_s == 300


# ---------------------------------------------------------------------------
# Upgrade path — M-feature-2 wired into WatchOrchestrator.check_once()
#
# Policy: priority 0 = 14:00-14:05 (best), priority 1 = 14:05-14:30 (fallback).
# Tests cover both the store-record path (Gate 3) and the live-reservation path
# (_check_course). Both paths must delegate to UpgradeOrchestrator.maybe_upgrade()
# when a higher-priority slot is available and policy is enabled.
# ---------------------------------------------------------------------------


def _two_pm_policy() -> OneBookingPolicyConfig:
    """Two-window afternoon policy for upgrade tests."""
    return OneBookingPolicyConfig(
        enabled=True,
        priority_slots=[
            PrioritySlotConfig(
                priority=0,
                course_id=str(COURSE_ID),
                time_window_earliest=time(14, 0),
                time_window_latest=time(14, 5),
            ),
            PrioritySlotConfig(
                priority=1,
                course_id=str(COURSE_ID),
                time_window_earliest=time(14, 5),
                time_window_latest=time(14, 30),
            ),
        ],
    )


def _pm_slot(*, hour: int = 14, minute: int = 0) -> TeeTimeSlot:
    """An afternoon slot at `hour:minute` ET on TARGET_DATE."""
    return TeeTimeSlot(
        course_id=COURSE_ID,
        slot_id=SlotId(f"slot-{hour:02d}{minute:02d}"),
        tee_time=datetime(
            TARGET_DATE.year, TARGET_DATE.month, TARGET_DATE.day, hour, minute, tzinfo=ET
        ),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


async def test_watch_upgrades_from_store_booked_terminal_when_policy_enabled() -> None:
    """Gate 3 path: when store has BOOKED at a lower-priority slot and a
    higher-priority slot becomes available, check_once() upgrades and returns
    the new booking."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    # Higher-priority slot available at 14:00 (priority 0 window: 14:00-14:05).
    adapter.set_search_response([_pm_slot(hour=14, minute=0)])
    req = _request()
    store = InMemoryStore()

    # Existing managed booking at 14:15 → priority 1 (14:05-14:30).
    prior = BookingResult(
        request_id=req.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=COURSE_ID,
        slot=_pm_slot(hour=14, minute=15),
        confirmation_code="TTB:prior-1415",
        booked_at=DURING_POLLING_UTC,
        attempts=1,
    )
    await store.record_terminal(prior, TARGET_DATE)

    watch, store, _ = _build(adapter, store=store, policy=_two_pm_policy())
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code != "TTB:prior-1415"
    assert adapter.book_call_count == 1
    assert adapter.cancel_call_count == 1
    # Store must now hold the upgraded booking, not the old one.
    stored = await store.get_terminal(req.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.confirmation_code != "TTB:prior-1415"


async def test_watch_does_not_upgrade_when_policy_is_none() -> None:
    """Without a policy, check_once() returns the prior BOOKED terminal unchanged
    (backwards-compatible behavior for callers that don't pass a policy)."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_pm_slot(hour=14, minute=0)])
    req = _request()
    store = InMemoryStore()

    prior = BookingResult(
        request_id=req.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=COURSE_ID,
        slot=_pm_slot(hour=14, minute=15),
        confirmation_code="TTB:prior-1415",
        booked_at=DURING_POLLING_UTC,
        attempts=1,
    )
    await store.record_terminal(prior, TARGET_DATE)

    watch, _, _ = _build(adapter, store=store, policy=None)
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.confirmation_code == "TTB:prior-1415"  # unchanged
    assert adapter.book_call_count == 0
    assert adapter.cancel_call_count == 0


async def test_watch_already_at_highest_priority_returns_prior_unchanged() -> None:
    """When the current booking is at priority 0 (highest), no upgrade is
    attempted and the prior booking is returned unchanged."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([_pm_slot(hour=14, minute=0)])
    req = _request()
    store = InMemoryStore()

    # Current booking at 14:02 — inside priority 0 window (14:00-14:05).
    prior = BookingResult(
        request_id=req.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=COURSE_ID,
        slot=_pm_slot(hour=14, minute=2),
        confirmation_code="TTB:prior-1402",
        booked_at=DURING_POLLING_UTC,
        attempts=1,
    )
    await store.record_terminal(prior, TARGET_DATE)

    watch, _, _ = _build(adapter, store=store, policy=_two_pm_policy())
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.confirmation_code == "TTB:prior-1402"  # unchanged
    assert adapter.book_call_count == 0
    assert adapter.cancel_call_count == 0


async def test_watch_no_upgrade_when_higher_priority_slot_unavailable() -> None:
    """When policy is enabled but no higher-priority slots are returned by search,
    check_once() returns the prior booking unchanged."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    adapter.set_search_response([])  # Nothing in the higher-priority window.
    req = _request()
    store = InMemoryStore()

    prior = BookingResult(
        request_id=req.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=COURSE_ID,
        slot=_pm_slot(hour=14, minute=15),
        confirmation_code="TTB:prior-1415",
        booked_at=DURING_POLLING_UTC,
        attempts=1,
    )
    await store.record_terminal(prior, TARGET_DATE)

    watch, _, _ = _build(adapter, store=store, policy=_two_pm_policy())
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.confirmation_code == "TTB:prior-1415"  # unchanged
    assert adapter.book_call_count == 0


async def test_watch_upgrades_from_live_reservation_when_no_store_record() -> None:
    """_check_course() path: when list_reservations() finds a lower-priority
    booking with no corresponding store record, and a higher-priority slot is
    available, check_once() synthesizes a managed booking record, upgrades it,
    and persists the new booking to the store."""
    adapter = FakeAdapter(course_id=COURSE_ID)
    req = _request()

    # Live reservation at 14:15 (priority 1) — no store record.
    adapter.set_existing_reservations(
        [
            ExistingReservation(
                course_id=COURSE_ID,
                confirmation_code="live-1415",
                tee_time=datetime(
                    TARGET_DATE.year,
                    TARGET_DATE.month,
                    TARGET_DATE.day,
                    14,
                    15,
                    tzinfo=ET,
                ),
                party_size=len(req.players),
            )
        ]
    )
    # Higher-priority slot at 14:00 (priority 0 window: 14:00-14:05).
    adapter.set_search_response([_pm_slot(hour=14, minute=0)])

    watch, store, _ = _build(adapter, policy=_two_pm_policy())
    result = await watch.check_once(req, TARGET_DATE)

    assert result is not None
    assert result.outcome == BookingOutcome.BOOKED
    assert adapter.book_call_count == 1
    assert adapter.cancel_call_count == 1
    # New booking must be persisted in the store.
    stored = await store.get_terminal(req.request_id, TARGET_DATE)
    assert stored is not None
    assert stored.outcome == BookingOutcome.BOOKED
