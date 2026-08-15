"""STAGGER_PLAN.md: the T0 blind-POST burst fires each POST at its own offset
relative to T0 instead of firing the whole burst at one instant.

Why this exists: every blind-POST drop in the retention window came back 3/3 or 0/3,
never mixed. A genuine slot race cannot produce that — our POSTs land within ~100 ms of
the window opening. A single simultaneous burst is a POINT SAMPLE of ForeUP's release
flip: if it lands before the flip, every POST gets the same
``400 {"success":false,"msg":"Time not available."}`` that a claimed slot returns, and the
1-second-resolution server ``Date`` header cannot tell the two apart.

Staggering makes the outcome pattern ORDERED BY OFFSET, so one drop distinguishes a
pre-open boundary rejection (clean cutoff) from a real race (unordered) — and guarantees
at least one POST is sent no earlier than T0.

The rank-0 (best) slot keeps today's exact fire instant, so a drop we currently win is
unchanged (STAGGER_PLAN §2.1).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from teetime.core.clock import Clock, FakeClock
from teetime.core.config import SchedulerConfig
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    CourseCredentials,
    CourseId,
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

TARGET = date(2026, 5, 13)
WINDOW = TimeWindow(earliest=time(7, 0), latest=time(9, 30))


class RecordingClock:
    """A Clock that records every requested sleep WITHOUT advancing time.

    Not advancing is the point: ``_fire_blind_post`` computes its delay from a single
    ``now_utc()`` reading, so a frozen clock makes each task's computed delay exact and
    independent of coroutine scheduling order. ``FakeClock`` advances additively, which
    would make three CONCURRENT stagger sleeps interfere and the assertion meaningless.

    Never drive ``busy_wait_until`` with this — it would spin forever.
    """

    def __init__(self, *, start: datetime) -> None:
        self._now = start.astimezone(UTC)
        self.sleeps: list[float] = []

    def now_utc(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _request(*, course_ids: tuple[CourseId, ...]) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET,),
        time_windows=(WINDOW,),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=course_ids,
        dry_run=False,
    )


def _slot(cid: CourseId, hour: int, minute: int) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=cid,
        slot_id=SlotId(f"s-{hour:02d}{minute:02d}"),
        tee_time=datetime(2026, 5, 13, hour, minute, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def _build(
    *,
    clock: Clock,
    stagger: tuple[int, ...] = (-500, -250, 0),
    early_arrival_ms: int = 500,
) -> tuple[Orchestrator, FakeAdapter, CourseId]:
    cid = CourseId("fake:mb")
    fa = FakeAdapter(course_id=cid, supports_blind_post=True)
    sched = SchedulerConfig(
        timezone="America/New_York",
        fire_time=time(6, 0, 0),
        early_arrival_ms=early_arrival_ms,
        blind_post_stagger_ms=stagger,
    )
    orch = Orchestrator(
        adapters={cid: fa},
        store=InMemoryStore(),
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=sched,
        creds={cid: CourseCredentials(username="u", password="p")},
        prefetch_book=True,
    )
    return orch, fa, cid


# --- config -------------------------------------------------------------


def test_stagger_default_spans_the_t0_boundary() -> None:
    """The default keeps the rank-0 POST at today's instant (-early_arrival_ms) and SENDS
    at least one POST no earlier than T0 — the hedge that makes a 0/3 wipeout impossible
    under the boundary hypothesis.

    `>= 0`, not `> 0`: the shipped tail offset is exactly 0, i.e. sent AT 06:00:00.000.
    Network latency (~50-150 ms) carries it across the boundary on ARRIVAL, which is what
    actually matters — so 0 is the tightest possible post-open probe, losing the least
    ground in a genuine race. Nothing may be scheduled EARLIER than the rank-0 offset.
    """
    sched = SchedulerConfig()
    assert sched.blind_post_stagger_ms[0] == -sched.early_arrival_ms
    assert min(sched.blind_post_stagger_ms) == -sched.early_arrival_ms
    assert max(sched.blind_post_stagger_ms) >= 0


def test_stagger_empty_tuple_is_the_legacy_escape_hatch() -> None:
    assert SchedulerConfig(blind_post_stagger_ms=()).blind_post_stagger_ms == ()


def test_stagger_rejects_a_descending_offset() -> None:
    """Offsets pair with RANK-ordered slots, so a descending list POSTs a WORSE slot before
    a better one — ForeUP's 1-reservation-per-day rule then rejects the better sibling and
    we silently keep the worse tee time (`_keep_best` can only rank what booked).

    `(-500, 0, -250)` is the dangerous shape precisely because it satisfies every
    config-parity assertion: `stagger[0] == -early_arrival_ms`, `min == -early_arrival_ms`,
    `max >= 0`. Only a monotonicity check catches it.
    """
    with pytest.raises(ValidationError, match="non-decreasing"):
        SchedulerConfig(blind_post_stagger_ms=(-500, 0, -250))


def test_stagger_allows_equal_adjacent_offsets() -> None:
    """Non-DECREASING, not strictly increasing: repeated offsets are how the tail degrades
    to simultaneous firing when there are more slots than configured offsets."""
    assert SchedulerConfig(blind_post_stagger_ms=(-500, 0, 0)).blind_post_stagger_ms == (
        -500,
        0,
        0,
    )


def test_offset_before_the_wakeup_is_clamped_to_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An offset earlier than ``-early_arrival_ms`` cannot be honoured — the busy-wait has
    not woken us. It is clamped to the wakeup and the EFFECTIVE offsets are what get
    logged, so the offset→outcome diagnostic never claims a POST went out earlier than it
    did."""
    orch, _, _ = _build(
        clock=FakeClock(start=datetime(2026, 5, 13, tzinfo=UTC)),
        stagger=(-900, -250, 250),
        early_arrival_ms=500,
    )
    with caplog.at_level(logging.WARNING):
        assert orch._stagger_offsets_for(3) == [-500, -250, 250]
    assert "clamped" in caplog.text


def test_offsets_within_the_wakeup_are_not_clamped_or_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    orch, _, _ = _build(
        clock=FakeClock(start=datetime(2026, 5, 13, tzinfo=UTC)),
        stagger=(-500, 250),
        early_arrival_ms=500,
    )
    with caplog.at_level(logging.WARNING):
        assert orch._stagger_offsets_for(2) == [-500, 250]
    assert "clamped" not in caplog.text


# --- offset pairing -----------------------------------------------------


def test_offsets_pair_positionally_with_ranked_slots() -> None:
    orch, _, _ = _build(clock=FakeClock(start=datetime(2026, 5, 13, tzinfo=UTC)))
    assert orch._stagger_offsets_for(3) == [-500, -250, 0]


def test_surplus_slots_reuse_the_last_offset() -> None:
    """A widened blind_post_max_count must not silently drop POSTs — the tail degrades to
    today's simultaneous behaviour instead."""
    orch, _, _ = _build(clock=FakeClock(start=datetime(2026, 5, 13, tzinfo=UTC)))
    assert orch._stagger_offsets_for(5) == [-500, -250, 0, 0, 0]


def test_fewer_slots_than_offsets_takes_the_leading_offsets() -> None:
    orch, _, _ = _build(clock=FakeClock(start=datetime(2026, 5, 13, tzinfo=UTC)))
    assert orch._stagger_offsets_for(2) == [-500, -250]


def test_empty_stagger_fires_every_post_immediately() -> None:
    """Legacy path: offset 0 relative to the busy-wait wakeup means no sleep at all."""
    orch, _, _ = _build(
        clock=FakeClock(start=datetime(2026, 5, 13, tzinfo=UTC)),
        stagger=(),
        early_arrival_ms=500,
    )
    assert orch._stagger_offsets_for(3) == [-500, -500, -500]


# --- per-POST firing ----------------------------------------------------


async def test_fire_blind_post_sleeps_to_its_target_instant() -> None:
    """A POST scheduled for T0+250ms, issued at the busy-wait wakeup (T0-500ms),
    sleeps exactly 750 ms before POSTing."""
    t0 = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    clock = RecordingClock(start=t0 - timedelta(milliseconds=500))
    orch, fa, cid = _build(clock=clock)

    result = await orch._fire_blind_post(
        fa, _slot(cid, 8, 15), _request(course_ids=(cid,)), offset_ms=250, t0=t0
    )

    assert clock.sleeps == [pytest.approx(0.75)]
    assert result.outcome == BookingOutcome.BOOKED
    assert fa.book_call_count == 1


async def test_fire_blind_post_does_not_sleep_when_its_instant_has_passed() -> None:
    """A late-landing cron can start past an offset. It must POST IMMEDIATELY rather than
    wait (a negative delay must never become a sleep)."""
    t0 = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    clock = RecordingClock(start=t0 + timedelta(seconds=3))
    orch, fa, cid = _build(clock=clock)

    await orch._fire_blind_post(
        fa, _slot(cid, 8, 15), _request(course_ids=(cid,)), offset_ms=-500, t0=t0
    )

    assert clock.sleeps == []
    assert fa.book_call_count == 1


async def test_fire_blind_post_at_the_wakeup_offset_does_not_sleep() -> None:
    """The rank-0 POST keeps today's timing: offset == -early_arrival_ms means it fires
    the instant the busy-wait returns, with no added latency (STAGGER_PLAN §2.1)."""
    t0 = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    clock = RecordingClock(start=t0 - timedelta(milliseconds=500))
    orch, fa, cid = _build(clock=clock)

    await orch._fire_blind_post(
        fa, _slot(cid, 8, 15), _request(course_ids=(cid,)), offset_ms=-500, t0=t0
    )

    assert clock.sleeps == []


# --- burst integration --------------------------------------------------


async def test_burst_books_best_slot_and_fires_in_rank_order() -> None:
    """The staggered burst still keeps the rank-0 slot, and POSTs go out in RANK order —
    so ForeUP's 1-reservation-per-day rule can only ever reject a WORSE sibling
    (STAGGER_PLAN §2.2)."""
    t0 = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0 - timedelta(seconds=200))
    orch, fa, cid = _build(clock=clock)
    # 08:15 is the window midpoint → rank-0.
    fa.set_blind_slots([_slot(cid, 8, 0), _slot(cid, 8, 15), _slot(cid, 8, 30)])

    result = await orch.run(_request(course_ids=(cid,)))

    assert result.outcome == BookingOutcome.BOOKED
    assert result.slot is not None
    assert result.slot.slot_id == "s-0815"
    assert fa.book_call_count == 3
    assert fa.book_slot_ids[0] == "s-0815"  # best slot POSTs first


class BurstRecordingClock:
    """Frozen clock that records sleeps PER CONCURRENT TASK.

    ``asyncio.current_task()`` keys the recording, so three concurrent burst tasks cannot
    smear into one list the way a shared counter would. Time never advances, so each task's
    delay is computed from one reading — deterministic regardless of scheduling order.
    """

    def __init__(self, *, start: datetime) -> None:
        self._now = start.astimezone(UTC)
        self.by_task: dict[object, list[float]] = {}

    def now_utc(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.by_task.setdefault(asyncio.current_task(), []).append(seconds)


async def test_burst_wires_each_offset_to_its_own_post() -> None:
    """The COMPOSITION of _stagger_offsets_for + _fire_blind_post inside _blind_post_course.

    Both are well covered in isolation, but nothing pinned the wiring between them: the
    burst would still book, count 3 POSTs, and POST the best slot first if it passed
    ``offsets[0]`` to all three tasks (i.e. no stagger at all) or if the offset↔slot zip
    were transposed. This asserts the three tasks sleep 0 / 250 / 500 ms — the shipped
    ``(-500, -250, 0)`` ladder measured from the busy-wait wakeup at T0-500ms.
    """
    t0 = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    clock = BurstRecordingClock(start=t0 - timedelta(milliseconds=500))
    orch, fa, cid = _build(clock=clock)
    fa.set_blind_slots([_slot(cid, 8, 0), _slot(cid, 8, 15), _slot(cid, 8, 30)])

    await orch._blind_post_course(fa, cid, _request(course_ids=(cid,)))

    # One entry per task that slept; the rank-0 POST fires immediately so it never sleeps.
    slept = sorted(s for sleeps in clock.by_task.values() for s in sleeps)
    assert slept == [pytest.approx(0.25), pytest.approx(0.5)]
    assert fa.book_slot_ids == ["s-0815", "s-0800", "s-0830"]  # rank order: 08:15 midpoint


async def test_burst_diagnostic_reports_the_measured_send_offset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A LATE-STARTING run must not claim POSTs went out on the planned ladder.

    If the cron lands past T0 every delay is non-positive and all three POSTs go out
    SIMULTANEOUSLY. Logging the planned offsets would show outcomes spread across three
    instants that never happened — an operator reading a 0/3 would conclude "unordered, so
    not the boundary" and aim next week's fix at the wrong thing. The line must report the
    MEASURED send offset, with the planned one alongside so the late start is visible.
    """
    t0 = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    clock = BurstRecordingClock(start=t0 + timedelta(seconds=3))
    orch, fa, cid = _build(clock=clock)
    fa.set_blind_slots([_slot(cid, 8, 0), _slot(cid, 8, 15), _slot(cid, 8, 30)])

    with caplog.at_level(logging.INFO):
        await orch._blind_post_course(fa, cid, _request(course_ids=(cid,)))

    assert not clock.by_task, "a run starting past T0 must not sleep at all"
    # All three actually went out at T0+3000ms; none of the planned offsets happened.
    assert caplog.text.count("sent +3000ms") == 3
    assert "sent -500ms" not in caplog.text
    assert "sent -250ms" not in caplog.text
    # The planned ladder is still reported alongside, so the late start is diagnosable.
    assert "(planned -500ms)" in caplog.text
