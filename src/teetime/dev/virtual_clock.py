"""Discrete-event virtual clock for multi-account timing tests (MULTIUSER_PLAN §4.4, round-1 SF4).

``FakeClock.sleep`` advances ONE shared ``_now`` by each caller's delta (``core/clock.py``), so with
N concurrent sleepers (N busy-waits, N staggered bursts) simulated time runs ~N-times fast and the
measured send offsets get scrambled. ``VirtualClock`` keeps a heap of (deadline, waiter). A sleeper
registers ``now + seconds`` and parks. When every task is parked, the driver advances ``now`` to the
EARLIEST deadline and wakes only that waiter (ties wake in registration order). ``FakeClock`` is
unchanged; single-account tests keep using it.

STUB — implemented in MULTIUSER_PLAN MU-9a0.
"""

from __future__ import annotations

from datetime import datetime

_MU9A = "MULTIUSER_PLAN.md MU-9a0"


class VirtualClock:
    """Satisfies the ``core.clock.Clock`` Protocol (``now_utc`` + ``sleep``)."""

    def __init__(self, *, start: datetime) -> None:
        raise NotImplementedError(_MU9A)

    def now_utc(self) -> datetime:
        raise NotImplementedError(_MU9A)

    async def sleep(self, seconds: float) -> None:
        """Park until virtual time reaches ``now + seconds``; never advances time for others."""
        raise NotImplementedError(_MU9A)
