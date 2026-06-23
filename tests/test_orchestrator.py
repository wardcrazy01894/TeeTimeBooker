"""M2.T1 happy path + idempotency + course fallback tests for Orchestrator.

An UNCERTAIN book (timeout/5xx) raises out of the run — there is no in-run
RECONCILING transition (M2.T3 was cut; the watcher reconciles asynchronously,
PLAN.md §9.1). This file pins the v0 happy + idempotent + dry-run +
course-fallback + already-booked paths.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core.adapter import (
    AuthError,
    CaptchaError,
    InventoryNotPublishedError,
    NoInventoryError,
    RateLimitError,
    SlotGoneError,
)
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
from teetime.dev.fake_adapter import FakeAdapter
from teetime.notifications.notifier import NoopNotifier
from teetime.persistence.in_memory_store import InMemoryStore
from teetime.persistence.store import ConcurrentRunError

# --- Fixtures local to this module --------------------------------------


def _request(
    *,
    request_id: RequestId | None = None,
    course_ids: tuple[CourseId, ...] = (CourseId("fake:course"),),
    dry_run: bool = False,
) -> BookingRequest:
    return BookingRequest(
        request_id=request_id or RequestId(uuid4()),
        target_dates=(date(2026, 5, 13),),
        time_windows=(TimeWindow(earliest=time(7, 0), latest=time(9, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=course_ids,
        dry_run=dry_run,
    )


def _slot(course_id: CourseId, hour: int = 8, price: str = "45.00") -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=course_id,
        slot_id=SlotId(f"slot-{hour}"),
        tee_time=datetime(2026, 5, 13, hour, 0, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal(price),
        cart_included=True,
    )


def _scheduler(early_ms: int = 100) -> SchedulerConfig:
    return SchedulerConfig(
        timezone="America/New_York",
        fire_time=time(6, 0, 0),
        early_arrival_ms=early_ms,
        poll_interval_ms=10,
        max_poll_seconds=1,
    )


def _build(
    adapters: dict[CourseId, FakeAdapter],
    *,
    store: InMemoryStore | None = None,
    clock: FakeClock | None = None,
    scheduler: SchedulerConfig | None = None,
    prefetch_book: bool = False,
) -> tuple[Orchestrator, InMemoryStore, FakeClock]:
    store = store or InMemoryStore()
    clock = clock or FakeClock(start=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC))
    creds = {cid: CourseCredentials(username="u", password="p") for cid in adapters}
    orch = Orchestrator(
        adapters=adapters,
        store=store,
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=scheduler or _scheduler(),
        creds=creds,
        prefetch_book=prefetch_book,
    )
    return orch, store, clock


# --- Happy path ---------------------------------------------------------


async def test_run_happy_path_returns_booked() -> None:
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    orch, store, _ = _build({cid: fa})

    req = _request(course_ids=(cid,))
    result = await orch.run(req)

    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code is not None
    assert result.course_id == cid
    assert fa.book_call_count == 1
    # Persisted.
    assert (await store.get_terminal(req.request_id, req.target_dates[0])) == result


async def test_run_logs_resolved_terminal(caplog: pytest.LogCaptureFixture) -> None:
    """L3 (full-repo-scan): run() must log its resolved terminal (outcome + course +
    confirmation + date) to the structured app log. The ConsoleNotifier writes a SEPARATE
    stdout stream and the `run` CLI only logs on failure, so without this the orchestrator's
    decision was absent from the (stderr→Log Analytics) app log."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    orch, _, _ = _build({cid: fa})

    with caplog.at_level(logging.INFO, logger="teetime.core.orchestrator"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    line = next((r.getMessage() for r in caplog.records if "run terminal" in r.getMessage()), None)
    assert line is not None, "expected a 'booking: run terminal' log line"
    assert "outcome=booked" in line
    assert str(cid) in line


async def test_run_persists_authenticate_call() -> None:
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    orch, _, _ = _build({cid: fa})

    await orch.run(_request(course_ids=(cid,)))
    assert fa.authenticate_call_count == 1


# --- Idempotency --------------------------------------------------------


async def test_run_idempotent_short_circuits_to_prior_terminal() -> None:
    """If the store has a BOOKED for (rid, date), orchestrator returns it
    without touching the adapter."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    store = InMemoryStore()
    rid = RequestId(uuid4())
    d = date(2026, 5, 13)
    prior = BookingResult(
        request_id=rid,
        outcome=BookingOutcome.BOOKED,
        course_id=cid,
        slot=None,
        confirmation_code="PRIOR-1",
        booked_at=datetime(2026, 5, 6, 10, 0, 1, tzinfo=UTC),
        attempts=1,
    )
    await store.record_terminal(prior, d)

    orch, _, _ = _build({cid: fa}, store=store)
    result = await orch.run(_request(request_id=rid, course_ids=(cid,)))

    assert result == prior
    assert fa.search_call_count == 0
    assert fa.book_call_count == 0
    assert fa.authenticate_call_count == 0


async def test_run_idempotent_replay_logs_terminal(caplog: pytest.LogCaptureFixture) -> None:
    """L3 follow-up (PR #143 review): the idempotency short-circuit (prior terminal exists)
    must ALSO log its resolved terminal — a re-run's decision should be in the app log, not
    silently returned."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    store = InMemoryStore()
    rid = RequestId(uuid4())
    d = date(2026, 5, 13)
    prior = BookingResult(
        request_id=rid,
        outcome=BookingOutcome.BOOKED,
        course_id=cid,
        slot=None,
        confirmation_code="TTB:PRIOR-1",
        booked_at=datetime(2026, 5, 6, 10, 0, 1, tzinfo=UTC),
        attempts=1,
    )
    await store.record_terminal(prior, d)
    orch, _, _ = _build({cid: fa}, store=store)

    with caplog.at_level(logging.INFO, logger="teetime.core.orchestrator"):
        await orch.run(_request(request_id=rid, course_ids=(cid,)))

    assert any(
        "run terminal (idempotent replay)" in r.getMessage() and "outcome=booked" in r.getMessage()
        for r in caplog.records
    ), "expected an idempotent-replay terminal log line"


# --- Dry run -----------------------------------------------------------


async def test_run_dry_run_skips_book_post() -> None:
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    orch, _, _ = _build({cid: fa})

    result = await orch.run(_request(course_ids=(cid,), dry_run=True))

    assert result.outcome == BookingOutcome.DRY_RUN
    assert fa.book_call_count == 0
    assert result.slot is not None  # we DID find a slot, just didn't POST


# --- No inventory & course fallback -----------------------------------


async def test_run_returns_no_inventory_when_all_courses_empty() -> None:
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([])
    orch, _, _ = _build({cid: fa})

    result = await orch.run(_request(course_ids=(cid,)))
    assert result.outcome == BookingOutcome.NO_INVENTORY
    assert fa.book_call_count == 0


async def test_run_logs_give_up_reason_when_course_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A course that yields no bookable inventory logs an INFO give-up line (with the
    target date) from _poll_for_slots before falling through — so a 06:00 'found nothing'
    is diagnosable from logs alone. full-repo-scan observability finding."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([])
    orch, _, _ = _build({cid: fa})

    with caplog.at_level(logging.INFO, logger="teetime.core.orchestrator"):
        await orch.run(_request(course_ids=(cid,)))

    assert any("no slots found" in r.getMessage() for r in caplog.records), (
        "expected a give-up INFO line from _poll_for_slots"
    )


async def test_run_logs_give_up_reason_on_no_inventory_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A course whose search() raises NoInventoryError logs its own give-up INFO line
    (distinct from the empty-list-at-deadline branch) before falling through."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_to_raise(NoInventoryError("nada"))
    orch, _, _ = _build({cid: fa})

    with caplog.at_level(logging.INFO, logger="teetime.core.orchestrator"):
        await orch.run(_request(course_ids=(cid,)))

    assert any(
        "no slots found" in r.getMessage() and "no inventory" in r.getMessage()
        for r in caplog.records
    ), "expected a NoInventoryError give-up INFO line from _poll_for_slots"


async def test_run_logs_give_up_reason_when_inventory_never_published(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A course whose search() raises InventoryNotPublishedError on every poll through the
    max_poll_seconds deadline logs the unpublished-at-deadline give-up line before falling
    through — the 06:00 'window never opened' case must be diagnosable from logs alone."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_to_raise(InventoryNotPublishedError("not yet"))
    orch, _, _ = _build({cid: fa})

    with caplog.at_level(logging.INFO, logger="teetime.core.orchestrator"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.NO_INVENTORY
    assert any(
        "no slots found" in r.getMessage() and "unpublished" in r.getMessage()
        for r in caplog.records
    ), "expected an inventory-unpublished give-up INFO line from _poll_for_slots"


async def test_run_falls_back_to_next_course_when_first_empty() -> None:
    c1 = CourseId("fake:c1")
    c2 = CourseId("fake:c2")
    fa1 = FakeAdapter(course_id=c1)
    fa1.set_search_response([])
    fa2 = FakeAdapter(course_id=c2)
    fa2.set_search_response([_slot(c2)])
    orch, _, _ = _build({c1: fa1, c2: fa2})

    result = await orch.run(_request(course_ids=(c1, c2)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == c2
    assert fa1.book_call_count == 0
    assert fa2.book_call_count == 1


async def test_run_retries_next_slot_when_first_is_gone() -> None:
    """If the best slot is taken (409), orchestrator tries the next candidate."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    slot1 = _slot(cid, hour=7)
    slot2 = _slot(cid, hour=8)
    fa.set_search_response([slot1, slot2])
    fa.set_book_side_effects([SlotGoneError("slot1 taken"), BookingOutcome.BOOKED])

    orch, _, _ = _build({cid: fa})
    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.book_call_count == 2


async def test_run_returns_no_inventory_when_all_slots_gone() -> None:
    """If every candidate slot for the only course is taken, the orchestrator records a
    NO_INVENTORY terminal (and notifies) rather than crashing the job with an uncaught
    SlotGoneError — slot-exhaustion is a graceful 'no bookable inventory', not a fatal."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid, hour=7), _slot(cid, hour=8)])
    fa.set_book_side_effects([SlotGoneError("slot1 taken"), SlotGoneError("slot2 taken")])

    orch, store, _ = _build({cid: fa})
    req = _request(course_ids=(cid,))
    result = await orch.run(req)

    assert result.outcome == BookingOutcome.NO_INVENTORY
    assert fa.book_call_count == 2  # both candidates were attempted
    # The terminal was recorded (a re-run short-circuits to the same NO_INVENTORY result).
    persisted = await store.get_terminal(req.request_id, req.target_dates[0])
    assert persisted is not None and persisted.outcome == BookingOutcome.NO_INVENTORY


async def test_run_falls_back_to_next_course_when_all_slots_gone() -> None:
    """All candidates gone on the first course must fall through to the next course
    (not crash) — the inter-course fallback now covers slot-exhaustion, not just empties."""
    c1 = CourseId("fake:c1")
    c2 = CourseId("fake:c2")
    fa1 = FakeAdapter(course_id=c1)
    fa1.set_search_response([_slot(c1, hour=7), _slot(c1, hour=8)])
    fa1.set_book_side_effects([SlotGoneError("c1 slot1 gone"), SlotGoneError("c1 slot2 gone")])
    fa2 = FakeAdapter(course_id=c2)
    fa2.set_search_response([_slot(c2)])
    orch, _, _ = _build({c1: fa1, c2: fa2})

    result = await orch.run(_request(course_ids=(c1, c2)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == c2
    assert fa1.book_call_count == 2  # both c1 candidates tried before falling through
    assert fa2.book_call_count == 1


async def test_run_falls_back_on_no_inventory_error() -> None:
    """Adapter that raises NoInventoryError is treated equivalent to empty list
    for fallback purposes."""
    c1 = CourseId("fake:c1")
    c2 = CourseId("fake:c2")
    fa1 = FakeAdapter(course_id=c1)
    fa1.set_search_to_raise(NoInventoryError("nada"))
    fa2 = FakeAdapter(course_id=c2)
    fa2.set_search_response([_slot(c2)])
    orch, _, _ = _build({c1: fa1, c2: fa2})

    result = await orch.run(_request(course_ids=(c1, c2)))
    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == c2


# --- Search/hedge errors exit clean, not crash (full-repo-scan PR C) ---


async def test_run_rate_limit_records_no_inventory_not_crash() -> None:
    """A 429 from search() at the drop must NOT escape run() as an uncaught crash with no
    terminal. Like an empty course, it records a clean NO_INVENTORY terminal (+ notifies)
    so the job exits cleanly instead of dying with a traceback and no record."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_to_raise(RateLimitError("429", retry_after_s=30))
    orch, store, _ = _build({cid: fa})

    req = _request(course_ids=(cid,))
    result = await orch.run(req)  # must not raise

    assert result.outcome == BookingOutcome.NO_INVENTORY
    assert fa.book_call_count == 0
    # Terminal was recorded (a re-run short-circuits to the same NO_INVENTORY result).
    persisted = await store.get_terminal(req.request_id, req.target_dates[0])
    assert persisted is not None and persisted.outcome == BookingOutcome.NO_INVENTORY


async def test_run_rate_limit_on_first_course_falls_back_to_next() -> None:
    """A 429 on the first course is a per-course skip, not a whole-run abort: the run
    still tries the fallback course and books it."""
    c1 = CourseId("fake:c1")
    c2 = CourseId("fake:c2")
    fa1 = FakeAdapter(course_id=c1)
    fa1.set_search_to_raise(RateLimitError("429", retry_after_s=30))
    fa2 = FakeAdapter(course_id=c2)
    fa2.set_search_response([_slot(c2)])
    orch, _, _ = _build({c1: fa1, c2: fa2})

    result = await orch.run(_request(course_ids=(c1, c2)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.course_id == c2


async def test_run_captcha_error_propagates_for_nonzero_exit() -> None:
    """CaptchaError is an OPERATOR-ACTION error (the solver is failing): it must still
    propagate out of run() so the booking job exits non-zero. It is NOT swallowed into a
    NO_INVENTORY terminal — that would hide a broken CAPTCHA pipeline behind a clean exit."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_to_raise(CaptchaError("captcha challenge"))
    orch, _, _ = _build({cid: fa})

    with pytest.raises(CaptchaError):
        await orch.run(_request(course_ids=(cid,)))


async def test_run_auth_error_propagates_for_nonzero_exit() -> None:
    """AuthError (credentials rejected) is operator-action too: it propagates out of run()
    for a non-zero exit rather than being masked as NO_INVENTORY."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_authenticate_side_effects([AuthError("bad creds")])
    orch, _, _ = _build({cid: fa})

    with pytest.raises(AuthError):
        await orch.run(_request(course_ids=(cid,)))


# --- Pre-book remote check (PLAN §9 layer 2) ---------------------------


async def test_run_short_circuits_when_existing_reservation_matches() -> None:
    """If list_reservations shows an existing reservation for the target date,
    orchestrator must NOT POST again. Outcome=ALREADY_BOOKED."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_existing_reservations(
        [
            ExistingReservation(
                course_id=cid,
                confirmation_code="EXISTING-1",
                tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
                party_size=1,
            )
        ]
    )
    fa.set_search_response([_slot(cid)])
    orch, _, _ = _build({cid: fa})

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.ALREADY_BOOKED
    assert result.confirmation_code == "EXISTING-1"
    assert fa.book_call_count == 0


async def test_run_short_circuits_when_4player_reservation_matches() -> None:
    """list_reservations guard fires for a 4-player request when an existing
    4-player reservation is found on the target date. Outcome=ALREADY_BOOKED."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    four_players = tuple(
        Player(first_name=f"G{i}", last_name="Player", email=f"g{i}@x.test") for i in range(4)
    )
    fa.set_existing_reservations(
        [
            ExistingReservation(
                course_id=cid,
                confirmation_code="EXISTING-4P",
                tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
                party_size=4,
            )
        ]
    )
    fa.set_search_response([_slot(cid)])
    orch, _, _ = _build({cid: fa})

    req = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 5, 13),),
        time_windows=(TimeWindow(earliest=time(7, 0), latest=time(9, 30)),),
        players=four_players,
        course_preferences=(cid,),
        dry_run=False,
    )
    result = await orch.run(req)

    assert result.outcome == BookingOutcome.ALREADY_BOOKED
    assert result.confirmation_code == "EXISTING-4P"
    assert fa.book_call_count == 0


async def test_run_proceeds_when_existing_reservation_party_size_differs() -> None:
    """A prior 2-player reservation does NOT match a new 4-player request —
    party_size must equal len(request.players) exactly (PLAN §9 layer 2).

    This documents the transition behavior: switching party size from 2→4
    means an existing 2-player booking for the same date will NOT short-circuit
    the new run. Operators should be aware of this when changing party size
    between production runs."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    four_players = tuple(
        Player(first_name=f"G{i}", last_name="Player", email=f"g{i}@x.test") for i in range(4)
    )
    fa.set_existing_reservations(
        [
            ExistingReservation(
                course_id=cid,
                confirmation_code="OLD-2P",
                tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
                party_size=2,  # old booking; does NOT match 4-player request
            )
        ]
    )
    fa.set_search_response([_slot(cid)])
    orch, _, _ = _build({cid: fa})

    req = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 5, 13),),
        time_windows=(TimeWindow(earliest=time(7, 0), latest=time(9, 30)),),
        players=four_players,
        course_preferences=(cid,),
        dry_run=False,
    )
    result = await orch.run(req)

    # Guard did NOT fire — booking proceeds normally.
    assert result.outcome == BookingOutcome.BOOKED
    assert fa.book_call_count == 1


# --- Concurrent run defense (PLAN §9 layer 5) -------------------------


async def test_run_concurrent_request_id_raises() -> None:
    """Two runs against the same RequestId on the same store must serialize
    via ConcurrentRunError — fail-fast, no waiting."""
    cid = CourseId("fake:course")
    rid = RequestId(uuid4())
    store = InMemoryStore()
    fa1 = FakeAdapter(course_id=cid)
    fa1.set_search_response([_slot(cid)])
    fa2 = FakeAdapter(course_id=cid)
    fa2.set_search_response([_slot(cid)])

    _, _, _ = _build({cid: fa1}, store=store)
    orch2, _, _ = _build({cid: fa2}, store=store)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_book() -> BookingResult:
        async with store.request_lock(rid):
            started.set()
            await release.wait()
            return BookingResult(
                request_id=rid,
                outcome=BookingOutcome.BOOKED,
                course_id=cid,
                slot=None,
                confirmation_code="SLOW",
                booked_at=datetime(2026, 5, 6, 10, 0, 1, tzinfo=UTC),
                attempts=1,
            )

    holder = asyncio.create_task(slow_book())
    await started.wait()
    with pytest.raises(ConcurrentRunError):
        await orch2.run(_request(request_id=rid, course_ids=(cid,)))
    release.set()
    await holder


# --- Notifier wiring ---------------------------------------------------


async def test_run_calls_notifier_with_terminal_result() -> None:
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    delivered: list[BookingResult] = []

    class RecordingNotifier:
        async def notify(self, result: BookingResult) -> None:
            delivered.append(result)

    orch = Orchestrator(
        adapters={cid: fa},
        store=InMemoryStore(),
        notifier=RecordingNotifier(),
        clock=FakeClock(start=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)),
        scheduler=_scheduler(),
        creds={cid: CourseCredentials(username="u", password="p")},
    )
    result = await orch.run(_request(course_ids=(cid,)))
    assert delivered == [result]


# --- Race window timing -----------------------------------------------


async def test_run_busy_waits_until_t0() -> None:
    """FakeClock starts at T0 - 2s, scheduler.early_arrival_ms=100. After run,
    fake_clock.now ≈ T0 - 100ms (i.e., busy_wait stopped early as configured)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=2))
    orch, _, _ = _build({cid: fa}, clock=clock)

    await orch.run(_request(course_ids=(cid,)))
    delta = (t0 - clock.now_utc()).total_seconds()
    # Should have stopped at ~early_arrival_ms before T0 (= 100ms),
    # within fine_step tolerance (1ms).
    assert -0.05 <= delta <= 0.15, f"clock landed {delta * 1000:.1f}ms before T0"


async def test_run_logs_race_complete_at_t0(caplog: pytest.LogCaptureFixture) -> None:
    """M6 PR6 verification surface: run() emits a 'race: busy-wait complete' INFO line
    when the busy-wait returns, so a dev dry-run log PROVES the bot fired at T0 (the
    final POST is suppressed under dry-run, so the log is the only evidence)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=2))
    orch, _, _ = _build({cid: fa}, clock=clock)

    with caplog.at_level(logging.INFO):
        await orch.run(_request(course_ids=(cid,)))

    race = [r.message for r in caplog.records if "race: busy-wait complete" in r.message]
    assert race, "race-complete verification log not emitted"
    assert "drift_ms" in race[0]  # firing-vs-target drift is logged for the runbook


# --- Race-path CAPTCHA pre-fetch (the 2026-06-07 fix) -------------------


def _scheduler_with_lead(
    lead_s: int, *, early_ms: int = 100, prefetch_count: int = 3
) -> SchedulerConfig:
    return SchedulerConfig(
        timezone="America/New_York",
        fire_time=time(6, 0, 0),
        early_arrival_ms=early_ms,
        poll_interval_ms=10,
        max_poll_seconds=1,
        captcha_prefetch_lead_s=lead_s,
        captcha_prefetch_count=prefetch_count,
    )


async def test_run_prefetches_captcha_before_t0_when_enabled() -> None:
    """On the race path (prefetch_book=True), the orchestrator must call prepare_book()
    DURING the busy-wait — ~lead seconds BEFORE T0 — so book() at T0 consumes a cached
    token. The 2026-06-07 prod failure was the ~78s CAPTCHA solve running AFTER T0, which
    pushed the booking POST ~100s past the drop and lost the race."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    lead = 30
    clock = FakeClock(start=t0 - timedelta(seconds=lead + 2))
    orch, _, _ = _build(
        {cid: fa},
        clock=clock,
        scheduler=_scheduler_with_lead(lead, prefetch_count=4),
        prefetch_book=True,
    )

    # Record the clock instant at which the (collaborator) prepare_book is invoked.
    prefetch_at: list[datetime] = []
    orig_prepare = fa.prepare_book

    async def _recording_prepare(slot: object, request: object, *, count: int = 1) -> None:
        prefetch_at.append(clock.now_utc())
        await orig_prepare(slot, request, count=count)  # type: ignore[arg-type]

    fa.prepare_book = _recording_prepare  # type: ignore[assignment]

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.prepare_book_call_count == 1, "captcha was not pre-fetched"
    # The race path must pre-solve scheduler.captcha_prefetch_count tokens (not 1).
    assert fa.last_prepare_count == 4, "orchestrator must forward scheduler.captcha_prefetch_count"
    assert fa.book_call_count == 1
    # The pre-fetch happened before T0 and roughly `lead` seconds early (within tolerance).
    assert len(prefetch_at) == 1
    before_t0 = (t0 - prefetch_at[0]).total_seconds()
    assert before_t0 > 0, "prefetch must run BEFORE T0"
    assert lead - 1 <= before_t0 <= lead + 1, f"prefetch fired {before_t0:.1f}s before T0"


async def test_run_does_not_prefetch_when_disabled() -> None:
    """Off the race path (prefetch_book=False, the default), the orchestrator must NOT
    pre-fetch the CAPTCHA. This is the watcher/every-10-min posture: a token is only
    solved if we are actually about to book (inside book()/the upgrade path)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=2))
    orch, _, _ = _build({cid: fa}, clock=clock)  # prefetch_book defaults to False

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.prepare_book_call_count == 0, "must not pre-fetch when prefetch_book is False"


async def test_run_threads_skip_initial_spacing_on_race_path() -> None:
    """Change D / PR3: the booking Orchestrator passes skip_initial_spacing into search(),
    True on the race path (prefetch_book=True) and False otherwise. This drops the leading
    250ms courtesy sleep ONLY for the first post-T0 search GET, where the burst leads with
    that GET and there is nothing to space from. The watcher (its own search call) never
    passes it, so its inter-date-check spacing is preserved."""

    async def _seen_skip_flag(*, prefetch_book: bool) -> list[bool]:
        cid = CourseId("fake:course")
        fa = FakeAdapter(course_id=cid)
        fa.set_search_response([_slot(cid)])

        t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
        lead = 30
        clock = FakeClock(start=t0 - timedelta(seconds=lead + 2))
        orch, _, _ = _build(
            {cid: fa},
            clock=clock,
            scheduler=_scheduler_with_lead(lead),
            prefetch_book=prefetch_book,
        )

        seen: list[bool] = []
        orig_search = fa.search

        async def _recording_search(
            request: object, *, skip_initial_spacing: bool = False
        ) -> object:
            seen.append(skip_initial_spacing)
            return await orig_search(request, skip_initial_spacing=skip_initial_spacing)  # type: ignore[arg-type]

        fa.search = _recording_search  # type: ignore[assignment]
        await orch.run(_request(course_ids=(cid,)))
        return seen

    race = await _seen_skip_flag(prefetch_book=True)
    assert race and all(race), "race path must pass skip_initial_spacing=True"

    off = await _seen_skip_flag(prefetch_book=False)
    assert off and not any(off), "non-race path must pass skip_initial_spacing=False"


async def test_run_prefetch_failure_does_not_abort_race() -> None:
    """If the pre-fetch (CAPTCHA solve) fails, the race must still proceed to book —
    degrading to solving the token inside book(). A pre-fetch hiccup must never cost the
    booking outright."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    fa.set_prepare_book_to_raise(RuntimeError("2captcha timeout"))

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=12))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(10), prefetch_book=True
    )

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.prepare_book_call_count == 1
    assert fa.book_call_count == 1


async def test_run_warns_when_prefetch_lead_cannot_be_honored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the run starts AFTER the (T0 - lead) prefetch point — e.g. the DST gate admits
    all of hour 5 but the ACA cron landed late — the full CAPTCHA-prefetch lead can't be
    honored and the book() POST may fire after T0 (the 2026-06-07 late-POST failure mode).
    The orchestrator must SURFACE this with a WARNING rather than silently appearing
    on-time, and must still prefetch immediately (overlapping the solve with whatever time
    remains beats solving inline in book())."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    lead = 90
    # Start only half a lead before T0 — i.e. already PAST (T0 - lead), so the full lead
    # cannot be honored.
    clock = FakeClock(start=t0 - timedelta(seconds=lead // 2))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(lead), prefetch_book=True
    )

    with caplog.at_level(logging.WARNING, logger="teetime.core.orchestrator"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.prepare_book_call_count == 1, "must still prefetch when landing late"
    assert any("prefetch lead not fully honored" in r.getMessage() for r in caplog.records), (
        "expected a WARNING that the CAPTCHA-prefetch lead could not be honored"
    )


async def test_run_does_not_warn_when_prefetch_lead_is_honored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The late-landing warning must NOT fire on the normal on-time race path (started
    comfortably before T0 - lead)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    lead = 30
    clock = FakeClock(start=t0 - timedelta(seconds=lead + 5))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(lead), prefetch_book=True
    )

    with caplog.at_level(logging.WARNING, logger="teetime.core.orchestrator"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert not any("prefetch lead not fully honored" in r.getMessage() for r in caplog.records)


# --- PR1: race-path login pre-warm + pre-T0 reservation guard (RACE_PREWARM_PLAN §3) ---


def _existing(cid: CourseId, *, party_size: int = 1, hour: int = 8) -> ExistingReservation:
    """An existing reservation that matches _request()'s date (2026-05-13) and,
    by default, its single-player party size — so _first_matching_reservation hits."""
    return ExistingReservation(
        course_id=cid,
        confirmation_code=f"SERVER-{hour}",
        tee_time=datetime(2026, 5, 13, hour, 0, tzinfo=UTC),
        party_size=party_size,
    )


async def test_run_prewarms_login_before_t0_when_prefetch_enabled() -> None:
    """On the race path the orchestrator authenticates DURING the busy-wait (before T0)
    and does NOT re-authenticate at T0. MF3: the count==1 proves the ORCHESTRATOR skipped
    the post-T0 authenticate via prewarmed_course_ids — FakeAdapter has no idempotency guard
    (it increments on every call), so a second call would make this 2."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    lead = 30
    clock = FakeClock(start=t0 - timedelta(seconds=lead + 2))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(lead), prefetch_book=True
    )

    auth_at: list[datetime] = []
    orig_auth = fa.authenticate

    async def _recording_auth(creds: object) -> None:
        auth_at.append(clock.now_utc())
        await orig_auth(creds)  # type: ignore[arg-type]

    fa.authenticate = _recording_auth  # type: ignore[assignment]

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.authenticate_call_count == 1, "orchestrator must not re-authenticate at T0"
    assert len(auth_at) == 1
    # Pre-warm fires at the prefetch point (~lead before T0), NOT merely at the
    # early-arrival busy-wait exit (~100ms before T0).
    before_t0 = (t0 - auth_at[0]).total_seconds()
    assert before_t0 >= lead - 1, (
        f"authenticate must pre-warm ~{lead}s before T0 (got {before_t0:.1f}s)"
    )
    assert fa.book_call_count == 1


async def test_run_does_not_prewarm_login_when_prefetch_disabled() -> None:
    """Off the race path, authenticate happens at/after T0 (no pre-warm)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=2))
    orch, _, _ = _build({cid: fa}, clock=clock)  # prefetch_book defaults False

    auth_at: list[datetime] = []
    orig_auth = fa.authenticate

    async def _recording_auth(creds: object) -> None:
        auth_at.append(clock.now_utc())
        await orig_auth(creds)  # type: ignore[arg-type]

    fa.authenticate = _recording_auth  # type: ignore[assignment]

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.authenticate_call_count == 1
    assert len(auth_at) == 1
    # Without pre-warm, auth fires at the busy-wait exit (~early_arrival before T0),
    # NOT lead-seconds early. Pin it to "near T0", which a pre-warm would violate.
    assert abs((auth_at[0] - t0).total_seconds()) < 2, "no pre-warm: auth must land near T0"


async def test_run_prewarm_login_failure_falls_back_to_inline_auth() -> None:
    """A pre-warm login failure must NOT cost the booking: course_id is not added to
    prewarmed_course_ids, so _run_course authenticates inline at T0 and the run books.
    authenticate is called twice (prewarm attempt + inline retry)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    fa.set_authenticate_side_effects([RuntimeError("prewarm login blip"), None])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=12))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(10), prefetch_book=True
    )

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.authenticate_call_count == 2
    assert fa.book_call_count == 1


async def test_run_prewarm_soft_login_failure_reauths_at_t0(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RACE_PREWARM_PLAN §3.1 SF#1: a SOFT login failure (authenticate() RETURNS but no
    session — ForeUP's 400/401/rejected-body swallow) must NOT mark the course prewarmed.
    Otherwise run() skips the T0 re-auth and book() raises AuthError on a never-logged-in
    session — a transient 401 silently loses the booking. The course must re-authenticate
    inline at T0 (authenticate called twice: soft-fail prewarm + inline retry)."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    fa.set_auth_soft_fail()  # authenticate() returns, but is_authenticated stays False

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=12))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(10), prefetch_book=True
    )

    with caplog.at_level(logging.WARNING, logger="teetime.core.orchestrator"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.authenticate_call_count == 2, "soft-fail prewarm must NOT suppress the T0 re-auth"
    assert fa.book_call_count == 1
    # Pin the diagnostic line so a refactor can't silently drop the SF#1 signal that
    # explains a soft-login-then-reauth in prod logs.
    assert "established no session" in caplog.text


async def test_run_short_circuits_already_booked_found_pre_t0(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If pre-warm finds a matching existing reservation, the orchestrator records
    ALREADY_BOOKED, emits the SF6 verification line, and returns WITHOUT busy-waiting
    to T0 or searching."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_existing_reservations([_existing(cid)])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    lead = 30
    clock = FakeClock(start=t0 - timedelta(seconds=lead + 2))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(lead), prefetch_book=True
    )

    with caplog.at_level(logging.INFO, logger="teetime.core.orchestrator"):
        result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.ALREADY_BOOKED
    assert fa.search_call_count == 0
    assert fa.book_call_count == 0
    # Did NOT wait all the way to T0.
    assert (t0 - clock.now_utc()).total_seconds() > 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("race: short-circuited pre-T0" in m for m in msgs), "SF6 verification line missing"
    assert not any("race: busy-wait complete" in m for m in msgs), "must not have waited to T0"


async def test_run_prewarm_does_not_short_circuit_on_nonmatching_reservation() -> None:
    """An existing reservation for a DIFFERENT party size does not match — race proceeds."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    fa.set_existing_reservations([_existing(cid, party_size=2)])  # _request has 1 player

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    lead = 30
    clock = FakeClock(start=t0 - timedelta(seconds=lead + 2))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(lead), prefetch_book=True
    )

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.book_call_count == 1


async def test_prewarm_outer_gather_isolates_login_failure() -> None:
    """MF2: the login leg raising must NOT cancel the concurrent prefetch leg
    (return_exceptions=True on the outer gather). prepare_book still ran; run books."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid)])
    fa.set_authenticate_side_effects([RuntimeError("prewarm login blip"), None])

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=12))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(10), prefetch_book=True
    )

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert fa.prepare_book_call_count == 1, "prefetch leg must not be cancelled by login failure"


async def test_prewarm_outer_gather_isolates_prefetch_failure() -> None:
    """MF2 mirror: the prefetch leg raising must NOT cancel the login prewarm — the
    match-driven short-circuit still fires."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_existing_reservations([_existing(cid)])
    fa.set_prepare_book_to_raise(RuntimeError("2captcha timeout"))

    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=12))
    orch, _, _ = _build(
        {cid: fa}, clock=clock, scheduler=_scheduler_with_lead(10), prefetch_book=True
    )

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.ALREADY_BOOKED
    assert fa.book_call_count == 0
