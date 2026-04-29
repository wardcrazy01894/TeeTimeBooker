"""Playwright-based reCAPTCHA v2 invisible token provider for ForeUP bookings.

ForeUP's booking POST requires a reCAPTCHA v2 invisible token in the `captchaid`
field. This module navigates to the booking page in a real browser context so
Google's reCAPTCHA can run against the correct domain and a genuine browser
fingerprint. Tokens expire in ~2 minutes; call this immediately before book().

Site key confirmed from ForeUP's booking page source:
    6LfZGS0qAAAAAMVgxySjd43HvklGdg1Jady2TolK
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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


async def get_foreup_captcha_token(
    *,
    booking_page_url: str,
    site_key: str = FOREUP_RECAPTCHA_SITE_KEY,
) -> str:
    """Launch a headless browser, navigate to the ForeUP booking page, and return
    a fresh reCAPTCHA v2 invisible token.

    The page must be on foreupsoftware.com so the site key's domain restriction
    is satisfied. Raises TimeoutError if reCAPTCHA does not resolve in ~30 s.
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
    """Return a zero-argument async callable that fetches a fresh token."""

    async def _provider() -> str:
        return await get_foreup_captcha_token(booking_page_url=booking_page_url)

    return _provider
