"""MULTIDAY PR5: the ACA booking crons fire DAILY (was Sunday-only); the watch cron
stays daily. Job names dropped the `-sun` suffix (now `-edt`/`-est`, DST-half labels).

Static assertions over compute.bicep (bicep is not pytest-importable; CI's
`az bicep build` is the compile gate). The booking job fires every day; the bot's DST gate
selects the season and the booking-day gate selects the wanted weekdays (default Sat+Sun).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPUTE_BICEP = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "compute.bicep"
)
RELEASE_EVENTS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "release_events.json"


@pytest.fixture(scope="module")
def bicep() -> str:
    return COMPUTE_BICEP.read_text()


def test_booking_jobs_are_daily(bicep: str) -> None:
    # Two daily crons (EDT + EST halves)...
    assert "50 9 * * *" in bicep  # 09:50 UTC = 05:50 EDT, every day
    assert "50 10 * * *" in bicep  # 10:50 UTC = 05:50 EST, every day
    # ...and the old Sunday-only crons are gone.
    assert "50 9 * * 0" not in bicep
    assert "50 10 * * 0" not in bicep
    # Job-name suffixes dropped `-sun` (now DST-half labels).
    assert "-edt'" in bicep
    assert "-est'" in bicep
    assert "-edt-sun" not in bicep
    assert "-est-sun" not in bicep


def test_watch_cron_is_daily_every_10_min(bicep: str) -> None:
    assert "'*/10 * * * *'" in bicep


def test_exactly_two_booking_jobs_for_killswitch_parity(bicep: str) -> None:
    # The killswitch hardcodes the MB event's 2 jobs/env (edt + est) + watch + web. Job COUNT
    # for MB must stay 2, which holds iff release_events.json's single event is NOT deduped
    # (both DST halves differ) — MU-15a derives the loop from that JSON, not a hand-written pair.
    assert "loadJsonContent('../release_events.json')" in bicep
    events = json.loads(RELEASE_EVENTS.read_text())
    assert len(events) == 1
    (event,) = events
    assert event["cronDst"] != event["cronStd"], "MB event must not be deduped (2 jobs expected)"


def test_jobname_output_index_zero_is_a_real_job(bicep: str) -> None:
    # `output jobName = bookingJob[0].name` — index 0 must resolve to a real job (-edt).
    assert "bookingJob[0].name" in bicep
    assert bicep.index("-edt'") < bicep.index("-est'")


def test_enable_schedules_param_toggles_both_jobs(bicep: str) -> None:
    # enableSchedules=false → Manual triggers (no auto-fire) so a non-primary env can be
    # silenced once prod is live. Both jobs (booking + watch) gate on it.
    assert "param enableSchedules bool = true" in bicep
    assert bicep.count("triggerType: enableSchedules ? 'Schedule' : 'Manual'") == 2
