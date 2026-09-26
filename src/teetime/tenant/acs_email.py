"""Azure Communication Services Email over REST (MULTIUSER_PLAN §8.7 / §10, MU-11 — unwired).

A small async ``EmailSender`` over httpx — no Azure SDK dependency. Requests are signed with
ACS's HMAC-SHA256 scheme:

* ``x-ms-content-sha256`` = base64(SHA-256(body))
* ``x-ms-date``           = RFC 1123 UTC date
* string-to-sign          = ``VERB\\n<path?query>\\n<x-ms-date>;<host>;<x-ms-content-sha256>``
* ``Authorization``       = ``HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256&
  Signature=<base64(HMAC-SHA256(base64decode(access key), string-to-sign))>``

``send`` POSTs ``/emails:send`` and polls the ``Operation-Location`` until the operation is
``Succeeded`` / ``Failed`` / ``Canceled`` or a bounded timeout passes. 429 / 5xx / transport
errors are retried a bounded number of times honouring ``Retry-After`` (capped); one
``repeatability-request-id`` is kept across the retries of a send, so ACS deduplicates a POST
that landed but whose response was lost. **It RETURNS an ``EmailSendResult`` and never raises**:
a mail failure must never mask a booking outcome (the caller decides what it means — the
operator-summary failure forces a non-zero exit, ``notify.deliver_operator_summary``).

Configuration comes from env vars, by NAME only: ``ACS_EMAIL_CONNECTION`` (the connection string,
a Key Vault secret — ``endpoint=https://…/;accesskey=…``) and ``ACS_EMAIL_SENDER`` (the
Azure-managed domain's sender address, e.g. ``DoNotReply@<guid>.azurecomm.net``).
``load_acs_settings`` registers the access key as an E7 secret literal so it is masked in every
log line even if some future code path echoes it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import format_datetime
from urllib.parse import urlsplit

import httpx

from ..core.clock import Clock, RealClock
from ..core.redaction import redact_text, register_secret_literals
from .notify import EmailMessage, EmailSendResult

log = logging.getLogger(__name__)

ACS_EMAIL_CONNECTION_ENV = "ACS_EMAIL_CONNECTION"
ACS_EMAIL_SENDER_ENV = "ACS_EMAIL_SENDER"
ACS_API_VERSION = "2023-03-31"

_SIGNED_HEADERS = "x-ms-date;host;x-ms-content-sha256"
_TERMINAL_OK = "Succeeded"
_TERMINAL_FAIL = frozenset({"Failed", "Canceled"})
_ERROR_DETAIL_MAX = 200


class AcsConfigError(ValueError):
    """Missing or malformed ACS configuration. Messages name env vars / fields, never values."""


@dataclass(frozen=True, slots=True)
class AcsConnection:
    endpoint: str  # https://<resource>.communication.azure.com (no trailing slash)
    access_key: str = field(repr=False)  # base64; a secret

    @property
    def host(self) -> str:
        return urlsplit(self.endpoint).netloc


@dataclass(frozen=True, slots=True)
class AcsSettings:
    connection: AcsConnection
    sender_address: str


def parse_connection_string(value: str) -> AcsConnection:
    """Parse ``endpoint=https://…/;accesskey=…``. Errors never echo the value (it holds the key)."""
    parts: dict[str, str] = {}
    for chunk in value.split(";"):
        name, sep, rest = chunk.partition("=")
        if sep:
            parts[name.strip().lower()] = rest.strip()
    endpoint = parts.get("endpoint", "").rstrip("/")
    key = parts.get("accesskey", "")
    if not endpoint.startswith("https://") or not urlsplit(endpoint).netloc:
        raise AcsConfigError("ACS connection string has no valid https 'endpoint='")
    if not key:
        raise AcsConfigError("ACS connection string has no 'accesskey='")
    try:
        base64.b64decode(key, validate=True)
    except ValueError:
        raise AcsConfigError("ACS connection string 'accesskey' is not base64") from None
    return AcsConnection(endpoint=endpoint, access_key=key)


def load_acs_settings(env: Mapping[str, str] | None = None) -> AcsSettings:
    """Read ``ACS_EMAIL_CONNECTION`` + ``ACS_EMAIL_SENDER`` and register the access key as an E7
    secret literal. ``env`` defaults to ``os.environ``."""
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(ACS_EMAIL_CONNECTION_ENV, "").strip()
    if not raw:
        raise AcsConfigError(f"{ACS_EMAIL_CONNECTION_ENV} is not set")
    sender = source.get(ACS_EMAIL_SENDER_ENV, "").strip()
    if not sender:
        raise AcsConfigError(f"{ACS_EMAIL_SENDER_ENV} is not set")
    connection = parse_connection_string(raw)
    register_secret_literals([connection.access_key])
    return AcsSettings(connection=connection, sender_address=sender)


# --- signing (pure) ------------------------------------------------------------------------------


def content_hash(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def string_to_sign(
    method: str, path_and_query: str, *, date: str, host: str, content_hash: str
) -> str:
    return f"{method.upper()}\n{path_and_query}\n{date};{host};{content_hash}"


def sign_request(
    *, method: str, url: str, body: bytes, access_key: str, date: datetime
) -> dict[str, str]:
    """The three ACS auth headers for one request. ``date`` must be tz-aware."""
    parsed = urlsplit(url)
    path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    rfc1123 = format_datetime(date, usegmt=True)
    body_hash = content_hash(body)
    to_sign = string_to_sign(
        method, path_and_query, date=rfc1123, host=parsed.netloc, content_hash=body_hash
    )
    digest = hmac.new(base64.b64decode(access_key), to_sign.encode("utf-8"), hashlib.sha256)
    signature = base64.b64encode(digest.digest()).decode("ascii")
    return {
        "x-ms-date": rfc1123,
        "x-ms-content-sha256": body_hash,
        "Authorization": f"HMAC-SHA256 SignedHeaders={_SIGNED_HEADERS}&Signature={signature}",
    }


# --- client --------------------------------------------------------------------------------------


class _RetriesExhaustedError(Exception):
    def __init__(self, status: str, error: str | None) -> None:
        super().__init__(status)
        self.status = status
        self.error = error


def _is_retryable(status_code: int) -> bool:
    return (
        status_code == httpx.codes.TOO_MANY_REQUESTS
        or status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )


def _error_detail(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None
    text = " ".join(str(err[k]) for k in ("code", "message") if err.get(k))
    return redact_text(text)[:_ERROR_DETAIL_MAX] or None


class AcsEmailClient:
    """``EmailSender`` for ACS Email. Construct from ``load_acs_settings()``; see module doc."""

    def __init__(
        self,
        connection: AcsConnection,
        *,
        sender_address: str,
        clock: Clock | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        retry_backoff_s: float = 2.0,
        max_retry_after_s: float = 30.0,
        poll_interval_s: float = 2.0,
        poll_timeout_s: float = 60.0,
        request_timeout_s: float = 20.0,
    ) -> None:
        self._conn = connection
        self._sender = sender_address
        self._clock: Clock = clock or RealClock()
        self._http = http_client
        self._max_retries = max_retries
        self._backoff = retry_backoff_s
        self._max_retry_after = max_retry_after_s
        self._poll_interval = poll_interval_s
        self._poll_timeout = poll_timeout_s
        self._timeout = request_timeout_s

    async def send(self, message: EmailMessage) -> EmailSendResult:
        try:
            if self._http is not None:
                return await self._send(self._http, message)
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await self._send(client, message)
        except _RetriesExhaustedError as exc:
            result = EmailSendResult(ok=False, status=exc.status, error=exc.error)
        except Exception as exc:  # never raise: a mail failure must not mask an outcome
            result = EmailSendResult(ok=False, status="error", error=type(exc).__name__)
        log.warning("ACS email not sent: status=%s error=%s", result.status, result.error)
        return result

    async def _send(self, client: httpx.AsyncClient, message: EmailMessage) -> EmailSendResult:
        body = json.dumps(
            {
                "senderAddress": self._sender,
                "content": {"subject": message.subject, "plainText": message.body},
                "recipients": {"to": [{"address": message.to}]},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        url = f"{self._conn.endpoint}/emails:send?api-version={ACS_API_VERSION}"
        request_id = str(uuid.uuid4())
        first_sent = format_datetime(self._clock.now_utc(), usegmt=True)
        extra = {"repeatability-request-id": request_id, "repeatability-first-sent": first_sent}
        response = await self._request(client, "POST", url, body=body, extra_headers=extra)
        if response.status_code != httpx.codes.ACCEPTED:
            detail = _error_detail(response)
            log.warning("ACS email rejected: HTTP %s %s", response.status_code, detail or "")
            return EmailSendResult(ok=False, status=f"HTTP {response.status_code}", error=detail)
        op_url = response.headers.get("Operation-Location")
        op_id = self._operation_id(response)
        if not op_url:
            return EmailSendResult(
                ok=False, status="accepted", operation_id=op_id, error="no Operation-Location"
            )
        return await self._poll(client, op_url, op_id)

    @staticmethod
    def _operation_id(response: httpx.Response) -> str | None:
        try:
            data = response.json()
        except ValueError:
            return None
        op_id = data.get("id") if isinstance(data, dict) else None
        return str(op_id) if op_id else None

    async def _poll(
        self, client: httpx.AsyncClient, op_url: str, op_id: str | None
    ) -> EmailSendResult:
        deadline = self._clock.now_utc().timestamp() + self._poll_timeout
        while True:
            response = await self._request(client, "GET", op_url, body=b"")
            if response.status_code != httpx.codes.OK:
                detail = _error_detail(response)
                return EmailSendResult(
                    ok=False,
                    status=f"HTTP {response.status_code}",
                    operation_id=op_id,
                    error=detail,
                )
            data = response.json()
            status = str(data.get("status", "")) if isinstance(data, dict) else ""
            op_id = op_id or (
                str(data["id"]) if isinstance(data, dict) and data.get("id") else None
            )
            if status == _TERMINAL_OK:
                log.info("ACS email delivered to the service: operation=%s", op_id)
                return EmailSendResult(ok=True, status=status, operation_id=op_id)
            if status in _TERMINAL_FAIL:
                return EmailSendResult(
                    ok=False, status=status, operation_id=op_id, error=_error_detail(response)
                )
            wait = self._poll_interval
            if self._clock.now_utc().timestamp() + wait > deadline:
                return EmailSendResult(
                    ok=False, status="timeout", operation_id=op_id, error=f"last status {status}"
                )
            await self._clock.sleep(wait)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        body: bytes,
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One signed request with bounded retry on 429 / 5xx / transport errors. Returns the
        final non-retryable response; raises ``_RetriesExhaustedError`` when retries run out."""
        attempt = 0
        while True:
            headers = sign_request(
                method=method,
                url=url,
                body=body,
                access_key=self._conn.access_key,
                date=self._clock.now_utc(),
            )
            headers["Content-Type"] = "application/json"
            headers.update(extra_headers or {})
            error: str | None
            retry_after: float | None
            try:
                response = await client.request(method, url, content=body, headers=headers)
            except httpx.TransportError as exc:
                status, error, retry_after = "transport", type(exc).__name__, None
            else:
                if not _is_retryable(response.status_code):
                    return response
                status = f"HTTP {response.status_code}"
                error = _error_detail(response)
                retry_after = self._retry_after(response)
            if attempt >= self._max_retries:
                raise _RetriesExhaustedError(status, error)
            attempt += 1
            delay = retry_after if retry_after is not None else self._backoff * attempt
            log.info(
                "ACS %s %s: %s; retry %d in %.1fs",
                method,
                url.split("?", maxsplit=1)[0],
                status,
                attempt,
                delay,
            )
            await self._clock.sleep(delay)

    def _retry_after(self, response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After", "").strip()
        if not raw:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            return None
        return max(0.0, min(seconds, self._max_retry_after))
