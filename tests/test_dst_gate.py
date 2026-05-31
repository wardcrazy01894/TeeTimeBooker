"""M6 PR2: the DST-half gate (`core/dst_gate.py`).

`should_proceed` returns True iff the course-local (ET) wall-clock HOUR at fire time
equals `fire_time.hour - 1`. The two same-day ACA crons fire at :50 of the hour
preceding T0=06:00 ET, so the CORRECT season reads ET hour 5; the WRONG season reads
hour 4 (EDT-tuned cron firing in EST) or hour 6 (EST-tuned cron firing in EDT). The
gate makes the wrong-season cron exit instead of booking ~50 min late (past T0) or
busy-waiting ~70 min into the replica timeout (future T0).
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from teetime.core.clock import FakeClock
from teetime.core.dst_gate import should_proceed

ET = "America/New_York"
FIRE6 = time(6, 0, 0)


def _clock(y: int, mo: int, d: int, h: int, mi: int) -> FakeClock:
    return FakeClock(start=datetime(y, mo, d, h, mi, tzinfo=UTC))


@pytest.mark.parametrize(
    ("clock", "fire", "expect"),
    [
        # Correct-season crons → ET hour 5 → proceed.
        pytest.param(_clock(2026, 5, 31, 9, 50), FIRE6, True, id="edt_correct_cron"),  # 05:50 EDT
        pytest.param(_clock(2026, 12, 6, 10, 50), FIRE6, True, id="est_correct_cron"),  # 05:50 EST
        # Wrong-season crons → ET hour 4 or 6 → skip (reviewer item 5: both directions).
        pytest.param(
            _clock(2026, 12, 6, 9, 50), FIRE6, False, id="edt_cron_in_est_season"
        ),  # 04:50 EST
        pytest.param(
            _clock(2026, 5, 31, 10, 50), FIRE6, False, id="est_cron_in_edt_season"
        ),  # 06:50 EDT
        # Spring-forward Sunday (2026-03-08): EDT already in effect by 05:00.
        pytest.param(
            _clock(2026, 3, 8, 9, 50), FIRE6, True, id="spring_forward_morning"
        ),  # 05:50 EDT
        pytest.param(
            _clock(2026, 3, 8, 10, 50), FIRE6, False, id="spring_forward_wrong_cron"
        ),  # 06:50 EDT
        # Fall-back Sunday (2026-11-01): rollback at 02:00 settles to EST well before 05:00.
        pytest.param(_clock(2026, 11, 1, 10, 50), FIRE6, True, id="fall_back_morning"),  # 05:50 EST
        pytest.param(
            _clock(2026, 11, 1, 9, 50), FIRE6, False, id="fall_back_wrong_cron"
        ),  # 04:50 EST
        # Predicate uses fire_hour-1, not a hardcoded 5: fire_time=07:00, 06:30 ET → proceed.
        pytest.param(_clock(2026, 5, 31, 10, 30), time(7, 0, 0), True, id="generic_fire_minus_one"),
    ],
)
def test_dst_gate_decision(clock: FakeClock, fire: time, expect: bool) -> None:
    assert should_proceed(clock, timezone=ET, fire_time=fire) is expect
