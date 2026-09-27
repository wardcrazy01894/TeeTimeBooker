"""Unit tests for captcha.py — 2captcha provider logic.

These tests cover the 2captcha HTTP flow (submit + poll) and the site-key drift
guard using respx mocks — no live network. 2captcha is the only solver (the
Playwright headless-browser provider was removed).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from teetime.courses.foreup.captcha import (
    FOREUP_RECAPTCHA_SITE_KEY,
    get_foreup_captcha_token_2captcha,
    resolve_invisible_site_key,
)

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


@respx.mock
async def test_2captcha_poll_http_error_does_not_leak_api_key() -> None:
    """SECURITY (full-repo-scan 2026-07-09 H1): the result poll carries the API key as a
    URL QUERY PARAM. A non-2xx from res.php must raise an error whose message does NOT
    embed the request URL (httpx.HTTPStatusError does — `... for url '...res.php?key=
    <REAL_KEY>&...'`), because that message propagates to the `exc_info=True` booking-run
    log and lands in Log Analytics in cleartext. The status code must survive for
    diagnosis."""
    respx.post(_SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "task-99"})
    )
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(RuntimeError) as excinfo:
        await get_foreup_captcha_token_2captcha(
            api_key="fakekeyfakekeyfakekeyfakekey",
            page_url=_PAGE_URL,
            poll_interval_s=0.0,
        )
    assert "fakekeyfakekeyfakekeyfakekey" not in str(excinfo.value)
    assert "500" in str(excinfo.value)  # actionable: status preserved


@respx.mock
async def test_2captcha_submit_http_error_does_not_leak_api_key() -> None:
    """Same guard for the SUBMIT leg. The key travels in the POST body there (not the
    URL), so this pins the current-safe behavior against a refactor that moves it into
    the URL or lets an unsanitized HTTPStatusError escape."""
    respx.post(_SUBMIT_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(RuntimeError) as excinfo:
        await get_foreup_captcha_token_2captcha(
            api_key="fakekeyfakekeyfakekeyfakekey",
            page_url=_PAGE_URL,
            poll_interval_s=0.0,
            submit_backoff_s=0.0,
        )
    assert "fakekeyfakekeyfakekeyfakekey" not in str(excinfo.value)
    assert "503" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Transient-failure retries (retry audit 2026-09-27). A single blip used to throw away the
# whole solve: a submit 5xx / transport error / ERROR_NO_SLOT_AVAILABLE (2captcha's documented
# "busy, retry shortly"), or ONE failed result poll of an already-paid task.
# ---------------------------------------------------------------------------


def _ready() -> httpx.Response:
    return httpx.Response(200, json={"status": 1, "request": "tok"})


def _queued() -> httpx.Response:
    return httpx.Response(200, json={"status": 1, "request": "task-7"})


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(503),
        httpx.Response(429),
        httpx.Response(200, json={"status": 0, "request": "ERROR_NO_SLOT_AVAILABLE"}),
        httpx.ConnectError("blip"),
    ],
)
@respx.mock
async def test_2captcha_submit_retries_a_transient_failure(first: object) -> None:
    submit = respx.post(_SUBMIT_URL).mock(side_effect=[first, _queued()])
    respx.get(_RESULT_URL).mock(return_value=_ready())

    token = await get_foreup_captcha_token_2captcha(
        api_key="k", page_url=_PAGE_URL, poll_interval_s=0.0, submit_backoff_s=0.0
    )

    assert token == "tok"
    assert submit.call_count == 2


@respx.mock
async def test_2captcha_submit_does_not_retry_a_permanent_error() -> None:
    submit = respx.post(_SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"status": 0, "request": "ERROR_ZERO_BALANCE"})
    )
    with pytest.raises(RuntimeError, match="ERROR_ZERO_BALANCE"):
        await get_foreup_captcha_token_2captcha(
            api_key="k", page_url=_PAGE_URL, poll_interval_s=0.0, submit_backoff_s=0.0
        )
    assert submit.call_count == 1


@respx.mock
async def test_2captcha_submit_retries_are_bounded() -> None:
    submit = respx.post(_SUBMIT_URL).mock(return_value=httpx.Response(502))
    with pytest.raises(RuntimeError, match="502"):
        await get_foreup_captcha_token_2captcha(
            api_key="k", page_url=_PAGE_URL, poll_interval_s=0.0, submit_backoff_s=0.0
        )
    assert submit.call_count == 3  # 1 + the default 2 retries


@pytest.mark.parametrize("blip", [httpx.Response(502), httpx.ReadTimeout("slow")])
@respx.mock
async def test_2captcha_poll_blip_consumes_one_poll_not_the_solve(blip: object) -> None:
    respx.post(_SUBMIT_URL).mock(return_value=_queued())
    poll = respx.get(_RESULT_URL).mock(side_effect=[blip, _ready()])

    token = await get_foreup_captcha_token_2captcha(
        api_key="k", page_url=_PAGE_URL, poll_interval_s=0.0
    )

    assert token == "tok"
    assert poll.call_count == 2


@respx.mock
async def test_2captcha_persistent_poll_failures_raise_sanitized() -> None:
    respx.post(_SUBMIT_URL).mock(return_value=_queued())
    poll = respx.get(_RESULT_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(RuntimeError) as excinfo:
        await get_foreup_captcha_token_2captcha(
            api_key="fakekeyfakekeyfakekeyfakekey", page_url=_PAGE_URL, poll_interval_s=0.0
        )

    assert poll.call_count == 3  # the default max_poll_errors consecutive failures
    assert "fakekeyfakekeyfakekeyfakekey" not in str(excinfo.value)
    assert "ConnectError" in str(excinfo.value)


@respx.mock
async def test_2captcha_poll_blips_never_extend_the_poll_budget() -> None:
    respx.post(_SUBMIT_URL).mock(return_value=_queued())
    not_ready = httpx.Response(200, json={"status": 0, "request": "CAPCHA_NOT_READY"})
    poll = respx.get(_RESULT_URL).mock(side_effect=[httpx.Response(500), not_ready, not_ready])

    with pytest.raises(TimeoutError):
        await get_foreup_captcha_token_2captcha(
            api_key="k", page_url=_PAGE_URL, poll_interval_s=0.0, max_polls=3
        )
    assert poll.call_count == 3


# ---------------------------------------------------------------------------
# resolve_invisible_site_key — drift protection against ForeUP key rotation
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolve_site_key_returns_known_key_when_page_matches() -> None:
    """When the page still carries the hardcoded key, return it (no drift)."""
    html = f'<script>var CAPTCHA_INVISIBLE_SITE_KEY = "{FOREUP_RECAPTCHA_SITE_KEY}";</script>'
    respx.get(_PAGE_URL).mock(return_value=httpx.Response(200, text=html))
    assert await resolve_invisible_site_key(_PAGE_URL) == FOREUP_RECAPTCHA_SITE_KEY


@respx.mock
async def test_resolve_site_key_returns_live_key_on_rotation() -> None:
    """If ForeUP rotated the key, return the live value extracted from the page."""
    new_key = "6LeNEWKEY00000000000000000000000000000000"
    respx.get(_PAGE_URL).mock(
        return_value=httpx.Response(200, text=f"CAPTCHA_INVISIBLE_SITE_KEY: '{new_key}'")
    )
    assert await resolve_invisible_site_key(_PAGE_URL) == new_key


@respx.mock
async def test_resolve_site_key_falls_back_when_key_absent() -> None:
    """If the page has no recognizable key, fall back to the hardcoded constant."""
    respx.get(_PAGE_URL).mock(return_value=httpx.Response(200, text="<html>no key</html>"))
    assert await resolve_invisible_site_key(_PAGE_URL) == FOREUP_RECAPTCHA_SITE_KEY


@respx.mock
async def test_resolve_site_key_falls_back_on_http_error() -> None:
    """A network/HTTP failure must never raise — fall back to the hardcoded key."""
    respx.get(_PAGE_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await resolve_invisible_site_key(_PAGE_URL) == FOREUP_RECAPTCHA_SITE_KEY
