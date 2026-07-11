"""Email OTP retrieval for the booking flow (Mangrove Bay, effective 2026-07-15).

ForeUP emails a six-digit code when a tee time is selected; the booking must be
completed within five minutes by presenting that code. This module owns ONLY the
mailbox side: the `OtpSource` Protocol, the production `ImapOtpSource` (polls the
dedicated Gmail inbox the course's OTP mail is forwarded to), and `FakeOtpSource`
for tests — mirroring the Protocol + real + fake layout of `clock.py`. How the
ForeUP adapter requests and submits the code is wired separately once the live
challenge shape is known.

Security: the code value is a live credential for ~5 minutes and is NEVER logged;
logs carry poll counts and timings only.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import imaplib
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from .clock import Clock

_log = logging.getLogger(__name__)


class OtpError(Exception):
    """Base for OTP retrieval failures."""


class OtpTimeoutError(OtpError):
    """No usable code arrived within the caller's window."""


@runtime_checkable
class OtpSource(Protocol):
    """Fetch the one-time code the course emails during a booking attempt."""

    async def fetch_code(self, *, sent_after: datetime, timeout_s: float) -> str:
        """Newest code from a message received STRICTLY after `sent_after` (tz-aware UTC;
        pass `clock.now_utc()` readings — a non-UTC tz shifts the coarse SINCE day boundary).

        Polls until found or `timeout_s` elapses, then raises OtpTimeoutError.
        `sent_after` scopes the search to THIS attempt's code — a stale code from
        an earlier attempt (already consumed or expired) must never be returned.
        """
        ...


# Standalone six-digit run: longer/shorter digit runs (order numbers, ZIP+4,
# timestamps) never match. Format pinned by the MB announcement; revisit after
# the first live challenge is observed.
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# IMAP SINCE takes dd-Mon-yyyy with ENGLISH month abbreviations; strftime's %b
# is locale-sensitive (fr_FR → "juil."), so the table is hardcoded.
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_date(dt: datetime) -> str:
    return f"{dt.day:02d}-{_IMAP_MONTHS[dt.month - 1]}-{dt.year}"


def extract_otp_code(text: str) -> str | None:
    """First standalone six-digit run in `text`, or None."""
    m = _CODE_RE.search(text)
    return m.group(1) if m else None


class FakeOtpSource:
    """Deterministic OtpSource for tests: queued codes in order, timeout when dry.

    Records each `(sent_after, timeout_s)` in `calls` so tests can assert the
    caller scoped the fetch to its own attempt.
    """

    def __init__(self, *, codes: list[str] | None = None) -> None:
        self._codes = list(codes or [])
        self.calls: list[tuple[datetime, float]] = []

    async def fetch_code(self, *, sent_after: datetime, timeout_s: float) -> str:
        self.calls.append((sent_after, timeout_s))
        if not self._codes:
            raise OtpTimeoutError("FakeOtpSource exhausted")
        return self._codes.pop(0)


class _ImapClient(Protocol):
    """Structural slice of imaplib.IMAP4_SSL used here (tests inject a fake)."""

    def login(self, user: str, password: str) -> tuple[str, list[Any]]: ...

    def select(self, mailbox: str, readonly: bool = ...) -> tuple[str, list[Any]]: ...

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[Any]]: ...

    def fetch(self, message_set: str, message_parts: str) -> tuple[str, list[Any]]: ...

    def logout(self) -> tuple[str, list[Any]]: ...


class ImapOtpSource:
    """Polls a mailbox over IMAP for the OTP email and extracts the code.

    Blocking imaplib calls run via `asyncio.to_thread`; the wait cadence uses the
    injected Clock so tests are instant. Each poll is a FRESH connection —
    login → search each mailbox → logout — because a poll window is short and a
    persistent connection would add reconnect handling for no gain. Spam is
    checked by default: a first-ever sender + forwarded mail is exactly the
    profile spam filters mangle, and a missed code costs the booking.

    Resilience contract: a transient failure on ANY single poll (connect, TLS,
    login, a dropped socket) consumes that poll and the loop keeps going until
    `timeout_s` — one blip must not abort the OTP window. The socket itself is
    bounded by `connect_timeout_s` because a hung connect runs in a thread that
    `fetch_code`'s deadline cannot interrupt. Known risk (accepted, watch after
    the 07-15 cutover): fresh LOGINs every `poll_interval_s` could trip Gmail
    rate limiting on a long window; expected real windows are short (mail
    forwards in ~10 s), and a throttled poll is retried like any other blip.
    Corollary: a PERMANENTLY wrong app password also retries every poll for the
    full window before surfacing as OtpTimeoutError — a misconfigured deployment
    compounds the login-rate exposure rather than failing fast (accepted; the
    per-poll warnings are the diagnosis trail).

    Freshness: messages must be dated after `sent_after` MINUS
    `freshness_grace_s`. The grace absorbs mail-server clock skew (their Date
    header vs our clock) — without it a live code from a slightly-behind server
    would be silently rejected as stale, the fatal direction at the 06:00 drop.
    A prior attempt's code is minutes old (expired/consumed), so a small grace
    does not reopen the stale-code hole.
    """

    def __init__(
        self,
        *,
        email_address: str,
        app_password: str,
        clock: Clock,
        host: str = "imap.gmail.com",
        port: int = 993,
        sender_filter: str | None = "foreupsoftware.com",
        poll_interval_s: float = 2.0,
        connect_timeout_s: float = 15.0,
        freshness_grace_s: float = 60.0,
        mailboxes: tuple[str, ...] = ("INBOX", "[Gmail]/Spam"),
        _imap_factory: Callable[[], _ImapClient] | None = None,
    ) -> None:
        self._email = email_address
        self._password = app_password
        self._clock = clock
        self._sender_filter = sender_filter
        self._poll_interval_s = poll_interval_s
        self._freshness_grace_s = freshness_grace_s
        self._mailboxes = mailboxes
        self._imap_factory: Callable[[], _ImapClient] = _imap_factory or (
            lambda: imaplib.IMAP4_SSL(host, port, timeout=connect_timeout_s)
        )

    async def fetch_code(self, *, sent_after: datetime, timeout_s: float) -> str:
        if sent_after.tzinfo is None:
            raise ValueError("sent_after must be tz-aware")
        deadline = self._clock.now_utc() + timedelta(seconds=timeout_s)
        newer_than = sent_after - timedelta(seconds=self._freshness_grace_s)
        polls = 0
        while True:
            polls += 1
            try:
                code = await asyncio.to_thread(self._poll_once, newer_than)
            except (imaplib.IMAP4.error, OSError) as exc:
                # One blip consumes one poll, never the whole OTP window. A
                # PERSISTENT failure (bad app password, network down) surfaces
                # as OtpTimeoutError with these warnings as the diagnosis trail.
                code = None
                _log.warning("otp: poll %d failed transiently (%s); retrying", polls, exc)
            if code is not None:
                _log.info("otp: code found on poll %d", polls)
                return code
            if self._clock.now_utc() >= deadline:
                raise OtpTimeoutError(
                    f"no OTP mail newer than {sent_after.isoformat()} "
                    f"after {timeout_s:.0f}s ({polls} poll(s))"
                )
            await self._clock.sleep(self._poll_interval_s)

    # ------------------------------------------------------------ sync leg

    def _poll_once(self, newer_than: datetime) -> str | None:
        """One connect-search-extract pass. Returns the newest fresh code, or None."""
        client = self._imap_factory()
        try:
            client.login(self._email, self._password)
            best: tuple[datetime, str] | None = None
            for mailbox in self._mailboxes:
                typ, _ = client.select(f'"{mailbox}"', readonly=True)
                if typ != "OK":
                    _log.debug("otp: mailbox %s not selectable, skipping", mailbox)
                    continue
                for msg_date, text in self._fresh_messages(client, newer_than):
                    code = extract_otp_code(text)
                    if code is not None and (best is None or msg_date > best[0]):
                        best = (msg_date, code)
            return best[1] if best else None
        finally:
            try:
                client.logout()
            except Exception:
                _log.debug("otp: imap logout failed", exc_info=True)

    def _fresh_messages(
        self, client: _ImapClient, newer_than: datetime
    ) -> list[tuple[datetime, str]]:
        """(date, searchable-text) for messages in the SELECTED mailbox newer than `newer_than`
        (already grace-adjusted by the caller)."""
        criteria = [f"SINCE {_imap_date(newer_than)}"]  # day granularity
        if self._sender_filter:
            criteria.append(f'FROM "{self._sender_filter}"')
        typ, data = client.search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        out: list[tuple[datetime, str]] = []
        for mid in data[0].split():
            parsed = self._parse_message(client, mid.decode())
            # Date-header freshness is the precise filter; SINCE only prunes by day.
            if parsed is not None and parsed[0] > newer_than:
                out.append(parsed)
        return out

    def _parse_message(self, client: _ImapClient, mid: str) -> tuple[datetime, str] | None:
        typ, data = client.fetch(mid, "(RFC822)")
        if typ != "OK":
            return None
        raw = next(
            (
                part[-1]
                for part in data
                if isinstance(part, tuple) and isinstance(part[-1], bytes | bytearray)
            ),
            None,
        )
        if raw is None:
            return None
        # policy=default makes message_from_bytes yield EmailMessage (typeshed
        # overload infers this — no cast needed).
        msg = email.message_from_bytes(bytes(raw), policy=email.policy.default)
        date_header = msg["Date"]
        if not date_header:
            return None
        try:
            msg_date = email.utils.parsedate_to_datetime(str(date_header))
        except Exception:
            return None  # unparseable Date header — cannot establish freshness
        if msg_date.tzinfo is None:
            return None  # can't establish freshness — treat as stale
        return (msg_date, self._searchable_text(msg))

    @staticmethod
    def _searchable_text(msg: email.message.EmailMessage) -> str:
        """Subject + body text. HTML bodies are tag-stripped — the OTP mail's
        format is unknown until first observed, and an HTML-only mail must not
        hide the code."""
        parts = [str(msg["Subject"] or "")]
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is not None:
            try:
                content = str(body.get_content())
            except Exception:
                content = ""
            if body.get_content_type() == "text/html":
                content = re.sub(r"<[^>]+>", " ", content)
            parts.append(content)
        return "\n".join(parts)
