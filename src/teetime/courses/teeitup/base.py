"""Shared TeeItUp HTTP plumbing. Course-specific files supply only IDs + overrides.

Platform: TeeItUp by NBC Sports Next / Indigo Sports.
Backend API: https://phx-api-be-east-1b.kenna.io  (Kenna platform)
Booking frontend: https://{course_slug}.book.teeitup.com/

Confirmed endpoints (from HAR captures, Sydney Marovitz session):

    POST   /profile/authenticate
        json: {username, credentials, type: "basic"|"golfid"}
        Required headers: x-be-alias: {slug}
        Response: {sessionToken: "Fe26.2**...", customer: {...}}

    GET    /v2/tee-times
        query: date=YYYY-MM-DD, facilityIds={gn_facility_id}, returnPromotedRates=true

    GET    /tee-times/rate/{rateId}/invoice
        query: gncFacilityId={gnc_facility_id}, playerCount=N
        Response: full pricing, TeeTimeNotes, TermsAndConditions.

    --- Booking flow (confirmed from successful HAR, 4-person booking 2026-05-29) ---

    1.  GET    /tee-times/rate/{rateId}/invoice   (fetch terms + notes)
    2.  POST   /shopping-cart                      (server creates cart; returns {id, items:[]})
    3.  POST   /shopping-cart/{cartId}/cart-item   (add slot; include terms + notes from invoice)
    4.  PUT    /course/{kenna_id}/tee-time/lock    (json: {teetime, slots, expiresIn:10})
    5.  POST   /orders                             (json: {language:"en", cartId})
    6.  POST   /shopping-cart/{cartId}/cart-item/{itemId}/is-bookable (graceful 404)
    7.  POST   /order-teetime
                (json: {teetime,rateId,cartId,cartItemId,golferQuantity})
                Returns invoice with referenceId.
    8.  PUT    /v2/profile                         (sync customer details)
    9.  GET    /tr/token                           (transaction token for GNSVC payment)
    10. POST   https://tr.gnsvc.com/AddReservation
                (form-encoded; card number/expiry/CVV/billing direct)
                Returns {ReservationStatusID, Success, PaymentStatus}.
    11. PATCH  /order-teetime/status/{ReservationStatusID}?cartId=...&cartItemId=...
                                                   Returns state:"fulfilled" + gncReservationId.

    Note: Native TeeItUp accounts do NOT use GET /wallet or POST /reservation.
    Card details go directly to tr.gnsvc.com. The gncReservationId from step 11
    is the cancellation ID.

    list_reservations():
        GET /reservation/history?playDateMin={ISO datetime}
        Returns {reservations: {Reservations: [{ReservationID, ConfirmationNumber, Invoice, ...}]}}

    cancel_reservation():
        PUT /reservations/{ReservationID}/cancel
        json: {players: 0, reason: 7}   (reason 7 = "Other")
        200 = success OR already-cancelled (live observation: repeat cancel returns 200, not 404).
        404 = also treated as success (idempotent belt-and-suspenders).

Anti-bot etiquette:
    - Honest User-Agent
    - 250 ms minimum between non-booking requests
    - RateLimitError on 429
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

import httpx

from ...core.adapter import (
    AuthError,
    CancelError,
    InventoryNotPublishedError,
    RateLimitError,
    SlotGoneError,
)
from ...core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    SlotId,
    TeeTimeSlot,
)

_log = logging.getLogger(__name__)

_KENNA_API_BASE = "https://phx-api-be-east-1b.kenna.io"
_AUTH_PATH = "/profile/authenticate"
_TEE_TIMES_PATH = "/v2/tee-times"
_INVOICE_PATH = "/tee-times/rate/{rate_id}/invoice"
_SHOPPING_CART_PATH = "/shopping-cart"
_LOCK_PATH = "/course/{facility_id}/tee-time/lock"
_ORDERS_PATH = "/orders"
_ORDER_TEETIME_PATH = "/order-teetime"
_PROFILE_PATH = "/v2/profile"
_TR_TOKEN_PATH = "/tr/token"
_ORDER_TEETIME_STATUS_PATH = "/order-teetime/status/{reservation_status_id}"
_RESERVATION_HISTORY_PATH = "/reservation/history"
_CANCEL_PATH = "/reservations/{reservation_id}/cancel"

_GNSVC_BASE = "https://tr.gnsvc.com"
_ADD_RESERVATION_PATH = "/AddReservation"
_US_E164_DIGIT_COUNT = 11  # country-code (1) + 10-digit subscriber number
_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_NO_CONTENT = 204
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404
_HTTP_UNPROCESSABLE_ENTITY = 422
_HTTP_CONFLICT = 409

TEEITUP_BOOKING_BASE = "https://{slug}.book.teeitup.com"
_USER_AGENT = "TeeTimeBooker/0.0.0 (+https://github.com/wardcrazy01894/TeeTimeBooker)"
_MIN_BETWEEN_S = 0.25
_HTTP_RATE_LIMIT = 429
_HTTP_SERVER_ERROR = 500


def _raise_for_booking_step(r: httpx.Response, slot_id: str, step: str) -> None:
    """Map a non-success response at a PRE-PAYMENT reservation step to the right exception.

    A 4xx client error means the slot could not be held (taken / lock contention / stale
    cart) and NO reservation or charge was created — raise SlotGoneError so the orchestrator
    falls through to the next candidate, mirroring ForeUP's 4xx->SlotGoneError contract
    (see root CLAUDE.md "A book-POST 4xx is a try-next-slot signal"). EXCEPT a 429, which is
    a throttle signal, not a gone slot — surface it as RateLimitError (consistent with
    authenticate/search) so it is not silently swallowed. A 5xx is AMBIGUOUS — the request
    may have landed — so it propagates via raise_for_status (the §9 UNCERTAIN case). Drop-in
    for ``r.raise_for_status()``: a no-op on 2xx. MUST only be called at a step BEFORE the
    irreversible GNSVC payment (steps 3-7), never on the card POST."""
    if r.status_code == _HTTP_RATE_LIMIT:
        raise RateLimitError(f"Rate limited at {step} (429)")
    if _HTTP_BAD_REQUEST <= r.status_code < _HTTP_SERVER_ERROR:
        raise SlotGoneError(f"Slot {slot_id}: {step} returned {r.status_code} (slot unavailable)")
    r.raise_for_status()


class TeeItUpAdapter:
    """Base adapter for TeeItUp-backed courses. Implements the CourseAdapter Protocol.

    All methods are fully implemented. Subclasses supply the course-specific IDs
    (gn_facility_id, gnc_facility_id, kenna_facility_id, channel_id) and timezone;
    all HTTP logic lives here.

    Credentials requirements (via CourseCredentials.extra):
        cvv:                  str  — card CVV
        card_number:          str  — full credit card number
        expiry_month:         str  — expiry month (1 or 2 digits, e.g. "10")
        expiry_year:          str  — expiry year (4 digits, e.g. "2030")
        billing_address:      str  — street address for billing
        billing_postal_code:  str  — postal/ZIP code for billing
        billing_country:      str  — ISO country code, default "US"
        name_on_card:         str  — optional; defaults to first+last from auth response
    """

    booking_page_url: ClassVar[str] = ""

    def __init__(
        self,
        *,
        course_id: CourseId,
        course_slug: str,
        timezone: str,
        gn_facility_id: int,
        gnc_facility_id: int,
        kenna_facility_id: str,
        channel_id: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.course_id = course_id
        self._slug = course_slug
        self._timezone = timezone
        self._gn_facility_id = gn_facility_id
        self._gnc_facility_id = gnc_facility_id
        self._kenna_facility_id = kenna_facility_id
        self._channel_id = channel_id
        self._booking_origin = TEEITUP_BOOKING_BASE.format(slug=course_slug)
        self._session_token: str | None = None
        self._customer: dict[str, Any] = {}
        self._phone_number: str = ""
        self._cvv: str = ""
        self._card_number: str = ""
        self._expiry_month: str = ""
        self._expiry_year: str = ""
        self._name_on_card: str = ""
        self._billing_address: str = ""
        self._billing_postal_code: str = ""
        self._billing_country: str = "US"
        self._http = http_client or httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Headers
    # ------------------------------------------------------------------

    def _common_headers(self) -> dict[str, str]:
        return {
            "x-be-alias": self._slug,
            "origin": self._booking_origin,
            "referer": self._booking_origin + "/",
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
        }

    def _authed_headers(self) -> dict[str, str]:
        if not self._session_token:
            raise RuntimeError(
                "authenticate() must be called before search()/book(). "
                "No session token is available."
            )
        return {**self._common_headers(), "session": self._session_token}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _phone_digits(raw: str) -> str:
        """Strip to 10 US digits (drop country code prefix if present)."""
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) == _US_E164_DIGIT_COUNT and digits.startswith("1"):
            digits = digits[1:]
        return digits

    def _parse_slot(
        self, teetime: dict[str, Any], request: BookingRequest, tz: ZoneInfo
    ) -> TeeTimeSlot | None:
        party_size = len(request.players)
        max_players = int(teetime.get("maxPlayers") or 0)
        booked_players = int(teetime.get("bookedPlayers") or 0)
        available = max_players - booked_players

        if available < party_size:
            return None

        matching_rate: dict[str, Any] | None = None
        for rate in teetime.get("rates") or []:
            if party_size in (rate.get("allowedPlayers") or []):
                matching_rate = rate
                break
        if matching_rate is None:
            return None

        rate_holes = int(matching_rate.get("holes") or 0)
        if request.holes and rate_holes and rate_holes != request.holes:
            return None

        raw_tt = str(teetime.get("teetime") or "")
        if not raw_tt:
            return None
        tee_time_utc = datetime.fromisoformat(raw_tt.replace("Z", "+00:00"))
        tee_time_local = tee_time_utc.astimezone(tz)

        slot_time = tee_time_local.time().replace(second=0, microsecond=0)
        in_window = any(w.earliest <= slot_time <= w.latest for w in request.time_windows)
        green_fee_cents = int(matching_rate.get("greenFeeWalking") or 0)
        price_per_player = Decimal(green_fee_cents) / 100
        over_price = (
            request.max_price_per_player is not None
            and price_per_player > request.max_price_per_player
        )
        if not in_window or over_price:
            return None

        return TeeTimeSlot(
            course_id=self.course_id,
            slot_id=SlotId(str(matching_rate.get("_id"))),
            tee_time=tee_time_local,
            holes=rate_holes,
            available_spots=available,
            price_per_player=price_per_player,
            cart_included=False,
            raw={
                "teetime": teetime,
                "rate": matching_rate,
                "gncFacilityId": self._gnc_facility_id,
            },
        )

    # ------------------------------------------------------------------
    # CourseAdapter Protocol
    # ------------------------------------------------------------------

    async def authenticate(self, creds: CourseCredentials) -> None:
        """Establish an authenticated session and cache card credentials."""
        auth_type = creds.extra.get("auth_type", "basic")
        payload = {"username": creds.username, "credentials": creds.password, "type": auth_type}
        r = await self._http.post(
            f"{_KENNA_API_BASE}{_AUTH_PATH}", json=payload, headers=self._common_headers()
        )
        if r.status_code == _HTTP_UNAUTHORIZED:
            raise AuthError(f"TeeItUp login rejected for {creds.username!r}")
        if r.status_code == _HTTP_RATE_LIMIT:
            retry = r.headers.get("retry-after")
            raise RateLimitError(
                "TeeItUp rate-limited authenticate()",
                retry_after_s=float(retry) if retry else None,
            )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        self._session_token = data["sessionToken"]
        self._customer = data.get("customer") or {}
        phone_numbers = self._customer.get("phoneNumbers") or [{}]
        self._phone_number = str(phone_numbers[0].get("value", ""))
        _log.info("TeeItUp authenticated: user=%s", self._customer.get("username"))

        # Cache card credentials for book()
        self._cvv = creds.extra.get("cvv", "")
        self._card_number = creds.extra.get("card_number", "")
        self._expiry_month = str(creds.extra.get("expiry_month", ""))
        self._expiry_year = str(creds.extra.get("expiry_year", ""))
        self._billing_address = creds.extra.get("billing_address", "")
        self._billing_postal_code = creds.extra.get("billing_postal_code", "")
        self._billing_country = creds.extra.get("billing_country", "US")
        name = self._customer.get("name", {})
        default_name = f"{name.get('given', '')} {name.get('family', '')}".strip()
        self._name_on_card = creds.extra.get("name_on_card", default_name)

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        """Return slots matching request criteria.

        ``skip_initial_spacing`` is accepted for CourseAdapter Protocol parity (Change D /
        PR3) and ignored — TeeItUp is not on the ForeUP race path and has no leading sleep.
        """
        _ = self._authed_headers()
        tz = ZoneInfo(self._timezone)
        slots: list[TeeTimeSlot] = []

        for target_date in request.target_dates:
            date_str = target_date.strftime("%Y-%m-%d")
            r = await self._http.get(
                f"{_KENNA_API_BASE}{_TEE_TIMES_PATH}",
                params={
                    "date": date_str,
                    "facilityIds": self._gn_facility_id,
                    "returnPromotedRates": "true",
                },
                headers=self._authed_headers(),
            )
            if r.status_code == _HTTP_RATE_LIMIT:
                retry = r.headers.get("retry-after")
                raise RateLimitError(
                    "TeeItUp rate-limited search()",
                    retry_after_s=float(retry) if retry else None,
                )
            if r.status_code in (_HTTP_BAD_REQUEST, _HTTP_UNPROCESSABLE_ENTITY):
                raise InventoryNotPublishedError(
                    f"TeeItUp returned HTTP {r.status_code} for {date_str}"
                    " — window may not be open yet"
                )
            r.raise_for_status()

            for day in r.json():
                for teetime in day.get("teetimes") or []:
                    slot = self._parse_slot(teetime, request, tz)
                    if slot is not None:
                        slots.append(slot)

            await asyncio.sleep(_MIN_BETWEEN_S)

        _log.info("TeeItUp search: found %d matching slots", len(slots))
        return slots

    async def prepare_book(self, slot: TeeTimeSlot | None, request: BookingRequest) -> None:
        """No-op — TeeItUp uses session auth, no CAPTCHA pre-fetch required."""
        return

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        """Commit the booking via the Kenna+GNSVC flow (11 steps, confirmed from HAR).

        Requires credentials.extra with card_number, cvv, expiry_month, expiry_year,
        billing_address, billing_postal_code (set during authenticate()).
        dry_run=True halts before POST /order-teetime.
        """
        h = self._authed_headers()
        teetime_raw = cast(dict[str, Any], slot.raw["teetime"])
        rate_raw = cast(dict[str, Any], slot.raw["rate"])
        teetime_utc: str = str(teetime_raw["teetime"])
        rate_id = int(slot.slot_id)
        party_size = len(request.players)

        # Step 1: Fetch invoice for terms text
        invoice_r = await self._http.get(
            f"{_KENNA_API_BASE}{_INVOICE_PATH.format(rate_id=rate_id)}",
            params={"gncFacilityId": self._gnc_facility_id, "playerCount": party_size},
            headers=h,
        )
        invoice_r.raise_for_status()
        invoice_resp: dict[str, Any] = invoice_r.json()
        tee_time_notes: str = ""
        terms_and_conditions: str = ""
        for item in invoice_resp.get("PolicyItems") or []:
            key = item.get("Key", "")
            if key == "TEE_TIME_NOTES":
                tee_time_notes = item.get("Details", "")
            elif key == "TEE_TIME_POLICY":
                terms_and_conditions = item.get("Details", "")

        # Step 2: Create shopping cart (server assigns ID)
        r = await self._http.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}", headers=h)
        r.raise_for_status()
        cart_id: str = r.json()["id"]

        # Step 3: Add cart item
        cart_body = {
            "item": {
                "facilityId": self._gn_facility_id,
                "type": "TeeTime",
                "extra": {
                    "teetime": teetime_utc,
                    "players": party_size,
                    "groupSize": 1,
                    "isPnasSelected": False,
                    "price": float(slot.price_per_player),
                    "rate": {
                        "holes": slot.holes,
                        "price": float(slot.price_per_player),
                        "rateId": rate_id,
                        "rateSetId": self._gnc_facility_id,
                        "name": rate_raw.get("name", "9 Holes"),
                        "transactionFees": 0,
                        "transportation": "Walking",
                        "isSimulator": False,
                    },
                    "productLineups": [],
                    "termsAndConditions": terms_and_conditions,
                    "teetimeNotes": tee_time_notes,
                    "slots": [],
                },
            }
        }
        cart_item_url = f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}/{cart_id}/cart-item"
        r = await self._http.post(cart_item_url, json=cart_body, headers=h)
        if r.status_code == _HTTP_CONFLICT:
            raise SlotGoneError(f"Slot {slot.slot_id} unavailable adding to cart")
        _raise_for_booking_step(r, slot.slot_id, "add to cart")
        cart_item_id: str = r.json()["items"][0]["id"]

        # Step 4: Lock tee time
        r = await self._http.put(
            f"{_KENNA_API_BASE}{_LOCK_PATH.format(facility_id=self._kenna_facility_id)}",
            json={"teetime": teetime_utc, "slots": party_size, "expiresIn": 10},
            headers=h,
        )
        if r.status_code not in (_HTTP_OK, _HTTP_NO_CONTENT):
            _raise_for_booking_step(r, slot.slot_id, "lock tee time")

        # Step 5: Create order
        r = await self._http.post(
            f"{_KENNA_API_BASE}{_ORDERS_PATH}",
            json={"language": "en", "cartId": cart_id},
            headers=h,
        )
        if r.status_code not in (_HTTP_OK, _HTTP_CREATED):
            _raise_for_booking_step(r, slot.slot_id, "create order")

        # Step 6: Check bookable (graceful 404 — endpoint may be browser-session-scoped)
        r = await self._http.post(
            f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}/{cart_id}/cart-item/{cart_item_id}/is-bookable",
            json={"reservationCountsByTime": {}},
            headers=h,
        )
        if r.status_code == _HTTP_NOT_FOUND:
            _log.debug("is-bookable 404 — skipping, relying on order-teetime")
        elif r.is_success and not r.json().get("bookable"):
            raise SlotGoneError(f"Slot {slot.slot_id} is no longer bookable")
        elif not r.is_success and r.status_code != _HTTP_NOT_FOUND:
            # Same pre-payment 4xx->SlotGone / 429->RateLimit / 5xx->propagate contract as
            # the other steps (this is the last remaining non-404 client-error path here).
            _raise_for_booking_step(r, slot.slot_id, "is-bookable check")

        # dry_run: halt before irreversible GNSVC calls
        if request.dry_run:
            return BookingResult(
                request_id=request.request_id,
                outcome=BookingOutcome.DRY_RUN,
                course_id=self.course_id,
                slot=slot,
                confirmation_code=None,
                booked_at=None,
                attempts=1,
            )

        # Step 7: POST /order-teetime → get Kenna invoice with referenceId
        r = await self._http.post(
            f"{_KENNA_API_BASE}{_ORDER_TEETIME_PATH}",
            json={
                "teetime": teetime_utc,
                "rateId": rate_id,
                "cartId": cart_id,
                "cartItemId": cart_item_id,
                "golferQuantity": party_size,
            },
            headers=h,
        )
        if r.status_code == _HTTP_CONFLICT:
            raise SlotGoneError(f"Slot {slot.slot_id} taken at order-teetime (409)")
        _raise_for_booking_step(r, slot.slot_id, "order tee time")
        order_teetime_data: dict[str, Any] = r.json()

        kenna_invoice: dict[str, Any] | None = None
        for tt in order_teetime_data.get("teetimes") or []:
            for player in tt.get("players") or []:
                if player.get("isCaptain") and player.get("invoice"):
                    kenna_invoice = player["invoice"]
                    break
            if kenna_invoice:
                break

        if not kenna_invoice:
            raise RuntimeError(
                f"No invoice in order-teetime response (order {order_teetime_data.get('id')}). "
                "Cannot proceed to payment."
            )

        # Step 8: Sync profile with GolfNow
        name = self._customer.get("name", {})
        profile_body = {
            "emailAddress": self._customer.get("username", ""),
            "profileDetails": {
                "firstName": name.get("given", ""),
                "lastName": name.get("family", ""),
                "phoneNumber": self._phone_number,
                "facilities": [
                    {
                        "gnFacilityId": self._gn_facility_id,
                        "marketing": {"emailOptIn": False, "smsOptIn": False},
                        "transactional": {"smsOptIn": False},
                    }
                ],
            },
        }
        r = await self._http.put(f"{_KENNA_API_BASE}{_PROFILE_PATH}", json=profile_body, headers=h)
        if r.is_success and "sessionToken" in r.json():
            self._session_token = r.json()["sessionToken"]
            h = self._authed_headers()

        # Step 9: GET /tr/token — transaction token for GNSVC
        tr = await self._http.get(f"{_KENNA_API_BASE}{_TR_TOKEN_PATH}", headers=h)
        tr.raise_for_status()
        tr_token: str = tr.json()

        # Step 10: POST https://tr.gnsvc.com/AddReservation — card payment (form-encoded)
        phone_digits = self._phone_digits(self._phone_number)
        add_reservation_form = {
            "TeeTime.InventoryChannelID": self._channel_id,
            "TeeTime.FacilityID": str(self._gn_facility_id),
            "TeeTime.TeeTimeRateID": str(rate_id),
            "TeeTime.PlayerCount": str(party_size),
            "TeeTime.GroupSize": "1",
            "TeeTime.Amount": "-1",
            "TeeTime.ReferenceID": kenna_invoice["referenceId"],
            "Reservation.CustomerEmail": self._customer.get("username", ""),
            "SelectedCourses": str(self._gn_facility_id),
            "ENGINE": "5.0",
            "ALIAS": self._slug,
            "Reservation.TrackingCode": "TL:undefined",
            "tl.holes": str(kenna_invoice.get("holeCount", slot.holes)),
            "EmailCampaignId": "",
            "PaymentReturnURL": f"{self._booking_origin}/payment-authorization",
            "Payment.Name": self._name_on_card,
            "bookerFirstName": name.get("given", ""),
            "bookerLastName": name.get("family", ""),
            "Payments_1_CreditCard_NameOnCard": self._name_on_card,
            "BookerEmail": self._customer.get("username", ""),
            "Payment.Address.Line1": self._billing_address,
            "Payment.Address.PostalCode": self._billing_postal_code,
            "Payment.Address.Country": self._billing_country,
            "tl.customerMobile": phone_digits,
            "Payment.PhoneNumber": phone_digits,
            "Payment.CC.CreditCardNumber": self._card_number,
            "Payment.CC.CVVCode": self._cvv,
            "Payment.CC.ExpirationMonth": self._expiry_month,
            "Payment.CC.ExpirationYear": self._expiry_year,
            "Token": tr_token,
        }
        # follow_redirects=False: a redirect from a payment endpoint is always an
        # error — never silently re-POST card data to the redirect target.
        r = await self._http.post(
            f"{_GNSVC_BASE}{_ADD_RESERVATION_PATH}",
            data=add_reservation_form,
            headers={
                "origin": self._booking_origin,
                "referer": self._booking_origin + "/",
                "accept": "application/json, text/plain, */*",
            },
            timeout=60.0,  # payment processing can be slow
            follow_redirects=False,
        )
        r.raise_for_status()
        gnsvc_data: dict[str, Any] = r.json()
        if not gnsvc_data.get("Success"):
            # Only log safe fields — never echo the full response (may contain card echo).
            raise RuntimeError(
                f"TeeItUp GNSVC payment failed: "
                f"status={gnsvc_data.get('StatusCode')!r} "
                f"message={gnsvc_data.get('Message')!r}"
            )
        reservation_status_id: int = int(gnsvc_data["ReservationStatusID"])
        _log.info("TeeItUp GNSVC payment processed: ReservationStatusID=%d", reservation_status_id)

        # Step 11: PATCH /order-teetime/status/{id} — confirm in Kenna, get gncReservationId.
        # If this fails, the card has already been charged. Log reservation_status_id so it
        # can be surfaced to the operator even if the full confirmation code is unavailable.
        try:
            r = await self._http.patch(
                f"{_KENNA_API_BASE}{_ORDER_TEETIME_STATUS_PATH.format(reservation_status_id=reservation_status_id)}",
                params={"cartId": cart_id, "cartItemId": cart_item_id},
                json={},
                headers=h,
            )
            r.raise_for_status()
        except Exception as exc:
            _log.error(
                "TeeItUp PATCH /order-teetime/status failed after payment succeeded. "
                "Card may have been charged. ReservationStatusID=%d. Manual check required.",
                reservation_status_id,
            )
            raise RuntimeError(
                f"Booking payment succeeded (ReservationStatusID={reservation_status_id}) "
                f"but Kenna confirmation PATCH failed — manual verification required."
            ) from exc
        patch_data: dict[str, Any] = r.json()
        gnc_reservation_id: str = ""
        for tt in patch_data.get("teetimes") or []:
            for player in tt.get("players") or []:
                if player.get("isCaptain"):
                    gnc_reservation_id = str(player.get("gncReservationId", ""))
                    break
            if gnc_reservation_id:
                break

        if not gnc_reservation_id:
            gnc_reservation_id = str(reservation_status_id)
            _log.warning(
                "No gncReservationId in PATCH response; falling back to ReservationStatusID=%d",
                reservation_status_id,
            )

        _log.info("TeeItUp booking confirmed: gncReservationId=%s", gnc_reservation_id)

        return BookingResult(
            request_id=request.request_id,
            outcome=BookingOutcome.BOOKED,
            course_id=self.course_id,
            slot=slot,
            confirmation_code=f"{MANAGED_BOOKING_TAG}{gnc_reservation_id}",
            booked_at=datetime.now(tz=UTC),
            attempts=1,
            diagnostics={
                "gnc_reservation_id": gnc_reservation_id,
                "reservation_status_id": reservation_status_id,
            },
        )

    async def list_reservations(self) -> list[ExistingReservation]:
        """Return upcoming reservations via GET /reservation/history?playDateMin={now}."""
        tz = ZoneInfo(self._timezone)
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
        r = await self._http.get(
            f"{_KENNA_API_BASE}{_RESERVATION_HISTORY_PATH}",
            params={"playDateMin": now_iso},
            headers=self._authed_headers(),
        )
        if r.status_code == _HTTP_RATE_LIMIT:
            raise RateLimitError("TeeItUp rate-limited list_reservations()")
        r.raise_for_status()

        raw_reservations = r.json().get("reservations", {}).get("Reservations") or []
        result: list[ExistingReservation] = []
        for res in raw_reservations:
            # Status 1 = active/confirmed; Status 0 = cancelled. Skip non-active.
            if int(res.get("Status", 0)) != 1:
                continue
            invoice = res.get("Invoice") or {}
            time_str: str = str(invoice.get("Time", ""))
            if not time_str:
                continue
            party_size = int(invoice.get("PlayerCount", 0))
            if party_size == 0:
                # Active reservation (Status=1) with unknown party size — the
                # double-booking guard uses party_size to match, so this entry
                # would not block a re-booking attempt. Log a warning so it's
                # visible to an operator rather than silently absent.
                _log.warning(
                    "list_reservations: skipping Status=1 reservation %s — PlayerCount=0 "
                    "(unknown party size; double-booking guard will NOT see this reservation)",
                    res.get("ReservationID"),
                )
                continue
            naive = datetime.fromisoformat(time_str)
            tee_time = naive.replace(tzinfo=tz)
            result.append(
                ExistingReservation(
                    course_id=self.course_id,
                    confirmation_code=str(res["ReservationID"]),
                    tee_time=tee_time,
                    party_size=party_size,
                    raw=res,
                )
            )

        return result

    async def cancel_reservation(self, confirmation_code: str) -> None:
        """Cancel a reservation. Idempotent on 200 and 404. Raises CancelError on other failures.

        Live observation: TeeItUp returns 200 (not 404) when cancelling an already-cancelled
        reservation. Both are treated as success. confirmation_code is TTB:{ReservationID};
        strips the prefix to get the numeric ID.
        """
        raw_id = confirmation_code
        if raw_id.startswith(MANAGED_BOOKING_TAG):
            raw_id = raw_id[len(MANAGED_BOOKING_TAG) :]

        r = await self._http.put(
            f"{_KENNA_API_BASE}{_CANCEL_PATH.format(reservation_id=raw_id)}",
            json={"players": 0, "reason": 7},
            headers=self._authed_headers(),
        )
        if r.status_code == _HTTP_NOT_FOUND:
            _log.info("cancel_reservation: %s already gone (404) — success", raw_id)
            return
        if r.status_code == _HTTP_RATE_LIMIT:
            raise RateLimitError("TeeItUp rate-limited cancel_reservation()")
        if not r.is_success:
            # Status code only — the Kenna error body can echo the holder's name (PII).
            # Parity with the ForeUP path, which logs only StatusCode/Message.
            raise CancelError(f"TeeItUp cancel failed for {raw_id}: HTTP {r.status_code}")

    def __repr__(self) -> str:
        return f"<TeeItUpAdapter course={self.course_id!r}>"

    async def aclose(self) -> None:
        await self._http.aclose()
