"""Multi-token CAPTCHA prefetch pool tests for ForeUpAdapter (RACE_PREWARM_PLAN Change C).

prepare_book(count=N) pre-solves N reCAPTCHA tokens CONCURRENTLY during the pre-T0
busy-wait and stashes them in a FIFO deque. book() pops the oldest pooled token
(so late-firing fallbacks keep the freshest token), falling back to an inline solve
when the pool is empty. A stale POOLED token rejected as a captcha challenge is
recovered by ONE inline re-solve + re-POST of the same slot; an INLINE token gets no
such retry. See RACE_PREWARM_PLAN §4.

No real network — respx mocks the booking POST and a counting fake provider stands in
for 2captcha.
"""

from __future__ import annotations

import asyncio
import json as stdlib_json
from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from teetime.core.adapter import CaptchaError, SlotGoneError
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.courses.foreup.base import (
    FOREUP_BASE_URL,
    RESERVATION_PATH,
    ForeUpAdapter,
)

ET = ZoneInfo("America/New_York")
CID = CourseId("foreup:mangrove_bay")
TARGET_DATE = date(2026, 5, 13)
_CLIENT_KWARGS = {"base_url": FOREUP_BASE_URL}

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


def _slot() -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )


def _request() -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
    )


def _adapter(
    client: httpx.AsyncClient,
    provider: object | None = None,
) -> ForeUpAdapter:
    a = ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
        timezone="America/New_York",
        http_client=client,
        captcha_provider=provider,  # type: ignore[arg-type]
    )
    a._logged_in = True
    return a


class _CountingProvider:
    """Records each solve and returns sequential tokens t0, t1, t2, ..."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str:
        token = f"t{self.calls}"
        self.calls += 1
        return token


# --- prepare_book pool semantics -----------------------------------------


async def test_prepare_book_solves_count_tokens_concurrently() -> None:
    """count=N solves N tokens CONCURRENTLY (all enter the provider before any returns)
    and stashes them in the pool."""
    entered = 0
    release = asyncio.Event()

    async def gated_provider() -> str:
        nonlocal entered
        entered += 1
        # All N coroutines must be parked here together before any is allowed to finish.
        await release.wait()
        return f"tok-{entered}"

    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, gated_provider)
        task = asyncio.create_task(adapter.prepare_book(None, _request(), count=3))
        # Yield until all 3 solves have entered the provider concurrently.
        for _ in range(100):
            if entered == 3:
                break
            await asyncio.sleep(0)
        assert entered == 3, "solves did not run concurrently (sequential would enter 1 at a time)"
        release.set()
        await task
        assert len(adapter._captcha_tokens) == 3


async def test_prepare_book_count_default_is_one() -> None:
    """prepare_book with no count solves exactly one token (upgrade-path parity)."""
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)
        await adapter.prepare_book(_slot(), _request())
        assert provider.calls == 1
        assert len(adapter._captcha_tokens) == 1


async def test_prepare_book_partial_failure_keeps_successful_tokens() -> None:
    """One of three solves raises (gather return_exceptions); the pool keeps the other
    two and prepare_book does NOT raise."""
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("2captcha hiccup")
        return f"ok-{calls}"

    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, flaky)
        await adapter.prepare_book(None, _request(), count=3)  # must not raise
        assert len(adapter._captcha_tokens) == 2


async def test_prepare_book_count_one_total_failure_raises() -> None:
    """NI10: count=1 + the single solve fails → prepare_book RE-RAISES (upgrade abort)."""

    async def timing_out() -> str:
        raise TimeoutError("no solve")

    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, timing_out)
        with pytest.raises(CaptchaError):
            await adapter.prepare_book(_slot(), _request(), count=1)
        assert len(adapter._captcha_tokens) == 0


async def test_prepare_book_count_n_total_failure_does_not_raise() -> None:
    """NI10: count>1 + ALL solves fail → prepare_book does NOT raise; pool is empty."""

    async def timing_out() -> str:
        raise TimeoutError("no solve")

    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, timing_out)
        await adapter.prepare_book(None, _request(), count=3)  # must not raise
        assert len(adapter._captcha_tokens) == 0


async def test_prepare_book_noop_without_provider() -> None:
    """No provider configured → prepare_book is a no-op regardless of count."""
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, None)
        await adapter.prepare_book(None, _request(), count=3)
        assert len(adapter._captcha_tokens) == 0


# --- book() pops pooled tokens FIFO --------------------------------------


@respx.mock
async def test_book_pops_pooled_token_fifo_oldest_first() -> None:
    """Pre-seeded pool [t0,t1,t2]: two book()s send captchaid=t0 then t1; pool keeps [t2]."""

    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        side_effect=[
            httpx.Response(200, json={"id": "C1"}),
            httpx.Response(200, json={"id": "C2"}),
        ]
    )
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)
        adapter._captcha_tokens.extend(["t0", "t1", "t2"])
        await adapter.book(_slot(), _request())
        await adapter.book(_slot(), _request())
    assert provider.calls == 0, "pooled tokens were available — must not inline-solve"
    assert stdlib_json.loads(route.calls[0].request.content)["captchaid"] == "t0"
    assert stdlib_json.loads(route.calls[1].request.content)["captchaid"] == "t1"
    assert list(adapter._captcha_tokens) == ["t2"]


@respx.mock
async def test_book_inline_solves_when_pool_empty() -> None:
    """Empty pool + provider configured → book() inline-solves once; POST carries the token."""

    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "C1"})
    )
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)  # empty pool
        await adapter.book(_slot(), _request())
    assert provider.calls == 1
    assert stdlib_json.loads(route.calls[0].request.content)["captchaid"] == "t0"


# --- MF1: stale pooled token recovery ------------------------------------

_CAPTCHA_400 = {"success": False, "msg": "Captcha verification failed", "openNewWindow": True}


@respx.mock
async def test_book_stale_pooled_token_resolves_inline_and_retries_same_slot() -> None:
    """MF1: a pooled token rejected as a captcha challenge triggers ONE inline re-solve +
    re-POST of the SAME slot; the 2nd POST 2xx → BOOKED."""

    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        side_effect=[
            httpx.Response(400, json=_CAPTCHA_400, headers={"content-type": "application/json"}),
            httpx.Response(200, json={"id": "C-OK"}),
        ]
    )
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)
        adapter._captcha_tokens.append("stale")
        result = await adapter.book(_slot(), _request())
    assert result.outcome is BookingOutcome.BOOKED
    assert result.confirmation_code == "TTB:C-OK"
    assert provider.calls == 1, "exactly one inline re-solve"
    assert route.calls.call_count == 2
    assert stdlib_json.loads(route.calls[0].request.content)["captchaid"] == "stale"
    assert stdlib_json.loads(route.calls[1].request.content)["captchaid"] == "t0"


@respx.mock
async def test_book_stale_pooled_token_persistent_captcha_wall_raises() -> None:
    """MF1: pooled token; BOTH the first POST and the inline-retry POST are captcha
    challenges → CaptchaError (no infinite loop — exactly one retry)."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        side_effect=[
            httpx.Response(400, json=_CAPTCHA_400, headers={"content-type": "application/json"}),
            httpx.Response(400, json=_CAPTCHA_400, headers={"content-type": "application/json"}),
        ]
    )
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)
        adapter._captcha_tokens.append("stale")
        with pytest.raises(CaptchaError):
            await adapter.book(_slot(), _request())
    assert provider.calls == 1
    assert route.calls.call_count == 2


@respx.mock
async def test_book_stale_pooled_token_then_slot_gone_advances() -> None:
    """MF1: pooled token captcha-challenge 400, inline-retry POST returns plain
    400 Time-not-available → SlotGoneError (candidate loop advances)."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        side_effect=[
            httpx.Response(400, json=_CAPTCHA_400, headers={"content-type": "application/json"}),
            httpx.Response(
                400,
                json={"success": False, "msg": "Time not available"},
                headers={"content-type": "application/json"},
            ),
        ]
    )
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)
        adapter._captcha_tokens.append("stale")
        with pytest.raises(SlotGoneError):
            await adapter.book(_slot(), _request())
    assert provider.calls == 1
    assert route.calls.call_count == 2


@respx.mock
async def test_book_inline_token_captcha_challenge_raises_without_retry() -> None:
    """MF1: empty pool → inline solve; a captcha-challenge 400 → CaptchaError with NO
    second attempt (only POOLED tokens get the inline re-solve retry)."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            400, json=_CAPTCHA_400, headers={"content-type": "application/json"}
        )
    )
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)  # empty pool → inline solve
        with pytest.raises(CaptchaError):
            await adapter.book(_slot(), _request())
    assert provider.calls == 1, "one inline solve only — no re-solve for inline tokens"
    assert route.calls.call_count == 1, "inline token must NOT retry the POST"


@respx.mock
async def test_book_pooled_time_not_available_raises_slot_gone() -> None:
    """A pooled token accepted but the slot is gone (plain 400 Time-not-available) →
    SlotGoneError directly, no inline retry (token was fine; the SLOT was gone)."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            400,
            json={"success": False, "msg": "Time not available"},
            headers={"content-type": "application/json"},
        )
    )
    provider = _CountingProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider)
        adapter._captcha_tokens.append("good")
        with pytest.raises(SlotGoneError):
            await adapter.book(_slot(), _request())
    assert provider.calls == 0, "token was accepted — no inline re-solve"
    assert route.calls.call_count == 1, "no retry on a genuine slot-gone"
