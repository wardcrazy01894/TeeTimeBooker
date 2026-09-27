"""reCAPTCHA v2 invisible token provider for ForeUP bookings.

ForeUP's booking POST requires a valid reCAPTCHA v2 invisible token in the
`captchaid` field. Solving is delegated to 2captcha:

  2captcha (get_foreup_captcha_token_2captcha / make_2captcha_provider):
    Delegates solving to 2captcha.com's human/AI solver pool. Reliable, costs
    ~$0.003/solve (~$0.15/year for weekly bookings). Requires an API key from
    https://2captcha.com; set TWOCAPTCHA_API_KEY in .env.

  (A Playwright headless-browser solver existed previously but was removed: it was
  unreliable — Google's risk scorer rejected the automated browser — and the deployed
  container image carries no browser binary. 2captcha is the only live solver now.)

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

_TWOCAPTCHA_SUBMIT_URL = "https://2captcha.com/in.php"
_TWOCAPTCHA_RESULT_URL = "https://2captcha.com/res.php"
_TWOCAPTCHA_DEFAULT_POLL_INTERVAL_S = 5.0
_TWOCAPTCHA_DEFAULT_MAX_POLLS = 24  # 24 x 5 s = 2 min per attempt
_TWOCAPTCHA_DEFAULT_SUBMIT_RETRIES = 2
_TWOCAPTCHA_DEFAULT_SUBMIT_BACKOFF_S = 3.0  # linear: 3 s, then 6 s
_TWOCAPTCHA_DEFAULT_MAX_POLL_ERRORS = 3  # consecutive failed polls before giving up


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


# ---------------------------------------------------------------------------
# 2captcha provider (reliable, ~$0.003/solve)
# ---------------------------------------------------------------------------


class _TransientCaptchaError(Exception):
    """A 2captcha call failed in a way a quick replay can fix. Carries a SANITIZED description
    (status code or exception class, never the URL: the poll URL carries the API key)."""


# 2captcha's documented "no free workers right now, retry shortly" submit answer.
_TWOCAPTCHA_BUSY = "ERROR_NO_SLOT_AVAILABLE"
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500


def _is_transient_status(status: int) -> bool:
    return status == _HTTP_TOO_MANY_REQUESTS or status >= _HTTP_SERVER_ERROR


async def _submit_task(
    client: httpx.AsyncClient, data: dict[str, str], *, retries: int, backoff_s: float
) -> str:
    """POST the task, replaying a TRANSIENT failure (transport error, 429 / 5xx,
    ``ERROR_NO_SLOT_AVAILABLE``) up to ``retries`` times with linear backoff. A replay after a
    lost response can queue a second task: harmless (each solve is ~$0.003 and single-use), so
    the submit is safe to retry. A permanent answer (bad key, zero balance) is never replayed."""
    attempt = 0
    while True:
        try:
            r = await client.post(_TWOCAPTCHA_SUBMIT_URL, data=data)
        except httpx.TransportError as exc:
            failure = f"transport error ({type(exc).__name__})"
        else:
            # NOT raise_for_status(): httpx.HTTPStatusError embeds the full request URL in its
            # message, and that message reaches Log Analytics via the booking-run `exc_info`
            # log. Sanitize (status survives; the URL does not). full-repo-scan 2026-07-09 H1.
            if not r.is_success:
                if not _is_transient_status(r.status_code):
                    raise RuntimeError(f"2captcha submission failed: HTTP {r.status_code}")
                failure = f"HTTP {r.status_code}"
            else:
                submit: Any = r.json()
                if isinstance(submit, dict) and submit.get("status") == 1:
                    return str(submit["request"])
                detail = submit.get("request") if isinstance(submit, dict) else repr(submit)
                if detail != _TWOCAPTCHA_BUSY:
                    raise RuntimeError(f"2captcha submission failed: {detail}")
                failure = str(detail)
        if attempt >= retries:
            raise RuntimeError(f"2captcha submission failed: {failure}")
        attempt += 1
        _log.warning(
            "2captcha: submit transient failure (%s); retry %d/%d", failure, attempt, retries
        )
        if backoff_s:
            await asyncio.sleep(backoff_s * attempt)


async def _poll_once(client: httpx.AsyncClient, *, api_key: str, task_id: str) -> object:
    """One result poll. A transport error or 429 / 5xx raises ``_TransientCaptchaError``; any
    other non-2xx raises a sanitized RuntimeError (the key is in THIS request's query string, so
    an HTTPStatusError here would leak it into the logged exception message)."""
    try:
        r = await client.get(
            _TWOCAPTCHA_RESULT_URL,
            params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
        )
    except httpx.TransportError as exc:
        raise _TransientCaptchaError(f"transport error ({type(exc).__name__})") from None
    if not r.is_success:
        if _is_transient_status(r.status_code):
            raise _TransientCaptchaError(f"HTTP {r.status_code}")
        raise RuntimeError(f"2captcha result poll failed: HTTP {r.status_code} (task {task_id})")
    return r.json()


async def get_foreup_captcha_token_2captcha(
    *,
    api_key: str,
    page_url: str,
    site_key: str = FOREUP_RECAPTCHA_SITE_KEY,
    poll_interval_s: float = _TWOCAPTCHA_DEFAULT_POLL_INTERVAL_S,
    max_polls: int = _TWOCAPTCHA_DEFAULT_MAX_POLLS,
    submit_retries: int = _TWOCAPTCHA_DEFAULT_SUBMIT_RETRIES,
    submit_backoff_s: float = _TWOCAPTCHA_DEFAULT_SUBMIT_BACKOFF_S,
    max_poll_errors: int = _TWOCAPTCHA_DEFAULT_MAX_POLL_ERRORS,
) -> str:
    """Solve ForeUP's reCAPTCHA v2 invisible via 2captcha.com.

    Submits a task to 2captcha's solver pool, polls until the result is ready,
    and returns the token. Typically resolves in 15-30 seconds.

    Transient failures are retried (retry audit 2026-09-27): the submit up to
    ``submit_retries`` times; a failed result poll CONSUMES that poll (the paid task keeps
    solving server-side) and only ``max_poll_errors`` consecutive failures give up. Poll blips
    never add polls, so the total wait stays ``max_polls * poll_interval_s`` — the budget the
    race's 120 s prefetch lead is sized for.

    Raises RuntimeError on API errors, TimeoutError if max_polls is exhausted.
    """
    _log.info("2captcha: submitting CAPTCHA task...")
    async with httpx.AsyncClient() as client:
        task_id = await _submit_task(
            client,
            {
                "key": api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "invisible": "1",
                "json": "1",
            },
            retries=submit_retries,
            backoff_s=submit_backoff_s,
        )
        _log.info("2captcha: task %s queued, polling for result...", task_id)

        consecutive_errors = 0
        for i in range(max_polls):
            await asyncio.sleep(poll_interval_s)
            elapsed = int((i + 1) * poll_interval_s)
            _log.info(
                "2captcha: waiting for solve (attempt %d/%d, ~%ds elapsed)...",
                i + 1,
                max_polls,
                elapsed,
            )
            try:
                result: object = await _poll_once(client, api_key=api_key, task_id=task_id)
            except _TransientCaptchaError as exc:
                consecutive_errors += 1
                if consecutive_errors >= max_poll_errors:
                    raise RuntimeError(
                        f"2captcha result poll failed: {exc} (task {task_id})"
                    ) from None
                _log.warning(
                    "2captcha: result poll transient failure (%s, task %s); next poll continues",
                    exc,
                    task_id,
                )
                continue
            consecutive_errors = 0
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
