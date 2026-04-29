"""Unit tests for ForeUpAdapter using respx to mock httpx.

Tests cover: authenticate, search (filtering), book, list_reservations,
aclose, and the two parse helpers. No real network traffic.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from teetime.core.adapter import (
    AuthError,
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
    "time": "08:00:00",
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
        time_windows=(TimeWindow(earliest=time(7, 0), latest=time(9, 30)),),
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
async def test_authenticate_bad_password_raises_auth_error() -> None:
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
        with pytest.raises(AuthError, match="invalid"):
            await adapter.authenticate(CREDS)


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
    out_of_window = {**_RAW_SLOT, "time": "12:00:00"}  # noon, outside 07:00-09:30
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
        time_windows=(TimeWindow(earliest=time(7, 0), latest=time(9, 30)),),
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
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[])
    )
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
        result = await adapter.book(slot, _request())
    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code == "CONF-42"
    assert result.course_id == CID


@respx.mock
async def test_book_slot_gone_raises() -> None:
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(409)
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
        with pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())


# --- list_reservations ---------------------------------------------------


@respx.mock
async def test_list_reservations_returns_parsed_items() -> None:
    respx.get(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_RESERVATION])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1
    assert reservations[0].confirmation_code == "RES-123"
    assert reservations[0].party_size == 2


@respx.mock
async def test_list_reservations_skips_unparseable_items() -> None:
    bad = {"id": "X"}  # no tee_time field
    respx.get(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json=[bad, _RAW_RESERVATION])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1  # bad item skipped, good item kept


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
    assert slot.tee_time == datetime(2026, 5, 13, 8, 0, 0, tzinfo=tz)
    assert slot.price_per_player == Decimal("45.00")
    assert slot.holes == 18
    assert not slot.cart_included


def test_parse_reservation_maps_fields_correctly() -> None:
    tz = ZoneInfo("America/New_York")
    res = _parse_reservation(_RAW_RESERVATION, CID, tz)
    assert res.confirmation_code == "RES-123"
    assert res.tee_time == datetime(2026, 5, 13, 8, 0, 0, tzinfo=tz)
    assert res.party_size == 2
