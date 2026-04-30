"""reCAPTCHA v2 invisible token providers for ForeUP bookings.

ForeUP's booking POST requires a valid reCAPTCHA v2 invisible token in the
`captchaid` field. Two providers are available:

1. Playwright (get_foreup_captcha_token / make_captcha_provider):
   Launches a headless browser and executes reCAPTCHA on the real ForeUP page.
   Free but unreliable — Google's risk scorer often rejects Playwright browsers.
   Use only from residential IPs where bot detection is lenient.

2. 2captcha (get_foreup_captcha_token_2captcha / make_2captcha_provider):
   Delegates solving to 2captcha.com's human/AI solver pool. Reliable, costs
   ~$0.003/solve (~$0.15/year for weekly bookings). Requires an API key from
   https://2captcha.com; set TWOCAPTCHA_API_KEY in .env.

Site key confirmed from ForeUP's booking page source:
    6LfZGS0qAAAAAMVgxySjd43HvklGdg1Jady2TolK

Tokens expire in ~2 minutes; providers are called immediately before book().
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from playwright.async_api import async_playwright

FOREUP_RECAPTCHA_SITE_KEY = "6LfZGS0qAAAAAMVgxySjd43HvklGdg1Jady2TolK"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Arrow function: takes siteKey, returns Promise<string>.
# Renders a fresh invisible widget, executes it, resolves on the callback.
_RECAPTCHA_JS = """\
(siteKey) => new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('reCAPTCHA timeout')), 25000);
    const el = document.createElement('div');
    document.body.appendChild(el);
    const id = window.grecaptcha.render(el, {
        sitekey: siteKey,
        size: 'invisible',
        callback: token => { clearTimeout(t); resolve(token); },
        'error-callback': () => {
            clearTimeout(t);
            reject(new Error('reCAPTCHA error-callback fired'));
        }
    });
    window.grecaptcha.execute(id);
})
"""

_TWOCAPTCHA_SUBMIT_URL = "https://2captcha.com/in.php"
_TWOCAPTCHA_RESULT_URL = "https://2captcha.com/res.php"
_TWOCAPTCHA_DEFAULT_POLL_INTERVAL_S = 5.0
_TWOCAPTCHA_DEFAULT_MAX_POLLS = 24  # 24 x 5 s = 2 min max wait


# ---------------------------------------------------------------------------
# Playwright provider (free, may be blocked by Google bot detection)
# ---------------------------------------------------------------------------


async def get_foreup_captcha_token(
    *,
    booking_page_url: str,
    site_key: str = FOREUP_RECAPTCHA_SITE_KEY,
) -> str:
    """Launch a headless browser, navigate to the ForeUP booking page, and return
    a fresh reCAPTCHA v2 invisible token.

    Unreliable if Google's risk scorer detects the automated browser. Use
    get_foreup_captcha_token_2captcha for a reliable alternative.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.goto(booking_page_url, wait_until="load")

        # reCAPTCHA api.js is loaded by the booking widget; wait for the global.
        await page.wait_for_function(
            "typeof window.grecaptcha !== 'undefined' "
            "&& typeof window.grecaptcha.render === 'function'",
            timeout=15_000,
        )

        raw: object = await page.evaluate(_RECAPTCHA_JS, site_key)
        await browser.close()

    return str(raw)


def make_captcha_provider(booking_page_url: str) -> Callable[[], Awaitable[str]]:
    """Return a zero-argument async callable that fetches a fresh Playwright token."""

    async def _provider() -> str:
        return await get_foreup_captcha_token(booking_page_url=booking_page_url)

    return _provider


# ---------------------------------------------------------------------------
# 2captcha provider (reliable, ~$0.003/solve)
# ---------------------------------------------------------------------------


async def get_foreup_captcha_token_2captcha(
    *,
    api_key: str,
    page_url: str,
    site_key: str = FOREUP_RECAPTCHA_SITE_KEY,
    poll_interval_s: float = _TWOCAPTCHA_DEFAULT_POLL_INTERVAL_S,
    max_polls: int = _TWOCAPTCHA_DEFAULT_MAX_POLLS,
) -> str:
    """Solve ForeUP's reCAPTCHA v2 invisible via 2captcha.com.

    Submits a task to 2captcha's solver pool, polls until the result is ready,
    and returns the token. Typically resolves in 15-30 seconds.

    Raises RuntimeError on API errors, TimeoutError if max_polls is exhausted.
    """
    async with httpx.AsyncClient() as client:
        r = await client.post(
            _TWOCAPTCHA_SUBMIT_URL,
            data={
                "key": api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "invisible": "1",
                "json": "1",
            },
        )
        r.raise_for_status()
        submit: Any = r.json()
        if not isinstance(submit, dict) or submit.get("status") != 1:
            detail = submit.get("request") if isinstance(submit, dict) else repr(submit)
            raise RuntimeError(f"2captcha submission failed: {detail}")
        task_id = str(submit["request"])

        for _ in range(max_polls):
            await asyncio.sleep(poll_interval_s)
            r = await client.get(
                _TWOCAPTCHA_RESULT_URL,
                params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
            )
            r.raise_for_status()
            result: Any = r.json()
            if not isinstance(result, dict):
                raise RuntimeError(f"2captcha unexpected response: {result!r}")
            if result.get("status") == 1:
                return str(result["request"])
            if result.get("request") != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2captcha error: {result.get('request')}")

    raise TimeoutError(
        f"2captcha did not solve CAPTCHA within {max_polls * poll_interval_s:.0f}s"
    )


def make_2captcha_provider(
    api_key: str,
    page_url: str,
) -> Callable[[], Awaitable[str]]:
    """Return a zero-argument async callable that solves reCAPTCHA via 2captcha.com."""

    async def _provider() -> str:
        return await get_foreup_captcha_token_2captcha(api_key=api_key, page_url=page_url)

    return _provider
