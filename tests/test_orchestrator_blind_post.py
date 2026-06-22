"""PR3 of BLIND_POST_PLAN.md: the orchestrator blind-POST race path + hybrid fallback.

At the 06:00 ET drop, for a blind-CAPABLE primary course on the race path
(``prefetch_book=True``, not dry-run), the orchestrator fires the top-N ranked
in-window blind book POSTs CONCURRENTLY with the real search, keeps the best
reservation that books, and cancels the rest IN-RUN. If zero blind POSTs book, it
re-guards (re-auth + ``list_reservations``) against a landed-but-uncertain POST
before falling through to the existing sequential search-book loop.

The capability gate is two-part — ``isinstance(adapter, BlindPostCapable) and
adapter.supports_blind_post`` — AND race-path AND primary AND not-dry-run AND
``blind_post_max_count > 0``. Everything else uses the unchanged search path.

Collaborators are FakeAdapter / FakeClock / InMemoryStore (BLIND_POST_PLAN.md §6/§7/§11).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core.adapter import AdapterError, CancelError, RateLimitError, SlotGoneError
from teetime.core.clock import FakeClock
from teetime.core.config import SchedulerConfig
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
from teetime.core.orchestrator import Orchestrator
from teetime.core.slot_utils import rank_slots_for_request
from teetime.dev.fake_adapter import FakeAdapter
from teetime.notifications.notifier import NoopNotifier
from teetime.persistence.in_memory_store import InMemoryStore

# T0 = 06:00 ET on 2026-05-13 ≈ 10:00 UTC (EDT). The booking targets that date.
TARGET = date(2026, 5, 13)
# Window 07:00-09:30 → midpoint 08:15, so an 08:15 slot is the rank-0 best.
WINDOW = TimeWindow(earliest=time(7, 0), latest=time(9, 30))


def _request(
    *,
    course_ids: tuple[CourseId, ...],
    dry_run: bool = False,
) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET,),
        time_windows=(WINDOW,),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=course_ids,
        dry_run=dry_run,
    )


def _bslot(cid: CourseId, hour: int, minute: int) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=cid,
        slot_id=SlotId(f"s-{hour:02d}{minute:02d}"),
        tee_time=datetime(2026, 5, 13, hour, minute, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def _scheduler(*, lead_s: int = 30, blind_max: int = 12) -> SchedulerConfig:
    return SchedulerConfig(
        timezone="America/New_York",
        fire_time=time(6, 0, 0),
        early_arrival_ms=100,
        poll_interval_ms=10,
        max_poll_seconds=1,
        captcha_prefetch_lead_s=lead_s,
        captcha_prefetch_count=3,
        blind_post_max_count=blind_max,
    )


def _build(
    adapters: dict[CourseId, FakeAdapter],
    *,
    scheduler: SchedulerConfig | None = None,
    prefetch_book: bool = True,
    clock: FakeClock | None = None,
) -> tuple[Orchestrator, InMemoryStore, FakeClock]:
    store = InMemoryStore()
    sched = scheduler or _scheduler()
    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = clock or FakeClock(start=t0 - timedelta(seconds=sched.captcha_prefetch_lead_s + 2))
    creds = {cid: CourseCredentials(username="u", password="p") for cid in adapters}
    orch = Orchestrator(
        adapters=adapters,
        store=store,
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=sched,
        creds=creds,
        prefetch_book=prefetch_book,
    )
    return orch, store, clock


# --- happy fast path: keep best, cancel extras --------------------------


async def test_blind_post_books_best_and_cancels_extras() -> None:
    """3 blind POSTs all book → keep the rank-0 slot (08:15, the midpoint), cancel the
    other two IN-RUN via their own confirmation_code."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    blind = [_bslot(cid, 8, 0), _bslot(cid, 8, 15), _bslot(cid, 8, 30)]
    fa.set_blind_slots(blind)

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.slot is not None
    assert result.slot.slot_id == "s-0815"  # the midpoint slot is kept
    assert fa.book_call_count == 3  # all three blind POSTs fired
    assert fa.cancel_call_count == 2  # the two non-best reservations cancelled
    assert fa.synthesize_blind_slots_call_count >= 1


async def test_blind_post_keep_best_agrees_with_search_ranking() -> None:
    """Canary: 'keep best' is decided by the SAME rank_slots_for_request the search path
    uses, so blind and search agree by construction."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    blind = [_bslot(cid, 9, 0), _bslot(cid, 8, 15), _bslot(cid, 7, 30)]
    fa.set_blind_slots(blind)

    req = _request(course_ids=(cid,))
    orch, _, _ = _build({cid: fa})
    result = await orch.run(req)

    expected_best = rank_slots_for_request(blind, req)[0]
    assert result.slot is not None
    assert result.slot.slot_id == expected_best.slot_id


# --- fallback to search when blind comes up empty -----------------------


async def test_blind_post_all_gone_falls_back_to_search() -> None:
    """Every blind POST 4xx → SlotGoneError; the concurrent search returns a slot that
    books via the existing sequential fallback loop."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    blind = [_bslot(cid, 8, 0), _bslot(cid, 8, 15)]
    fa.set_blind_slots(blind)
    # 2 blind POSTs gone, then the fallback search-book succeeds.
    fa.set_book_side_effects([SlotGoneError("gone"), SlotGoneError("gone"), BookingOutcome.BOOKED])
    fa.set_search_response([_bslot(cid, 9, 0)])

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.slot is not None
    assert result.slot.slot_id == "s-0900"  # booked via the search fallback
    assert fa.book_call_count == 3  # 2 blind + 1 fallback
    assert fa.search_call_count >= 1


async def test_blind_post_token_exhaustion_fires_fewer() -> None:
    """Pool size 1 caps the burst: only 1 blind POST fires (N = min(grid, pool)),
    not all 3 synthesized."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15), _bslot(cid, 8, 30)])
    fa.set_captcha_pool_size(1)

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.book_call_count == 1  # only the single pooled token was spent
    assert fa.cancel_call_count == 0  # nothing to cancel — one booking


async def test_blind_post_wins_even_if_hedge_search_errors() -> None:
    """Regression: a blind POST books, but the concurrent hedge search GET fails with a
    non-cancelled error (429 RateLimitError). Abandoning the hedge must NOT re-raise that
    error and discard the real booking — the run returns BOOKED. (orchestrator._cancel_task
    suppressed only CancelledError, so an already-failed hedge task re-raised on await.)"""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    # The hedge search is throttled at the drop — the worst-case real-world race.
    fa.set_search_to_raise(RateLimitError("throttled", retry_after_s=30))

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED  # the blind booking is kept, not lost
    assert result.slot is not None
    assert result.slot.slot_id == "s-0815"  # midpoint slot kept
    assert fa.cancel_call_count == 1  # the one extra blind reservation cancelled


# --- gate exclusions ----------------------------------------------------


async def test_non_capable_course_never_blind_posts() -> None:
    """A non-capable course (supports_blind_post=False) uses the search path even on the
    race path — synthesize_blind_slots is never consulted."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)  # default: not capable
    fa.set_search_response([_bslot(cid, 8, 0)])

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.synthesize_blind_slots_call_count == 0
    assert fa.book_call_count == 1


async def test_dry_run_never_blind_posts() -> None:
    """Dry-run is excluded by the gate: capable + race but dry_run → DRY_RUN via the
    search path, no blind POSTs."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_search_response([_bslot(cid, 8, 0)])

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,), dry_run=True))

    assert result.outcome == BookingOutcome.DRY_RUN
    assert fa.synthesize_blind_slots_call_count == 0
    assert fa.book_call_count == 0


async def test_non_primary_course_uses_search_path() -> None:
    """Only the PRIMARY (first-preference) course blind-POSTs. A capable course in a
    fallback position uses the search path (synthesize never called on it)."""
    primary = CourseId("fake:primary")
    second = CourseId("fake:mb")
    fa1 = FakeAdapter(course_id=primary)  # not capable; primary
    fa1.set_search_response([])  # empty → skip to next course
    fa2 = FakeAdapter(course_id=second, supports_blind_post=True)  # capable but NOT primary
    fa2.set_search_response([_bslot(second, 8, 0)])

    orch, _, _ = _build({primary: fa1, second: fa2})
    result = await orch.run(_request(course_ids=(primary, second)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == second
    assert fa2.synthesize_blind_slots_call_count == 0  # search path, not blind
    assert fa1.synthesize_blind_slots_call_count == 0


async def test_blind_post_max_count_zero_disables_blind() -> None:
    """blind_post_max_count = 0 disables blind fan-out: the capable primary uses the
    single-POST search path."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_search_response([_bslot(cid, 8, 0)])

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=0))
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.synthesize_blind_slots_call_count == 0


# --- crash safety / uncertain handling ----------------------------------


async def test_cancel_extra_failure_still_returns_best(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If cancelling an extra raises CancelError, log CRITICAL and still RETURN the best
    booked reservation — the user has a booking."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    fa.set_cancel_to_raise(CancelError("server refused"))

    orch, _, _ = _build({cid: fa})
    with caplog.at_level("CRITICAL"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.slot is not None
    assert result.slot.slot_id == "s-0815"  # best still returned
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


async def test_uncertain_blind_post_does_not_crash_run() -> None:
    """One blind task raises a non-SlotGone (uncertain 5xx/timeout-style) error: gather
    captures it, it is dropped, and a sibling booked POST is still kept."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15), _bslot(cid, 8, 30)])
    # First POST uncertain (raises AdapterError, not SlotGone), other two book.
    fa.set_book_side_effects(
        [AdapterError("uncertain 5xx"), BookingOutcome.BOOKED, BookingOutcome.BOOKED]
    )

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    # Two booked → keep best, cancel one. The uncertain one is not a booked result.
    assert fa.cancel_call_count == 1


async def test_zero_booked_but_landed_uncertain_reguards_to_already_booked() -> None:
    """All blind POSTs 'fail' but one actually LANDED (uncertain): the re-guard
    re-authenticates, list_reservations now reveals the landed reservation, and the run
    short-circuits ALREADY_BOOKED — the fallback search-book is NEVER called."""
    cid = CourseId("fake:mb")

    class _ReguardAdapter(FakeAdapter):
        """Production-faithful ForeUP model: authenticate() is IDEMPOTENT (a 2nd call is
        a no-op — the _logged_in short-circuit — and does NOT rebuild the reservation
        cache), and ONLY refresh_reservations() forces a fresh snapshot. This is the
        regression guard for must-fix #1: the old reguard (which called authenticate())
        would never see the landed reservation here and would double-book."""

        def __init__(self) -> None:
            super().__init__(course_id=cid, supports_blind_post=True)
            self._logged_in = False
            self._reveal = False

        async def authenticate(self, creds: CourseCredentials) -> None:
            # Idempotent, like ForeUpAdapter.authenticate: once logged in, no-op. A
            # plain re-auth therefore does NOT rebuild the cache / reveal the booking.
            if self._logged_in:
                return
            self._logged_in = True
            await super().authenticate(creds)

        async def refresh_reservations(self, creds: CourseCredentials) -> None:
            # The forced re-login: clears the flag, re-authenticates, and the fresh
            # snapshot now reveals the reservation the uncertain blind POST created.
            self._logged_in = False
            await self.authenticate(creds)
            self._reveal = True

        async def list_reservations(self) -> list[ExistingReservation]:
            self.list_reservations_call_count += 1
            if not self._reveal:
                return []
            return [
                ExistingReservation(
                    course_id=cid,
                    confirmation_code="LANDED-123",
                    tee_time=datetime(2026, 5, 13, 8, 15, tzinfo=UTC),
                    party_size=1,
                )
            ]

    fa = _ReguardAdapter()
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    fa.set_book_side_effects([SlotGoneError("gone"), SlotGoneError("gone")])
    # If the fallback were (wrongly) reached, this search slot would be booked.
    fa.set_search_response([_bslot(cid, 9, 0)])

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.ALREADY_BOOKED
    assert result.confirmation_code == "LANDED-123"
    assert fa.book_call_count == 2  # only the 2 blind POSTs; NO fallback book


async def test_reguard_refreshes_before_listing() -> None:
    """_reguard_before_fallback must force a fresh snapshot (refresh_reservations on a
    ReservationCacheRefreshable adapter) BEFORE list_reservations() so a THIS-RUN blind
    reservation (in ForeUP's login cache) is visible (must-fix 1/3)."""
    cid = CourseId("fake:mb")

    class _OrderAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__(course_id=cid, supports_blind_post=True)
            self.order: list[str] = []

        async def refresh_reservations(self, creds: CourseCredentials) -> None:
            self.order.append("refresh")

        async def list_reservations(self) -> list[ExistingReservation]:
            self.order.append("list")
            return await super().list_reservations()

    fa = _OrderAdapter()
    orch, _, _ = _build({cid: fa})
    req = _request(course_ids=(cid,))

    await orch._reguard_before_fallback(fa, cid, req)

    assert fa.order == ["refresh", "list"]


async def test_reguard_falls_back_to_authenticate_for_non_refreshable() -> None:
    """An adapter WITHOUT the ReservationCacheRefreshable capability (e.g. a live-GET
    store) still gets authenticate() BEFORE list_reservations() — the refresh path is
    ForeUP-specific, not universal."""
    cid = CourseId("fake:mb")

    class _OrderAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__(course_id=cid, supports_blind_post=True)
            self.order: list[str] = []

        async def authenticate(self, creds: CourseCredentials) -> None:
            self.order.append("auth")
            await super().authenticate(creds)

        async def list_reservations(self) -> list[ExistingReservation]:
            self.order.append("list")
            return await super().list_reservations()

    fa = _OrderAdapter()
    orch, _, _ = _build({cid: fa})
    req = _request(course_ids=(cid,))

    await orch._reguard_before_fallback(fa, cid, req)

    assert fa.order == ["auth", "list"]


# --- CAPTCHA prefetch scales to blind fan-out ---------------------------


async def test_prefetch_scales_to_blind_fanout() -> None:
    """For a blind-capable primary, the pre-T0 CAPTCHA prefetch solves
    min(blind_post_max_count, len(blind_slots)) tokens — NOT the fixed
    captcha_prefetch_count."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    # 5 in-window blind slots, cap 12 → expect min(12, 5) = 5 tokens prefetched.
    fa.set_blind_slots(
        [
            _bslot(cid, 7, 30),
            _bslot(cid, 8, 0),
            _bslot(cid, 8, 15),
            _bslot(cid, 8, 30),
            _bslot(cid, 9, 0),
        ]
    )

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=12))
    await orch.run(_request(course_ids=(cid,)))

    assert fa.last_prepare_count == 5


async def test_non_blind_primary_prefetch_uses_fixed_count() -> None:
    """A non-capable primary keeps the single-POST race prefetch depth
    (captcha_prefetch_count), unaffected by blind_post_max_count."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)  # not capable
    fa.set_search_response([_bslot(cid, 8, 0)])

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=12))
    await orch.run(_request(course_ids=(cid,)))

    assert fa.last_prepare_count == 3  # captcha_prefetch_count, not blind_post_max_count
