"""Discrete-event virtual clock for multi-account timing tests (MULTIUSER_PLAN §4.4, round-1 SF4).

``FakeClock.sleep`` advances ONE shared ``_now`` by each caller's delta (``core/clock.py``), so with
N concurrent sleepers (N busy-waits, N staggered bursts) simulated time runs ~N-times fast and the
measured send offsets get scrambled. ``VirtualClock`` keeps a heap of (deadline, waiter). A sleeper
registers ``now + seconds`` and parks. When every task is parked, the driver advances ``now`` to the
EARLIEST deadline and wakes only that waiter (ties wake in registration order). ``FakeClock`` is
unchanged; single-account tests keep using it.

**How "every task is parked" is detected.** asyncio has no public quiescence hook (trio's
``wait_all_tasks_blocked`` has no counterpart), so the driver is a ``call_soon`` callback that
re-schedules itself while the loop's ready queue still holds other handles and advances time only
when it finds itself alone there. That reads ``loop._ready`` — a CPython ``BaseEventLoop`` detail
that has been stable since 3.4 and is the same deque ``_run_once`` drains. An event loop without it
(uvloop) is refused at the first ``sleep`` rather than silently mis-timed. Real timers
(``asyncio.sleep``, ``wait_for`` timeouts) are NOT coordinated with virtual time: every wait in a
VirtualClock-driven test must go through the clock, which is exactly the "Clock is injectable
everywhere" invariant.

Tests only — nothing on the production path imports this module.
"""

from __future__ import annotations

import asyncio
import heapq
from datetime import UTC, datetime, timedelta


class VirtualClock:
    """Satisfies the ``core.clock.Clock`` Protocol (``now_utc`` + ``sleep``)."""

    def __init__(self, *, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("VirtualClock start must be tz-aware")
        self._now = start.astimezone(UTC)
        # (deadline, registration seq, waiter). seq breaks deadline ties in registration order
        # and keeps the heap total-ordered without ever comparing futures.
        self._heap: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = 0
        self._tick_handle: asyncio.Handle | None = None
        self.sleep_count: int = 0

    def now_utc(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Park until virtual time reaches ``now + seconds``; never advances time for others.

        A non-positive ``seconds`` is a plain yield (deadline = now): the clock never runs
        backwards, mirroring ``busy_wait_until``'s exit contract.
        """
        self.sleep_count += 1
        loop = asyncio.get_running_loop()
        if not hasattr(loop, "_ready"):
            raise RuntimeError(
                "VirtualClock needs the pure-Python asyncio event loop (it reads loop._ready to "
                "detect quiescence); this loop has no _ready queue"
            )
        deadline = self._now + timedelta(seconds=max(seconds, 0.0))
        waiter: asyncio.Future[None] = loop.create_future()
        self._seq += 1
        heapq.heappush(self._heap, (deadline, self._seq, waiter))
        self._schedule_tick(loop)
        await waiter

    async def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``, waking every sleeper due on the way in
        deadline order. Returns with the clock exactly at ``now + seconds``."""
        await self.run_until(self._now + timedelta(seconds=seconds))

    async def run_until(self, target: datetime) -> None:
        """Move the clock to ``target``, waking every sleeper with a deadline <= ``target`` in
        deadline order first. Returns with the clock exactly at ``target``. Raises
        ``ValueError`` for a target in the past — a clock never runs backwards."""
        if target.tzinfo is None:
            raise ValueError("VirtualClock run_until target must be tz-aware")
        target = target.astimezone(UTC)
        if target < self._now:
            raise ValueError(
                f"VirtualClock cannot run backwards: now={self._now.isoformat()} "
                f"target={target.isoformat()}"
            )
        # Parking on ``target`` is exactly the required semantics: the driver wakes every
        # earlier deadline (and same-deadline sleepers registered before this one) first.
        await self.sleep((target - self._now).total_seconds())

    # --- driver ---------------------------------------------------------

    def _schedule_tick(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._tick_handle is None:
            self._tick_handle = loop.call_soon(self._tick, loop)

    def _tick(self, loop: asyncio.AbstractEventLoop) -> None:
        """Advance to the earliest deadline iff the loop is otherwise idle.

        ``_run_once`` pops a handle BEFORE running it, so when this callback runs,
        ``loop._ready`` holds exactly the OTHER handles still runnable this iteration plus
        anything scheduled during it. Non-empty means some task may yet register a sleeper
        against the CURRENT time, so time must not move: bounce and look again. Empty means
        every task is parked (on this clock, or on a future only a parked task can resolve),
        which is the discrete-event condition for jumping.
        """
        self._tick_handle = None
        if not self._heap:
            return
        ready: object = loop._ready  # type: ignore[attr-defined]  # see module docstring
        if len(ready) > 0:  # type: ignore[arg-type]
            self._schedule_tick(loop)
            return
        deadline, _, waiter = heapq.heappop(self._heap)
        if not waiter.cancelled():
            # Never move backwards (a sleeper registered against an older ``now`` after an
            # explicit advance could otherwise appear to do so).
            self._now = max(self._now, deadline)
            waiter.set_result(None)
        # The woken task may register a new sleeper (which re-schedules the tick itself), or
        # finish. Either way any remaining sleeper still needs a driver, so re-arm now; the
        # re-armed tick bounces while the woken task's step is pending in the ready queue.
        if self._heap:
            self._schedule_tick(loop)
