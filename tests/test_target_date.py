"""M6 PR7: target-date resolution anchored to the booking weekday.

The bot books a fixed weekday (Sunday) `offset` days ahead. The booking job only
RUNS on that weekday, so `today + offset` is right for it. But the watch job runs
DAILY — `today + offset` drifts off the target Sunday every day. Anchoring on the
most-recent booking weekday makes the target STABLE all week: it locks onto the
upcoming target date and only advances after that weekday passes.
"""

from __future__ import annotations

from datetime import date

import pytest

from teetime.core.target_date import (
    most_recent_weekday,
    resolve_target_dates,
    weekday_from_name,
)

SUNDAY = 6  # Python weekday(): Mon=0 .. Sun=6


def test_weekday_from_name() -> None:
    assert weekday_from_name("sunday") == 6
    assert weekday_from_name("Monday") == 0
    assert weekday_from_name(" SATURDAY ") == 5
    with pytest.raises(ValueError, match="invalid weekday"):
        weekday_from_name("someday")


@pytest.mark.parametrize(
    ("today", "expected_anchor"),
    [
        (date(2026, 5, 31), date(2026, 5, 31)),  # Sunday -> itself
        (date(2026, 6, 1), date(2026, 5, 31)),  # Monday -> prior Sunday
        (date(2026, 6, 6), date(2026, 5, 31)),  # Saturday -> prior Sunday
        (date(2026, 6, 7), date(2026, 6, 7)),  # next Sunday -> itself
    ],
)
def test_most_recent_sunday(today: date, expected_anchor: date) -> None:
    assert most_recent_weekday(today, SUNDAY) == expected_anchor


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        # The whole week May 31 (Sun) .. Jun 6 (Sat) locks onto June 7.
        (date(2026, 5, 31), date(2026, 6, 7)),
        (date(2026, 6, 1), date(2026, 6, 7)),
        (date(2026, 6, 3), date(2026, 6, 7)),
        (date(2026, 6, 6), date(2026, 6, 7)),
        # Once June 7 (Sun) arrives, it advances to June 14.
        (date(2026, 6, 7), date(2026, 6, 14)),
        (date(2026, 6, 8), date(2026, 6, 14)),
    ],
)
def test_resolve_target_dates_offset_7_sunday(today: date, expected: date) -> None:
    assert resolve_target_dates(today, [7], SUNDAY) == (expected,)


def test_resolve_target_dates_on_booking_sunday_equals_today_plus_offset() -> None:
    # On the booking weekday, anchor == today, so the result matches the booker's
    # historical `today + offset` — the 6 AM Sunday booker is unchanged.
    sunday = date(2026, 6, 7)
    assert resolve_target_dates(sunday, [7], SUNDAY) == (date(2026, 6, 14),)
