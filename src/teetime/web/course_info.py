"""Per-course facts a person needs around connecting a course (operator requests 2026-10-01).

The first new user connected Mangrove Bay and then asked when to book: every course now states
its release cycle, on its Connected Courses card, under the Connect form and next to both booking
forms. The words come from the course's ``ReleasePolicy`` (the same data the booker's cron and the
materializer run on) and the configured booking cutoff, so the page can never state a rule the bot
does not follow. Pure functions; ``tests/web/test_web_course_info.py`` pins the wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from ..core.release_policy import ReleasePolicy

# US zones a golfer reads as a word; anything else falls back to the city ("Dublin time").
ZONE_LABELS: dict[str, str] = {
    "America/New_York": "Eastern",
    "America/Chicago": "Central",
    "America/Denver": "Mountain",
    "America/Phoenix": "Arizona",
    "America/Los_Angeles": "Pacific",
    "America/Anchorage": "Alaska",
    "Pacific/Honolulu": "Hawaii",
}


def zone_label(timezone: str) -> str:
    label = ZONE_LABELS.get(timezone)
    if label is not None:
        return label
    return f"{timezone.rsplit('/', 1)[-1].replace('_', ' ')} time"


@dataclass(frozen=True, slots=True)
class ReleaseCycle:
    opens: str  # "Tee times open 7 days ahead, at 6:00 AM Eastern."
    tip: str  # when to book for first pick, and what happens for a closer date


def _clock(t: time) -> str:
    return f"{t:%I:%M %p}".lstrip("0")


def release_cycle(policy: ReleasePolicy, *, cutoff_text: str) -> ReleaseCycle:
    """The course's release cycle in two sentences. ``cutoff_text`` is ``ranking_explainer.
    cutoff_text`` of the configured cutoff ("4 PM the day before")."""
    when = f"at {_clock(policy.release_time)} {zone_label(policy.timezone)}"
    n = policy.advance_days
    if n == 0:
        return ReleaseCycle(
            opens=f"Tee times open the same day, {when}.",
            # No cutoff clause: a same-day release cannot also freeze "the day before".
            tip="The bot checks every few minutes, books what is free and keeps watching for "
            "cancellations.",
        )
    days = "1 day" if n == 1 else f"{n} days"
    return ReleaseCycle(
        opens=f"Tee times open {days} ahead, {when}.",
        tip=(
            f"For first pick of the tee sheet, book a date {n} or more days out: the bot is "
            "there the moment it opens. A closer date is already open, so the bot books what is "
            f"still free and keeps watching for cancellations until {cutoff_text}."
        ),
    )
