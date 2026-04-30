"""Unit tests for captcha.py — 2captcha provider logic.

The Playwright provider is an integration test (requires a real browser + Google);
those live in tests/cassettes or are marked integration. These tests cover the
2captcha HTTP flow using respx mocks.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from teetime.courses.foreup.captcha import get_foreup_captcha_token_2captcha

_SUBMIT_URL = "https://2captcha.com/in.php"
_RESULT_URL = "https://2captcha.com/res.php"
_PAGE_URL = "https://foreupsoftware.com/index.php/booking/19671/2149"


@respx.mock
async def test_2captcha_returns_token_after_one_not_ready_poll() -> None:
    """Returns the token once 2captcha is done: first poll not ready, second ready."""
    respx.post(_SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "task-99"})
    )
    call_count = [0]

    def _get_side_effect(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(200, json={"status": 0, "request": "CAPCHA_NOT_READY"})
        return httpx.Response(200, json={"status": 1, "request": "g-recaptcha-token-abc"})

    respx.get(_RESULT_URL).mock(side_effect=_get_side_effect)

    token = await get_foreup_captcha_token_2captcha(
        api_key="test-key",
        page_url=_PAGE_URL,
        poll_interval_s=0.0,
    )
    assert token == "g-recaptcha-token-abc"
    assert call_count[0] == 2


@respx.mock
async def test_2captcha_raises_on_submit_failure() -> None:
    """Raises RuntimeError if 2captcha rejects the submission (bad API key etc)."""
    respx.post(_SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"status": 0, "request": "ERROR_WRONG_USER_KEY"})
    )
    with pytest.raises(RuntimeError, match="ERROR_WRONG_USER_KEY"):
        await get_foreup_captcha_token_2captcha(
            api_key="bad-key",
            page_url=_PAGE_URL,
            poll_interval_s=0.0,
        )


@respx.mock
async def test_2captcha_raises_timeout_when_never_ready() -> None:
    """Raises TimeoutError if 2captcha never returns a result within max_polls."""
    respx.post(_SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "task-99"})
    )
    respx.get(_RESULT_URL).mock(
        return_value=httpx.Response(200, json={"status": 0, "request": "CAPCHA_NOT_READY"})
    )
    with pytest.raises(TimeoutError):
        await get_foreup_captcha_token_2captcha(
            api_key="test-key",
            page_url=_PAGE_URL,
            poll_interval_s=0.0,
            max_polls=2,
        )


@respx.mock
async def test_2captcha_raises_on_error_response() -> None:
    """Raises RuntimeError if 2captcha returns an error code (not CAPCHA_NOT_READY)."""
    respx.post(_SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "task-99"})
    )
    respx.get(_RESULT_URL).mock(
        return_value=httpx.Response(200, json={"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"})
    )
    with pytest.raises(RuntimeError, match="ERROR_CAPTCHA_UNSOLVABLE"):
        await get_foreup_captcha_token_2captcha(
            api_key="test-key",
            page_url=_PAGE_URL,
            poll_interval_s=0.0,
        )
