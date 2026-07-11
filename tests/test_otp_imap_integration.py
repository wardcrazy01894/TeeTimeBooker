"""Live ImapOtpSource round-trip against the real bot Gmail (integration-only).

Self-sends a simulated OTP mail over SMTP, then asserts ImapOtpSource pulls the
code back out of the live inbox. Requires the bot-mailbox env vars:

    OTP_EMAIL=<address> OTP_APP_PASSWORD=<app password> \
        uv run pytest -m integration tests/test_otp_imap_integration.py -v

Skipped by default and in CI (which runs `-m "not integration"`); also skipped
when the env vars are absent. A self-send lands in the sender's own INBOX as one
message (no cross-account dedupe), so this exercises search, freshness filtering,
and extraction end-to-end without touching the course or the forwarding filter.
"""

from __future__ import annotations

import os
import smtplib
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from teetime.core.clock import RealClock
from teetime.core.otp import ImapOtpSource

pytestmark = pytest.mark.integration

_ADDR = os.environ.get("OTP_EMAIL")
_PW = os.environ.get("OTP_APP_PASSWORD")


@pytest.mark.skipif(not (_ADDR and _PW), reason="OTP_EMAIL/OTP_APP_PASSWORD not set")
async def test_live_roundtrip_self_send() -> None:
    assert _ADDR is not None and _PW is not None
    code = f"{int(time.time()) % 1_000_000:06d}"
    sent_after = datetime.now(tz=UTC) - timedelta(seconds=2)

    msg = EmailMessage()
    msg["From"] = _ADDR
    msg["To"] = _ADDR
    msg["Subject"] = "TTB integration: booking verification"
    msg.set_content(f"Your verification code is {code}. It expires in five minutes.")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(_ADDR, _PW)
        smtp.send_message(msg)

    source = ImapOtpSource(
        email_address=_ADDR,
        app_password=_PW,
        clock=RealClock(),
        sender_filter=None,  # self-send; the prod sender filter is course-specific
    )
    fetched = await source.fetch_code(sent_after=sent_after, timeout_s=90.0)
    assert fetched == code
