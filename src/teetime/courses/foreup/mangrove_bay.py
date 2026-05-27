"""Mangrove Bay Golf Course (St. Petersburg, FL) — ForeUP adapter.

Booking page: https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes

    course_pk          = 19671
    booking_class_id   = 2149
    schedule_id        = 2149  (confirmed from DEFAULT_FILTER in page HTML)

This file is intentionally tiny: all behavior lives in base.py.
Adding another St. Pete muni is a sibling file with new IDs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from ...core.models import CourseId
from .base import FOREUP_BASE_URL, ForeUpAdapter

MANGROVE_BAY_COURSE_ID = CourseId("foreup:mangrove_bay")
MANGROVE_BAY_COURSE_PK = 19671
MANGROVE_BAY_BOOKING_CLASS_ID = 2149  # teesheet/URL ID (appears in booking page URL)
MANGROVE_BAY_SCHEDULE_ID = 2149  # confirmed from DEFAULT_FILTER in booking page HTML
MANGROVE_BAY_PUBLIC_BOOKING_CLASS_ID = 12239  # "Public" class from SCHEDULES JSON
MANGROVE_BAY_BOOKING_PAGE_URL = (
    f"{FOREUP_BASE_URL}/index.php/booking/{MANGROVE_BAY_COURSE_PK}/{MANGROVE_BAY_BOOKING_CLASS_ID}"
)


class MangroveBayAdapter(ForeUpAdapter):
    """Mangrove Bay specialization. Sets the IDs; inherits all HTTP logic from ForeUpAdapter."""

    booking_page_url = MANGROVE_BAY_BOOKING_PAGE_URL

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        captcha_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        super().__init__(
            course_id=MANGROVE_BAY_COURSE_ID,
            course_pk=MANGROVE_BAY_COURSE_PK,
            booking_class_id=MANGROVE_BAY_BOOKING_CLASS_ID,
            schedule_id=MANGROVE_BAY_SCHEDULE_ID,
            public_booking_class_id=MANGROVE_BAY_PUBLIC_BOOKING_CLASS_ID,
            timezone="America/New_York",
            http_client=http_client,
            captcha_provider=captcha_provider,
        )
