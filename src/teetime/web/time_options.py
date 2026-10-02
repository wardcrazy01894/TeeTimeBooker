"""The booking forms' time pickers (operator request 2026-10-02): a ``<select>`` of quarter hours
bounded by the course's tee-sheet hours (``courses/names.py::COURSE_TEE_SHEET_HOURS``), so nobody
scrolls past 4 AM or 9 PM. Pure functions:

- ``time_options(hours)``: the choices, ``STEP_MINUTES`` apart, first tee to last inclusive, an
  off-grid bound rounded OUTWARD (never hide a real tee time).
- ``union_hours(...)``: what a form shows before a course is chosen, or with script off (the
  ranked form has a course dropdown per row); ``app.js`` then narrows each row to the chosen
  course's ``data-first`` / ``data-last``.
- ``with_values(options, *times)``: a window saved before the picker (or typed on another form)
  stays selectable on its edit form.
- ``check_window(...)``: the server-side rule. The browser list is a convenience; this is the
  guarantee, and it names the course, its hours and the offending times (never an option
  number: ranks are renumbered 1..N, so a number could differ from the row the person typed).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import time

from ..courses.names import ALL_DAY, TeeSheetHours
from .services import InvalidInputError

STEP_MINUTES = 15


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _at(minutes: int) -> time:
    minutes = min(minutes, 23 * 60 + 45)
    return time(minutes // 60, minutes % 60)


def time_options(hours: TeeSheetHours) -> list[time]:
    first = _minutes(hours.first) // STEP_MINUTES * STEP_MINUTES
    last = -(-_minutes(hours.last) // STEP_MINUTES) * STEP_MINUTES
    return [_at(m) for m in range(first, last + 1, STEP_MINUTES)]


def union_hours(hours: Iterable[TeeSheetHours]) -> TeeSheetHours:
    spans = list(hours)
    if not spans:
        return ALL_DAY
    return TeeSheetHours(first=min(h.first for h in spans), last=max(h.last for h in spans))


def with_values(options: Iterable[time], *values: time) -> list[time]:
    return sorted(set(options) | set(values))


def time_label(t: time) -> str:
    """``6:30 AM``, ``12:00 PM``: the picker's wording, and the error message's."""
    return f"{t:%I:%M %p}".lstrip("0")


def check_window(course_name: str, hours: TeeSheetHours, earliest: time, latest: time) -> None:
    """Refuse a window that leaves the course's tee-sheet hours, naming the course, its hours and
    the times the person picked."""
    if hours.first <= earliest and latest <= hours.last:
        return
    raise InvalidInputError(
        f"{course_name} has tee times from {time_label(hours.first)} to "
        f"{time_label(hours.last)}; {time_label(earliest)} to {time_label(latest)} is outside "
        "those hours."
    )
