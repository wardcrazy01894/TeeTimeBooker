"""LEADTIME_SKIP_PLAN PR1 — hard booking cutoff core predicate.

The cutoff freezes a target date once wall-clock has passed `time_of_day` on the day
`days_before` days before it (default 16:00 ET the day before). Pure, tz-aware, and
FakeClock-driven — never reads the real wall clock. See LEADTIME_SKIP_PLAN §F1 / §8.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from teetime.__main__ import _build_request
from teetime.core.booking_cutoff import cutoff_instant, is_past_booking_cutoff
from teetime.core.clock import FakeClock
from teetime.core.config import (
    AppConfig,
    BookingCutoffConfig,
    CourseConfig,
    PlayerConfig,
    RequestConfig,
    TimeWindowConfig,
)

ET = "America/New_York"


def _clock_at_et(
    year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0
) -> FakeClock:
    """A FakeClock whose `now_utc()` corresponds to the given ET wall-clock instant."""
    return FakeClock(start=datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(ET)))


def test_cutoff_instant_is_4pm_day_before_in_et() -> None:
    inst = cutoff_instant(date_cls(2026, 6, 14), timezone=ET, cutoff=BookingCutoffConfig())
    assert inst == datetime(2026, 6, 13, 16, 0, tzinfo=ZoneInfo(ET))
    # June -> EDT -> UTC-4.
    assert inst.utcoffset() == timedelta(hours=-4)


def test_block_when_now_at_or_after_cutoff() -> None:
    cutoff = BookingCutoffConfig()
    target = date_cls(2026, 6, 14)
    # Exactly at the cutoff instant (16:00 ET on D-1) -> blocked (inclusive, >=).
    assert (
        is_past_booking_cutoff(
            _clock_at_et(2026, 6, 13, 16, 0, 0), target, timezone=ET, cutoff=cutoff
        )
        is True
    )
    # One second past -> blocked.
    assert (
        is_past_booking_cutoff(
            _clock_at_et(2026, 6, 13, 16, 0, 1), target, timezone=ET, cutoff=cutoff
        )
        is True
    )


def test_allow_when_now_before_cutoff() -> None:
    cutoff = BookingCutoffConfig()
    target = date_cls(2026, 6, 14)
    # One second before the cutoff -> still actionable.
    assert (
        is_past_booking_cutoff(
            _clock_at_et(2026, 6, 13, 15, 59, 59), target, timezone=ET, cutoff=cutoff
        )
        is False
    )


def test_cutoff_uses_injected_clock_not_wallclock() -> None:
    # Two FakeClocks either side of the boundary drive opposite decisions deterministically;
    # the real wall clock is never consulted.
    cutoff = BookingCutoffConfig()
    target = date_cls(2026, 6, 14)
    before = _clock_at_et(2020, 1, 1, 0, 0)  # years before
    after = _clock_at_et(2030, 1, 1, 0, 0)  # years after
    assert is_past_booking_cutoff(before, target, timezone=ET, cutoff=cutoff) is False
    assert is_past_booking_cutoff(after, target, timezone=ET, cutoff=cutoff) is True


def test_cutoff_spring_forward_day_before() -> None:
    # Target Mon 2026-03-09; D-1 = 2026-03-08, the spring-forward day (02:00->03:00 skipped).
    # 16:00 is well after the gap, so it localizes unambiguously to EDT (UTC-4).
    inst = cutoff_instant(date_cls(2026, 3, 9), timezone=ET, cutoff=BookingCutoffConfig())
    assert inst == datetime(2026, 3, 8, 16, 0, tzinfo=ZoneInfo(ET))
    assert inst.utcoffset() == timedelta(hours=-4)
    target = date_cls(2026, 3, 9)
    assert (
        is_past_booking_cutoff(
            _clock_at_et(2026, 3, 8, 15, 59), target, timezone=ET, cutoff=BookingCutoffConfig()
        )
        is False
    )
    assert (
        is_past_booking_cutoff(
            _clock_at_et(2026, 3, 8, 16, 0), target, timezone=ET, cutoff=BookingCutoffConfig()
        )
        is True
    )


def test_cutoff_fall_back_day_before() -> None:
    # Target Mon 2026-11-02; D-1 = 2026-11-01, the fall-back day (01:00-02:00 repeats).
    # 16:00 is after the repeat, firmly EST (UTC-5).
    inst = cutoff_instant(date_cls(2026, 11, 2), timezone=ET, cutoff=BookingCutoffConfig())
    assert inst == datetime(2026, 11, 1, 16, 0, tzinfo=ZoneInfo(ET))
    assert inst.utcoffset() == timedelta(hours=-5)
    target = date_cls(2026, 11, 2)
    assert (
        is_past_booking_cutoff(
            _clock_at_et(2026, 11, 1, 15, 59), target, timezone=ET, cutoff=BookingCutoffConfig()
        )
        is False
    )
    assert (
        is_past_booking_cutoff(
            _clock_at_et(2026, 11, 1, 16, 0), target, timezone=ET, cutoff=BookingCutoffConfig()
        )
        is True
    )


@pytest.mark.parametrize(
    "today",
    [
        # A summer (EDT) week and a winter (EST) week — every weekday.
        *(date_cls(2026, 6, 8) + timedelta(days=i) for i in range(7)),
        *(date_cls(2026, 1, 5) + timedelta(days=i) for i in range(7)),
    ],
)
def test_seven_day_out_date_never_cut_at_dawn(today: date_cls) -> None:
    # Edge E10: at the 05:50 ET booking cron, a today+7 target's cutoff is (today+6) 16:00 ET —
    # ~6 days in the FUTURE — so the cutoff can NEVER bite a legitimate 7-day-out booking.
    clock = _clock_at_et(today.year, today.month, today.day, 5, 50)
    target = today + timedelta(days=7)
    assert is_past_booking_cutoff(clock, target, timezone=ET, cutoff=BookingCutoffConfig()) is False


def test_configurable_cutoff_two_days_before() -> None:
    cutoff = BookingCutoffConfig(days_before=2, time_of_day=time(18, 0))
    inst = cutoff_instant(date_cls(2026, 6, 14), timezone=ET, cutoff=cutoff)
    assert inst == datetime(2026, 6, 12, 18, 0, tzinfo=ZoneInfo(ET))


def test_request_id_unaffected_by_booking_cutoff() -> None:
    # Edge E7: booking_cutoff is POLICY, not request identity — it must NOT change the RequestId
    # (which would orphan in-process idempotency keys). Two configs differing ONLY in the cutoff
    # produce the same RequestId.

    def _cfg(cutoff: BookingCutoffConfig) -> AppConfig:
        return AppConfig(
            courses=[
                CourseConfig(
                    id="foreup:mangrove_bay",
                    adapter="foreup.mangrove_bay",
                    username_env="MB_USERNAME",
                    password_env="MB_PASSWORD",
                )
            ],
            request=RequestConfig(
                target_offsets=[7],
                time_windows=[
                    TimeWindowConfig(weekday="sunday", earliest=time(8, 45), latest=time(10, 0))
                ],
                players=[PlayerConfig(first_name="A", last_name="B")],
                course_preferences=["foreup:mangrove_bay"],
                booking_cutoff=cutoff,
            ),
        )

    rid_default = _build_request(_cfg(BookingCutoffConfig()), dry_run=True).request_id
    rid_other = _build_request(
        _cfg(BookingCutoffConfig(days_before=3, time_of_day=time(12, 0))), dry_run=True
    ).request_id
    assert rid_default == rid_other
