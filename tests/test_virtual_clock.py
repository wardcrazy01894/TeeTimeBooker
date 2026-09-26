"""MULTIUSER_PLAN §4.4 proof 3 / MU-9a0: ``dev.virtual_clock.VirtualClock``.

``FakeClock.sleep`` advances ONE shared ``_now`` by each caller's delta, so N concurrent
sleepers (N busy-waits, N staggered blind bursts) run simulated time ~N-times fast and the
measured send offsets are scrambled. ``VirtualClock`` is a discrete-event scheduler: a sleeper
parks on its deadline and time jumps to the EARLIEST pending deadline only once every runnable
task is blocked, so each sleeper wakes at exactly its own instant regardless of how many others
are in flight.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from teetime.core.clock import Clock, FakeClock, busy_wait_until
from teetime.dev.virtual_clock import VirtualClock

START = datetime(2026, 5, 6, 9, 59, 0, tzinfo=UTC)


def test_virtual_clock_is_clock() -> None:
    """Structural contract: the orchestrator takes a ``Clock``; VirtualClock must satisfy it."""
    assert isinstance(VirtualClock(start=START), Clock)


def test_virtual_clock_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        VirtualClock(start=datetime(2026, 5, 6, 9, 59, 0))


def test_virtual_clock_normalises_start_to_utc() -> None:
    local = datetime(2026, 5, 6, 5, 59, 0, tzinfo=ZoneInfo("America/New_York"))
    vc = VirtualClock(start=local)
    assert vc.now_utc() == START
    assert vc.now_utc().tzinfo == UTC


async def test_virtual_clock_two_sleepers_wake_in_deadline_order() -> None:
    """A registered-first LONG sleeper must not wake before a registered-second SHORT one,
    and each wakes at its OWN deadline (FakeClock would report START+7 for both)."""
    vc = VirtualClock(start=START)
    wakes: list[tuple[str, datetime]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await vc.sleep(seconds)
        wakes.append((name, vc.now_utc()))

    await asyncio.gather(sleeper("long", 5), sleeper("short", 2))

    assert wakes == [
        ("short", START + timedelta(seconds=2)),
        ("long", START + timedelta(seconds=5)),
    ]
    assert vc.now_utc() == START + timedelta(seconds=5)


async def test_virtual_clock_wakes_earliest_deadline_only() -> None:
    """The plan's named test (§12 MU-9a0): when time jumps, ONLY the earliest sleeper wakes;
    a later one stays parked until its own deadline. Observed by sampling the clock from the
    woken task before the other has run."""
    vc = VirtualClock(start=START)
    seen_by_early: list[datetime] = []
    late_done = asyncio.Event()

    async def early() -> None:
        await vc.sleep(1)
        # At this instant the 3 s sleeper must still be parked.
        assert not late_done.is_set()
        seen_by_early.append(vc.now_utc())

    async def late() -> None:
        await vc.sleep(3)
        late_done.set()

    await asyncio.gather(early(), late())
    assert seen_by_early == [START + timedelta(seconds=1)]
    assert late_done.is_set()


async def test_virtual_clock_ties_wake_in_registration_order() -> None:
    vc = VirtualClock(start=START)
    order: list[str] = []

    async def sleeper(name: str) -> None:
        await vc.sleep(2)
        order.append(name)

    # Sequential registration: a, then b, then c — each task starts in creation order.
    await asyncio.gather(sleeper("a"), sleeper("b"), sleeper("c"))
    assert order == ["a", "b", "c"]
    assert vc.now_utc() == START + timedelta(seconds=2)


async def test_virtual_clock_n_concurrent_busy_waits_measure_correct_offsets() -> None:
    """Proof obligation 3 (MULTIUSER_PLAN §4.4): three concurrent waiters targeting
    T0-500 / T0-250 / T0+0 ms must each observe their wake at EXACTLY that offset, both for
    the ``busy_wait_until`` loop (the pre-T0 wait) and for the stagger's single-read
    ``sleep(delay)`` (``_fire_blind_post`` reads ``now_utc()`` ONCE, then sleeps the
    difference). Under FakeClock the three single-read sleeps stack onto one shared ``_now``
    (-500, then +29.75 s on top, then +30 s on top of THAT), so the measured offsets are
    scrambled; that contrast is pinned so this test cannot pass vacuously.
    """
    t0 = START + timedelta(seconds=30)
    offsets_ms = (-500, -250, 0)

    async def measure(clock: Clock, *, single_read: bool) -> list[int]:
        measured: list[int | None] = [None] * len(offsets_ms)

        async def wait(i: int, off: int) -> None:
            target = t0 + timedelta(milliseconds=off)
            if single_read:
                await clock.sleep((target - clock.now_utc()).total_seconds())
            else:
                await busy_wait_until(target, clock)
            measured[i] = round((clock.now_utc() - t0).total_seconds() * 1000)

        await asyncio.gather(*(wait(i, off) for i, off in enumerate(offsets_ms)))
        assert all(m is not None for m in measured)
        return [m for m in measured if m is not None]

    assert await measure(VirtualClock(start=START), single_read=False) == list(offsets_ms)
    assert await measure(VirtualClock(start=START), single_read=True) == list(offsets_ms)
    # Non-vacuity: the shared-``_now`` FakeClock gets the stagger pattern WRONG.
    assert await measure(FakeClock(start=START), single_read=True) != list(offsets_ms)


async def test_virtual_clock_no_sleeper_advance_is_explicit() -> None:
    """With no sleeper registered, time NEVER moves on its own — yielding to the loop does
    not tick it. Only an explicit ``advance``/``run_until`` (or a sleeper) moves it."""
    vc = VirtualClock(start=START)
    for _ in range(5):
        await asyncio.sleep(0)
    assert vc.now_utc() == START

    await vc.advance(3)
    assert vc.now_utc() == START + timedelta(seconds=3)

    await vc.run_until(START + timedelta(seconds=10))
    assert vc.now_utc() == START + timedelta(seconds=10)

    # A clock never runs backwards.
    with pytest.raises(ValueError, match="backwards"):
        await vc.run_until(START)


async def test_virtual_clock_run_until_wakes_intermediate_sleepers_in_order() -> None:
    """``run_until(t)`` drains every sleeper with a deadline <= t, in deadline order, and
    returns with the clock AT t."""
    vc = VirtualClock(start=START)
    wakes: list[datetime] = []

    async def sleeper(seconds: float) -> None:
        await vc.sleep(seconds)
        wakes.append(vc.now_utc())

    tasks = [asyncio.create_task(sleeper(s)) for s in (4, 1, 20)]
    await vc.run_until(START + timedelta(seconds=10))

    assert wakes == [START + timedelta(seconds=1), START + timedelta(seconds=4)]
    assert vc.now_utc() == START + timedelta(seconds=10)
    assert not tasks[2].done()  # the 20 s sleeper is still parked
    await vc.run_until(START + timedelta(seconds=20))
    await asyncio.gather(*tasks)
    assert wakes[-1] == START + timedelta(seconds=20)


async def test_virtual_clock_sleep_zero_yields_without_advancing() -> None:
    vc = VirtualClock(start=START)
    await vc.sleep(0)
    assert vc.now_utc() == START
    await vc.sleep(-1)  # never runs backwards; a non-positive sleep is a plain yield
    assert vc.now_utc() == START


async def test_virtual_clock_cancelled_sleeper_does_not_advance_time() -> None:
    """A parked task that is cancelled must not drag the clock to its deadline when a
    later-deadline sleeper is the next to wake."""
    vc = VirtualClock(start=START)

    async def parked() -> None:
        await vc.sleep(1)

    task = asyncio.create_task(parked())
    await asyncio.sleep(0)  # let it register
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await vc.sleep(5)
    assert vc.now_utc() == START + timedelta(seconds=5)


async def test_virtual_clock_counts_sleeps() -> None:
    """Parity with ``FakeClock.sleep_count`` so busy-wait split assertions can port over."""
    vc = VirtualClock(start=START)
    await vc.sleep(1)
    await vc.sleep(1)
    assert vc.sleep_count == 2
