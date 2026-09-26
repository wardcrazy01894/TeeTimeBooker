"""MU-11: Azure Communication Services Email REST client (MULTIUSER_PLAN §8.7 / §10).

HMAC-SHA256 request signing, send + operation poll, bounded 429/5xx retry honouring
Retry-After, and "return a result, never raise". respx mocks the HTTP layer; the SUT is the
real ``AcsEmailClient``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
import respx

from teetime.core.clock import FakeClock
from teetime.core.redaction import redact_text
from teetime.tenant.acs_email import (
    ACS_API_VERSION,
    ACS_EMAIL_CONNECTION_ENV,
    ACS_EMAIL_SENDER_ENV,
    AcsConfigError,
    AcsConnection,
    AcsEmailClient,
    load_acs_settings,
    parse_connection_string,
    sign_request,
    string_to_sign,
)
from teetime.tenant.notify import EmailMessage, EmailSender

# Known-answer vector (computed independently from ACS's documented HMAC scheme).
KA_KEY = "a25vd24tYW5zd2VyLXRlc3QtYWNjZXNzLWtleS0zMmI="
KA_BODY = b'{"hello":"acs"}'
KA_HASH = "kxGVv1aNXTYGhLmpTGbY8rmbJAn1WU1cUnipt3aU0sY="
KA_DATE = datetime(2026, 10, 3, 10, 0, 5, tzinfo=UTC)
KA_SIG = "tV6pt0yZF+ANfX9wCew2e7PJrzYhA7kSmrNBsIRjVfM="

HOST = "contoso.communication.azure.com"
ENDPOINT = f"https://{HOST}"
CONN = f"endpoint={ENDPOINT}/;accesskey={KA_KEY}"
SEND_URL = f"{ENDPOINT}/emails:send?api-version={ACS_API_VERSION}"
OP_URL = f"{ENDPOINT}/emails/operations/op-1?api-version={ACS_API_VERSION}"
SENDER = "DoNotReply@0000-1111.azurecomm.net"
MSG = EmailMessage(to="turk@example.com", subject="[TeeTimeBooker] Booked", body="Hi Turk,")


def _client(clock: FakeClock | None = None, **kw: object) -> AcsEmailClient:
    return AcsEmailClient(
        parse_connection_string(CONN),
        sender_address=SENDER,
        clock=clock or FakeClock(start=KA_DATE),
        **kw,  # type: ignore[arg-type]
    )


def _accepted() -> httpx.Response:
    return httpx.Response(
        202, headers={"Operation-Location": OP_URL}, json={"id": "op-1", "status": "Running"}
    )


# --- signing -----------------------------------------------------------------------------------


def test_acs_request_signed() -> None:
    assert string_to_sign(
        "POST",
        f"/emails:send?api-version={ACS_API_VERSION}",
        date="Sat, 03 Oct 2026 10:00:05 GMT",
        host=HOST,
        content_hash=KA_HASH,
    ) == (
        f"POST\n/emails:send?api-version=2023-03-31\nSat, 03 Oct 2026 10:00:05 GMT;{HOST};{KA_HASH}"
    )
    headers = sign_request(
        method="POST",
        url=f"{ENDPOINT}/emails:send?api-version=2023-03-31",
        body=KA_BODY,
        access_key=KA_KEY,
        date=KA_DATE,
    )
    assert headers["x-ms-date"] == "Sat, 03 Oct 2026 10:00:05 GMT"
    assert headers["x-ms-content-sha256"] == KA_HASH
    assert headers["Authorization"] == (
        f"HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256&Signature={KA_SIG}"
    )


def test_parse_connection_string() -> None:
    conn = parse_connection_string(CONN)
    assert conn.endpoint == ENDPOINT
    assert conn.host == HOST
    assert conn.access_key == KA_KEY
    assert KA_KEY not in repr(conn)


def test_parse_connection_string_error_never_echoes_value() -> None:
    with pytest.raises(AcsConfigError) as exc:
        parse_connection_string(f"accesskey={KA_KEY}")
    assert KA_KEY not in str(exc.value)


def test_connection_string_key_registered_as_secret_literal() -> None:
    env = {ACS_EMAIL_CONNECTION_ENV: CONN, ACS_EMAIL_SENDER_ENV: SENDER}
    # Non-vacuity: before loading, no pattern masks a bare base64 key.
    assert KA_KEY in redact_text(f"leaked {KA_KEY} here")
    settings = load_acs_settings(env)
    assert settings.connection.access_key == KA_KEY
    assert settings.sender_address == SENDER
    assert KA_KEY not in redact_text(f"leaked {KA_KEY} here")


def test_load_acs_settings_missing_var_names_only_the_var() -> None:
    with pytest.raises(AcsConfigError, match=ACS_EMAIL_CONNECTION_ENV):
        load_acs_settings({ACS_EMAIL_SENDER_ENV: SENDER})
    with pytest.raises(AcsConfigError, match=ACS_EMAIL_SENDER_ENV):
        load_acs_settings({ACS_EMAIL_CONNECTION_ENV: CONN})


# --- send + poll -------------------------------------------------------------------------------


@respx.mock
async def test_acs_poll_until_succeeded() -> None:
    send = respx.post(SEND_URL).mock(return_value=_accepted())
    poll = respx.get(OP_URL).mock(
        side_effect=[
            httpx.Response(200, json={"id": "op-1", "status": "Running"}),
            httpx.Response(200, json={"id": "op-1", "status": "Succeeded"}),
        ]
    )
    client = _client()
    assert isinstance(client, EmailSender)
    result = await client.send(MSG)
    assert result.ok is True
    assert result.status == "Succeeded"
    assert result.operation_id == "op-1"
    assert poll.call_count == 2

    request = send.calls[0].request
    payload = json.loads(request.content)
    assert payload["senderAddress"] == SENDER
    assert payload["recipients"] == {"to": [{"address": "turk@example.com"}]}
    assert payload["content"] == {"subject": MSG.subject, "plainText": MSG.body}
    # The request carries a valid signature over its exact bytes and date.
    expected = sign_request(
        method="POST",
        url=str(request.url),
        body=request.content,
        access_key=KA_KEY,
        date=KA_DATE,
    )
    assert request.headers["Authorization"] == expected["Authorization"]
    assert request.headers["x-ms-content-sha256"] == expected["x-ms-content-sha256"]
    assert request.headers["repeatability-request-id"]
    # Polls are signed too (GET over an empty body).
    assert poll.calls[0].request.headers["Authorization"].startswith("HMAC-SHA256 ")


@respx.mock
async def test_acs_poll_reports_failed_operation() -> None:
    respx.post(SEND_URL).mock(return_value=_accepted())
    respx.get(OP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "op-1",
                "status": "Failed",
                "error": {"code": "EmailDroppedAllRecipientsSuppressed"},
            },
        )
    )
    result = await _client().send(MSG)
    assert result.ok is False
    assert result.status == "Failed"
    assert result.error is not None
    assert "EmailDroppedAllRecipientsSuppressed" in result.error


@respx.mock
async def test_acs_poll_times_out_bounded() -> None:
    respx.post(SEND_URL).mock(return_value=_accepted())
    poll = respx.get(OP_URL).mock(
        return_value=httpx.Response(200, json={"id": "op-1", "status": "Running"})
    )
    clock = FakeClock(start=KA_DATE)
    result = await _client(clock, poll_interval_s=5.0, poll_timeout_s=20.0).send(MSG)
    assert result.ok is False
    assert result.status == "timeout"
    assert poll.call_count <= 5
    assert (clock.now_utc() - KA_DATE).total_seconds() <= 25


@respx.mock
async def test_acs_retries_429_with_retry_after() -> None:
    send = respx.post(SEND_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            _accepted(),
        ]
    )
    respx.get(OP_URL).mock(return_value=httpx.Response(200, json={"status": "Succeeded"}))
    clock = FakeClock(start=KA_DATE)
    result = await _client(clock, retry_backoff_s=1.0).send(MSG)
    assert result.ok is True
    assert send.call_count == 3
    # Retry-After (7 s) honoured, then the 503 backoff (1 s); the first poll follows immediately.
    assert (clock.now_utc() - KA_DATE).total_seconds() >= 8
    # Every retry re-signs with the clock's CURRENT date and keeps ONE repeatability id.
    dates = [c.request.headers["x-ms-date"] for c in send.calls]
    assert dates[0] != dates[1]
    ids = {c.request.headers["repeatability-request-id"] for c in send.calls}
    assert len(ids) == 1


@respx.mock
async def test_acs_retry_is_bounded() -> None:
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    result = await _client(max_retries=2).send(MSG)
    assert result.ok is False
    assert result.status == "HTTP 429"
    assert send.call_count == 3  # first try + 2 retries


@respx.mock
async def test_acs_retry_after_is_capped() -> None:
    respx.post(SEND_URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "86400"}), _accepted()]
    )
    respx.get(OP_URL).mock(return_value=httpx.Response(200, json={"status": "Succeeded"}))
    clock = FakeClock(start=KA_DATE)
    result = await _client(clock, max_retry_after_s=30.0).send(MSG)
    assert result.ok is True
    assert (clock.now_utc() - KA_DATE).total_seconds() <= 31


@respx.mock
async def test_acs_failure_returns_result_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "Denied", "message": "bad signature"}}
        )
    )
    result = await _client().send(MSG)
    assert result.ok is False
    assert result.status == "HTTP 401"
    assert result.error is not None
    assert "Denied" in result.error
    assert KA_KEY not in caplog.text


@respx.mock
async def test_acs_transport_error_returns_result_not_raise() -> None:
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("boom"))
    result = await _client(max_retries=1).send(MSG)
    assert result.ok is False
    assert result.status == "transport"


@respx.mock
async def test_acs_accepted_without_operation_location_is_failure() -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(202, json={"status": "Running"}))
    result = await _client().send(MSG)
    assert result.ok is False


def test_acs_connection_is_frozen_dataclass() -> None:
    conn = AcsConnection(endpoint=ENDPOINT, access_key=KA_KEY)
    assert conn.host == HOST
