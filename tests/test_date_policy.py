"""Shared booking-date policy primitive: `frozen_reason` (cutoff + skip composition).

`frozen_reason` is the SINGLE place that answers "is target date D frozen by booking
POLICY (the hard cutoff or an explicit skip) as of instant `now`?". Both the booker
(`booking_day_gate.should_book_today`) and the watcher
(`watch_orchestrator._should_stop_acting_on_date`) route their cutoff+skip decision
through it, so the two can never silently diverge (the divergence class CLAUDE.md
flags). Caller-specific gates — the booker's weekday gate, the watcher's deadline
gate — are deliberately NOT part of this primitive.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from teetime.core.booking_cutoff import REASON_CUTOFF, REASON_SKIP, frozen_reason
from teetime.core.booking_day_gate import should_book_today
from teetime.core.clock import FakeClock
from teetime.core.config import BookingCutoffConfig

ET = "America/New_York"
# 2026-06-14 is a Sunday; the day-before 16:00 ET cutoff instant is 2026-06-13 16:00 ET.
TARGET = date_cls(2026, 6, 14)


def _et(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(ET))


def test_actionable_returns_none() -> None:
    # Well before the cutoff, not skipped -> not frozen.
    assert (
        frozen_reason(
            _et(2026, 6, 7, 6, 0),
            TARGET,
            timezone=ET,
            cutoff=BookingCutoffConfig(),
            skip_dates=frozenset(),
        )
        is None
    )


def test_past_cutoff_returns_cutoff() -> None:
    # At/after 16:00 ET on the day before -> frozen by cutoff (inclusive).
    assert (
        frozen_reason(
            _et(2026, 6, 13, 16, 0, 0),
            TARGET,
            timezone=ET,
            cutoff=BookingCutoffConfig(),
            skip_dates=frozenset(),
        )
        == REASON_CUTOFF
    )


def test_skipped_returns_skip() -> None:
    # Before the cutoff but the date is in the skip set -> frozen by skip.
    assert (
        frozen_reason(
            _et(2026, 6, 7, 6, 0),
            TARGET,
            timezone=ET,
            cutoff=BookingCutoffConfig(),
            skip_dates=frozenset({TARGET}),
        )
        == REASON_SKIP
    )


def test_cutoff_takes_precedence_over_skip() -> None:
    # Both past-cutoff AND skipped -> cutoff is reported first (matches the watcher's
    # existing stop-acting reason ordering).
    assert (
        frozen_reason(
            _et(2026, 6, 13, 16, 30),
            TARGET,
            timezone=ET,
            cutoff=BookingCutoffConfig(),
            skip_dates=frozenset({TARGET}),
        )
        == REASON_CUTOFF
    )


def test_none_cutoff_is_back_compat() -> None:
    # cutoff=None disables the cutoff leg entirely (existing back-compat for callers
    # that pass no cutoff); only the skip leg remains.
    assert (
        frozen_reason(
            _et(2026, 6, 13, 23, 0),
            TARGET,
            timezone=ET,
            cutoff=None,
            skip_dates=frozenset(),
        )
        is None
    )
    assert (
        frozen_reason(
            _et(2026, 6, 7, 6, 0),
            TARGET,
            timezone=ET,
            cutoff=None,
            skip_dates=frozenset({TARGET}),
        )
        == REASON_SKIP
    )


@pytest.mark.parametrize(
    ("now", "skip_dates"),
    [
        (_et(2026, 6, 7, 6, 0), frozenset()),  # actionable
        (_et(2026, 6, 13, 16, 0), frozenset()),  # past cutoff
        (_et(2026, 6, 7, 6, 0), frozenset({TARGET})),  # skipped
        (_et(2026, 6, 13, 17, 0), frozenset({TARGET})),  # both
    ],
)
def test_booker_routes_through_frozen_reason(
    now: datetime, skip_dates: frozenset[date_cls]
) -> None:
    """The booker's should_book_today must agree with frozen_reason on the cutoff+skip
    decision: for a WANTED weekday, it returns False exactly when frozen_reason is
    non-None. This pins that the booker shares the one primitive (no divergence)."""
    cutoff = BookingCutoffConfig()
    # today + 7 = TARGET (a Sunday). clock is `now`; offset 7 lands on TARGET.
    clock = FakeClock(start=now)
    today = now.astimezone(ZoneInfo(ET)).date()
    assert today + timedelta(days=7) == TARGET  # sanity: scenario targets TARGET
    wanted = frozenset({TARGET.weekday()})  # Sunday wanted -> weekday gate always passes

    booker_ok = should_book_today(
        clock,
        timezone=ET,
        target_offset=7,
        wanted_weekdays=wanted,
        skip_dates=skip_dates,
        cutoff=cutoff,
    )
    frozen = frozen_reason(now, TARGET, timezone=ET, cutoff=cutoff, skip_dates=skip_dates)
    assert booker_ok is (frozen is None)
