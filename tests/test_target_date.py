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


# --- MULTIDAY PR3: watcher horizon helper (next occurrence of each wanted weekday) ---

from teetime.core.target_date import next_occurrences_within_horizon  # noqa: E402

_SAT_SUN = frozenset({5, 6})


@pytest.mark.parametrize(
    ("today", "wanted", "horizon", "expected"),
    [
        # Sat=5, Sun=6. Reference: 2026-05-31 is Sunday.
        pytest.param(
            date(2026, 5, 31), _SAT_SUN, 7, (date(2026, 5, 31), date(2026, 6, 6)), id="from_sunday"
        ),
        pytest.param(
            date(2026, 6, 1), _SAT_SUN, 7, (date(2026, 6, 6), date(2026, 6, 7)), id="from_monday"
        ),
        pytest.param(
            date(2026, 6, 5), _SAT_SUN, 7, (date(2026, 6, 6), date(2026, 6, 7)), id="from_friday"
        ),
        pytest.param(
            date(2026, 6, 6),
            _SAT_SUN,
            7,
            (date(2026, 6, 6), date(2026, 6, 7)),
            id="from_saturday_today_counts",
        ),
        pytest.param(
            date(2026, 6, 3), _SAT_SUN, 7, (date(2026, 6, 6), date(2026, 6, 7)), id="from_wednesday"
        ),
        # Sunday-only set → single date.
        pytest.param(
            date(2026, 6, 3), frozenset({6}), 7, (date(2026, 6, 7),), id="sunday_only_set"
        ),
        # Dedupe + sort (a redundant weekday in the set collapses).
        pytest.param(
            date(2026, 5, 31),
            frozenset({5, 6}),
            7,
            (date(2026, 5, 31), date(2026, 6, 6)),
            id="dedupe_sorted",
        ),
        # "Today counts": from Mon with wanted={Mon}, today IS the occurrence (delta 0),
        # NOT next Mon. delta = (w - today.weekday()) % 7 is always 0..6, never 7 — so the
        # real inclusive boundary is delta==6 (see includes_at_boundary below).
        pytest.param(
            date(2026, 6, 1), frozenset({0}), 7, (date(2026, 6, 1),), id="today_counts_single"
        ),
        # Exclusion/inclusion pair: from Tue 06-02, next Mon (06-08) is delta 6.
        pytest.param(date(2026, 6, 2), frozenset({0}), 5, (), id="excludes_beyond"),
        pytest.param(
            date(2026, 6, 2), frozenset({0}), 6, (date(2026, 6, 8),), id="includes_at_boundary"
        ),
    ],
)
def test_next_occurrences_within_horizon(
    today: date, wanted: frozenset[int], horizon: int, expected: tuple[date, ...]
) -> None:
    assert next_occurrences_within_horizon(today, wanted, horizon) == expected


def test_horizon_never_returns_past_date() -> None:
    today = date(2026, 6, 6)  # Saturday
    out = next_occurrences_within_horizon(today, _SAT_SUN, 7)
    assert all(d >= today for d in out)
    assert min(out) == today  # today counts when today is wanted


def test_horizon_uses_max_offset_not_literal() -> None:
    # Reviewer item 3: horizon is derived from max(target_offsets), not a second literal 7.
    horizon = max([7])
    out = next_occurrences_within_horizon(date(2026, 6, 1), _SAT_SUN, horizon)
    assert out == (date(2026, 6, 6), date(2026, 6, 7))
