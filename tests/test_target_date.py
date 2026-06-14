"""target_date helpers: weekday-name mapping + the watcher's multi-date horizon helper.

(The old `most_recent_weekday` / `resolve_target_dates` anchor functions were removed with
the multi-day re-arch — the booking job books `today + offset` gated by the booking-day gate,
and the watcher uses `next_occurrences_within_horizon`.)
"""

from __future__ import annotations

from datetime import date

import pytest

from teetime.core.target_date import next_occurrences_within_horizon, weekday_from_name


def test_weekday_from_name() -> None:
    assert weekday_from_name("sunday") == 6
    assert weekday_from_name("Monday") == 0
    assert weekday_from_name(" SATURDAY ") == 5
    with pytest.raises(ValueError, match="invalid weekday"):
        weekday_from_name("someday")


# --- watcher horizon helper (next occurrence of each wanted weekday) ---


_SAT_SUN = frozenset({5, 6})


@pytest.mark.parametrize(
    ("today", "wanted", "horizon", "expected"),
    [
        # Sat=5, Sun=6. Reference: 2026-05-31 is Sunday.
        # When today IS a wanted weekday (delta=0), both today AND today+7 are included
        # (if today+7 still fits within horizon_days). See 2026-06-14 prod post-mortem.
        pytest.param(
            date(2026, 5, 31),
            _SAT_SUN,
            7,
            (date(2026, 5, 31), date(2026, 6, 6), date(2026, 6, 7)),
            id="from_sunday",
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
            (date(2026, 6, 6), date(2026, 6, 7), date(2026, 6, 13)),
            id="from_saturday_today_counts",
        ),
        pytest.param(
            date(2026, 6, 3), _SAT_SUN, 7, (date(2026, 6, 6), date(2026, 6, 7)), id="from_wednesday"
        ),
        # Sunday-only set → single date (today is Wednesday, so delta=4; delta+7=11 > 7).
        pytest.param(
            date(2026, 6, 3), frozenset({6}), 7, (date(2026, 6, 7),), id="sunday_only_set"
        ),
        # Dedupe + sort. today=Sunday → includes today AND today+7 (next Sunday).
        pytest.param(
            date(2026, 5, 31),
            frozenset({5, 6}),
            7,
            (date(2026, 5, 31), date(2026, 6, 6), date(2026, 6, 7)),
            id="dedupe_sorted",
        ),
        # "Today counts" AND "today+7 also counts when within horizon".
        # From Mon with wanted={Mon}: today (delta=0) + today+7 (delta+7=7 <= 7) both returned.
        pytest.param(
            date(2026, 6, 1),
            frozenset({0}),
            7,
            (date(2026, 6, 1), date(2026, 6, 8)),
            id="today_counts_single",
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


def test_sunday_on_sunday_includes_next_sunday_regression() -> None:
    """Regression for 2026-06-14 prod incident.

    Booking job failed for Sunday June 21. The watcher ran all day on Sunday June 14
    but its targets were ['2026-06-20'] only — June 21 never appeared. Root cause:
    next_occurrences_within_horizon(June 14, {Sat, Sun}, 7) returned (June 14, June 20)
    because delta=0 for Sunday yielded today, and delta+7=7 was never checked. After
    _is_past_watch_deadline dropped June 14, June 21 was silently unmonitored for 24h.
    """
    sunday = date(2026, 6, 14)
    result = next_occurrences_within_horizon(sunday, _SAT_SUN, 7)
    assert date(2026, 6, 14) in result  # today (Sunday) — still included
    assert date(2026, 6, 20) in result  # next Saturday
    assert date(2026, 6, 21) in result  # NEXT Sunday 7 days out — was the unfixed bug


def test_saturday_on_saturday_includes_next_saturday() -> None:
    """Same delta=0 scenario for Saturday — both today and today+7 appear."""
    saturday = date(2026, 6, 6)
    result = next_occurrences_within_horizon(saturday, _SAT_SUN, 7)
    assert date(2026, 6, 6) in result  # today (Saturday)
    assert date(2026, 6, 7) in result  # next Sunday
    assert date(2026, 6, 13) in result  # NEXT Saturday 7 days out
