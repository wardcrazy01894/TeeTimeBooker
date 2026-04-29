"""Wall-clock helpers built around an injectable Clock so tests can fast-forward.

Why a Protocol instead of just `time.time()`: the 6:00 AM ET race is the heart of
this bot. We must be able to test "fire 250 ms before T0, busy-wait, retry on
empty inventory" without waiting 7 days. Production uses RealClock; tests use
FakeClock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Minimal time interface. Everything else (windows, deadlines) is built on this."""

    def now_utc(self) -> datetime:
        """Current time as tz-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Async sleep. Real impl: asyncio.sleep. Fake: advances simulated time."""
        ...


class RealClock:
    """Production clock backed by `datetime.now(UTC)` and `asyncio.sleep`. Stub."""

    def now_utc(self) -> datetime:
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


async def busy_wait_until(
    target_utc: datetime,
    clock: Clock,
    *,
    coarse_threshold_s: float = 2.0,
    coarse_step_s: float = 0.5,
    fine_step_s: float = 0.001,
    fine_accuracy_s: float = 0.05,
) -> None:
    """Sleep coarsely until ~`coarse_threshold_s` before target, then short-sleep
    `fine_step_s` (default 1 ms) per iteration until target — yielding the event
    loop each iteration so a hot Python loop can't starve the runner.

    Test contract: with a FakeClock, the wall-clock returned from `clock.now_utc()`
    on exit is within `fine_accuracy_s` of `target_utc`. `fine_accuracy_s` is the
    desired ACCURACY of the wakeup; `fine_step_s` is the loop CADENCE. Keeping
    them distinct prevents the conflation flagged in v0 review item 8.

    Implementation outline (M1.T1):
        while True:
            delta = (target_utc - clock.now_utc()).total_seconds()
            if delta <= 0:
                return
            if delta > coarse_threshold_s:
                await clock.sleep(min(coarse_step_s, delta - coarse_threshold_s))
            else:
                await clock.sleep(fine_step_s)
    """
    raise NotImplementedError
