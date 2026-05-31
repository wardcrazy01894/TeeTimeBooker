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

Invisible site key confirmed from ForeUP's booking page source (CAPTCHA_INVISIBLE_SITE_KEY):
    6Le0bf4pAAAAALufPGSllYP0-QN79MW_XTUa-24h
The page also defines CAPTCHA_VISIBLE_SITE_KEY (6LfZGS0q...) — that is the wrong key;
the booking widget callback uses the invisible key.

Tokens expire in ~2 minutes. In the normal booking path the provider is called inline by
book(); in the upgrade path prepare_book() pre-fetches it just before cancel_reservation()
to shrink the cancel-to-book no-booking window (the cancel round-trip is ~1-2 s).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from playwright.async_api import async_playwright

_log = logging.getLogger(__name__)

FOREUP_RECAPTCHA_SITE_KEY = "6Le0bf4pAAAAALufPGSllYP0-QN79MW_XTUa-24h"

# ForeUP's booking page defines `CAPTCHA_INVISIBLE_SITE_KEY = "<key>"` in inline JS.
# We extract it at pre-flight to detect a key rotation (which would otherwise make
# every solve fail with an invalid-key error from Google).
_INVISIBLE_SITE_KEY_RE = re.compile(r"""CAPTCHA_INVISIBLE_SITE_KEY\s*[=:]\s*['"]([^'"]+)['"]""")

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
    _log.info("Playwright: launching headless browser...")
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
        _log.info("Playwright: navigating to ForeUP booking page...")
        await page.goto(booking_page_url, wait_until="load")

        _log.info("Playwright: waiting for reCAPTCHA to load...")
        await page.wait_for_function(
            "typeof window.grecaptcha !== 'undefined' "
            "&& typeof window.grecaptcha.render === 'function'",
            timeout=15_000,
        )

        _log.info("Playwright: executing reCAPTCHA widget (up to 25s)...")
        raw: object = await page.evaluate(_RECAPTCHA_JS, site_key)
        await browser.close()

    _log.info("Playwright: reCAPTCHA token obtained")
    return str(raw)


async def resolve_invisible_site_key(
    booking_page_url: str,
    *,
    fallback: str = FOREUP_RECAPTCHA_SITE_KEY,
    timeout_s: float = 15.0,
) -> str:
    """Fetch the booking page and return the live invisible reCAPTCHA site key.

    Guards against ForeUP silently rotating the key (which would make every solve
    fail with an invalid-key error). Returns ``fallback`` on any error or if no key
    is found, and logs a WARNING when the live key differs from the hardcoded one.

    Best-effort and network-touching: **call this as a pre-flight, before the T0
    busy-wait — never in the race path.**
    """
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                booking_page_url,
                headers={"User-Agent": _USER_AGENT},
                timeout=timeout_s,
            )
            r.raise_for_status()
            match = _INVISIBLE_SITE_KEY_RE.search(r.text)
    except Exception as exc:
        _log.warning("Site-key resolve failed for %s (%s); using fallback", booking_page_url, exc)
        return fallback
    if match is None:
        _log.warning(
            "Invisible reCAPTCHA site key not found on %s; using fallback", booking_page_url
        )
        return fallback
    live = match.group(1)
    if live != fallback:
        _log.warning(
            "ForeUP reCAPTCHA invisible site key changed: %s -> %s; using live key",
            fallback,
            live,
        )
    return live


def make_captcha_provider(
    booking_page_url: str,
    site_key: str = FOREUP_RECAPTCHA_SITE_KEY,
) -> Callable[[], Awaitable[str]]:
    """Return a zero-argument async callable that fetches a fresh Playwright token."""

    async def _provider() -> str:
        return await get_foreup_captcha_token(booking_page_url=booking_page_url, site_key=site_key)

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
    _log.info("2captcha: submitting CAPTCHA task...")
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
        _log.info("2captcha: task %s queued, polling for result...", task_id)

        for i in range(max_polls):
            await asyncio.sleep(poll_interval_s)
            elapsed = int((i + 1) * poll_interval_s)
            _log.info(
                "2captcha: waiting for solve (attempt %d/%d, ~%ds elapsed)...",
                i + 1,
                max_polls,
                elapsed,
            )
            r = await client.get(
                _TWOCAPTCHA_RESULT_URL,
                params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
            )
            r.raise_for_status()
            result: Any = r.json()
            if not isinstance(result, dict):
                raise RuntimeError(f"2captcha unexpected response: {result!r}")
            if result.get("status") == 1:
                _log.info("2captcha: token received after ~%ds", elapsed)
                return str(result["request"])
            if result.get("request") != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2captcha error: {result.get('request')}")

    raise TimeoutError(f"2captcha did not solve CAPTCHA within {max_polls * poll_interval_s:.0f}s")


def make_2captcha_provider(
    api_key: str,
    page_url: str,
    site_key: str = FOREUP_RECAPTCHA_SITE_KEY,
) -> Callable[[], Awaitable[str]]:
    """Return a zero-argument async callable that solves reCAPTCHA via 2captcha.com."""

    async def _provider() -> str:
        return await get_foreup_captcha_token_2captcha(
            api_key=api_key, page_url=page_url, site_key=site_key
        )

    return _provider
