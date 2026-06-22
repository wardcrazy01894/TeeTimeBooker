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
import collections
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

    # Blind-POST capability (BLIND_POST_PLAN.md §3). A bare ForeUP course is NOT
    # capable — a subclass must ship + validate its own static payload template and
    # tee-time grid, then override this to True and implement synthesize_blind_slots
    # (Mangrove Bay does so in PR2). The orchestrator gates the blind path on
    # `isinstance(adapter, BlindPostCapable) and adapter.supports_blind_post`, so
    # leaving this False keeps a course on the existing search→book path.
    supports_blind_post: ClassVar[bool] = False

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
        max_concurrent_captcha_solves: int = 6,
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
        # CAPTCHA tokens pre-fetched by prepare_book() for use in book(), held as a
        # FIFO pool (RACE_PREWARM_PLAN Change C). prepare_book(count=N) solves N tokens
        # CONCURRENTLY and appends them here; book() pops the OLDEST (popleft) so a
        # late-firing fallback candidate keeps the freshest token. Empty pool means
        # book() solves inline (single-token / normal path). Single-use: a popped token
        # is never returned to the pool.
        self._captcha_tokens: collections.deque[str] = collections.deque()
        # Bounds CONCURRENT inline CAPTCHA solves. The blind-POST burst fires up to
        # captcha_pool_size() book()s at once; if their pooled tokens went stale (solved
        # pre-T0, expired by T0) they ALL hit the MF1 inline re-solve simultaneously — an
        # N-way herd of ~75s 2captcha solves at T0, threatening the booking replicaTimeout
        # and the provider's rate limit. This semaphore caps that herd (single-book paths —
        # upgrade, sequential fallback — are single-threaded, so it never blocks them). The
        # pre-T0 prepare_book prefetch is UNbounded by this (it calls the provider directly,
        # not _solve_captcha_inline) — that concurrency is off the critical path and intended.
        # Default 6: a balance — high enough not to over-serialise a real all-stale burst
        # (prepare_book already fires up to blind_post_max_count=12 concurrent solves pre-T0,
        # so 2captcha tolerates that concurrency; an all-stale 12-burst at 6 is 2 waves, well
        # within replicaTimeout=1200s) yet still a guardrail against a pathological runaway.
        self._captcha_solve_sem = asyncio.Semaphore(max(1, max_concurrent_captcha_solves))
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

    def _is_captcha_challenge(self, r: httpx.Response) -> bool:
        """True iff the response is a ForeUP captcha/browser-challenge signal.

        Non-raising sibling of _guard_captcha — used by book()'s MF1 path to decide
        whether a POOLED token was rejected as stale (warranting one inline re-solve)
        without raising on the first attempt.
        """
        if "application/json" not in r.headers.get("content-type", ""):
            return False
        try:
            data: object = r.json()
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        msg = str(data.get("msg", ""))
        return "captcha" in msg.lower() or bool(data.get("openNewWindow"))

    def _guard_captcha(self, r: httpx.Response) -> None:
        """Raise CaptchaError on any ForeUP captcha/browser-challenge signal."""
        if not self._is_captcha_challenge(r):
            return
        try:
            data: object = r.json()
        except ValueError:
            data = {}
        msg = str(data.get("msg", "")) if isinstance(data, dict) else ""
        raise CaptchaError(msg or "browser challenge (openNewWindow) required")

    async def authenticate(self, creds: CourseCredentials) -> None:
        """Warm up PHPSESSID, then attempt username/password login.

        The PHPSESSID warmup alone is enough for search(). A successful login
        is required for book(). If the login fails (e.g. account uses Google
        OAuth), search() still works; book() will raise AuthError.
        """
        if self._client is None:
            self._client = self._make_client()
        # Idempotency guard (RACE_PREWARM_PLAN §3.1): once a real login has succeeded, a
        # second authenticate() is a no-op — skip the warm-up GET + login POST. Keys ONLY on
        # _logged_in, which a soft login failure (400/401 or rejected body) leaves False, so a
        # later inline authenticate() correctly retries the full login. This is hygiene (a real
        # ForeUP re-login is wasteful); the orchestrator's pre-warm skip is the load-bearing path.
        if self._logged_in:
            _log.info("ForeUP: already logged in — skipping re-authentication")
            return
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
            # A 200 whose body isn't JSON (e.g. an HTML WAF/interstitial page). We still
            # trust the 200 and proceed, but with no JWT and no reservation cache — a
            # session that LOOKS healthy. Warn so this is diagnosable (otherwise a later
            # empty list_reservations() vacuously passes the pre-book guard). Log only the
            # body length, never the body (may carry markup/identifiers).
            _log.warning(
                "ForeUP: login returned 200 but body was not JSON (len=%d) — proceeding "
                "without JWT/reservation cache",
                len(r.text),
            )
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

    @property
    def is_authenticated(self) -> bool:
        """``AuthStateReportable`` capability (RACE_PREWARM_PLAN §3.1 SF#1). True iff a
        username/password login has actually established a session. A soft login failure
        (400/401/rejected body) is swallowed by ``authenticate()`` and leaves this False,
        so the race pre-warm skips recording this course and re-authenticates at T0."""
        return self._logged_in

    async def refresh_reservations(self, creds: CourseCredentials) -> None:
        """Force a fresh login so ``list_reservations()`` returns a CURRENT snapshot.

        ``ReservationCacheRefreshable`` capability (BLIND_POST_PLAN.md §6 must-fix).
        ``list_reservations()`` reads the cache built from the ``POST /login`` response
        body, and ``authenticate()`` is idempotent — once ``_logged_in`` is True it
        short-circuits before the login POST, so it will NOT rebuild that cache. The
        blind-POST re-guard needs a snapshot taken AFTER the T0 burst (to see a
        landed-but-uncertain blind reservation); clearing ``_logged_in`` first makes the
        next ``authenticate()`` re-run the warm-up GET + login POST and repopulate
        ``_reservations_from_login`` with the current server state."""
        self._logged_in = False
        await self.authenticate(creds)

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        """GET /times for each target_date, filter by time_windows/holes/price/spots.

        ``skip_initial_spacing`` (Change D / PR3) drops the leading courtesy sleep before
        the FIRST date's GET — race-path only. The 2nd+ date GETs are always spaced, so the
        watcher's inter-date-check etiquette is untouched even if the flag is ever set.
        """
        client = self._c()
        tz = ZoneInfo(self._timezone)
        results: list[TeeTimeSlot] = []

        for i, target_date in enumerate(request.target_dates):
            if not (i == 0 and skip_initial_spacing):
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
                raise InventoryNotPublishedError(
                    f"Unexpected /times shape: {redact_text(r.text[:200])}"
                )

            _log.info("ForeUP: got %d raw slot(s) for %s, filtering...", len(raw_list), target_date)
            before = len(results)
            dropped = 0
            sample_keys: list[str] | None = None
            for raw in raw_list:
                try:
                    slot = _parse_slot(raw, target_date, self.course_id, tz)
                except (KeyError, ValueError, InvalidOperation):
                    # Don't drop silently: search() backs the 06:00 booking decision. A ForeUP
                    # /times schema change makes EVERY slot unparseable → search returns [] →
                    # the bot reports NO_INVENTORY, indistinguishable from a genuinely empty
                    # teesheet. We log a PII-free AGGREGATE below (count + the first dropped
                    # item's keys, never values), mirroring the list_reservations parse-drop log
                    # — an aggregate, not per-slot, so a wholesale break is loud without spam.
                    dropped += 1
                    if sample_keys is None and isinstance(raw, dict):
                        sample_keys = sorted(raw)
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
            if dropped:
                _log.warning(
                    "ForeUP: dropped %d/%d unparseable slot(s) for %s (sample keys=%s)",
                    dropped,
                    len(raw_list),
                    target_date,
                    sample_keys,
                )
            matched = results[before:]
            _log.info("ForeUP: %d slot(s) match filters for %s", len(matched), target_date)
            # Log the matched (in-window) tee times — not just the count — so a real 06:00
            # drop can be diffed against the blind-POST derived grid to detect grid drift
            # (BLIND_POST_PLAN.md PR2 retroactive validation). Times only → PII-free.
            _log.info(
                "ForeUP: matched tee times for %s: %s",
                target_date,
                [s.tee_time.astimezone(tz).strftime("%H:%M") for s in matched],
            )

        return results

    async def _solve_captcha_inline(self) -> str:
        """Solve one CAPTCHA token inline, mapping a provider timeout to CaptchaError.

        Callers must guard `self._captcha_provider is not None` first.
        """
        assert self._captcha_provider is not None
        # Bound concurrent solves (see _captcha_solve_sem): in the blind-POST burst many
        # book()s can reach here at once; without this they would fire an N-way herd of
        # ~75s 2captcha solves at T0.
        try:
            async with self._captcha_solve_sem:
                return await self._captcha_provider()
        except TimeoutError as exc:
            raise CaptchaError(f"CAPTCHA solve timed out: {exc}") from exc

    def captcha_pool_size(self) -> int:
        """Number of pre-solved CAPTCHA tokens currently in the FIFO pool.

        BlindPostCapable member (BLIND_POST_PLAN.md §3). The orchestrator sizes the
        blind burst at ``min(len(blind_slots), captcha_pool_size())`` so every
        concurrent ``book()`` pops a pooled token rather than inline-solving at T0.
        """
        return len(self._captcha_tokens)

    def synthesize_blind_slots(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        max_count: int,
    ) -> list[TeeTimeSlot]:
        """Build blind-POST candidate slots WITHOUT searching (BlindPostCapable).

        The base ForeUP class is NOT blind-capable: it ships no committed payload
        template or tee-time grid, so this raises. A capable subclass (Mangrove Bay,
        PR2) overrides it to enumerate its morning grid, compute each ForeUP
        ``start_front`` id, and return ranked candidate slots.
        """
        raise NotImplementedError(
            "This ForeUP course has no committed blind-POST template/grid. "
            "Override synthesize_blind_slots in the course subclass. "
            "See BLIND_POST_PLAN.md PR2."
        )

    async def prepare_book(
        self,
        slot: TeeTimeSlot | None,
        request: BookingRequest,
        *,
        count: int = 1,
    ) -> None:
        """Pre-solve `count` CAPTCHA tokens CONCURRENTLY and stash them in the pool.

        Two callers (see CourseAdapter.prepare_book):
        - UpgradeOrchestrator (slot set, count=1) — shrinks the cancel-to-book window.
        - Orchestrator on the race path (slot=None, count=N) — moves the ~75s CAPTCHA
          solve off the post-T0 critical path AND pre-solves N tokens so the first N
          ranked candidates each fire near-instantly instead of re-solving a fresh
          single-use token inline. The CAPTCHA is a page-level reCAPTCHA, so `slot`
          is unused either way (RACE_PREWARM_PLAN §4).

        The N solves run under a single asyncio.gather(return_exceptions=True): every
        successful token is appended to the FIFO pool, individual failures are dropped.

        NI10 raise contract:
        - count == 1 and the lone solve fails → RE-RAISE (a TimeoutError becomes
          CaptchaError). This keeps the upgrade-path caller's abort-on-failure behaviour.
        - count > 1 → NEVER raise, even if every solve fails (best-effort race prefetch;
          book() falls back to an inline solve). The pool simply ends up with however
          many succeeded (possibly zero).

        No CAPTCHA provider configured (dry-run or test) → no-op regardless of count.
        """
        if self._captcha_provider is None:
            return
        _log.info("ForeUP: pre-fetching %d CAPTCHA token(s) concurrently...", count)
        provider = self._captcha_provider
        results = await asyncio.gather(
            *(provider() for _ in range(count)),
            return_exceptions=True,
        )
        tokens = [r for r in results if isinstance(r, str)]
        self._captcha_tokens.extend(tokens)
        failures = [r for r in results if isinstance(r, BaseException)]
        if tokens:
            _log.info(
                "ForeUP: pre-fetched %d/%d CAPTCHA token(s) — pool size %d.",
                len(tokens),
                count,
                len(self._captcha_tokens),
            )
            return
        # Nothing solved.
        if count == 1 and failures:
            exc = failures[0]
            if isinstance(exc, TimeoutError):
                raise CaptchaError(f"CAPTCHA pre-fetch timed out: {exc}") from exc
            raise exc
        _log.warning("ForeUP: all %d CAPTCHA pre-fetches failed — book() will solve inline.", count)

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
        # from_pool tracks whether the token came from the pre-fetched FIFO pool. Only
        # pooled tokens (solved pre-T0, possibly stale by book time) get the MF1 inline
        # re-solve below; a freshly inline-solved token does not.
        from_pool = False
        if self._captcha_provider is not None:
            if self._captcha_tokens:
                # Pop the OLDEST pooled token (FIFO) — single-use, never returned.
                body["captchaid"] = self._captcha_tokens.popleft()
                from_pool = True
                _log.info(
                    "ForeUP: using pooled CAPTCHA token (%d left in pool), posting booking...",
                    len(self._captcha_tokens),
                )
            else:
                _log.info("ForeUP: requesting CAPTCHA token (this can take 15-30s)...")
                body["captchaid"] = await self._solve_captcha_inline()
                _log.info("ForeUP: CAPTCHA token obtained, posting booking...")
        else:
            _log.info("ForeUP: posting booking (no CAPTCHA)...")
        _log.info(
            "ForeUP: booking slot %s at %s...",
            slot.slot_id,
            slot.tee_time.strftime("%Y-%m-%d %H:%M %Z"),
        )
        r = await client.post(RESERVATION_PATH, json=body)
        # MF1: a POOLED token rejected as a captcha challenge is likely STALE (solved
        # pre-T0, expired by the time we book). Re-solve ONCE inline and re-POST the SAME
        # slot. Exactly one retry — the re-POST is classified normally below, so a second
        # challenge surfaces as CaptchaError (no infinite loop). INLINE tokens get NO such
        # retry: they were just solved, a challenge on them is a real wall. (See §4.3 MF1.)
        if from_pool and self._is_captcha_challenge(r):
            _log.warning(
                "ForeUP: pooled CAPTCHA token rejected as stale — re-solving inline once..."
            )
            body["captchaid"] = await self._solve_captcha_inline()
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
            # Real Mangrove Bay book responses are a FLAT dict whose id is in
            # TTID/teetime_id (the SAME two fields _parse_reservation reads — keep
            # the relative order in sync). MB's response carries none of the six
            # fields above, so without these the chain returned None on every live MB
            # booking, leaving blind-POST cancel-extras with no id to cancel.
            or data.get("TTID")
            or data.get("teetime_id")
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
            except (KeyError, ValueError, TypeError) as exc:
                # Don't drop silently: this list backs the layer-2 double-booking guard,
                # the blind-POST re-guard, and the watcher reconcile. A ForeUP field-shape
                # change would empty the list with no signal → the bot books a second time.
                # Log the exception TYPE and the item's keys (for schema-drift diagnosis),
                # never values (PII). We deliberately do NOT log `exc` itself: a parse error
                # message can embed a field VALUE (e.g. ValueError(f"Cannot parse tee_time:
                # {raw_t!r}")), which would leak data into the (unredacted) app log.
                _log.warning(
                    "ForeUP: skipping unparseable reservation item (%s): keys=%s",
                    type(exc).__name__,
                    sorted(item) if isinstance(item, dict) else type(item).__name__,
                )
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
