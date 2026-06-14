"""Weekday-name mapping + the watcher's multi-date horizon helper.

The booking job books a single date (`today + offset`, gated to a wanted weekday by
`core/booking_day_gate.py`). The watch job runs DAILY and watches the next occurrence of
EACH wanted weekday within the bookable horizon (the upcoming Sat AND Sun) via
`next_occurrences_within_horizon`. Wanted weekdays are derived from the configured per-day
windows (`RequestConfig.wanted_weekday_indices`). See MULTIDAY_PLAN.md / PERDAY_WINDOWS_PLAN.md.
"""

from __future__ import annotations

from datetime import date, timedelta

# Python's date.weekday(): Monday=0 .. Sunday=6.
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def weekday_from_name(name: str) -> int:
    """Map a weekday name (case/space-insensitive) to Python's weekday index."""
    try:
        return _WEEKDAYS[name.strip().lower()]
    except KeyError:
        raise ValueError(f"invalid weekday {name!r}; expected one of {sorted(_WEEKDAYS)}") from None


def next_occurrences_within_horizon(
    today: date, wanted_weekdays: frozenset[int], horizon_days: int
) -> tuple[date, ...]:
    """The next upcoming occurrence(s) of EACH wanted weekday within `horizon_days` of today.

    For each weekday w in `wanted_weekdays`, includes the smallest date d with
    d.weekday() == w and 0 <= (d - today).days <= horizon_days. "Today counts": if today
    is a wanted weekday, today itself is the occurrence (delta 0) — never a past date.
    When delta == 0 (today IS the wanted weekday) AND today+7 is still within the horizon,
    BOTH today AND today+7 are returned. This ensures a watcher run on Sunday still
    monitors the upcoming Sunday 7 days out even after _is_past_watch_deadline drops today.
    Returned dates are sorted ascending and de-duplicated; a weekday whose next occurrence
    is strictly beyond the horizon is omitted.

    The watcher passes `horizon_days = max(target_offsets)` so the bookable window is
    defined in ONE place (config), never hardcoded here. See MULTIDAY_PLAN.md PR3.
    """
    out: set[date] = set()
    for w in wanted_weekdays:
        delta = (w - today.weekday()) % 7  # 0 when today is that weekday (today counts)
        if delta <= horizon_days:
            out.add(today + timedelta(days=delta))
        # When today IS the wanted weekday (delta=0), also include today+7 if within horizon.
        # Without this, a watcher run on Sunday computes delta=0 → returns today, and after
        # _is_past_watch_deadline drops today the next Sunday (today+7) is never monitored.
        # See 2026-06-14 prod post-mortem: Sunday booking failed, watcher never recovered it.
        if delta + 7 <= horizon_days:
            out.add(today + timedelta(days=delta + 7))
    return tuple(sorted(out))
