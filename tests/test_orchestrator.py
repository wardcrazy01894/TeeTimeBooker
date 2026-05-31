"""M2.T1 happy path + idempotency + course fallback tests for Orchestrator.

Full §9.1 state machine (UNCERTAIN → RECONCILING → BOOKED/LOST) is M2.T3.
This file pins the v0 happy + idempotent + dry-run + course-fallback +
already-booked paths.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core.adapter import NoInventoryError, SlotGoneError
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
) -> tuple[Orchestrator, InMemoryStore, FakeClock]:
    store = store or InMemoryStore()
    clock = clock or FakeClock(start=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC))
    creds = {cid: CourseCredentials(username="u", password="p") for cid in adapters}
    orch = Orchestrator(
        adapters=adapters,
        store=store,
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=_scheduler(),
        creds=creds,
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
    """If every candidate slot is taken, orchestrator raises the last SlotGoneError."""
    cid = CourseId("fake:course")
    fa = FakeAdapter(course_id=cid)
    fa.set_search_response([_slot(cid, hour=7), _slot(cid, hour=8)])
    fa.set_book_side_effects([SlotGoneError("slot1 taken"), SlotGoneError("slot2 taken")])

    orch, _, _ = _build({cid: fa})
    with pytest.raises(SlotGoneError):
        await orch.run(_request(course_ids=(cid,)))


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
