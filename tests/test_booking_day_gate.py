"""MULTIDAY_PLAN PR2: the booking-day gate (`core/booking_day_gate.py`).

`should_book_today` returns True iff `today + target_offset` (computed in the course-local
timezone — the same zone the bot books in) falls on a wanted weekday. The daily-firing
booking cron uses this to fast-exit (~2 s) on the 5/7 mornings whose target isn't a wanted
day, doing no auth/search/busy-wait.

The gate is WEEKDAY-ONLY and DST/season-agnostic — it reads the date, not the season.
Season correctness (the wrong-season cron exiting) is the DST gate's concern
(`test_dst_gate.py`); in `_run` the DST gate runs FIRST, so this gate is only ever
evaluated on the correct-season cron. The tests below pin the UTC instant at the
correct-season :50 to mirror the real cron, but the gate ignores the clock's hour.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from teetime.core.booking_day_gate import should_book_today
from teetime.core.clock import FakeClock
from teetime.core.config import BookingCutoffConfig

ET = "America/New_York"
SAT_SUN = frozenset({5, 6})
SUN_ONLY = frozenset({6})


def _clock(y: int, mo: int, d: int, h: int, mi: int) -> FakeClock:
    return FakeClock(start=datetime(y, mo, d, h, mi, tzinfo=UTC))


@pytest.mark.parametrize(
    ("clock", "wanted", "offset", "expect"),
    [
        # today+7 lands on Sun/Sat → book (Sat+Sun wanted).
        pytest.param(_clock(2026, 5, 31, 9, 50), SAT_SUN, 7, True, id="target_sunday"),
        pytest.param(_clock(2026, 5, 30, 9, 50), SAT_SUN, 7, True, id="target_saturday"),
        # today+7 lands on a weekday → skip.
        pytest.param(_clock(2026, 5, 25, 9, 50), SAT_SUN, 7, False, id="target_monday"),
        pytest.param(_clock(2026, 5, 29, 9, 50), SAT_SUN, 7, False, id="target_friday"),
        # Sunday-only set: a Saturday target is skipped; a Sunday target books.
        pytest.param(_clock(2026, 5, 30, 9, 50), SUN_ONLY, 7, False, id="sun_only_skips_sat"),
        pytest.param(_clock(2026, 5, 31, 9, 50), SUN_ONLY, 7, True, id="sun_only_books_sun"),
        # EST season (gate is season-agnostic; instant pinned at the EST :50 cron).
        pytest.param(_clock(2026, 12, 6, 10, 50), SAT_SUN, 7, True, id="est_target_sunday"),
        pytest.param(_clock(2026, 12, 2, 10, 50), SAT_SUN, 7, False, id="est_target_wednesday"),
        # Spring-forward week (DST starts 2026-03-08).
        pytest.param(_clock(2026, 3, 8, 9, 50), SAT_SUN, 7, True, id="spring_forward_sunday"),
        pytest.param(_clock(2026, 3, 10, 9, 50), SAT_SUN, 7, False, id="spring_forward_tuesday"),
        # Fall-back week (DST ends 2026-11-01).
        pytest.param(_clock(2026, 10, 31, 9, 50), SAT_SUN, 7, True, id="fall_back_saturday"),
        pytest.param(_clock(2026, 11, 5, 10, 50), SAT_SUN, 7, False, id="fall_back_thursday"),
        # Offset is honoured (not hardcoded 7): Fri + 1 = Sat → book.
        pytest.param(_clock(2026, 5, 29, 9, 50), SAT_SUN, 1, True, id="offset_param_honoured"),
    ],
)
def test_should_book_today(
    clock: FakeClock, wanted: frozenset[int], offset: int, expect: bool
) -> None:
    assert (
        should_book_today(clock, timezone=ET, target_offset=offset, wanted_weekdays=wanted)
        is expect
    )


def test_booking_gate_defense_blocks_past_cutoff() -> None:
    """LEADTIME_SKIP_PLAN PR2 (defense-in-depth): when the candidate target is already past
    its 4PM-day-before cutoff, the gate refuses even though the weekday is wanted. Synthetic:
    Fri 17:00 ET + offset 1 → Sat target whose cutoff (Fri 16:00 ET) has passed."""
    clock = _clock(2026, 5, 29, 21, 0)  # Fri 2026-05-29 17:00 ET (past the Sat target's cutoff)
    # Without a cutoff the gate would book (Saturday is wanted) — proves it's the cutoff blocking.
    assert should_book_today(clock, timezone=ET, target_offset=1, wanted_weekdays=SAT_SUN) is True
    assert (
        should_book_today(
            clock,
            timezone=ET,
            target_offset=1,
            wanted_weekdays=SAT_SUN,
            cutoff=BookingCutoffConfig(),
        )
        is False
    )


@pytest.mark.parametrize(
    "clock",
    [
        _clock(2026, 5, 30, 9, 50),  # EDT Sat→ target Sat 06-06
        _clock(2026, 5, 31, 9, 50),  # EDT Sun→ target Sun 06-07
        _clock(2026, 12, 5, 10, 50),  # EST Sat
        _clock(2026, 12, 6, 10, 50),  # EST Sun
    ],
)
def test_booking_gate_never_blocks_normal_seven_day_out(clock: FakeClock) -> None:
    """Edge E10: with the default cutoff a legit today+7 booking is NEVER cut — the cutoff
    for a 7-day-out target is ~6 days in the future at the 05:50 cron. The cutoff param must
    not change the weekday-only decision for a normal run."""
    assert (
        should_book_today(
            clock,
            timezone=ET,
            target_offset=7,
            wanted_weekdays=SAT_SUN,
            cutoff=BookingCutoffConfig(),
        )
        is True
    )


def test_booking_gate_skips_skipped_date() -> None:
    """LEADTIME_SKIP_PLAN PR4: today+offset is a wanted weekday BUT in skip_dates → refuse."""
    clock = _clock(2026, 5, 31, 9, 50)  # today 2026-05-31 (Sun); target = 2026-06-07 (Sun)
    assert (
        should_book_today(
            clock,
            timezone=ET,
            target_offset=7,
            wanted_weekdays=SAT_SUN,
            skip_dates=frozenset({date(2026, 6, 7)}),
        )
        is False
    )


def test_booking_gate_books_unskipped_wanted_date() -> None:
    """A skip set that does NOT contain the target does not break a normal booking."""
    clock = _clock(2026, 5, 31, 9, 50)  # target = 2026-06-07
    assert (
        should_book_today(
            clock,
            timezone=ET,
            target_offset=7,
            wanted_weekdays=SAT_SUN,
            skip_dates=frozenset({date(2026, 6, 14)}),  # a different Sunday
        )
        is True
    )


def test_skip_execution_day_not_target_does_not_block() -> None:
    """Off-by-one pin: skip is compared against the RESERVATION date (today+offset), NEVER the
    execution day. A skip date equal to *today* but not the target must NOT block."""
    clock = _clock(2026, 5, 31, 9, 50)  # execution day 2026-05-31; target 2026-06-07
    assert (
        should_book_today(
            clock,
            timezone=ET,
            target_offset=7,
            wanted_weekdays=SAT_SUN,
            skip_dates=frozenset({date(2026, 5, 31)}),  # today, not the target
        )
        is True
    )


def test_timezone_determines_target_date() -> None:
    """`today` is read in the course-local tz, not UTC. At 2026-06-01 03:00 UTC it's still
    2026-05-31 (Sun) in ET; +7 = Sun 06-07 → book. A naive UTC read would see Mon 06-01
    → Mon 06-08 → skip. Pins the timezone-correctness (reviewer item 1)."""
    clock = FakeClock(start=datetime(2026, 6, 1, 3, 0, tzinfo=UTC))  # 2026-05-31 23:00 ET (Sun)
    assert should_book_today(clock, timezone=ET, target_offset=7, wanted_weekdays=SAT_SUN) is True
