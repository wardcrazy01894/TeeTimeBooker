"""Unit tests for ForeUpAdapter using respx to mock httpx.

Tests cover: authenticate, search (filtering), book, list_reservations,
aclose, and the two parse helpers. No real network traffic.
"""

from __future__ import annotations

import json as stdlib_json
from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from teetime.core.adapter import (
    CancelError,
    CaptchaError,
    CourseAdapter,
    RateLimitError,
    SlotGoneError,
)
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    CourseCredentials,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.courses.foreup.base import (
    FOREUP_BASE_URL,
    LOGIN_PATH,
    RESERVATION_PATH,
    TIMES_PATH,
    ForeUpAdapter,
    _parse_reservation,
    _parse_slot,
)
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter

# All injected test clients need base_url so relative paths resolve correctly.
_CLIENT_KWARGS = {"base_url": FOREUP_BASE_URL}

ET = ZoneInfo("America/New_York")
CID = CourseId("foreup:mangrove_bay")
CREDS = CourseCredentials(username="user@example.com", password="secret")
TARGET_DATE = date(2026, 5, 13)

_RAW_SLOT = {
    "teesheet_id": 99001,
    "time": "09:30:00",
    "holes": 18,
    "available_spots": 4,
    "green_fee": "45.00",
    "rate_type": "walking",
    "course_id": 19671,
    "schedule_id": 2149,
}

_RAW_RESERVATION = {
    "id": "RES-123",
    "tee_time": "2026-05-13 08:00:00",
    "players": 2,
}

# Matches the actual ForeUP login-response shape discovered via live API probe.
# Field names differ from the legacy GET-endpoint shape above.
_RAW_FOREUP_LOGIN_RESERVATION = {
    "TTID": "TTID_05271417087kr17",
    "teetime_id": "TTID_05271417087kr17",
    "type": "teetime",
    "start_datetime": "2026-06-03 14:15:00",
    "player_count": "4",
    "course_id": "19671",
}


def _adapter(client: httpx.AsyncClient) -> ForeUpAdapter:
    return ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
        timezone="America/New_York",
        http_client=client,
    )


def _retry_adapter(client: httpx.AsyncClient, *, max_retries: int = 2) -> ForeUpAdapter:
    """Adapter with retries enabled and zero backoff (no real sleeps in tests)."""
    return ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
        timezone="America/New_York",
        http_client=client,
        max_retries=max_retries,
        retry_backoff_s=0.0,
    )


def _request(*, dry_run: bool = False) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        dry_run=dry_run,
    )


# --- Protocol structural check -------------------------------------------


def test_mangrove_bay_adapter_satisfies_protocol() -> None:
    assert isinstance(MangroveBayAdapter(), CourseAdapter)


# --- authenticate --------------------------------------------------------


@respx.mock
async def test_authenticate_success() -> None:
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "msg": "ok"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)  # should not raise


@respx.mock
async def test_authenticate_bad_password_soft_fails() -> None:
    """401 login is a soft-fail: PHPSESSID alone still allows search().
    book() will refuse until _logged_in=True."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(
            401,
            json={"success": False, "msg": "Username or password is invalid"},
            headers={"content-type": "application/json"},
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)  # must not raise
        assert adapter._logged_in is False


@respx.mock
async def test_authenticate_captcha_raises_captcha_error() -> None:
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(
            400,
            json={"success": False, "msg": "Refresh required.", "openNewWindow": True},
            headers={"content-type": "application/json"},
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(CaptchaError):
            await adapter.authenticate(CREDS)


@respx.mock
async def test_authenticate_non_json_200_does_not_crash() -> None:
    """HTTP 200 with a non-JSON body must not raise UnboundLocalError.

    The prior bug: `data` was unbound after `ValueError` from `r.json()`, causing
    `if isinstance(data, dict)` to raise UnboundLocalError. The fix initializes
    `data = {}` before the try block. A non-JSON 200 is treated as a successful
    login (status 200 is trusted), but no JWT or reservations are extracted.
    """
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, text="<html>error page</html>")
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)  # must not raise UnboundLocalError
    # 200 status → _logged_in=True; no JWT or reservations extracted from HTML body
    assert adapter._auth_token is None
    assert adapter._reservations_from_login == []


@respx.mock
async def test_authenticate_soft_fail_clears_reservations_cache() -> None:
    """A 401 soft-fail resets _reservations_from_login so a stale prior cache is never returned."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(401, json={"success": False})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        # Simulate a stale cache from a previous successful login
        adapter._reservations_from_login = [_RAW_RESERVATION]
        await adapter.authenticate(CREDS)  # soft-fail
    # Cache must be cleared — not left as stale data
    assert adapter._reservations_from_login == []


async def test_list_reservations_raises_if_not_authenticated() -> None:
    """list_reservations() must raise RuntimeError when authenticate() was never called.
    Guards PLAN §9 layer-2: silent empty list would pass the pre-book check vacuously."""
    adapter = ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
    )
    with pytest.raises(RuntimeError, match="authenticate"):
        await adapter.list_reservations()


# --- search --------------------------------------------------------------


@respx.mock
async def test_search_returns_matching_slots() -> None:
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(_request())
    assert len(slots) == 1
    assert slots[0].slot_id == SlotId("99001")
    assert slots[0].price_per_player == Decimal("45.00")
    assert slots[0].holes == 18
    assert slots[0].available_spots == 4


@respx.mock
async def test_search_filters_out_of_window_slots() -> None:
    out_of_window = {**_RAW_SLOT, "time": "12:00:00"}  # noon, outside 09:00-10:30
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[out_of_window])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(_request())
    assert slots == []


@respx.mock
async def test_search_filters_by_max_price() -> None:
    expensive = {**_RAW_SLOT, "green_fee": "100.00"}
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[expensive])
    )
    req = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        max_price_per_player=Decimal("55.00"),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(req)
    assert slots == []


@respx.mock
async def test_search_rate_limited_raises() -> None:
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(RateLimitError) as exc_info:
            await adapter.search(_request())
    assert exc_info.value.retry_after_s == 30.0


@respx.mock
async def test_search_empty_list_returns_no_slots() -> None:
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(return_value=httpx.Response(200, json=[]))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(_request())
    assert slots == []


# --- transient-error retry (idempotent calls only) -----------------------


@respx.mock
async def test_search_retries_transient_timeout_then_succeeds() -> None:
    """A single httpx.ReadTimeout on the /times GET is retried, not abandoned.

    Reproduces the prod failure mode: ForeUP occasionally read-times-out for one
    poll while the server is up (adjacent polls succeed). The watcher's per-course
    catch turned that into a wasted 10-minute cycle. With retry, the call recovers
    in-run. See the prod log incident 2026-06-01."""
    route = respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        side_effect=[httpx.ReadTimeout("upstream slow"), httpx.Response(200, json=[_RAW_SLOT])]
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        slots = await adapter.search(_request())
    assert route.call_count == 2  # 1 timeout + 1 successful retry
    assert len(slots) == 1


@respx.mock
async def test_search_raises_after_retries_exhausted() -> None:
    """A persistent transport failure (server genuinely unreachable) still raises
    after the bounded retries — we do not retry forever."""
    route = respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        side_effect=httpx.ReadTimeout("upstream down")
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client, max_retries=2)
        with pytest.raises(httpx.TransportError):
            await adapter.search(_request())
    assert route.call_count == 3  # 1 initial attempt + 2 retries


@respx.mock
async def test_authenticate_retries_transient_connect_blip() -> None:
    """The warm-up GET and login POST are idempotent and retried on a connect blip."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        side_effect=[httpx.ConnectError("blip"), httpx.Response(200, text="<html/>")]
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"success": True})]
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        await adapter.authenticate(CREDS)  # must not raise
    assert adapter._logged_in is True


@respx.mock
async def test_http_status_errors_are_not_retried() -> None:
    """Retry is scoped to transport failures. A 500 surfaces via raise_for_status
    on the first attempt — it must NOT be retried (it isn't a transient transport blip)."""
    route = respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(500, text="server error")
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.search(_request())
    assert route.call_count == 1  # no retry on HTTP status errors


@respx.mock
async def test_book_is_not_retried_on_transient_error() -> None:
    """book()'s POST is single-attempt: a transport error propagates and is NEVER
    retried (§9 double-booking defense — a timed-out book is the UNCERTAIN case for
    M2.T3 reconciliation, not a safe re-fire)."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        adapter._logged_in = True  # simulate successful authenticate()
        with pytest.raises(httpx.TransportError):
            await adapter.book(slot, _request())
    assert route.call_count == 1  # book is NOT retried


# --- book ----------------------------------------------------------------


@respx.mock
async def test_book_success_returns_booked_result() -> None:
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-42"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True  # simulate successful authenticate()
        result = await adapter.book(slot, _request())
    assert result.outcome == BookingOutcome.BOOKED
    # MF-1 (Option A): confirmation_code is stamped with the TTB: managed-booking
    # prefix. The raw ForeUP id "CONF-42" is stored as "TTB:CONF-42".
    assert result.confirmation_code == "TTB:CONF-42"
    assert result.course_id == CID


@respx.mock
async def test_book_slot_gone_raises() -> None:
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(return_value=httpx.Response(409))
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True  # simulate successful authenticate()
        with pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())


@respx.mock
async def test_book_400_raises_slot_gone_so_orchestrator_tries_next() -> None:
    """A 400 from the reservation POST means ForeUP definitively rejected the booking
    (no reservation created — the usual cause is the slot was claimed between search and
    book). It must surface as SlotGoneError so the orchestrator's candidate loop tries the
    next-ranked slot instead of crashing with an uncaught HTTPStatusError (the 2026-06-07
    prod failure: a 400 killed the job and the 5 backup slots were never attempted)."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(400, json={"msg": "That time is no longer available."})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())


@respx.mock
async def test_book_logs_response_body_on_error(caplog: pytest.LogCaptureFixture) -> None:
    """On any non-2xx reservation POST, the response body must be logged (it used to be
    thrown away by raise_for_status, leaving us blind to WHY ForeUP rejected the booking —
    see the 2026-06-07 prod 400). The distinctive server message must appear in the logs."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(400, json={"msg": "unique-server-rejection-reason"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with caplog.at_level("WARNING"), pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())
    assert "unique-server-rejection-reason" in caplog.text


@respx.mock
async def test_book_includes_captchaid_when_provider_given() -> None:
    """book() must include captchaid in the POST body when captcha_provider is set."""
    captcha_token = "test-captcha-token-xyz"

    async def fake_provider() -> str:
        return captcha_token

    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-99"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = ForeUpAdapter(
            course_id=CID,
            course_pk=19671,
            booking_class_id=2149,
            schedule_id=2149,
            timezone="America/New_York",
            http_client=client,
            captcha_provider=fake_provider,
        )
        adapter._logged_in = True
        result = await adapter.book(slot, _request())
    # MF-1 (Option A): confirmation_code is stamped with the TTB: prefix.
    assert result.confirmation_code == "TTB:CONF-99"
    body = stdlib_json.loads(route.calls[0].request.content)
    assert body.get("captchaid") == captcha_token


@respx.mock
async def test_book_sends_false_for_player_list() -> None:
    """ForeUP booking does not require player details — player_list must be False.
    The website confirms the booking with just a player count, no individual data."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-42"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        await adapter.book(slot, _request())
    body = stdlib_json.loads(route.calls[0].request.content)
    assert body.get("player_list") is False


@respx.mock
async def test_book_omits_captchaid_without_provider() -> None:
    """book() must NOT send captchaid when no captcha_provider is given."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-42"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        await adapter.book(slot, _request())
    body = stdlib_json.loads(route.calls[0].request.content)
    assert "captchaid" not in body


@respx.mock
async def test_book_captcha_timeout_raises_captcha_error() -> None:
    """A TimeoutError from the inline CAPTCHA solve must surface as CaptchaError.

    Prod impact 2026-06-14: 2captcha was degraded; the inline solve in book() raised
    TimeoutError which propagated uncaught through _run_course → run() → job crash with
    a Python traceback. Fix: book() catches TimeoutError from _captcha_provider() and
    re-raises as CaptchaError so the job exits cleanly with a non-zero code.
    No HTTP booking POST should be made when the CAPTCHA solve fails.
    """

    async def _timing_out() -> str:
        raise TimeoutError("2captcha did not solve CAPTCHA within 180s")

    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = ForeUpAdapter(
            course_id=CID,
            course_pk=19671,
            booking_class_id=2149,
            schedule_id=2149,
            timezone="America/New_York",
            http_client=client,
            captcha_provider=_timing_out,
        )
        adapter._logged_in = True
        with pytest.raises(CaptchaError):
            await adapter.book(slot, _request())


# --- list_reservations ---------------------------------------------------


async def test_list_reservations_returns_parsed_items() -> None:
    """list_reservations() returns items from _reservations_from_login (no HTTP call)."""
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._reservations_from_login = [_RAW_RESERVATION]
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1
    assert reservations[0].confirmation_code == "RES-123"
    assert reservations[0].party_size == 2


async def test_list_reservations_skips_unparseable_items() -> None:
    """Items that can't be parsed are silently skipped."""
    bad = {"id": "X"}  # no tee_time field
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._reservations_from_login = [bad, _RAW_RESERVATION]
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1  # bad item skipped, good item kept


async def test_list_reservations_returns_foreup_login_shape() -> None:
    """list_reservations() correctly parses the actual ForeUP login-response field names."""
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._reservations_from_login = [_RAW_FOREUP_LOGIN_RESERVATION]
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1
    r = reservations[0]
    assert r.confirmation_code == "TTID_05271417087kr17"
    assert r.tee_time == datetime(2026, 6, 3, 14, 15, 0, tzinfo=ET)
    assert r.party_size == 4


@respx.mock
async def test_authenticate_populates_reservations_from_login_response() -> None:
    """Login response with reservations list → cached in _reservations_from_login."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "logged_in": True,
                "reservations": [_RAW_FOREUP_LOGIN_RESERVATION],
            },
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
    assert adapter._reservations_from_login == [_RAW_FOREUP_LOGIN_RESERVATION]


@respx.mock
async def test_authenticate_stores_jwt_field_name() -> None:
    """Login response with a 'jwt' field (actual ForeUP name) → stored as _auth_token."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"logged_in": True, "jwt": "real-jwt-xyz"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
    assert adapter._auth_token == "real-jwt-xyz"


# --- cancel_reservation --------------------------------------------------


CANCEL_RESERVATION_ID = "TTID_05271410334cux8"
CANCEL_URL = f"{FOREUP_BASE_URL}{RESERVATION_PATH}/{CANCEL_RESERVATION_ID}"


@respx.mock
async def test_authenticate_stores_jwt_from_login_response() -> None:
    """login response with a 'token' field → stored as _auth_token for cancel auth."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "token": "fake-jwt-abc"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
    assert adapter._auth_token == "fake-jwt-abc"


@respx.mock
async def test_cancel_reservation_success() -> None:
    """DELETE returning 200 {"success":true,...} returns normally (no exception)."""
    respx.delete(CANCEL_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "msg": "Reservation Cancelled"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._auth_token = "test-token"
        await adapter.cancel_reservation(CANCEL_RESERVATION_ID)  # must not raise


@respx.mock
async def test_cancel_reservation_404_is_idempotent() -> None:
    """DELETE returning 404 (already cancelled) returns normally — idempotent."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.cancel_reservation(CANCEL_RESERVATION_ID)  # must not raise


@respx.mock
async def test_cancel_reservation_non404_error_raises_cancel_error() -> None:
    """DELETE returning a non-404 error status raises CancelError."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(400, text="Bad Request"))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(CancelError):
            await adapter.cancel_reservation(CANCEL_RESERVATION_ID)


@respx.mock
async def test_cancel_reservation_strips_ttb_prefix() -> None:
    """TTB:-prefixed confirmation_code is stripped; raw id is used in the DELETE path."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        # Pass TTB:-prefixed code as stored in BookingResult.confirmation_code
        await adapter.cancel_reservation(f"TTB:{CANCEL_RESERVATION_ID}")  # must not raise
    # Verify the DELETE was called with the raw id (no TTB: prefix)
    assert respx.calls.last.request.url.path == f"{RESERVATION_PATH}/{CANCEL_RESERVATION_ID}"


@respx.mock
async def test_cancel_reservation_sends_authorization_header() -> None:
    """When _auth_token is set, DELETE includes x-authorization: Bearer <token>."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._auth_token = "my-jwt-token"
        await adapter.cancel_reservation(CANCEL_RESERVATION_ID)
    assert respx.calls.last.request.headers["x-authorization"] == "Bearer my-jwt-token"


# --- aclose --------------------------------------------------------------


async def test_aclose_releases_owned_client() -> None:
    adapter = ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
    )
    adapter._client = httpx.AsyncClient()
    await adapter.aclose()
    assert adapter._client is None


# --- parse helpers -------------------------------------------------------


def test_parse_slot_maps_fields_correctly() -> None:
    tz = ZoneInfo("America/New_York")
    slot = _parse_slot(_RAW_SLOT, TARGET_DATE, CID, tz)
    assert slot.slot_id == SlotId("99001")
    assert slot.tee_time == datetime(2026, 5, 13, 9, 30, 0, tzinfo=tz)
    assert slot.price_per_player == Decimal("45.00")
    assert slot.holes == 18
    assert not slot.cart_included


def test_parse_reservation_maps_fields_correctly() -> None:
    tz = ZoneInfo("America/New_York")
    res = _parse_reservation(_RAW_RESERVATION, CID, tz)
    assert res.confirmation_code == "RES-123"
    assert res.tee_time == datetime(2026, 5, 13, 8, 0, 0, tzinfo=tz)
    assert res.party_size == 2


def test_parse_reservation_foreup_login_shape() -> None:
    # _parse_reservation handles login-response field names: TTID, start_datetime, player_count
    tz = ZoneInfo("America/New_York")
    res = _parse_reservation(_RAW_FOREUP_LOGIN_RESERVATION, CID, tz)
    assert res.confirmation_code == "TTID_05271417087kr17"
    assert res.tee_time == datetime(2026, 6, 3, 14, 15, 0, tzinfo=tz)
    assert res.party_size == 4


@respx.mock
async def test_book_does_not_log_response_pii(caplog: pytest.LogCaptureFixture) -> None:
    """A successful book must NOT log the full ForeUP response body — it echoes the account
    holder's name/email/phone, and ACA ships stdout to Log Analytics. Only the confirmation
    id (already safe) should be logged. Security review High finding."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "reservation": {"id": "CONF-42", "email": "leak@example.test", "phone": "555-LEAK"}
            },
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with caplog.at_level("INFO"):
            result = await adapter.book(slot, _request())
    assert result.confirmation_code == "TTB:CONF-42"
    assert "CONF-42" in caplog.text  # the confirmation id IS logged (safe)
    assert "leak@example.test" not in caplog.text  # PII must NOT be logged
    assert "555-LEAK" not in caplog.text
