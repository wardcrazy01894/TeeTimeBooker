"""PR3 of BLIND_POST_PLAN.md: the orchestrator blind-POST race path + hybrid fallback.

At the 06:00 ET drop, for a blind-CAPABLE primary course on the race path
(``prefetch_book=True``, not dry-run), the orchestrator fires the top-N ranked
in-window blind book POSTs CONCURRENTLY with the real search, keeps the best
reservation that books, and cancels the rest IN-RUN. If zero blind POSTs book, it
re-guards (re-auth + ``list_reservations``) against a landed-but-uncertain POST
before falling through to the existing sequential search-book loop.

The capability gate is the explicit ``adapter.capabilities.blind_post`` flag — AND
race-path AND primary AND not-dry-run AND ``blind_post_max_count > 0``. Everything else
uses the unchanged search path.

Collaborators are FakeAdapter / FakeClock / InMemoryStore (BLIND_POST_PLAN.md §6/§7/§11).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core.adapter import AdapterError, CancelError, SlotGoneError
from teetime.core.clock import FakeClock
from teetime.core.config import SchedulerConfig
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


def _scheduler(*, lead_s: int = 30, blind_max: int = 12, reserve: int = 2) -> SchedulerConfig:
    return SchedulerConfig(
        timezone="America/New_York",
        fire_time=time(6, 0, 0),
        early_arrival_ms=100,
        poll_interval_ms=10,
        max_poll_seconds=1,
        captcha_prefetch_lead_s=lead_s,
        captcha_prefetch_count=3,
        blind_post_max_count=blind_max,
        blind_post_fallback_token_reserve=reserve,
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
    assert fa.search_call_count == 1  # exactly one FRESH fallback search (no concurrent hedge)


async def test_zero_booked_empty_fresh_search_skips_course() -> None:
    """0 blind booked + the FRESH fallback search finds nothing → the course is skipped and
    the run records NO_INVENTORY (graceful, not a crash). Exactly one search GET fired
    (RESEARCH_FALLBACK_PLAN §2 Q1)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    fa.set_book_side_effects([SlotGoneError("gone"), SlotGoneError("gone")])
    fa.set_search_response([])  # the fresh fallback search is empty too

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.NO_INVENTORY
    assert fa.search_call_count >= 1  # a FRESH fallback search was attempted (then polled empty)


async def test_non_capable_course_issues_exactly_one_search() -> None:
    """COURSE-DEPENDENCY no-regression: a non-blind-capable primary takes the normal
    _run_course search path untouched — it issues EXACTLY ONE search (no blind burst, no
    hedge, no second search) and never enters the blind path (RESEARCH_FALLBACK_PLAN §2,
    course-dependency requirement)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)  # not blind-capable
    fa.set_search_response([_bslot(cid, 8, 0)])

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.search_call_count == 1
    assert fa.synthesize_blind_slots_call_count == 0  # never entered the blind path


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


async def test_happy_path_issues_no_search() -> None:
    """REPLACES test_blind_post_wins_even_if_hedge_search_errors. The concurrent hedge search
    is GONE (RESEARCH_FALLBACK_PLAN §2 Q1): when a blind POST books, the happy path returns
    WITHOUT issuing any search GET. Pins the hedge removal AND happy-path no-regression (no
    wasted/cancelled GET at the most latency-critical instant)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    # Both blind POSTs book (default side effect is BOOKED) → keep best, cancel the extra.

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.slot is not None
    assert result.slot.slot_id == "s-0815"  # midpoint slot kept
    assert fa.cancel_call_count == 1  # the one extra blind reservation cancelled
    assert fa.search_call_count == 0  # NO hedge — the blind win needs no search GET


async def test_zero_booked_fires_fresh_search_after_blind_burst() -> None:
    """When 0 blind POSTs book, EXACTLY ONE fresh search fires, and it runs AFTER the whole
    blind burst (not a concurrent hedge): ``search_book_counts == [2]`` proves the single
    search observed both blind books before it ran. The fallback then books the search slot
    (RESEARCH_FALLBACK_PLAN §2 Q1)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    fa.set_book_side_effects([SlotGoneError("gone"), SlotGoneError("gone"), BookingOutcome.BOOKED])
    fa.set_search_response([_bslot(cid, 9, 0)])

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.slot is not None
    assert result.slot.slot_id == "s-0900"  # booked via the fresh fallback search
    assert fa.search_call_count == 1
    assert fa.search_book_counts == [2]  # the search ran AFTER both blind books


async def test_fresh_search_runs_after_reguard() -> None:
    """The fresh fallback search runs STRICTLY AFTER the re-guard (refresh → list), so it
    books off the freshest post-re-auth snapshot and never races the shared-client cookie
    rotation (RESEARCH_FALLBACK_PLAN §2 Q2). Driven via _blind_post_course DIRECTLY: a full
    run()'s pre-T0 prewarm + T0 layer-2 guard also call list_reservations, which would muddy a
    whole-run order recording (see the existing reguard order tests, which drive it directly
    too)."""
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

        async def search(
            self, request: BookingRequest, *, skip_initial_spacing: bool = False
        ) -> list[TeeTimeSlot]:
            self.order.append("search")
            return await super().search(request, skip_initial_spacing=skip_initial_spacing)

    fa = _OrderAdapter()
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    fa.set_book_side_effects([SlotGoneError("gone"), SlotGoneError("gone"), BookingOutcome.BOOKED])
    fa.set_search_response([_bslot(cid, 9, 0)])

    orch, _, _ = _build({cid: fa})
    await orch._blind_post_course(fa, cid, _request(course_ids=(cid,)))

    assert fa.order == ["refresh", "list", "search"]


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
    # The CRITICAL log must NAME the leaked reservation (its confirmation_code) so an
    # operator can cancel it manually — asserting level alone would let a regression that
    # drops the id stay green. full-repo-scan observability finding.
    assert any(r.levelname == "CRITICAL" and "s-0800" in r.getMessage() for r in caplog.records)


async def test_cancel_extras_with_no_confirmation_code_logs_critical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A surplus blind reservation whose book() returned no confirmation_code CANNOT be
    cancelled — log CRITICAL (the user may hold an extra) but never crash, and never call
    cancel for it. Pins the no-conf branch of _cancel_extras (only the CancelError branch
    was previously covered)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    orch, _, _ = _build({cid: fa})

    extra = BookingResult(
        request_id=RequestId(uuid4()),
        outcome=BookingOutcome.BOOKED,
        course_id=cid,
        slot=_bslot(cid, 8, 0),
        confirmation_code=None,  # extraction failed → no id to cancel by
        booked_at=datetime(2026, 5, 13, 10, 0, tzinfo=UTC),
        attempts=1,
    )

    with caplog.at_level("CRITICAL"):
        await orch._cancel_extras(fa, [extra])

    assert fa.cancel_call_count == 0  # can't cancel without an id
    assert any(
        r.levelname == "CRITICAL" and "no confirmation_code" in r.getMessage()
        for r in caplog.records
    )


async def test_blind_post_logs_slot_gone_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When blind POSTs come back SlotGone (4xx — slot claimed between synthesize and
    book), log an aggregate count so an operator can see WHY each candidate died at the
    drop instead of a silent fall-through. full-repo-scan observability finding."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15)])
    fa.set_book_side_effects([SlotGoneError("gone1"), SlotGoneError("gone2")])
    fa.set_search_response([])  # hedge search finds nothing → NO_INVENTORY

    orch, _, _ = _build({cid: fa})
    with caplog.at_level(logging.INFO, logger="teetime.core.orchestrator"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.NO_INVENTORY
    assert any("slot-gone" in r.getMessage() and "2" in r.getMessage() for r in caplog.records), (
        "expected an aggregate slot-gone count line from _blind_post_course"
    )


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


async def test_uncertain_blind_post_drop_log_names_the_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When a blind POST raises an uncertain (non-SlotGone) error and is dropped, the
    WARNING must name WHICH slot was dropped (its slot_id), not just the exception —
    otherwise an operator triaging a missed booking cannot tell which tee time the
    landed-but-uncertain POST may have created. Observability polish (PR-D)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 15)])  # single slot → no ordering ambiguity
    fa.set_book_side_effects([AdapterError("uncertain 5xx")])
    fa.set_search_response([])  # fallback search finds nothing

    orch, _, _ = _build({cid: fa})
    with caplog.at_level("WARNING"):
        await orch.run(_request(course_ids=(cid,)))

    drop_logs = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
        and "blind POST" in r.getMessage()
        and "dropping candidate" in r.getMessage()
    ]
    assert drop_logs, "expected a blind-POST drop warning"
    assert any("s-0815" in m for m in drop_logs), (
        f"drop warning must name the slot_id; got: {drop_logs}"
    )


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
    assert fa.search_call_count == 0  # reguard match short-circuits BEFORE the fresh search


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


async def test_prefetch_scales_to_blind_fanout_plus_reserve() -> None:
    """For a blind-capable primary, the pre-T0 CAPTCHA prefetch solves the burst
    min(blind_post_max_count, len(blind_slots)) PLUS blind_post_fallback_token_reserve
    spare tokens for the 0-booked fresh-search fallback (RESEARCH_FALLBACK_PLAN §2 Q3) —
    NOT the fixed captcha_prefetch_count."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    # 5 blind slots, cap 12, reserve 2 → min(12, 5) + 2 = 7 tokens prefetched.
    fa.set_blind_slots(
        [
            _bslot(cid, 7, 30),
            _bslot(cid, 8, 0),
            _bslot(cid, 8, 15),
            _bslot(cid, 8, 30),
            _bslot(cid, 9, 0),
        ]
    )

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=12, reserve=2))
    await orch.run(_request(course_ids=(cid,)))

    assert fa.last_prepare_count == 7


async def test_prefetch_reserve_respects_blind_max() -> None:
    """The burst portion stays capped by blind_post_max_count; the reserve is added on top.
    cap 3, an 8-slot grid, reserve 2 → min(3, 8) + 2 = 5 (synthesize truncates to cap)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 7, m) for m in (0, 7, 14, 21, 28, 35, 42, 49)])  # 8 slots

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=3, reserve=2))
    await orch.run(_request(course_ids=(cid,)))

    assert fa.last_prepare_count == 5


async def test_prefetch_reserve_zero_grid_uses_default() -> None:
    """A blind-capable primary with an EMPTY grid falls back to the single-POST prefetch
    depth (captcha_prefetch_count) and adds NO reserve — the reserve is only for a real
    blind burst (RESEARCH_FALLBACK_PLAN §2 Q3, 0-grid degenerate case)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([])  # empty grid → min(cap, 0) = 0
    fa.set_search_response([_bslot(cid, 8, 0)])

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=12, reserve=2))
    await orch.run(_request(course_ids=(cid,)))

    assert fa.last_prepare_count == 3  # captcha_prefetch_count, no reserve added


async def test_reserve_does_not_increase_blind_burst() -> None:
    """The inflated prefetch (burst + reserve) must NOT enlarge the blind burst: the burst
    is bounded by len(blind_slots), not the deepened pool. 3-slot grid, pool 5 (= 3 burst +
    2 reserve) → exactly 3 blind book POSTs fire (RESEARCH_FALLBACK_PLAN §2 Q3)."""
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    fa.set_blind_slots([_bslot(cid, 8, 0), _bslot(cid, 8, 15), _bslot(cid, 8, 30)])  # 3 slots
    fa.set_captcha_pool_size(5)  # mimic the reserve-deepened pool (3 burst + 2 reserve)
    fa.set_book_side_effects([BookingOutcome.BOOKED, BookingOutcome.BOOKED, BookingOutcome.BOOKED])

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=3, reserve=2))
    await orch.run(_request(course_ids=(cid,)))

    assert fa.book_call_count == 3  # min(len(blind_slots)=3, pool=5), NOT 5


async def test_non_blind_primary_prefetch_uses_fixed_count() -> None:
    """A non-capable primary keeps the single-POST race prefetch depth
    (captcha_prefetch_count), unaffected by blind_post_max_count OR the reserve."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)  # not capable
    fa.set_search_response([_bslot(cid, 8, 0)])

    orch, _, _ = _build({cid: fa}, scheduler=_scheduler(blind_max=12, reserve=2))
    await orch.run(_request(course_ids=(cid,)))

    assert fa.last_prepare_count == 3  # captcha_prefetch_count, not blind_post_max_count + reserve
