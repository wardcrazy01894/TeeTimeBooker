"""Tests for TeeItUpAdapter and SydneyMarovitzAdapter.

All CourseAdapter methods are tested with respx mocks against the Kenna API
and tr.gnsvc.com. Payment flow confirmed from HAR capture (2026-05-29).
"""

from __future__ import annotations

import json as json_mod
from datetime import date, time
from decimal import Decimal
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import respx

from teetime.core.adapter import (
    AuthError,
    CancelError,
    CourseAdapter,
    InventoryNotPublishedError,
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
    TimeWindow,
)
from teetime.courses.teeitup.base import (
    _ADD_RESERVATION_PATH,
    _AUTH_PATH,
    _GNSVC_BASE,
    _KENNA_API_BASE,
    _ORDER_TEETIME_PATH,
    _ORDERS_PATH,
    _PROFILE_PATH,
    _SHOPPING_CART_PATH,
    _TEE_TIMES_PATH,
    _TR_TOKEN_PATH,
)
from teetime.courses.teeitup.sydney_marovitz import (
    SYDNEY_MAROVITZ_ADVANCE_BOOKING_DAYS,
    SYDNEY_MAROVITZ_CHANNEL_ID,
    SYDNEY_MAROVITZ_GN_FACILITY_ID,
    SYDNEY_MAROVITZ_GNC_FACILITY_ID,
    SYDNEY_MAROVITZ_HOLES,
    SYDNEY_MAROVITZ_KENNA_FACILITY_ID,
    SYDNEY_MAROVITZ_SLUG,
    SydneyMarovitzAdapter,
)

CID = CourseId("teeitup:sydney_marovitz")
CREDS = CourseCredentials(
    username="user@example.com",
    password="s3cr3t",
    extra={
        "cvv": "123",
        "card_number": "4111111111111111",
        "expiry_month": "12",
        "expiry_year": "2030",
        "billing_address": "123 Test St",
        "billing_postal_code": "12345",
    },
)
TARGET_DATE = date(2026, 6, 7)

_FAKE_TOKEN = "Fe26.2**fake_token**"
_FAKE_CART_ID = "fa9afe01-4d8b-4434-b946-f2799514f552"
_FAKE_CART_ITEM_ID = "17ee0b69-428c-483d-ab97-aea08153cd0d"
_FAKE_TR_TOKEN = "test-tr-token-uuid"
_FAKE_RESERVATION_STATUS_ID = 43850921
_FAKE_GNC_RESERVATION_ID = "423530092"
# Used in cancel tests (live reservation IDs from actual cancel runs)
_FAKE_CANCEL_ID = 423523114

_AUTH_RESPONSE = {
    "sessionToken": _FAKE_TOKEN,
    "customer": {
        "username": "user@example.com",
        "id": "abc123",
        "facilityId": SYDNEY_MAROVITZ_KENNA_FACILITY_ID,
        "name": {"given": "Test", "family": "User"},
        "phoneNumbers": [{"primary": True, "value": "18149339111"}],
    },
    "authenticationType": "basic",
}

_INVOICE_RESPONSE = {
    "PolicyItems": [
        {"Key": "TEE_TIME_NOTES", "Details": "9 hole green fees included."},
        {"Key": "TEE_TIME_POLICY", "Details": "24-hour cancellation policy."},
    ],
    "InventoryChannelID": 20972,
}

# Slot at 2026-06-07T13:00:00Z = 08:00 CDT
_MATCHING_TEETIME = {
    "courseId": SYDNEY_MAROVITZ_KENNA_FACILITY_ID,
    "teetime": "2026-06-07T13:00:00.000Z",
    "backNine": False,
    "rates": [
        {
            "_id": 99001,
            "name": "9 Holes",
            "externalId": "99001",
            "allowedPlayers": [2, 3, 4],
            "holes": 9,
            "greenFeeWalking": 3027,
            "dueOnlineWalking": 0,
            "golfnow": {
                "TTTeeTimeId": 99001,
                "GolfCourseId": SYDNEY_MAROVITZ_GNC_FACILITY_ID,
                "GolfFacilityId": SYDNEY_MAROVITZ_GN_FACILITY_ID,
            },
        }
    ],
    "bookedPlayers": 0,
    "minPlayers": 2,
    "maxPlayers": 4,
    "players": [],
    "source": "API-2.1",
}

_SINGLES_ONLY_TEETIME = {
    "courseId": SYDNEY_MAROVITZ_KENNA_FACILITY_ID,
    "teetime": "2026-06-07T13:09:00.000Z",
    "rates": [{"_id": 99002, "allowedPlayers": [1], "holes": 9, "greenFeeWalking": 3027}],
    "bookedPlayers": 0,
    "minPlayers": 1,
    "maxPlayers": 1,
    "players": [],
    "source": "API-2.1",
}

_OUTSIDE_WINDOW_TEETIME = {
    "courseId": SYDNEY_MAROVITZ_KENNA_FACILITY_ID,
    "teetime": "2026-06-07T19:00:00.000Z",
    "rates": [{"_id": 99003, "allowedPlayers": [2, 3, 4], "holes": 9, "greenFeeWalking": 3027}],
    "bookedPlayers": 0,
    "minPlayers": 2,
    "maxPlayers": 4,
    "players": [],
    "source": "API-2.1",
}

_SEARCH_RESPONSE = [
    {"dayInfo": {}, "teetimes": [_MATCHING_TEETIME, _SINGLES_ONLY_TEETIME, _OUTSIDE_WINDOW_TEETIME]}
]

_CART_RESPONSE = {"alias": SYDNEY_MAROVITZ_SLUG, "id": _FAKE_CART_ID, "items": []}

_CART_ITEM_RESPONSE = {
    "alias": SYDNEY_MAROVITZ_SLUG,
    "id": _FAKE_CART_ID,
    "items": [
        {
            "id": _FAKE_CART_ITEM_ID,
            "facilityId": SYDNEY_MAROVITZ_GN_FACILITY_ID,
            "type": "TeeTime",
            "extra": {},
        }
    ],
}

_ORDER_RESPONSE = {
    "id": "6a19dfa9f090bfdd44079015",
    "orderNumber": 1780080553282,
    "state": "pending",
    "shoppingCartId": _FAKE_CART_ID,
}

_IS_BOOKABLE_RESPONSE = {"bookable": True, "reservationCountsByTime": {}}

_KENNA_INVOICE = {
    "facilityId": SYDNEY_MAROVITZ_GN_FACILITY_ID,
    "referenceId": "7a118947-7379-43af-8407-b1f6ee584137",
    "time": "2026-06-07T08:00:00",
    "teeTimeRateId": 99001,
    "rateName": "9 Holes",
    "playerCount": 2,
    "teeTimeNotes": "9 hole green fees included.",
    "termsAndConditions": "24-hour cancellation policy.",
    "holeCount": 9,
    "isHotDeal": False,
    "transportation": "Walking",
    "totalReservationPrice": {"currencyCode": "USD", "value": 60.54},
    "pricing": {
        "greensFees": {"currencyCode": "USD", "value": 60.54},
        "originalGreensFees": {"currencyCode": "USD", "value": 60.54},
        "dueOnline": {"currencyCode": "USD", "value": 0},
        "dueAtCourse": {"currencyCode": "USD", "value": 60.54},
        "totalDue": {"currencyCode": "USD", "value": 60.54},
        "salesTaxTotal": {"currencyCode": "USD", "value": 0},
        "transactionFee": {"currencyCode": "USD", "value": 0},
    },
    "currencyCode": "USD",
    "dueOnline": {
        "summary": {"original": 0, "discount": 0, "subTotal": 0, "salesTax": 0, "total": 0},
        "items": [],
    },
    "dueAtCourse": {
        "summary": {
            "original": 60.54,
            "discount": 0,
            "subTotal": 60.54,
            "salesTax": 0,
            "total": 60.54,
        },
        "items": [],
    },
    "totalDue": {
        "summary": {
            "original": 60.54,
            "discount": 0,
            "subTotal": 60.54,
            "salesTax": 0,
            "total": 60.54,
        },
        "items": [],
    },
}

_ORDER_TEETIME_RESPONSE = {
    "id": "6a19dfa9f090bfdd44079015",
    "state": "pending",
    "teetimes": [
        {
            "playTime": "2026-06-07T13:00:00.000Z",
            "players": [
                {
                    "name": "Test User",
                    "emailAddress": "user@example.com",
                    "state": "pending",
                    "isCaptain": True,
                    "invoice": _KENNA_INVOICE,
                },
                {
                    "name": "guest",
                    "emailAddress": None,
                    "state": "pending",
                    "isCaptain": False,
                    "invoice": None,
                },
            ],
        }
    ],
}

_ADD_RESERVATION_RESPONSE = {
    "ReservationStatusID": _FAKE_RESERVATION_STATUS_ID,
    "StatusCode": 200,
    "PaymentStatus": "Processed",
    "RedirectUrl": None,
    "Success": True,
    "Message": "",
    "ValidationErrors": [],
}

_ORDER_TEETIME_STATUS_RESPONSE = {
    "id": "6a19dfa9f090bfdd44079015",
    "teetimes": [
        {
            "playTime": "2026-06-07T13:00:00.000Z",
            "players": [
                {
                    "state": "fulfilled",
                    "isCaptain": True,
                    "gncReservationId": _FAKE_GNC_RESERVATION_ID,
                },
            ],
        }
    ],
}

_RESERVATION_HISTORY_RESPONSE = {
    "reservations": {
        "Reservations": [
            {
                "ReservationID": _FAKE_CANCEL_ID,
                "ConfirmationNumber": "654447032",
                "Status": 1,
                "EligibleForCancellation": True,
                "Invoice": {
                    "Time": "2026-06-07T08:00:00",
                    "PlayerCount": 2,
                    "HoleCount": 9,
                    "FacilityID": SYDNEY_MAROVITZ_GN_FACILITY_ID,
                },
            }
        ]
    },
    "cancellationReasons": [{"CancellationReasonId": 7, "Description": "Other"}],
}


def _adapter(client: httpx.AsyncClient) -> SydneyMarovitzAdapter:
    return SydneyMarovitzAdapter(http_client=client)


def _request(
    *,
    party_size: int = 2,
    holes: int = 9,
    earliest: time = time(7, 0),
    latest: time = time(10, 0),
    max_price: Decimal | None = None,
    dry_run: bool = False,
) -> BookingRequest:
    players = tuple(
        Player(first_name=f"P{i}", last_name="Test", email=f"p{i}@x.test")
        for i in range(party_size)
    )
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=earliest, latest=latest),),
        players=players,
        course_preferences=(CID,),
        holes=holes,
        max_price_per_player=max_price,
        dry_run=dry_run,
    )


def _mock_auth() -> None:
    respx.post(f"{_KENNA_API_BASE}{_AUTH_PATH}").mock(
        return_value=httpx.Response(200, json=_AUTH_RESPONSE)
    )


def _mock_search() -> None:
    respx.get(f"{_KENNA_API_BASE}{_TEE_TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=_SEARCH_RESPONSE)
    )


def _mock_full_book_flow(dry_run: bool = False) -> None:
    _mock_auth()
    _mock_search()
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(204))
    respx.post(f"{_KENNA_API_BASE}{_ORDERS_PATH}").mock(
        return_value=httpx.Response(201, json=_ORDER_RESPONSE)
    )
    respx.post(url__regex=r".*/is-bookable$").mock(
        return_value=httpx.Response(200, json=_IS_BOOKABLE_RESPONSE)
    )
    if not dry_run:
        respx.post(f"{_KENNA_API_BASE}{_ORDER_TEETIME_PATH}").mock(
            return_value=httpx.Response(200, json=_ORDER_TEETIME_RESPONSE)
        )
        respx.put(f"{_KENNA_API_BASE}{_PROFILE_PATH}").mock(
            return_value=httpx.Response(200, json=_AUTH_RESPONSE)
        )
        respx.get(f"{_KENNA_API_BASE}{_TR_TOKEN_PATH}").mock(
            return_value=httpx.Response(200, json=_FAKE_TR_TOKEN)
        )
        respx.post(f"{_GNSVC_BASE}{_ADD_RESERVATION_PATH}").mock(
            return_value=httpx.Response(200, json=_ADD_RESERVATION_RESPONSE)
        )
        respx.patch(url__regex=r".*/order-teetime/status/\d+.*").mock(
            return_value=httpx.Response(200, json=_ORDER_TEETIME_STATUS_RESPONSE)
        )


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------


def test_sydney_marovitz_satisfies_protocol() -> None:
    assert isinstance(SydneyMarovitzAdapter(), CourseAdapter)


def test_sydney_marovitz_course_id() -> None:
    assert SydneyMarovitzAdapter().course_id == CID


def test_sydney_marovitz_constants() -> None:
    assert SYDNEY_MAROVITZ_SLUG == "sydney-r-marovitz-golf-course"
    assert SYDNEY_MAROVITZ_GN_FACILITY_ID == 4014
    assert SYDNEY_MAROVITZ_GNC_FACILITY_ID == 7218
    assert SYDNEY_MAROVITZ_KENNA_FACILITY_ID == "54f14cb60c8ad60378b02bfb"
    assert SYDNEY_MAROVITZ_CHANNEL_ID == "20972"
    assert SYDNEY_MAROVITZ_ADVANCE_BOOKING_DAYS == 15
    assert SYDNEY_MAROVITZ_HOLES == 9


@pytest.mark.asyncio
async def test_prepare_book_is_noop() -> None:
    result = await SydneyMarovitzAdapter().prepare_book(None, None)  # type: ignore[arg-type]
    assert result is None


# ---------------------------------------------------------------------------
# authenticate()
# ---------------------------------------------------------------------------


@respx.mock
async def test_authenticate_stores_session_token_and_card_creds() -> None:
    _mock_auth()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
    assert adapter._session_token == _FAKE_TOKEN
    assert adapter._card_number == "4111111111111111"
    assert adapter._cvv == "123"
    assert adapter._expiry_month == "12"
    assert adapter._expiry_year == "2030"
    assert adapter._billing_address == "123 Test St"
    assert adapter._billing_postal_code == "12345"
    assert adapter._billing_country == "US"
    # name_on_card defaults to first+last from auth response
    assert adapter._name_on_card == "Test User"


@respx.mock
async def test_authenticate_raises_auth_error_on_401() -> None:
    respx.post(f"{_KENNA_API_BASE}{_AUTH_PATH}").mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthError):
            await _adapter(client).authenticate(CREDS)


@respx.mock
async def test_authenticate_accepts_explicit_name_on_card() -> None:
    _mock_auth()
    creds = CourseCredentials(
        username="u@x.test",
        password="p",
        extra={**CREDS.extra, "name_on_card": "John Doe"},
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(creds)
    assert adapter._name_on_card == "John Doe"


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_requires_authenticate_first() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="authenticate"):
            await _adapter(client).search(_request())


@respx.mock
async def test_search_returns_matching_slot() -> None:
    _mock_auth()
    _mock_search()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
    assert len(slots) == 1
    slot = slots[0]
    assert slot.slot_id == "99001"
    assert slot.holes == 9
    assert slot.price_per_player == Decimal("30.27")
    assert slot.cart_included is False
    assert slot.tee_time.hour == 8
    assert str(slot.tee_time.tzinfo) == "America/Chicago"


@respx.mock
async def test_search_filters_by_party_size() -> None:
    _mock_auth()
    _mock_search()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=1, holes=9, latest=time(11, 0)))
    slot_ids = {s.slot_id for s in slots}
    assert "99002" in slot_ids
    assert "99001" not in slot_ids


@respx.mock
async def test_search_raises_inventory_not_published_on_400() -> None:
    _mock_auth()
    respx.get(f"{_KENNA_API_BASE}{_TEE_TIMES_PATH}").mock(return_value=httpx.Response(400))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        with pytest.raises(InventoryNotPublishedError):
            await adapter.search(_request())


# ---------------------------------------------------------------------------
# book()
# ---------------------------------------------------------------------------


@respx.mock
async def test_book_dry_run_skips_order_teetime() -> None:
    _mock_full_book_flow(dry_run=True)
    order_teetime_route = respx.post(f"{_KENNA_API_BASE}{_ORDER_TEETIME_PATH}").mock(
        return_value=httpx.Response(200, json=_ORDER_TEETIME_RESPONSE)
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        result = await adapter.book(slots[0], _request(party_size=2, holes=9, dry_run=True))
    assert result.outcome == BookingOutcome.DRY_RUN
    assert result.confirmation_code is None
    assert not order_teetime_route.called


@respx.mock
async def test_book_returns_confirmation_code() -> None:
    _mock_full_book_flow()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        result = await adapter.book(slots[0], _request(party_size=2, holes=9))
    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code == f"TTB:{_FAKE_GNC_RESERVATION_ID}"
    assert result.diagnostics["gnc_reservation_id"] == _FAKE_GNC_RESERVATION_ID
    assert result.diagnostics["reservation_status_id"] == _FAKE_RESERVATION_STATUS_ID
    assert result.booked_at is not None


@respx.mock
async def test_book_slot_gone_on_is_bookable_false() -> None:
    _mock_auth()
    _mock_search()
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(204))
    respx.post(f"{_KENNA_API_BASE}{_ORDERS_PATH}").mock(
        return_value=httpx.Response(201, json=_ORDER_RESPONSE)
    )
    respx.post(url__regex=r".*/is-bookable$").mock(
        return_value=httpx.Response(200, json={"bookable": False})
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        with pytest.raises(SlotGoneError):
            await adapter.book(slots[0], _request(party_size=2, holes=9))


@respx.mock
async def test_book_slot_gone_on_non_409_4xx_at_lock() -> None:
    """Parity with ForeUP's 4xx->SlotGoneError: a non-409 client error (e.g. 400/422) at a
    pre-payment reservation step (here the lock PUT) means the slot could not be held and NO
    booking/charge happened — it must map to SlotGoneError so the orchestrator tries the next
    candidate, not crash the candidate loop with an uncaught HTTPStatusError."""
    _mock_auth()
    _mock_search()
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    # Lock fails with a 400 (slot taken / lock contention), NOT a 409.
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(400))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        with pytest.raises(SlotGoneError):
            await adapter.book(slots[0], _request(party_size=2, holes=9))


@respx.mock
async def test_book_raises_rate_limit_not_slot_gone_at_lock() -> None:
    """A 429 at a pre-payment step is a THROTTLE signal, not a gone slot — it must surface
    as RateLimitError (consistent with authenticate/search), not be swallowed as SlotGoneError
    and silently burn the candidate."""
    _mock_auth()
    _mock_search()
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        with pytest.raises(RateLimitError):
            await adapter.book(slots[0], _request(party_size=2, holes=9))


@respx.mock
async def test_book_propagates_5xx_at_lock_as_uncertain() -> None:
    """A 5xx at a pre-payment step is AMBIGUOUS (the request may have landed) — it must NOT
    be swallowed as SlotGoneError; it propagates as HTTPStatusError (the UNCERTAIN case)."""
    _mock_auth()
    _mock_search()
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.book(slots[0], _request(party_size=2, holes=9))


def _setup_full_book_mocks() -> tuple[respx.Route, respx.Route]:
    """Register all mocks; return (order_teetime_route, add_reservation_route)."""
    respx.post(f"{_KENNA_API_BASE}{_AUTH_PATH}").mock(
        return_value=httpx.Response(200, json=_AUTH_RESPONSE)
    )
    respx.get(f"{_KENNA_API_BASE}{_TEE_TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=_SEARCH_RESPONSE)
    )
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(204))
    respx.post(f"{_KENNA_API_BASE}{_ORDERS_PATH}").mock(
        return_value=httpx.Response(201, json=_ORDER_RESPONSE)
    )
    respx.post(url__regex=r".*/is-bookable$").mock(
        return_value=httpx.Response(200, json=_IS_BOOKABLE_RESPONSE)
    )
    order_route = respx.post(f"{_KENNA_API_BASE}{_ORDER_TEETIME_PATH}").mock(
        return_value=httpx.Response(200, json=_ORDER_TEETIME_RESPONSE)
    )
    respx.put(f"{_KENNA_API_BASE}{_PROFILE_PATH}").mock(
        return_value=httpx.Response(200, json=_AUTH_RESPONSE)
    )
    respx.get(f"{_KENNA_API_BASE}{_TR_TOKEN_PATH}").mock(
        return_value=httpx.Response(200, json=_FAKE_TR_TOKEN)
    )
    add_res_route = respx.post(f"{_GNSVC_BASE}{_ADD_RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json=_ADD_RESERVATION_RESPONSE)
    )
    respx.patch(url__regex=r".*/order-teetime/status/\d+.*").mock(
        return_value=httpx.Response(200, json=_ORDER_TEETIME_STATUS_RESPONSE)
    )
    return order_route, add_res_route


@respx.mock
async def test_book_sends_correct_order_teetime_payload() -> None:
    order_route, _ = _setup_full_book_mocks()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        await adapter.book(slots[0], _request(party_size=2, holes=9))
    body = json_mod.loads(order_route.calls[0].request.read())
    assert body["rateId"] == 99001
    assert body["golferQuantity"] == 2
    assert body["teetime"] == "2026-06-07T13:00:00.000Z"


@respx.mock
async def test_book_sends_correct_add_reservation_form() -> None:
    # Verify the form-encoded payload sent to tr.gnsvc.com matches HAR-confirmed structure.
    _, add_res_route = _setup_full_book_mocks()
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        await adapter.book(slots[0], _request(party_size=2, holes=9))
    raw = add_res_route.calls[0].request.read().decode()
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    assert form["Payment.CC.CreditCardNumber"] == "4111111111111111"
    assert form["Payment.CC.CVVCode"] == "123"
    assert form["Payment.CC.ExpirationMonth"] == "12"
    assert form["Payment.CC.ExpirationYear"] == "2030"
    assert form["Payment.Address.Line1"] == "123 Test St"
    assert form["Payment.Address.PostalCode"] == "12345"
    assert form["Payment.Address.Country"] == "US"
    assert form["TeeTime.InventoryChannelID"] == SYDNEY_MAROVITZ_CHANNEL_ID
    assert form["TeeTime.FacilityID"] == str(SYDNEY_MAROVITZ_GN_FACILITY_ID)
    assert form["TeeTime.ReferenceID"] == _KENNA_INVOICE["referenceId"]
    assert form["Token"] == _FAKE_TR_TOKEN
    assert form["ALIAS"] == SYDNEY_MAROVITZ_SLUG
    assert form["ENGINE"] == "5.0"
    assert form["TeeTime.Amount"] == "-1"
    assert form["Payment.Name"] == "Test User"


@respx.mock
async def test_book_payment_failure_raises_runtime_error() -> None:
    _mock_auth()
    _mock_search()
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(204))
    respx.post(f"{_KENNA_API_BASE}{_ORDERS_PATH}").mock(
        return_value=httpx.Response(201, json=_ORDER_RESPONSE)
    )
    respx.post(url__regex=r".*/is-bookable$").mock(
        return_value=httpx.Response(200, json=_IS_BOOKABLE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_ORDER_TEETIME_PATH}").mock(
        return_value=httpx.Response(200, json=_ORDER_TEETIME_RESPONSE)
    )
    respx.put(f"{_KENNA_API_BASE}{_PROFILE_PATH}").mock(
        return_value=httpx.Response(200, json=_AUTH_RESPONSE)
    )
    respx.get(f"{_KENNA_API_BASE}{_TR_TOKEN_PATH}").mock(
        return_value=httpx.Response(200, json=_FAKE_TR_TOKEN)
    )
    respx.post(f"{_GNSVC_BASE}{_ADD_RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "Success": False,
                "Message": "Card declined",
                "ReservationStatusID": 0,
                "ValidationErrors": [],
            },
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        with pytest.raises(RuntimeError, match="payment failed"):
            await adapter.book(slots[0], _request(party_size=2, holes=9))


@respx.mock
async def test_book_payment_failure_message_does_not_echo_response_dict() -> None:
    # When Message is empty (falsy), the error must NOT fall back to dumping the full
    # response dict (which could contain card-echo fields from the payment processor).
    _mock_auth()
    _mock_search()
    respx.get(url__regex=r".*/tee-times/rate/\d+/invoice.*").mock(
        return_value=httpx.Response(200, json=_INVOICE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_SHOPPING_CART_PATH}").mock(
        return_value=httpx.Response(201, json=_CART_RESPONSE)
    )
    respx.post(url__regex=r".*/shopping-cart/[^/]+/cart-item$").mock(
        return_value=httpx.Response(200, json=_CART_ITEM_RESPONSE)
    )
    respx.put(url__regex=r".*/tee-time/lock$").mock(return_value=httpx.Response(204))
    respx.post(f"{_KENNA_API_BASE}{_ORDERS_PATH}").mock(
        return_value=httpx.Response(201, json=_ORDER_RESPONSE)
    )
    respx.post(url__regex=r".*/is-bookable$").mock(
        return_value=httpx.Response(200, json=_IS_BOOKABLE_RESPONSE)
    )
    respx.post(f"{_KENNA_API_BASE}{_ORDER_TEETIME_PATH}").mock(
        return_value=httpx.Response(200, json=_ORDER_TEETIME_RESPONSE)
    )
    respx.put(f"{_KENNA_API_BASE}{_PROFILE_PATH}").mock(
        return_value=httpx.Response(200, json=_AUTH_RESPONSE)
    )
    respx.get(f"{_KENNA_API_BASE}{_TR_TOKEN_PATH}").mock(
        return_value=httpx.Response(200, json=_FAKE_TR_TOKEN)
    )
    # Message="" (falsy) — the old code would dump gnsvc_data entirely into the exception
    respx.post(f"{_GNSVC_BASE}{_ADD_RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "Success": False,
                "Message": "",
                "StatusCode": 402,
                "ValidationErrors": [],
                "SensitiveEchoField": "card-data",
            },
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        slots = await adapter.search(_request(party_size=2, holes=9))
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.book(slots[0], _request(party_size=2, holes=9))
    assert "SensitiveEchoField" not in str(exc_info.value)
    assert "card-data" not in str(exc_info.value)
    assert "402" in str(exc_info.value)  # StatusCode is a safe field


# ---------------------------------------------------------------------------
# list_reservations()
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_reservations_returns_upcoming() -> None:
    _mock_auth()
    respx.get(url__regex=r".*/reservation/history.*").mock(
        return_value=httpx.Response(200, json=_RESERVATION_HISTORY_RESPONSE)
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1
    r = reservations[0]
    assert r.course_id == CID
    assert r.confirmation_code == str(_FAKE_CANCEL_ID)
    assert r.party_size == 2
    assert r.tee_time.hour == 8
    assert str(r.tee_time.tzinfo) == "America/Chicago"


@respx.mock
async def test_list_reservations_empty_when_none() -> None:
    _mock_auth()
    respx.get(url__regex=r".*/reservation/history.*").mock(
        return_value=httpx.Response(
            200, json={"reservations": {"Reservations": []}, "cancellationReasons": []}
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        reservations = await adapter.list_reservations()
    assert reservations == []


# ---------------------------------------------------------------------------
# cancel_reservation()
# ---------------------------------------------------------------------------


@respx.mock
async def test_cancel_reservation_calls_correct_endpoint() -> None:
    _mock_auth()
    cancel_route = respx.put(url__regex=rf".*/reservations/{_FAKE_CANCEL_ID}/cancel$").mock(
        return_value=httpx.Response(200, json={"ReservationID": _FAKE_CANCEL_ID})
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        await adapter.cancel_reservation(f"TTB:{_FAKE_CANCEL_ID}")
    assert cancel_route.called
    body = json_mod.loads(cancel_route.calls[0].request.read())
    assert body == {"players": 0, "reason": 7}


@respx.mock
async def test_cancel_reservation_idempotent_on_repeat_200() -> None:
    # Live observation (2026-05-29): TeeItUp returns HTTP 200 (not 404) when
    # cancelling an already-cancelled reservation. Must not raise.
    _mock_auth()
    respx.put(url__regex=r".*/reservations/.*/cancel$").mock(
        return_value=httpx.Response(200, json={})
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        await adapter.cancel_reservation(f"TTB:{_FAKE_CANCEL_ID}")  # should not raise


@respx.mock
async def test_cancel_reservation_idempotent_on_404() -> None:
    _mock_auth()
    respx.put(url__regex=r".*/reservations/.*/cancel$").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        await adapter.cancel_reservation(f"TTB:{_FAKE_CANCEL_ID}")  # should not raise


@respx.mock
async def test_cancel_reservation_raises_cancel_error_on_failure() -> None:
    _mock_auth()
    respx.put(url__regex=r".*/reservations/.*/cancel$").mock(
        return_value=httpx.Response(403, json={"error": "not eligible"})
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        with pytest.raises(CancelError):
            await adapter.cancel_reservation(f"TTB:{_FAKE_CANCEL_ID}")
