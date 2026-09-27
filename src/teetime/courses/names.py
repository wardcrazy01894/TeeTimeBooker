"""Human course names: the ONE place a course id maps to what a person reads.

Course ids (``foreup:mangrove_bay``, ``foreup:19671:2149``) are storage keys, never UI text. The
web pages and the user emails show ``course_display_name(course_id)``; an id with no entry falls
back to the raw id so a newly added course is still identifiable before it gets a name.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..core.models import CourseId
from .foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID
from .teeitup.sydney_marovitz import SYDNEY_MAROVITZ_COURSE_ID

__all__ = ["COURSE_DISPLAY_NAMES", "course_display_name"]

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
