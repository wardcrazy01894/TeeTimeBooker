"""Hard booking cutoff (LEADTIME_SKIP_PLAN F1).

Pure tz-aware predicate: a target date D is FROZEN (no new booking, no upgrade) once
wall-clock ``now`` has reached ``cutoff.time_of_day`` on the day ``cutoff.days_before``
days before D. Absolute wall-clock relative to the RESERVATION date — tee-time-
independent (matches the operator's "4 PM the day before" framing). Computed via
``zoneinfo`` so spring-forward / fall-back on D-1 resolve correctly. Takes the injected
``Clock`` (FakeClock in tests), never ``datetime.now``.

This is the core predicate; PR2 wires it into the watcher's stop-acting gate and (as
defense-in-depth) the booking-day gate.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .clock import Clock
from .config import BookingCutoffConfig


def cutoff_instant(
    target_date: date_cls, *, timezone: str, cutoff: BookingCutoffConfig
) -> datetime:
    """The tz-aware instant at/after which ``target_date`` is frozen.

    ``= datetime.combine(target_date - days_before, time_of_day)`` localized in
    ``timezone``. Calendar subtraction FIRST, THEN localize, so ``zoneinfo`` resolves the
    correct UTC offset for the D-1 calendar day (handles spring-forward / fall-back). We
    localize a wall-clock time in that zone via ``.replace(tzinfo=ZoneInfo(...))`` — NOT
    ``astimezone`` of a UTC value (which would shift the wall-clock reading).
    """
    d = target_date - timedelta(days=cutoff.days_before)
    return datetime.combine(d, cutoff.time_of_day).replace(tzinfo=ZoneInfo(timezone))


def is_past_booking_cutoff(
    clock: Clock, target_date: date_cls, *, timezone: str, cutoff: BookingCutoffConfig
) -> bool:
    """True iff ``clock.now_utc() >= cutoff_instant(...)`` (INCLUSIVE — Edge E8).

    True  -> freeze: the caller must NOT book or upgrade ``target_date``.
    False -> still actionable.

    Pure function of ``(clock, target_date, timezone, cutoff)``; FakeClock-deterministic
    (never reads the real wall clock). Both operands are tz-aware (``now_utc()`` is UTC,
    the cutoff instant is zone-aware), so the comparison is offset-correct across zones.
    """
    return clock.now_utc() >= cutoff_instant(target_date, timezone=timezone, cutoff=cutoff)
