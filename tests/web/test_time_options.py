"""Time-window pickers list only the hours a course has tee times (operator request 2026-10-02:
"no reason to show 4 AM or 9 PM"), per course. The hours are one line per course in
``courses/names.py::COURSE_TEE_SHEET_HOURS``; ``web/time_options.py`` turns them into the picker's
15-minute choices, the union over a person's courses (what a page shows before a course is chosen,
or with script off) and the server-side check that a saved window lies inside the course's hours.
"""

from __future__ import annotations

from datetime import time

import pytest

from teetime.core.models import CourseId
from teetime.courses.foreup.mangrove_bay import BLIND_POST_MORNING_GRID, MANGROVE_BAY_COURSE_ID
from teetime.courses.names import (
    ALL_DAY,
    COURSE_DISPLAY_NAMES,
    COURSE_TEE_SHEET_HOURS,
    TeeSheetHours,
    tee_sheet_hours,
)
from teetime.web.services import InvalidInputError
from teetime.web.time_options import (
    STEP_MINUTES,
    check_window,
    time_label,
    time_options,
    union_hours,
    with_values,
)

MB_HOURS = TeeSheetHours(first=time(6, 30), last=time(19, 0))


# --- the per-course table ---------------------------------------------------------------------


def test_every_named_course_has_tee_sheet_hours_and_they_are_sane() -> None:
    for course in COURSE_DISPLAY_NAMES:
        hours = COURSE_TEE_SHEET_HOURS[course]
        assert hours.first < hours.last, course
        assert hours.first >= time(5, 0) and hours.last <= time(21, 0), course  # golf, not 4 AM


def test_mangrove_bay_hours_cover_the_blind_grid_and_the_observed_last_tee() -> None:
    """Live 2026-10-02 (no login, party of 2): remaining tee times ran 13:52-17:45 on a Sunday
    and 11:15-15:22 on a Wednesday; the first tee is the 07:00 grid start (07:37 seen live).
    6:30 AM-7:00 PM leaves room for summer evenings; the bound is a one-line edit."""
    hours = tee_sheet_hours(MANGROVE_BAY_COURSE_ID)
    assert hours == MB_HOURS
    assert BLIND_POST_MORNING_GRID is not None
    for hhmm in BLIND_POST_MORNING_GRID:
        assert hours.first <= time.fromisoformat(hhmm) <= hours.last
    assert hours.first <= time(17, 45) <= hours.last


def test_unknown_course_falls_back_to_all_day() -> None:
    assert tee_sheet_hours("foreup:nowhere") == ALL_DAY
    assert tee_sheet_hours(CourseId("x"), {"x": MB_HOURS}) == MB_HOURS
    assert ALL_DAY.first == time(0, 0) and ALL_DAY.last == time(23, 45)


# --- the picker's choices ---------------------------------------------------------------------


def test_time_options_are_quarter_hours_from_first_to_last_inclusive() -> None:
    assert STEP_MINUTES == 15
    opts = time_options(MB_HOURS)
    assert opts[0] == time(6, 30) and opts[-1] == time(19, 0)
    assert len(opts) == (12 * 60 + 30) // 15 + 1
    assert all(t.minute % 15 == 0 for t in opts)
    assert time(4, 0) not in opts and time(21, 0) not in opts


def test_time_options_round_an_off_grid_bound_outward() -> None:
    opts = time_options(TeeSheetHours(first=time(6, 37), last=time(17, 52)))
    assert opts[0] == time(6, 30) and opts[-1] == time(18, 0)


def test_union_is_the_earliest_first_and_the_latest_last() -> None:
    a = TeeSheetHours(first=time(7, 0), last=time(18, 0))
    assert union_hours([a, MB_HOURS]) == MB_HOURS
    assert union_hours([a]) == a
    assert union_hours([]) == ALL_DAY


def test_with_values_keeps_a_stored_off_grid_window_selectable() -> None:
    """A rule saved as 09:22-10:07 (typed before the picker) must still show its own times."""
    opts = with_values(time_options(MB_HOURS), time(9, 22), time(10, 7), time(8, 0))
    assert time(9, 22) in opts and time(10, 7) in opts
    assert opts == sorted(opts) and len(opts) == len(set(opts))


def test_time_label_reads_like_a_clock() -> None:
    assert time_label(time(6, 30)) == "6:30 AM"
    assert time_label(time(12, 0)) == "12:00 PM"
    assert time_label(time(19, 0)) == "7:00 PM"
    assert time_label(time(0, 0)) == "12:00 AM"


# --- the server-side check --------------------------------------------------------------------


def test_check_window_names_the_course_and_its_hours() -> None:
    with pytest.raises(InvalidInputError) as e:
        check_window("Mangrove Bay", MB_HOURS, time(5, 0), time(8, 0), label="option 2")
    msg = str(e.value)
    assert "Mangrove Bay" in msg and "6:30 AM" in msg and "7:00 PM" in msg and "option 2" in msg


def test_check_window_accepts_the_bounds_themselves_and_all_day() -> None:
    check_window("Mangrove Bay", MB_HOURS, time(6, 30), time(19, 0), label="option 1")
    check_window("Nowhere", ALL_DAY, time(0, 0), time(23, 45), label="option 1")
    with pytest.raises(InvalidInputError):
        check_window("Mangrove Bay", MB_HOURS, time(18, 0), time(19, 15), label="option 1")
