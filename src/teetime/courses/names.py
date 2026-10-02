"""Human course names: the ONE place a course id maps to what a person reads.

Course ids (``foreup:mangrove_bay``, ``foreup:19671:2149``) are storage keys, never UI text. The
web pages and the user emails show ``course_display_name(course_id)``; an id with no entry falls
back to the raw id so a newly added course is still identifiable before it gets a name.

``course_signup_url`` is the course's OWN booking site, where a person creates (or checks) the
course login the bot will use: the Connect form links it (operator request 2026-10-01: the first
new user wondered whether connecting created an account; it does not).

``tee_sheet_hours`` is the span of the course's tee sheet, first tee to last, year-round and
generous (operator request 2026-10-02): the booking forms' time pickers list only those hours, per
course, instead of 4 AM or 9 PM, and the server refuses a window outside them
(``web/time_options.py``). A course with no entry gets ``ALL_DAY``. A new course adds all three
entries here (``src/teetime/courses/CLAUDE.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time

from ..core.models import CourseId
from .foreup.mangrove_bay import MANGROVE_BAY_BOOKING_PAGE_URL, MANGROVE_BAY_COURSE_ID
from .teeitup.sydney_marovitz import SYDNEY_MAROVITZ_BOOKING_PAGE_URL, SYDNEY_MAROVITZ_COURSE_ID

__all__ = [
    "ALL_DAY",
    "COURSE_DISPLAY_NAMES",
    "COURSE_SIGNUP_URLS",
    "COURSE_TEE_SHEET_HOURS",
    "TeeSheetHours",
    "course_display_name",
    "course_signup_url",
    "tee_sheet_hours",
]

COURSE_DISPLAY_NAMES: Mapping[CourseId, str] = {
    MANGROVE_BAY_COURSE_ID: "Mangrove Bay",
    SYDNEY_MAROVITZ_COURSE_ID: "Sydney R. Marovitz",
}


def course_display_name(course_id: str, names: Mapping[str, str] | None = None) -> str:
    """The course's human name from ``names`` (default ``COURSE_DISPLAY_NAMES``), else the id."""
    raw = str(course_id)
    if names is None:
        return COURSE_DISPLAY_NAMES.get(CourseId(raw), raw)
    return names.get(raw, raw)


# Where a person creates or checks the course login the bot will use: the course's own booking
# site (ForeUP's booking page carries its "Create account" / sign-in; TeeItUp's likewise).
COURSE_SIGNUP_URLS: Mapping[CourseId, str] = {
    MANGROVE_BAY_COURSE_ID: MANGROVE_BAY_BOOKING_PAGE_URL,
    SYDNEY_MAROVITZ_COURSE_ID: SYDNEY_MAROVITZ_BOOKING_PAGE_URL,
}


def course_signup_url(course_id: str) -> str | None:
    """The course's own booking/sign-in page, or None for a course with no entry (no link shown)."""
    return COURSE_SIGNUP_URLS.get(CourseId(str(course_id)))


@dataclass(frozen=True, slots=True)
class TeeSheetHours:
    """The course's first and last tee time of the day, as a year-round bound: wide enough for the
    longest day (a picker that hides a real June evening tee is worse than one that shows an
    unbookable December one), never a season's exact sheet."""

    first: time
    last: time


# A course with no entry: every quarter hour (the picker's full day), so nothing is refused.
ALL_DAY = TeeSheetHours(first=time(0, 0), last=time(23, 45))

COURSE_TEE_SHEET_HOURS: Mapping[CourseId, TeeSheetHours] = {
    # Live 2026-10-02 (unauthenticated search, party of 2): remaining tee times ran 13:52-17:45 on
    # a Sunday and 11:15-15:22 on a Wednesday; the blind grid starts at 07:00 (07:37 seen live).
    # 6:30 AM-7:00 PM leaves room for the summer evening sheet.
    MANGROVE_BAY_COURSE_ID: TeeSheetHours(first=time(6, 30), last=time(19, 0)),
    # Chicago lakefront, dawn-to-dusk in summer; not observed live yet (Spike S-M4 territory).
    SYDNEY_MAROVITZ_COURSE_ID: TeeSheetHours(first=time(6, 0), last=time(19, 0)),
}


def tee_sheet_hours(
    course_id: str, hours: Mapping[str, TeeSheetHours] | None = None
) -> TeeSheetHours:
    """The course's tee-sheet span from ``hours`` (default ``COURSE_TEE_SHEET_HOURS``), else
    ``ALL_DAY``."""
    raw = str(course_id)
    if hours is None:
        return COURSE_TEE_SHEET_HOURS.get(CourseId(raw), ALL_DAY)
    return hours.get(raw, ALL_DAY)
