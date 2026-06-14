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

    NOTE: GET /index.php/api/booking/users/reservations returns a ~6 MB user
        profile object with "reservations": false (a lazy-load flag, NOT a
        reservation list). Actual upcoming reservations are embedded in the
        POST /login response body under "reservations": [...]. authenticate()
        caches this list; list_reservations() reads from the cache.

    DELETE /index.php/api/booking/users/reservations/<id>
        Cancels a reservation. 200 → success. 404 → already cancelled (idempotent).

Anti-bot etiquette:
    - Honest User-Agent
    - api-key: "no_limits" (community-observed fixed value)
    - 250 ms minimum between non-booking requests
    - RateLimitError on 429 with retry-after pass-through
    - CaptchaError on any captcha/openNewWindow signal
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from ...core.adapter import (
    AuthError,
    CancelError,
    CaptchaError,
    CourseAdapter,
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
from ...core.redaction import redact_text

_log = logging.getLogger(__name__)

FOREUP_BASE_URL = "https://foreupsoftware.com"
LOGIN_PATH = "/index.php/api/booking/users/login"
TIMES_PATH = "/index.php/api/booking/times"
RESERVATION_PATH = "/index.php/api/booking/users/reservations"
_API_KEY = "no_limits"
_USER_AGENT = "TeeTimeBooker/0.0.0 (+https://github.com/wardcrazy01894/TeeTimeBooker)"
_MIN_BETWEEN_S = 0.25  # anti-bot courtesy delay between non-booking requests
_HTTP_RATE_LIMIT = 429
_HTTP_SLOT_GONE = 409
_HTTP_NOT_FOUND = 404
# ForeUP returns 400 on the reservation POST when it definitively rejects the
# booking — observed in prod (2026-06-07) when the slot was claimed between search
# and book. Unlike a 5xx/timeout (the §9 UNCERTAIN case), a 4xx rejection is
# unambiguous that NO reservation was created, so it is safe to try the next slot.
_HTTP_BOOK_REJECTED = 400


class ForeUpAdapter(CourseAdapter):
    """Base for any ForeUP-backed course. Subclasses set course_pk, booking_class_id,
    schedule_id, and optionally timezone. All HTTP logic lives here.

    To add a new ForeUP course:
      1. Create a sibling file (e.g. twin_brooks.py) that subclasses ForeUpAdapter,
         sets the four IDs, and overrides booking_page_url.
      2. Register it in __main__._ADAPTER_REGISTRY under the adapter name used in TOML.
      3. Add a [[courses]] entry in your TOML config.
    """

    course_id: CourseId

    # Each subclass MUST override this with the ForeUP booking page URL for that course.
    # Used by _build_adapters() to configure the CAPTCHA provider with the correct page.
    # Format: "https://foreupsoftware.com/index.php/booking/<course_pk>/<booking_class_id>"
    booking_page_url: ClassVar[str] = ""

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
        max_retries: int = 2,
        retry_backoff_s: float = 0.5,
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
        # JWT extracted from the login response — sent as x-authorization: Bearer <token>
        # on cancel_reservation() requests. None if login hasn't been called or the
        # response didn't contain a recognisable token field.
        self._auth_token: str | None = None
        # Reservations cached from the login response body. ForeUP embeds the full
        # reservation list in the POST /login response under "reservations". The
        # separate GET /reservations endpoint returns the user profile with
        # "reservations": false (a lazy-load flag, not actual data). authenticate()
        # populates this; list_reservations() reads from it.
        self._reservations_from_login: list[Any] = []
        # CAPTCHA token pre-fetched by prepare_book() for use in book().
        # None means no token has been pre-fetched; book() will solve it inline
        # (normal booking path). When set, book() consumes and clears it
        # (single-use) so the token is never silently reused.
        self._captcha_token: str | None = None
        # Transient-failure retry budget for IDEMPOTENT calls only (warm-up GET,
        # login POST, search GET, cancel DELETE). Reproduces+fixes the prod failure
        # where a single httpx.ReadTimeout against ForeUP (server up, adjacent polls
        # green) wasted a whole 10-minute watch cycle. book()'s POST is NEVER retried
        # — single-attempt rule, §9 double-booking defense.
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s

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

    async def _send_with_retry(
        self,
        send: Callable[[], Awaitable[httpx.Response]],
        *,
        op: str,
    ) -> httpx.Response:
        """Issue an IDEMPOTENT HTTP request, retrying transient transport failures.

        Retries on httpx.TransportError — read/connect timeouts and network blips,
        the failure observed in prod (httpx.ReadTimeout against ForeUP while the
        server was up and adjacent polls succeeded). HTTP status errors are NOT
        retried here: they surface via raise_for_status() at the call site, after
        this returns. `send` must be a thunk that issues a FRESH request each call.

        MUST NOT wrap book()'s POST — that is single-attempt by contract (§9
        double-booking defense; a timed-out book is the UNCERTAIN case M2.T3 owns).
        Backoff is linear (retry_backoff_s * attempt); set retry_backoff_s=0 in tests.
        """
        attempts = self._max_retries + 1
        for i in range(attempts):
            try:
                return await send()
            except httpx.TransportError as exc:
                if i == attempts - 1:
                    _log.warning("ForeUP: %s failed after %d attempt(s): %r", op, attempts, exc)
                    raise
                _log.info(
                    "ForeUP: %s transient error (attempt %d/%d), retrying: %r",
                    op,
                    i + 1,
                    attempts,
                    exc,
                )
                if self._retry_backoff_s:
                    await asyncio.sleep(self._retry_backoff_s * (i + 1))
        # Unreachable: the loop either returns a response or raises on the final
        # attempt. Present only to satisfy the type checker's return analysis.
        raise AssertionError("unreachable")  # pragma: no cover

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
        _log.info("ForeUP: warming up session cookie...")
        warmup_path = f"/index.php/booking/{self._course_pk}/{self._booking_class_id}"
        await self._send_with_retry(lambda: self._c().get(warmup_path), op="warm-up")
        _log.info("ForeUP: logging in as %s...", creds.username)
        r = await self._send_with_retry(
            lambda: self._c().post(
                LOGIN_PATH,
                data={
                    "username": creds.username,
                    "password": creds.password,
                    "api_key": "",  # booking widget uses empty api_key (not "no_limits")
                    "booking_class_id": str(self._public_booking_class_id),
                    "course_id": str(self._course_pk),
                },
            ),
            op="login",
        )
        self._guard_captcha(r)
        if r.status_code in (400, 401):
            _log.warning(
                "ForeUP: login failed (status %d) — search still possible, book requires re-auth",
                r.status_code,
            )
            self._reservations_from_login = []  # don't leave stale cache from prior auth
            return
        r.raise_for_status()
        # Initialize data before the try block so `isinstance(data, dict)` below
        # is always safe — if r.json() raises ValueError (e.g. HTML error page on a
        # 200), we fall through to the soft-fail path rather than crashing with
        # UnboundLocalError.
        data: object = {}
        try:
            data = r.json()
            if isinstance(data, dict) and not data.get("success", True):
                _log.warning("ForeUP: login rejected by server — search ok, book requires re-auth")
                self._reservations_from_login = []  # don't leave stale cache
                return
        except ValueError:
            pass
        self._logged_in = True
        # Extract JWT for use in cancel_reservation() requests.
        # ForeUP returns the session token in the login response body. The
        # current API uses "jwt"; older / community-documented versions used
        # "token" or "access_token". Try all known variants.
        if isinstance(data, dict):
            inner: dict[str, object] = data.get("data") or {}
            raw_tok: object = (
                data.get("jwt")  # actual ForeUP field name (confirmed live)
                or data.get("token")
                or data.get("access_token")
                or inner.get("token")
                or inner.get("access_token")
            )
            if isinstance(raw_tok, str) and raw_tok:
                self._auth_token = raw_tok
            # Cache the reservation list from the login response. ForeUP embeds
            # upcoming reservations directly in the login response body — the
            # separate GET /reservations endpoint returns a user profile, not data.
            raw_res: object = data.get("reservations")
            if isinstance(raw_res, list):
                self._reservations_from_login = raw_res
        _log.info("ForeUP: login successful")

    async def search(self, request: BookingRequest) -> list[TeeTimeSlot]:
        """GET /times for each target_date, filter by time_windows/holes/price/spots."""
        client = self._c()
        tz = ZoneInfo(self._timezone)
        results: list[TeeTimeSlot] = []

        for target_date in request.target_dates:
            await asyncio.sleep(_MIN_BETWEEN_S)
            _log.info(
                "ForeUP: fetching tee times for %s (%d player(s))...",
                target_date,
                len(request.players),
            )
            params: dict[str, str | int | bool] = {
                "time": "all",
                "date": f"{target_date.month}-{target_date.day}-{target_date.year}",
                "holes": request.holes,
                "players": len(request.players),
                "booking_class": False,
                "schedule_id": self._schedule_id,
                "specials_only": 0,
                "api_key": _API_KEY,
            }
            # partial binds `params` by value (avoids a loop-variable closure) and
            # types cleanly as a zero-arg thunk for _send_with_retry.
            r = await self._send_with_retry(
                partial(client.get, TIMES_PATH, params=params), op="search"
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

            _log.info("ForeUP: got %d raw slot(s) for %s, filtering...", len(raw_list), target_date)
            before = len(results)
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
            _log.info("ForeUP: %d slot(s) match filters for %s", len(results) - before, target_date)

        return results

    async def prepare_book(self, slot: TeeTimeSlot | None, request: BookingRequest) -> None:
        """Pre-solve CAPTCHA and cache the token for use in book().

        Two callers (see CourseAdapter.prepare_book):
        - UpgradeOrchestrator (slot set) — shrinks the cancel-to-book window.
        - Orchestrator on the race path (slot=None) — moves the ~75s CAPTCHA solve
          off the post-T0 critical path so book() at the 06:00 drop fires immediately.
        The CAPTCHA is a page-level reCAPTCHA, so `slot` is unused either way.

        After this returns, book() will use the cached token instead of calling the
        CAPTCHA provider. If no CAPTCHA provider is configured (dry-run or test), this
        is a no-op. Raises any exception from the CAPTCHA provider as-is.
        """
        if self._captcha_provider is None:
            return
        _log.info("ForeUP: pre-fetching CAPTCHA token (this can take 15-30s)...")
        self._captcha_token = await self._captcha_provider()
        _log.info("ForeUP: CAPTCHA token pre-fetched — booking can now proceed.")

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
            "player_list": False,
        }
        if self._captcha_provider is not None:
            if self._captcha_token is not None:
                # Token was pre-fetched by prepare_book() — consume it immediately.
                # Single-use: clear so it's never silently reused on a retry.
                _log.info("ForeUP: using pre-fetched CAPTCHA token, posting booking...")
                body["captchaid"] = self._captcha_token
                self._captcha_token = None
            else:
                _log.info("ForeUP: requesting CAPTCHA token (this can take 15-30s)...")
                try:
                    body["captchaid"] = await self._captcha_provider()
                except TimeoutError as exc:
                    raise CaptchaError(f"CAPTCHA solve timed out: {exc}") from exc
                _log.info("ForeUP: CAPTCHA token obtained, posting booking...")
        else:
            _log.info("ForeUP: posting booking (no CAPTCHA)...")
        _log.info(
            "ForeUP: booking slot %s at %s...",
            slot.slot_id,
            slot.tee_time.strftime("%Y-%m-%d %H:%M %Z"),
        )
        r = await client.post(RESERVATION_PATH, json=body)
        if r.is_error:
            # Log status + body BEFORE raising. raise_for_status() discards the body,
            # which left us blind to WHY ForeUP rejected the 2026-06-07 booking. r.text
            # is truncated to keep logs sane (and any card data already lives only in
            # the request, never the response).
            _log.warning(
                "ForeUP: book POST for slot %s → HTTP %d. Response: %s",
                slot.slot_id,
                r.status_code,
                redact_text(r.text[:500]),
            )
        if r.status_code == _HTTP_SLOT_GONE:
            raise SlotGoneError(f"Slot gone (409): {redact_text(r.text[:300])}")
        # A captcha/browser challenge can come back as a 400; classify it as such
        # (CaptchaError) BEFORE the generic 400 → SlotGone mapping below.
        self._guard_captcha(r)
        if r.status_code == _HTTP_BOOK_REJECTED:
            # 400 = ForeUP definitively rejected this booking; no reservation was
            # created (typically the slot was claimed between search and book). Raise
            # SlotGoneError so the orchestrator's candidate loop tries the next-ranked
            # slot instead of crashing. NOT the §9 UNCERTAIN case (a 4xx is unambiguous
            # that nothing was booked). See PLAN §9.
            raise SlotGoneError(f"Slot unbookable (HTTP 400): {redact_text(r.text[:300])}")
        r.raise_for_status()
        data: Any = r.json() if r.text else {}
        # Do NOT log the full response body — ForeUP echoes the account holder's
        # name/email/phone, and ACA forwards stdout to Log Analytics. Only the extracted
        # confirmation id is logged (below), which is safe. The full body is retained in
        # BookingResult.diagnostics (in-process only; never logged or persisted). See the
        # security review (PII-in-logs). For debugging, raise the level deliberately.
        _log.debug(
            "ForeUP: booking response keys=%s",
            list(data) if isinstance(data, dict) else type(data).__name__,
        )
        # ForeUP returns {"reservation": {"pending_reservation_id": ..., ...}}
        # Fall back through several field names seen across ForeUP API versions.
        reservation: Any = data.get("reservation") if isinstance(data, dict) else None
        conf_raw = (
            (reservation.get("pending_reservation_id") if isinstance(reservation, dict) else None)
            or (reservation.get("id") if isinstance(reservation, dict) else None)
            or data.get("pending_reservation_id")
            or data.get("id")
            or data.get("booking_id")
            or data.get("confirmation_code")
        )
        conf_raw_str = str(conf_raw) if conf_raw is not None else None
        # Option A (MF-1): stamp the TTB: prefix so ExistingReservation.is_managed
        # works correctly. BookingResult.confirmation_code stores "TTB:<raw_id>".
        # cancel_reservation() strips this prefix before calling ForeUP.
        # list_reservations() returns raw server IDs (no prefix) → is_managed=False
        # for bookings not made by this system, which is the correct behaviour.
        conf = (MANAGED_BOOKING_TAG + conf_raw_str) if conf_raw_str is not None else None
        _log.info("ForeUP: booking confirmed! confirmation_code=%s", conf)
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
        """Return reservations cached from the authenticate() login response.

        ForeUP embeds the user's upcoming reservation list directly in the login
        POST response body (under "reservations"). The separate GET endpoint
        (/api/booking/users/reservations) returns a ~6 MB user-profile object
        with "reservations": false — it is NOT a reservation list. authenticate()
        must be called before this method.

        Raises RuntimeError if authenticate() has never been called (client not
        initialized). This prevents a silent empty-list return from vacuously
        passing the PLAN §9 layer-2 pre-book guard in misconfigured deployments.
        """
        # Mirror the _c() guard used by search() and book(): if authenticate()
        # was never called, _client is None and we must fail loudly rather than
        # return an empty list that looks like "no existing bookings".
        if self._client is None:
            raise RuntimeError("authenticate() must be called before list_reservations()")
        tz = ZoneInfo(self._timezone)
        out: list[ExistingReservation] = []
        for item in self._reservations_from_login:
            try:
                out.append(_parse_reservation(item, self.course_id, tz))
            except (KeyError, ValueError, TypeError):
                continue
        _log.info("ForeUP: found %d existing reservation(s)", len(out))
        return out

    async def cancel_reservation(self, confirmation_code: str) -> None:
        """Cancel an existing reservation by confirmation_code.

        Endpoint confirmed via HAR capture (Spike S4, resolved): the implementation below
        uses `DELETE /index.php/api/booking/users/reservations/<id>`.

        Behaviour contract:
        - If the endpoint returns 404 (already cancelled), this method MUST
          return normally (idempotent post-condition satisfied).
        - If the endpoint returns any other 4xx or 5xx, raise CancelError so
          the caller knows the booking is still live.
        - The confirmation_code passed here may contain the TTB: prefix (it comes
          from BookingResult.confirmation_code which stores "TTB:<raw_id>"). This
          method MUST strip the prefix before passing the raw id to ForeUP. See
          PLAN.md §20 "MANAGED_BOOKING_TAG implementation (Option A)".

        MANAGED_BOOKING_TAG enforcement (whether to cancel or not) is the
        UpgradeOrchestrator's responsibility, not this method.

        See PLAN.md M-feature-2.T1 for the implementation contract.
        """
        # Strip TTB: prefix if present — the raw ForeUP id is what the server expects.
        # This is the Option A contract: BookingResult stores "TTB:<raw>", this method
        # strips to get the raw id. Implemented here so the caller never has to think
        # about it.
        raw_id = (
            confirmation_code[len(MANAGED_BOOKING_TAG) :]
            if confirmation_code.startswith(MANAGED_BOOKING_TAG)
            else confirmation_code
        )
        # Endpoint confirmed via HAR capture (Spike S4):
        #   DELETE /index.php/api/booking/users/reservations/<id>
        #   Success: HTTP 200, {"success": true, "msg": "Reservation Cancelled"}
        #   Already-cancelled: HTTP 404 → treat as success (idempotent post-condition).
        client = self._c()
        await asyncio.sleep(_MIN_BETWEEN_S)
        extra_headers: dict[str, str] = {"x-fu-golfer-location": "foreup"}
        if self._auth_token:
            extra_headers["x-authorization"] = f"Bearer {self._auth_token}"
        _log.info("ForeUP: cancelling reservation %s...", raw_id)
        # Cancel is idempotent (404 already-cancelled → success), so a transient
        # transport failure is safe to retry.
        r = await self._send_with_retry(
            lambda: client.delete(f"{RESERVATION_PATH}/{raw_id}", headers=extra_headers),
            op="cancel",
        )
        if r.status_code == _HTTP_NOT_FOUND:
            # Already cancelled — the desired post-condition is satisfied.
            _log.info("ForeUP: reservation %s already cancelled (404), treating as success", raw_id)
            return
        if r.status_code == _HTTP_RATE_LIMIT:
            raise RateLimitError(
                "Rate limited by ForeUP during cancel",
                retry_after_s=float(r.headers.get("retry-after", 60)),
            )
        self._guard_captcha(r)
        try:
            r.raise_for_status()
        except Exception as exc:
            raise CancelError(
                f"Cancel failed ({r.status_code}): {redact_text(r.text[:300])}"
            ) from exc
        _log.info("ForeUP: reservation %s cancelled successfully", raw_id)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


# --- Free functions for parsing raw ForeUP JSON ---------------------------


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
    """Map one ForeUP reservation item to an ExistingReservation.

    Handles two shapes:
    - Login-response shape (current API): TTID/teetime_id, start_datetime, player_count
    - Legacy GET-endpoint shape: id/booking_id, tee_time/teetime/time, players
    """
    raw_t = str(
        item.get("tee_time")
        or item.get("teetime")
        or item.get("start_datetime")  # login-response field
        or item.get("time")
        or ""
    )
    tee_time: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            tee_time = datetime.strptime(raw_t, fmt).replace(tzinfo=tz)
            break
        except ValueError:
            continue
    if tee_time is None:
        raise ValueError(f"Cannot parse tee_time: {raw_t!r}")
    conf = str(
        item.get("id")
        or item.get("booking_id")
        or item.get("confirmation_code")
        or item.get("TTID")  # login-response field
        or item.get("teetime_id")  # login-response field
        or ""
    )
    return ExistingReservation(
        course_id=course_id,
        confirmation_code=conf,
        tee_time=tee_time,
        party_size=int(item.get("players") or item.get("player_count") or 1),
        raw=dict(item),
    )
