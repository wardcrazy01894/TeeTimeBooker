"""Sydney Marovitz Golf Course (Chicago, IL) — TeeItUp adapter.

Booking page: https://sydney-r-marovitz-golf-course.book.teeitup.com/
Platform: TeeItUp (NBC Sports Next / Indigo Sports), operated by Chicago Park District.

Confirmed constants (from three HAR captures):
    course_slug              = "sydney-r-marovitz-golf-course"
    timezone                 = "America/Chicago"
    gn_facility_id           = 4014   (GolfNow facility ID; used in tee-time search)
    gnc_facility_id          = 7218   (GolfNow course ID; used in invoice/pricing calls)
    kenna_facility_id        = "54f14cb60c8ad60378b02bfb"  (Kenna internal facility ID)
    channel_id               = "20972" (TeeItUp/GolfNow inventory channel ID for EZRTS)
    advance_booking_days     = 15     (CPD policy; confirm exact boundary via live test)
    min_players              = 2      (singles must call the shop)
    holes_per_round          = 9      (9-hole par-3 course; set holes=9 in your TOML config)

Credentials: uses type="basic" (native TeeItUp account, not GolfNow OAuth).
Required extras (CourseCredentials.extra) — see TeeItUpAdapter class docstring:
    card_number:         str  — full credit card number
    cvv:                 str  — card CVV
    expiry_month:        str  — expiry month (1-2 digits)
    expiry_year:         str  — expiry year (4 digits)
    billing_address:     str  — billing street address
    billing_postal_code: str  — billing ZIP/postal code
Optional extras:
    billing_country:     str  — ISO country code (default "US")
    name_on_card:        str  — defaults to first+last from auth response

This file is intentionally tiny: all behavior lives in base.py.
Adding another CPD/TeeItUp course is a sibling file with a new slug + IDs.
"""

from __future__ import annotations

import httpx

from ...core.models import CourseId
from .base import TEEITUP_BOOKING_BASE, TeeItUpAdapter

SYDNEY_MAROVITZ_COURSE_ID = CourseId("teeitup:sydney_marovitz")
SYDNEY_MAROVITZ_SLUG = "sydney-r-marovitz-golf-course"
SYDNEY_MAROVITZ_BOOKING_PAGE_URL = TEEITUP_BOOKING_BASE.format(slug=SYDNEY_MAROVITZ_SLUG)
SYDNEY_MAROVITZ_GN_FACILITY_ID = 4014
SYDNEY_MAROVITZ_GNC_FACILITY_ID = 7218
SYDNEY_MAROVITZ_KENNA_FACILITY_ID = "54f14cb60c8ad60378b02bfb"
SYDNEY_MAROVITZ_CHANNEL_ID = "20972"
SYDNEY_MAROVITZ_ADVANCE_BOOKING_DAYS = 15
SYDNEY_MAROVITZ_HOLES = 9


class SydneyMarovitzAdapter(TeeItUpAdapter):
    """Sydney Marovitz specialization. Sets all IDs; inherits HTTP logic."""

    booking_page_url = SYDNEY_MAROVITZ_BOOKING_PAGE_URL

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            course_id=SYDNEY_MAROVITZ_COURSE_ID,
            course_slug=SYDNEY_MAROVITZ_SLUG,
            timezone="America/Chicago",
            gn_facility_id=SYDNEY_MAROVITZ_GN_FACILITY_ID,
            gnc_facility_id=SYDNEY_MAROVITZ_GNC_FACILITY_ID,
            kenna_facility_id=SYDNEY_MAROVITZ_KENNA_FACILITY_ID,
            channel_id=SYDNEY_MAROVITZ_CHANNEL_ID,
            http_client=http_client,
        )
