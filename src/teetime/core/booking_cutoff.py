"""Booking-date policy predicates (LEADTIME_SKIP_PLAN F1 + F2).

Two layered concerns, both tz-aware and clock/now-injected (FakeClock in tests, never
``datetime.now``):

- **Hard cutoff (F1).** A target date D is FROZEN (no new booking, no upgrade) once
  wall-clock has reached ``cutoff.time_of_day`` on the day ``cutoff.days_before`` days
  before D. Absolute wall-clock relative to the RESERVATION date — tee-time-independent
  (the operator's "4 PM the day before" framing). Computed via ``zoneinfo`` so
  spring-forward / fall-back on D-1 resolve correctly.
- **Skip dates (F2).** D is also frozen if it is in the operator's skip set.

``frozen_reason`` is the SINGLE composition of the two — the shared "is this date frozen
by booking policy?" primitive that BOTH the booker (``booking_day_gate.should_book_today``)
and the watcher (``watch_orchestrator._should_stop_acting_on_date``) route through, so the
cutoff/skip decision (and its ordering) lives in exactly one place and the two callers can
never silently diverge. Caller-specific gates — the booker's weekday gate, the watcher's
deadline gate — are deliberately NOT part of this primitive. ``is_past_booking_cutoff`` is
the cutoff-only convenience (a clock-reading wrapper over ``frozen_reason``).
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .clock import Clock
from .config import BookingCutoffConfig

# Freeze reasons returned by ``frozen_reason`` (and surfaced as the watcher's distinct
# stop-acting log lines). Kept as named constants so callers + log-message keys can't drift.
REASON_CUTOFF = "cutoff"
REASON_SKIP = "skip"


def cutoff_instant(
    target_date: date_cls, *, timezone: str, cutoff: BookingCutoffConfig
) -> datetime:
    """The tz-aware instant at/after which ``target_date`` is frozen.

    ``= datetime.combine(target_date - days_before, time_of_day)`` localized in
    ``timezone``. Calendar subtraction FIRST, THEN localize, so ``zoneinfo`` resolves the
    correct UTC offset for the D-1 calendar day (handles spring-forward / fall-back). We
    localize a wall-clock time in that zone via ``.replace(tzinfo=ZoneInfo(...))`` — NOT
    ``astimezone`` of a UTC value (which would shift the wall-clock reading).

    CONSTRAINT: ``cutoff.time_of_day`` must NOT fall inside a DST transition window
    (roughly 01:00-03:00 local on the two transition Sundays). ``.replace(tzinfo=...)``
    leaves ``fold=0``, so a time in the fall-back ambiguous hour resolves to the first
    occurrence and one in the spring-forward gap is non-existent -- either would mis-place
    the instant by an hour. The shipped default (16:00) is safely clear of this; only a
    custom early-morning cutoff could hit it.
    """
    d = target_date - timedelta(days=cutoff.days_before)
    return datetime.combine(d, cutoff.time_of_day).replace(tzinfo=ZoneInfo(timezone))


def frozen_reason(
    now: datetime,
    target_date: date_cls,
    *,
    timezone: str,
    cutoff: BookingCutoffConfig | None,
    skip_dates: frozenset[date_cls] = frozenset(),
) -> str | None:
    """Why ``target_date`` is frozen by booking POLICY as of ``now``, or None if actionable.

    The SINGLE shared cutoff+skip composition (see module docstring). Returns:

    - ``REASON_CUTOFF`` if ``cutoff`` is set and ``now >= cutoff_instant(...)`` (INCLUSIVE,
      Edge E8) — the hard 4PM-day-before freeze.
    - ``REASON_SKIP`` if ``target_date`` is in ``skip_dates`` (F2).
    - ``None`` if neither applies (still actionable).

    Cutoff is checked FIRST, so a date that is both past-cutoff AND skipped reports
    ``REASON_CUTOFF`` (matches the watcher's historical stop-acting reason ordering; the
    booker only cares whether the result is non-None). ``cutoff=None`` disables the cutoff
    leg (back-compat for callers that pass no cutoff). Pure function of its inputs; ``now``
    is tz-aware (UTC or zone-aware) and compared against the zone-aware cutoff instant, so
    the comparison is offset-correct across zones. Caller-specific gates (the booker's
    weekday gate, the watcher's deadline gate) are NOT evaluated here.
    """
    if cutoff is not None and now >= cutoff_instant(target_date, timezone=timezone, cutoff=cutoff):
        return REASON_CUTOFF
    if target_date in skip_dates:
        return REASON_SKIP
    return None


def is_past_booking_cutoff(
    clock: Clock, target_date: date_cls, *, timezone: str, cutoff: BookingCutoffConfig
) -> bool:
    """True iff ``clock.now_utc() >= cutoff_instant(...)`` (INCLUSIVE — Edge E8).

    True  -> freeze: the caller must NOT book or upgrade ``target_date``.
    False -> still actionable.

    Cutoff-only convenience: a thin clock-reading wrapper over ``frozen_reason`` (skip set
    empty), so the cutoff comparison lives in exactly one place. FakeClock-deterministic
    (never reads the real wall clock).
    """
    return (
        frozen_reason(
            clock.now_utc(), target_date, timezone=timezone, cutoff=cutoff, skip_dates=frozenset()
        )
        == REASON_CUTOFF
    )
