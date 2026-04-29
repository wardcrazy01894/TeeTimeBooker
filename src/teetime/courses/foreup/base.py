"""Shared ForeUP HTTP plumbing. Course-specific files supply only IDs + overrides.

Endpoints (community-confirmed via reverse-engineering reads — see PLAN.md
"Spike S1: confirm ForeUP endpoints"):

    POST   /index.php/api/booking/users/login
        form: api_key, booking_class_id, password, username
        returns JWT in Set-Cookie / response body; PHPSESSID cookie tracked.

    GET    /index.php/api/booking/times
        query: time, date (MM-DD-YYYY), holes, players, booking_class,
               schedule_id, specials_only, api_key
        returns array of slot objects.

    POST   /index.php/api/booking/users/reservations
        json: { teesheet_id, course_id, course_name, time, schedule_name, holes,
                green_fee, players, ...promo fields }
        returns reservation confirmation.

Anti-bot etiquette enforced here:
    - User-Agent: identifies us honestly ("TeeTimeBooker/0.0.0 (+contact email)")
    - api-key header: "no_limits" (community-observed value; will revisit if it
      proves to be a bot signal — see Spike S1)
    - request rate cap (>= 250ms between calls except during the T0 race window)
    - automatic backoff on 429 / 503

Captcha contingency: if a response carries a captcha challenge, raise
CaptchaError immediately. v0 does not solve them; orchestrator notifies user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.adapter import CourseAdapter
from ...core.models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
)

if TYPE_CHECKING:
    import httpx


FOREUP_BASE_URL = "https://foreupsoftware.com"
LOGIN_PATH = "/index.php/api/booking/users/login"
TIMES_PATH = "/index.php/api/booking/times"
RESERVATION_PATH = "/index.php/api/booking/users/reservations"

DEFAULT_USER_AGENT = "TeeTimeBooker/0.0.0 (+https://github.com/alanc3939/TeeTimeBooker)"
DEFAULT_API_KEY_HEADER = "no_limits"


class ForeUpAdapter(CourseAdapter):
    """Base class for any ForeUP-backed course. Subclasses set course_id, course_pk,
    booking_class_id, schedule_id, and any per-course quirks. Stub.

    Note: declared as a class implementing the Protocol — useful for shared
    state / mixins. Subclasses inherit __init__; they only need to set the
    config dataclass below.
    """

    course_id: CourseId

    def __init__(
        self,
        *,
        course_id: CourseId,
        course_pk: int,            # the numeric ID after /booking/ in the URL
        booking_class_id: int,     # the second numeric ID
        schedule_id: int | None,   # may equal course_pk; some courses split them
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.course_id = course_id
        self._course_pk = course_pk
        self._booking_class_id = booking_class_id
        self._schedule_id = schedule_id
        self._client = http_client  # injected for testability; built in connect()

    async def authenticate(self, creds: CourseCredentials) -> None:
        raise NotImplementedError

    async def search(self, request: BookingRequest) -> list[TeeTimeSlot]:
        raise NotImplementedError

    async def book(
        self,
        slot: TeeTimeSlot,
        request: BookingRequest,
    ) -> BookingResult:
        raise NotImplementedError

    async def list_reservations(self) -> list[ExistingReservation]:
        # M5.T3: GET /index.php/api/booking/users/reservations (or similar —
        # confirmed in Spike S1) and map to ExistingReservation. Read-only.
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError
