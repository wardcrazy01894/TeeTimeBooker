"""Human course names: the ONE place a course id maps to what a person reads.

Course ids (``foreup:mangrove_bay``, ``foreup:19671:2149``) are storage keys, never UI text. The
web pages and the user emails show ``course_display_name(course_id)``; an id with no entry falls
back to the raw id so a newly added course is still identifiable before it gets a name.

``course_signup_url`` is the course's OWN booking site, where a person creates (or checks) the
course login the bot will use: the Connect form links it (operator request 2026-10-01: the first
new user wondered whether connecting created an account; it does not). A new course adds both
entries here (``src/teetime/courses/CLAUDE.md``).
"""

from __future__ import annotations

from collections.abc import Mapping

from ..core.models import CourseId
from .foreup.mangrove_bay import MANGROVE_BAY_BOOKING_PAGE_URL, MANGROVE_BAY_COURSE_ID
from .teeitup.sydney_marovitz import SYDNEY_MAROVITZ_BOOKING_PAGE_URL, SYDNEY_MAROVITZ_COURSE_ID

__all__ = [
    "COURSE_DISPLAY_NAMES",
    "COURSE_SIGNUP_URLS",
    "course_display_name",
    "course_signup_url",
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
