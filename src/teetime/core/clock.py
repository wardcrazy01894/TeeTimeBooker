"""Wall-clock helpers built around an injectable Clock so tests can fast-forward.

Why a Protocol instead of just `time.time()`: the 6:00 AM ET race is the heart of
this bot. We must be able to test "fire 250 ms before T0, busy-wait, retry on
empty inventory" without waiting 7 days. Production uses RealClock; tests use
FakeClock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import ntplib

_log = logging.getLogger(__name__)


@runtime_checkable
class Clock(Protocol):
    """Minimal time interface. Everything else (windows, deadlines) is built on this."""

    def now_utc(self) -> datetime:
        """Current time as tz-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Async sleep. Real impl: asyncio.sleep. Fake: advances simulated time."""
        ...


class RealClock:
    """Production clock backed by `datetime.now(UTC)` and `asyncio.sleep`.

    An optional `offset` (a measured NTP correction; see `measure_ntp_offset`)
    is added to every `now_utc()` reading so the T0 busy-wait targets true time
    rather than a drifted system clock. Defaults to zero — unchanged behaviour.
    """

    def __init__(self, *, offset: timedelta = timedelta(0)) -> None:
        self._offset = offset

    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC) + self._offset

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def measure_ntp_offset(
    server: str = "pool.ntp.org",
    *,
    timeout: float = 2.0,
    _client_factory: Callable[[], Any] | None = None,
) -> timedelta:
    """Best-effort one-shot NTP offset (server_time minus local_time) as a timedelta.

    The race against a 06:00:00 booking-window open can be lost to a system clock
    that drifts even a second; a one-time NTP correction at startup closes that gap.
    This is an OPTIMISATION, never a hard dependency: any failure — blocked UDP:123
    (common in locked-down cloud egress), DNS error, or timeout — degrades to a
    zero offset and logs a warning, never raises. Call once at startup, before the
    busy-wait; pass the result to `RealClock(offset=...)`.
    """
    factory = _client_factory if _client_factory is not None else ntplib.NTPClient
    try:
        response = factory().request(server, version=3, timeout=timeout)
        return timedelta(seconds=float(response.offset))
    except Exception as exc:
        _log.warning("NTP offset measurement failed (%s); using zero offset", exc)
        return timedelta(0)


class FakeClock:
    """Controllable Clock for deterministic tests.

    `now_utc()` returns the internal `_now`; `sleep(s)` advances it by `s`
    simulated seconds while awaiting `asyncio.sleep(0)` so other coroutines
    on the loop are not starved. `sleep_count` lets tests assert on the
    fine/coarse split in `busy_wait_until` without sampling timestamps.
    """

    def __init__(self, *, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be tz-aware")
        self._now = start.astimezone(UTC)
        self.sleep_count: int = 0

    def now_utc(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleep_count += 1
        self._now = self._now + timedelta(seconds=seconds)
        await asyncio.sleep(0)


async def busy_wait_until(
    target_utc: datetime,
    clock: Clock,
    *,
    coarse_threshold_s: float = 2.0,
    coarse_step_s: float = 0.5,
    fine_step_s: float = 0.001,
) -> None:
    """Sleep coarsely until ~`coarse_threshold_s` before target, then short-sleep
    `fine_step_s` (default 1 ms) per iteration until target — yielding the event
    loop each iteration so a hot Python loop can't starve the runner.

    Test contract: with a FakeClock, the wall-clock returned from `clock.now_utc()`
    on exit is at or just past `target_utc` (the loop returns when `delta <= 0`), so
    the wakeup accuracy is bounded by `fine_step_s`, the loop CADENCE.
    """
    while True:
        delta = (target_utc - clock.now_utc()).total_seconds()
        if delta <= 0:
            return
        if delta > coarse_threshold_s:
            await clock.sleep(min(coarse_step_s, delta - coarse_threshold_s))
        else:
            await clock.sleep(fine_step_s)
