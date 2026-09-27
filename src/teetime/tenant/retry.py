"""Bounded retry of TRANSIENT tenant-store failures (the retry audit, 2026-09-27).

What the azure-cosmos async SDK (4.17) already does with no options passed, so this layer does
NOT repeat it inside one call: 429 is retried up to 9 times / 30 s honouring
``x-ms-retry-after-ms`` (reads AND writes — a throttled request was never processed); 503 and a
connection that was never established are retried for every operation; 408 / 5xx / a read timeout
(the response was lost) are retried for READS only, because a write whose response was lost may
have landed (``retry_write`` stays 0). ``DefaultAzureCredential``'s managed-identity token fetch
is retried by azure-core's own pipeline policy.

What reaches the store's callers is therefore what the SDK gave up on. ``retry_transient`` is the
call-site layer on top of it, and it is for IDEMPOTENT calls only — a read, a same-owner claim,
or an IfMatch'd write (a replay after an ambiguous success re-reads the row and is refused by the
etag instead of double-applying). Never put a non-idempotent write (a counter increment, a plain
create with no deterministic id) behind it.

``is_transient_store_error`` deliberately excludes the built-in ``TimeoutError`` the runner's
``_store_call`` raises when a call ate its whole budget: that bound stays the bound.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from azure.cosmos.exceptions import CosmosClientTimeoutError

from ..core.clock import Clock

log = logging.getLogger(__name__)

# 449 = Cosmos "retry with" (a concurrent write conflict the server asks us to replay).
TRANSIENT_STATUSES = frozenset({408, 429, 449, 500, 502, 503, 504})
_RETRY_AFTER_HEADER = "x-ms-retry-after-ms"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """``attempts`` counts the first try; the n-th retry sleeps ``backoff_s * n`` (or the
    server's retry-after hint), capped at ``max_delay_s``. Tests pass ``backoff_s=0``."""

    attempts: int = 3
    backoff_s: float = 1.0
    max_delay_s: float = 10.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")
        if self.backoff_s < 0 or self.max_delay_s < 0:
            raise ValueError("backoff_s and max_delay_s must be >= 0")


DEFAULT_STORE_RETRY = RetryPolicy()
NO_RETRY = RetryPolicy(attempts=1)


def is_transient_store_error(exc: BaseException) -> bool:
    """True for a failure a quick replay can fix: a transient HTTP status, a connection that
    failed or whose response was lost, the SDK's own client timeout. An exception GROUP (what
    ``record_outcomes`` raises) is transient only when EVERY leaf is — one refusal makes the
    whole group a decision, not a blip."""
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(is_transient_store_error(e) for e in exc.exceptions)
    if isinstance(exc, HttpResponseError):
        return getattr(exc, "status_code", None) in TRANSIENT_STATUSES
    return isinstance(exc, ServiceRequestError | ServiceResponseError | CosmosClientTimeoutError)


def _retry_after_s(exc: BaseException) -> float | None:
    headers = getattr(exc, "headers", None)
    if not headers:
        return None
    try:
        return float(headers.get(_RETRY_AFTER_HEADER)) / 1000.0
    except (TypeError, ValueError):
        return None


async def retry_transient[T](
    make_call: Callable[[], Awaitable[T]],
    *,
    label: str,
    clock: Clock,
    policy: RetryPolicy = DEFAULT_STORE_RETRY,
    not_after: datetime | None = None,
) -> T:
    """Await ``make_call()`` (a thunk: each attempt issues a FRESH call), replaying it on a
    transient failure up to ``policy.attempts`` times. A retry whose sleep would end at or past
    ``not_after`` is not attempted (the race-window guard): the last error is raised. Logs the
    exception CLASS only (a driver message can carry connection details)."""
    attempt = 1
    while True:
        try:
            return await make_call()
        except Exception as exc:
            if not is_transient_store_error(exc) or attempt >= policy.attempts:
                raise
            hint = _retry_after_s(exc)
            delay = min(
                policy.max_delay_s, hint if hint is not None else policy.backoff_s * attempt
            )
            if not_after is not None and clock.now_utc() + timedelta(seconds=delay) >= not_after:
                log.warning(
                    "%s: transient %s; no retry budget left before %s",
                    label,
                    type(exc).__name__,
                    not_after.isoformat(),
                )
                raise
            log.warning(
                "%s: transient %s (attempt %d/%d); retrying in %.1fs",
                label,
                type(exc).__name__,
                attempt,
                policy.attempts,
                delay,
            )
            await clock.sleep(delay)
            attempt += 1
