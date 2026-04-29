"""Shared ForeUP HTTP plumbing. Course-specific files supply only IDs + overrides.

Endpoints confirmed via page-source analysis and community reverse-engineering:

    POST   /index.php/api/booking/users/login
        form: api_key, booking_class_id, password, username
        Pre-warm with GET to booking page first to get PHPSESSID.
        Sets session cookies (PHPSESSID + token JWT).

    GET    /index.php/api/booking/times
        query: time=all, date=M-D-YYYY (no leading zeros), holes, players,
               booking_class=false, schedule_id, specials_only=0, api_key=no_limits
        Returns JSON array of slot objects.

    POST   /index.php/api/booking/users/reservations
        json: echo of slot raw fields + overridden player/fee/total values.
        Returns {id, ...} confirmation.

    GET    /index.php/api/booking/users/reservations
        Returns array of existing user reservations.

Anti-bot etiquette:
    - Honest User-Agent
    - api-key: "no_limits" (community-observed fixed value)
    - 250 ms minimum between non-booking requests
    - RateLimitError on 429 with retry-after pass-through
    - CaptchaError on any captcha/openNewWindow signal
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ...core.adapter import (
    AuthError,
    CaptchaError,
    CourseAdapter,
    InventoryNotPublishedError,
    RateLimitError,
    SlotGoneError,
)
from ...core.models import (
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    Player,
    SlotId,
    TeeTimeSlot,
)

FOREUP_BASE_URL = "https://foreupsoftware.com"
LOGIN_PATH = "/index.php/api/booking/users/login"
TIMES_PATH = "/index.php/api/booking/times"
RESERVATION_PATH = "/index.php/api/booking/users/reservations"
_API_KEY = "no_limits"
_USER_AGENT = "TeeTimeBooker/0.0.0 (+https://github.com/wardcrazy01894/TeeTimeBooker)"
_MIN_BETWEEN_S = 0.25  # anti-bot courtesy delay between non-booking requests
_HTTP_RATE_LIMIT = 429
_HTTP_SLOT_GONE = 409


class ForeUpAdapter(CourseAdapter):
    """Base for any ForeUP-backed course. Subclasses set course_pk, booking_class_id,
    schedule_id, and optionally timezone. All HTTP logic lives here.
    """

    course_id: CourseId

    def __init__(
        self,
        *,
        course_id: CourseId,
        course_pk: int,
        booking_class_id: int,
        schedule_id: int,
        public_booking_class_id: int | None = None,
        timezone: str = "America/New_York",
        http_client: httpx.AsyncClient | None = None,
        captcha_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self.course_id = course_id
        self._course_pk = course_pk
        self._booking_class_id = booking_class_id  # teesheet/URL ID (e.g. 2149)
        self._schedule_id = schedule_id
        # The "Public" booking class used in login + search (e.g. 12239 for Mangrove Bay).
        # Distinct from booking_class_id which is the teesheet URL parameter.
        self._public_booking_class_id = public_booking_class_id or booking_class_id
        self._timezone = timezone
        self._client = http_client
        self._owns_client = http_client is None
        self._logged_in = False  # True only after a successful username/password login
        self._captcha_provider = captcha_provider

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=FOREUP_BASE_URL,
            headers={
                "User-Agent": _USER_AGENT,
                "x-requested-with": "XMLHttpRequest",
                "api-key": _API_KEY,
                "Referer": (
                    f"{FOREUP_BASE_URL}/index.php/booking/"
                    f"{self._course_pk}/{self._booking_class_id}"
                ),
            },
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("authenticate() must be called before search/book/list_reservations")
        return self._client

    def _guard_captcha(self, r: httpx.Response) -> None:
        """Raise CaptchaError on any ForeUP captcha/browser-challenge signal."""
        if "application/json" not in r.headers.get("content-type", ""):
            return
        try:
            data: object = r.json()
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        msg = str(data.get("msg", ""))
        if "captcha" in msg.lower() or data.get("openNewWindow"):
            raise CaptchaError(msg or "browser challenge (openNewWindow) required")

    async def authenticate(self, creds: CourseCredentials) -> None:
        """Warm up PHPSESSID, then attempt username/password login.

        The PHPSESSID warmup alone is enough for search(). A successful login
        is required for book(). If the login fails (e.g. account uses Google
        OAuth), search() still works; book() will raise AuthError.
        """
        if self._client is None:
            self._client = self._make_client()
        # Always warm up — PHPSESSID alone is sufficient for search().
        await self._client.get(
            f"/index.php/booking/{self._course_pk}/{self._booking_class_id}"
        )
        r = await self._client.post(
            LOGIN_PATH,
            data={
                "username": creds.username,
                "password": creds.password,
                "api_key": "",  # booking widget uses empty api_key (not "no_limits")
                "booking_class_id": str(self._public_booking_class_id),
                "course_id": str(self._course_pk),
            },
        )
        self._guard_captcha(r)
        if r.status_code in (400, 401):
            # Login failed — session cookie is still valid for search/list.
            # book() will refuse to proceed without _logged_in=True.
            return
        r.raise_for_status()
        try:
            data: object = r.json()
            if isinstance(data, dict) and not data.get("success", True):
                return  # same as above: partial session only
        except ValueError:
            pass
        self._logged_in = True

    async def search(self, request: BookingRequest) -> list[TeeTimeSlot]:
        """GET /times for each target_date, filter by time_windows/holes/price/spots."""
        client = self._c()
        tz = ZoneInfo(self._timezone)
        results: list[TeeTimeSlot] = []

        for target_date in request.target_dates:
            await asyncio.sleep(_MIN_BETWEEN_S)
            r = await client.get(
                TIMES_PATH,
                params={
                    "time": "all",
                    "date": f"{target_date.month}-{target_date.day}-{target_date.year}",
                    "holes": request.holes,
                    "players": len(request.players),
                    "booking_class": False,
                    "schedule_id": self._schedule_id,
                    "specials_only": 0,
                    "api_key": _API_KEY,
                },
            )
            if r.status_code == _HTTP_RATE_LIMIT:
                raise RateLimitError(
                    "Rate limited by ForeUP",
                    retry_after_s=float(r.headers.get("retry-after", 60)),
                )
            self._guard_captcha(r)
            r.raise_for_status()

            raw_list: Any = r.json() if r.text else []
            if not isinstance(raw_list, list):
                raise InventoryNotPublishedError(f"Unexpected /times shape: {r.text[:200]}")

            for raw in raw_list:
                try:
                    slot = _parse_slot(raw, target_date, self.course_id, tz)
                except (KeyError, ValueError, InvalidOperation):
                    continue
                local_time = slot.tee_time.astimezone(tz).time()
                if not any(w.earliest <= local_time <= w.latest for w in request.time_windows):
                    continue
                if slot.holes != request.holes:
                    continue
                if slot.available_spots < len(request.players):
                    continue
                if (
                    request.max_price_per_player is not None
                    and slot.price_per_player > request.max_price_per_player
                ):
                    continue
                results.append(slot)

        return results

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        """POST /reservations echoing slot raw fields with overridden player/fee totals."""
        if not self._logged_in:
            raise AuthError(
                "Full login required to book. authenticate() must succeed before book(). "
                "Check that MB_USERNAME and MB_PASSWORD are correct."
            )
        client = self._c()
        players = len(request.players)
        green_fee = float(slot.price_per_player)
        total = round(green_fee * players, 2)
        body: dict[str, object] = {
            **slot.raw,
            "players": players,
            "holes": request.holes,
            "green_fee": green_fee,
            "total": total,
            "pay_total": total,
            "pay_subtotal": total,
            "subtotal": total,
            "pay_players": players,
            "carts": 0,
            "pay_carts": 0,
            "cart_fee": 0,
            "promo_code": "",
            "promo_discount": 0,
            "discount": 0,
            "purchased": 0,
            "paid_player_count": 0,
            "discount_percent": 0,
            "player_list": [_player_payload(p) for p in request.players],
        }
        if self._captcha_provider is not None:
            body["captchaid"] = await self._captcha_provider()
        r = await client.post(RESERVATION_PATH, json=body)
        if r.status_code == _HTTP_SLOT_GONE:
            raise SlotGoneError(f"Slot gone (409): {r.text[:300]}")
        self._guard_captcha(r)
        r.raise_for_status()
        data: Any = r.json() if r.text else {}
        conf_raw = data.get("id") or data.get("booking_id") or data.get("confirmation_code")
        conf = str(conf_raw) if conf_raw is not None else None
        return BookingResult(
            request_id=request.request_id,
            outcome=BookingOutcome.BOOKED,
            course_id=self.course_id,
            slot=slot,
            confirmation_code=conf,
            booked_at=datetime.now(UTC),
            attempts=1,
            diagnostics={"foreup_response": data},
        )

    async def list_reservations(self) -> list[ExistingReservation]:
        """GET /reservations and return all existing bookings for the authenticated user."""
        client = self._c()
        await asyncio.sleep(_MIN_BETWEEN_S)
        r = await client.get(RESERVATION_PATH)
        r.raise_for_status()
        raw: Any = r.json() if r.text else []
        items: list[Any] = (
            raw if isinstance(raw, list)
            else raw.get("data", []) if isinstance(raw, dict)
            else []
        )
        tz = ZoneInfo(self._timezone)
        out: list[ExistingReservation] = []
        for item in items:
            try:
                out.append(_parse_reservation(item, self.course_id, tz))
            except (KeyError, ValueError, TypeError):
                continue
        return out

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


# --- Free functions -------------------------------------------------------


def _player_payload(player: Player) -> dict[str, str]:
    """Map a Player to the object ForeUP's booking POST expects."""
    payload: dict[str, str] = {
        "first_name": player.first_name,
        "last_name": player.last_name,
        "email": player.email,
    }
    if player.phone:
        payload["phone"] = player.phone
    if player.member_number:
        payload["member_id"] = player.member_number
    return payload


# --- Parsing raw ForeUP JSON ----------------------------------------------


def _parse_slot(
    raw: dict[str, Any], target_date: date, course_id: CourseId, tz: ZoneInfo
) -> TeeTimeSlot:
    """Map one ForeUP /times item to a TeeTimeSlot.

    ForeUP returns time as "YYYY-MM-DD HH:MM" (full datetime string).
    teesheet_id is the schedule (same for all slots); start_front is the
    unique per-slot integer used as slot_id.
    """
    time_str = str(raw["time"])
    if " " in time_str:
        # "YYYY-MM-DD HH:MM" — ForeUP's actual format
        tee_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    else:
        # "HH:MM:SS" or "HH:MM" — fallback for alternate format
        _min_parts = 2
        parts = time_str.split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > _min_parts else 0
        tee_time = datetime(
            target_date.year, target_date.month, target_date.day, h, m, s, tzinfo=tz
        )
    slot_id = str(raw.get("start_front") or raw.get("teesheet_side_id") or raw["teesheet_id"])
    return TeeTimeSlot(
        course_id=course_id,
        slot_id=SlotId(slot_id),
        tee_time=tee_time,
        holes=int(raw.get("holes", 18)),
        available_spots=int(raw.get("available_spots", 4)),
        price_per_player=Decimal(str(raw.get("green_fee", "0"))).quantize(Decimal("0.01")),
        cart_included=str(raw.get("rate_type", "")).lower() in ("cart", "riding"),
        raw=dict(raw),
    )


def _parse_reservation(
    item: dict[str, Any], course_id: CourseId, tz: ZoneInfo
) -> ExistingReservation:
    """Map one ForeUP reservation item to an ExistingReservation."""
    raw_t = str(item.get("tee_time") or item.get("teetime") or item.get("time") or "")
    tee_time: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            tee_time = datetime.strptime(raw_t, fmt).replace(tzinfo=tz)
            break
        except ValueError:
            continue
    if tee_time is None:
        raise ValueError(f"Cannot parse tee_time: {raw_t!r}")
    conf = str(item.get("id") or item.get("booking_id") or item.get("confirmation_code") or "")
    return ExistingReservation(
        course_id=course_id,
        confirmation_code=conf,
        tee_time=tee_time,
        party_size=int(item.get("players") or 1),
        raw=dict(item),
    )
