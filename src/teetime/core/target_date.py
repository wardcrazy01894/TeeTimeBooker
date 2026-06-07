"""Resolve the booking target date(s), anchored to the booking weekday.

The bot books a fixed weekday (Sunday) `offset` days ahead. The booking job only
RUNS on that weekday, so `today + offset` is correct for it. But the watch job runs
DAILY, and `today + offset` would drift off the target Sunday every day (from a
Wednesday it isn't even a Sunday). Anchoring on the most-recent booking weekday makes
the target STABLE across the week: it locks onto the upcoming target date and only
advances after that weekday passes. On the booking weekday itself the anchor is today,
so the result equals the historical `today + offset` — the 6 AM booker is unchanged.
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


def most_recent_weekday(today: date, weekday: int) -> date:
    """The most recent date <= today whose weekday == `weekday` (Mon=0..Sun=6).

    Returns `today` itself when today already is that weekday.
    """
    days_since = (today.weekday() - weekday) % 7
    return today - timedelta(days=days_since)


def resolve_target_dates(today: date, offsets: list[int], weekday: int) -> tuple[date, ...]:
    """Anchor on the most-recent booking weekday, then apply each offset.

    Stable across the week (does not drift daily like `today + offset`). Offsets are
    sorted for a deterministic RequestId fingerprint.
    """
    anchor = most_recent_weekday(today, weekday)
    return tuple(anchor + timedelta(days=o) for o in sorted(offsets))


def next_occurrences_within_horizon(
    today: date, wanted_weekdays: frozenset[int], horizon_days: int
) -> tuple[date, ...]:
    """The next upcoming occurrence of EACH wanted weekday within `horizon_days` of today.

    For each weekday w in `wanted_weekdays`, includes the smallest date d with
    d.weekday() == w and 0 <= (d - today).days <= horizon_days. "Today counts": if today
    is a wanted weekday, today itself is the occurrence (delta 0) — never a past date,
    never next week's same weekday. Returned dates are sorted ascending and de-duplicated;
    a weekday whose next occurrence is strictly beyond the horizon is omitted.

    The watcher passes `horizon_days = max(target_offsets)` so the bookable window is
    defined in ONE place (config), never hardcoded here. See MULTIDAY_PLAN.md PR3.
    """
    out: set[date] = set()
    for w in wanted_weekdays:
        delta = (w - today.weekday()) % 7  # 0 when today is that weekday (today counts)
        if delta <= horizon_days:
            out.add(today + timedelta(days=delta))
    return tuple(sorted(out))
