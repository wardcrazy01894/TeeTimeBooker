"""Mangrove Bay Golf Course (St. Petersburg, FL) — ForeUP adapter.

Booking page (confirmed live): https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes

    course_pk          = 19671
    booking_class_id   = 2149
    schedule_id        = TBD — must be confirmed in Spike S1 (likely 19671 or a
                         distinct integer surfaced by the GET /times response).

This file is intentionally tiny: behavior lives in `base.py`. Adding another
St. Pete muni (e.g. Twin Brooks, Cypress Links) is a sibling file with new IDs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.models import CourseId
from .base import ForeUpAdapter

if TYPE_CHECKING:
    import httpx

MANGROVE_BAY_COURSE_ID = CourseId("foreup:mangrove_bay")
MANGROVE_BAY_COURSE_PK = 19671
MANGROVE_BAY_BOOKING_CLASS_ID = 2149
MANGROVE_BAY_SCHEDULE_ID: int | None = None  # confirm in Spike S1


class MangroveBayAdapter(ForeUpAdapter):
    """Mangrove Bay specialization. Sets the IDs; otherwise inherits from ForeUpAdapter."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            course_id=MANGROVE_BAY_COURSE_ID,
            course_pk=MANGROVE_BAY_COURSE_PK,
            booking_class_id=MANGROVE_BAY_BOOKING_CLASS_ID,
            schedule_id=MANGROVE_BAY_SCHEDULE_ID,
            http_client=http_client,
        )
