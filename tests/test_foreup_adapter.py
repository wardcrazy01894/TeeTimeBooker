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
