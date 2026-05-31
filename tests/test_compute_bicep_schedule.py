"""M6 PR5: the ACA booking schedule is Sunday-only; the watch cron stays daily.

Static assertions over compute.bicep (bicep is not pytest-importable; CI's
`az bicep build` is the compile gate). One booking per Sunday: keep the EDT + EST
Sunday crons, drop both Saturday crons. The wrong-season cron is handled by the
DST gate (core/dst_gate.py), not by removing a cron.
"""

from __future__ import annotations

from pathlib import Path

import pytest

COMPUTE_BICEP = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "compute.bicep"
)


@pytest.fixture(scope="module")
def bicep() -> str:
    return COMPUTE_BICEP.read_text()


def test_booking_jobs_are_sunday_only(bicep: str) -> None:
    # The two Sunday crons remain (EDT + EST halves)...
    assert "50 9 * * 0" in bicep  # 09:50 UTC = 05:50 EDT Sunday
    assert "50 10 * * 0" in bicep  # 10:50 UTC = 05:50 EST Sunday
    # ...and BOTH Saturday crons are gone.
    assert "50 9 * * 6" not in bicep
    assert "50 10 * * 6" not in bicep
    # Job-name suffixes follow suit.
    assert "-edt-sun" in bicep
    assert "-est-sun" in bicep
    assert "-edt-sat" not in bicep
    assert "-est-sat" not in bicep


def test_watch_cron_is_daily_every_10_min(bicep: str) -> None:
    assert "'*/10 * * * *'" in bicep


def test_no_orphan_saturday_cron_vars(bicep: str) -> None:
    assert "cronEdtSat" not in bicep
    assert "cronEstSat" not in bicep


def test_jobname_output_index_zero_is_a_real_sunday_job(bicep: str) -> None:
    # `output jobName = bookingJob[0].name` — index 0 must resolve to a job that
    # exists post-PR5. Assert the first bookingJobs entry is -edt-sun.
    assert "bookingJob[0].name" in bicep
    assert bicep.index("-edt-sun") < bicep.index("-est-sun")
