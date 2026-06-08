"""Booking-day gate. Sibling to ``core/dst_gate.py`` for the multi-day re-architecture.

Background: in the multi-day design the booking ACA Job no longer fires only on the
booking weekday — it fires DAILY (two crons, one per DST half). On most mornings the
date it would book (``today + offset``) is NOT a wanted booking weekday, so the run must
fast-exit cheaply (~2 s) without doing any auth / search / busy-wait. ``__main__._run``
evaluates this gate (and the DST gate) BEFORE building adapters or resolving ForeUP site
keys, so a non-booking-day cron makes no live ForeUP request at all.

This gate is a PURE function of ``(clock, timezone, target_offset, wanted_weekdays)``.
It computes the candidate target date the booking run WOULD book (``today + offset``,
where ``today`` is the current calendar date in the COURSE-LOCAL timezone — the same
zone the bot books in, so the weekday it tests is the weekday it would actually book)
and returns True iff that date's weekday is in the wanted set.

Ordering relative to ``dst_gate.should_proceed`` (see ``__main__._run``): the DST gate
runs FIRST. A wrong-season cron is rejected before the booking-day gate is consulted,
so the booking-day decision is only ever evaluated at the correct ET wall clock (hour
== fire_time.hour - 1). This matters because ``today`` is read at gate-evaluation time:
on the correct-season cron the runner lands at ~05:50 ET, unambiguously on the intended
calendar day. (The wrong-season EDT-cron-in-EST case lands at 04:50 ET — still the same
calendar day — but it is skipped by the DST gate before this gate runs, so we never rely
on its ``today``.) See MULTIDAY_PLAN.md §"Booking-day gate" for the full truth table.

Single source of truth for the horizon: the booking run uses ``target_offsets`` from
config; this gate takes the SAME offset value the run will book with. It does not
hardcode 7. The watcher's horizon helper (``core/target_date.py``) likewise derives its
horizon from ``max(target_offsets)`` so the 7-day window is defined in exactly one place.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from .clock import Clock


def should_book_today(
    clock: Clock,
    *,
    timezone: str,
    target_offset: int,
    wanted_weekdays: frozenset[int],
) -> bool:
    """Return True iff ``today + target_offset`` (course-local) falls on a wanted weekday.

    Args:
        clock: injected Clock; ``clock.now_utc()`` is converted to ``timezone`` to read
            the current calendar date. FakeClock-driven in tests (no real ``now()``).
        timezone: IANA name, e.g. ``"America/New_York"``. The candidate target date is
            computed in this zone so the weekday tested matches the date the bot books.
        target_offset: days ahead the booking run targets (config ``target_offsets[0]``;
            v0 is always 7). Passed in — never hardcoded here.
        wanted_weekdays: Python ``date.weekday()`` indices (Mon=0..Sun=6) of the days the
            operator wants booked, e.g. ``frozenset({5, 6})`` for Saturday+Sunday.

    Returns:
        True  -> proceed: ``today + target_offset`` is a wanted booking day; the caller
                 continues to the busy-wait + book of that single date.
        False -> skip: not a wanted booking day; the caller logs a clear "not a booking
                 day" line and exits 0 (a non-booking-day firing is NOT an error).

    Pure function of its inputs; no I/O beyond reading the injected clock.
    """
    today = clock.now_utc().astimezone(ZoneInfo(timezone)).date()
    target = today + timedelta(days=target_offset)
    return target.weekday() in wanted_weekdays
