"""OTP retrieval (Mangrove Bay email OTP, effective 2026-07-15) — mailbox side.

Part 1 of the OTP feature: the `OtpSource` Protocol, the pure code extractor,
`FakeOtpSource`, and `ImapOtpSource` against a fake IMAP client. Adapter wiring
(how the ForeUP book flow requests/consumes the code) lands in a follow-up PR
once the live challenge shape is known.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from teetime.core.clock import FakeClock
from teetime.core.otp import (
    FakeOtpSource,
    ImapOtpSource,
    OtpError,
    OtpSource,
    OtpTimeoutError,
    extract_otp_code,
)

T0 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=UTC)


def _rfc822(
    *,
    sent_at: datetime,
    subject: str = "Mangrove Bay booking verification",
    body: str = "Your verification code is 654321.",
    sender: str = "noreply@foreupsoftware.com",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Date"] = sent_at
    msg.set_content(body)
    return bytes(msg)


class _FakeImapClient:
    """Scripted stand-in for imaplib.IMAP4_SSL — the collaborator, never the SUT.

    `mailboxes` maps mailbox name -> list of RFC822 byte blobs. `hide_for_searches`
    makes every mailbox appear empty for the first N search calls (drives the
    poll-until-appears test).
    """

    def __init__(
        self,
        mailboxes: dict[str, list[bytes]],
        *,
        hide_for_searches: int = 0,
    ) -> None:
        self.mailboxes = mailboxes
        self.hide_for_searches = hide_for_searches
        self.search_calls: list[tuple[str, tuple[str, ...]]] = []
        self.selected: str | None = None
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return ("OK", [b"Logged in"])

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        name = mailbox.strip('"')
        if name not in self.mailboxes:
            return ("NO", [b"nonexistent"])
        self.selected = name
        return ("OK", [str(len(self.mailboxes[name])).encode()])

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
        assert self.selected is not None
        self.search_calls.append((self.selected, criteria))
        if self.hide_for_searches > 0:
            self.hide_for_searches -= 1
            return ("OK", [b""])
        n = len(self.mailboxes[self.selected])
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(n))])

    def fetch(self, message_set: bytes | str, message_parts: str) -> tuple[str, list[object]]:
        assert self.selected is not None
        idx = int(message_set) - 1
        blob = self.mailboxes[self.selected][idx]
        return ("OK", [(f"{int(message_set)} (RFC822 {{{len(blob)}}}".encode(), blob), b")"])

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("BYE", [b"bye"])


def _source(
    client: _FakeImapClient,
    clock: FakeClock,
    *,
    sender_filter: str | None = "foreupsoftware.com",
    poll_interval_s: float = 2.0,
    mailboxes: tuple[str, ...] = ("INBOX",),
) -> ImapOtpSource:
    return ImapOtpSource(
        email_address="bot@example.com",
        app_password="app-pw",
        clock=clock,
        sender_filter=sender_filter,
        poll_interval_s=poll_interval_s,
        mailboxes=mailboxes,
        _imap_factory=lambda: client,
    )


# ---------------------------------------------------------------- structural


def test_imap_source_satisfies_protocol() -> None:
    clock = FakeClock(start=T0)
    src = _source(_FakeImapClient({"INBOX": []}), clock)
    assert isinstance(src, OtpSource)


def test_fake_source_satisfies_protocol() -> None:
    assert isinstance(FakeOtpSource(codes=["111111"]), OtpSource)


def test_timeout_error_is_otp_error() -> None:
    assert issubclass(OtpTimeoutError, OtpError)


# ------------------------------------------------------------- extract code


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Your verification code is 654321.", "654321"),
        ("Code: 000042 expires in five minutes", "000042"),
        ("654321", "654321"),
        ("no digits here", None),
        ("order #12345678 confirmed", None),  # 8-digit run is not a code
        ("short 12345 run", None),  # 5-digit run is not a code
        ("", None),
    ],
)
def test_extract_otp_code(text: str, expected: str | None) -> None:
    assert extract_otp_code(text) == expected


def test_extract_prefers_first_code() -> None:
    assert extract_otp_code("use 111111 not 222222") == "111111"


# ---------------------------------------------------------------- fake source


async def test_fake_source_returns_codes_in_order() -> None:
    fake = FakeOtpSource(codes=["111111", "222222"])
    assert await fake.fetch_code(sent_after=T0, timeout_s=5.0) == "111111"
    assert await fake.fetch_code(sent_after=T0, timeout_s=5.0) == "222222"


async def test_fake_source_raises_timeout_when_exhausted() -> None:
    fake = FakeOtpSource(codes=[])
    with pytest.raises(OtpTimeoutError):
        await fake.fetch_code(sent_after=T0, timeout_s=5.0)


def test_fake_source_records_calls() -> None:
    fake = FakeOtpSource(codes=["111111"])
    assert fake.calls == []


# ---------------------------------------------------------------- imap source


async def test_returns_code_from_fresh_message() -> None:
    clock = FakeClock(start=T0)
    client = _FakeImapClient({"INBOX": [_rfc822(sent_at=T0 - timedelta(seconds=10))]})
    src = _source(client, clock)
    code = await src.fetch_code(sent_after=T0 - timedelta(seconds=30), timeout_s=60.0)
    assert code == "654321"
    assert client.logged_out


async def test_stale_message_is_ignored_and_times_out() -> None:
    clock = FakeClock(start=T0)
    stale = _rfc822(sent_at=T0 - timedelta(minutes=30), body="old code 999999")
    client = _FakeImapClient({"INBOX": [stale]})
    src = _source(client, clock)
    with pytest.raises(OtpTimeoutError):
        await src.fetch_code(sent_after=T0 - timedelta(seconds=5), timeout_s=10.0)


async def test_newest_matching_message_wins() -> None:
    clock = FakeClock(start=T0)
    older = _rfc822(sent_at=T0 - timedelta(seconds=20), body="code 111111")
    newer = _rfc822(sent_at=T0 - timedelta(seconds=5), body="code 222222")
    client = _FakeImapClient({"INBOX": [older, newer]})
    src = _source(client, clock)
    code = await src.fetch_code(sent_after=T0 - timedelta(seconds=60), timeout_s=60.0)
    assert code == "222222"


async def test_polls_until_message_appears() -> None:
    clock = FakeClock(start=T0)
    client = _FakeImapClient(
        {"INBOX": [_rfc822(sent_at=T0 + timedelta(seconds=1))]},
        hide_for_searches=2,
    )
    src = _source(client, clock, poll_interval_s=2.0)
    code = await src.fetch_code(sent_after=T0 - timedelta(seconds=1), timeout_s=60.0)
    assert code == "654321"
    assert len(client.search_calls) == 3
    assert clock.now_utc() >= T0 + timedelta(seconds=4)  # two poll sleeps


async def test_times_out_when_no_message_ever_arrives() -> None:
    clock = FakeClock(start=T0)
    client = _FakeImapClient({"INBOX": []})
    src = _source(client, clock, poll_interval_s=2.0)
    with pytest.raises(OtpTimeoutError):
        await src.fetch_code(sent_after=T0, timeout_s=10.0)
    assert clock.now_utc() >= T0 + timedelta(seconds=10)


async def test_sender_filter_is_pushed_into_imap_search() -> None:
    clock = FakeClock(start=T0)
    client = _FakeImapClient({"INBOX": [_rfc822(sent_at=T0)]})
    src = _source(client, clock, sender_filter="foreupsoftware.com")
    await src.fetch_code(sent_after=T0 - timedelta(seconds=1), timeout_s=60.0)
    _, criteria = client.search_calls[0]
    assert any("foreupsoftware.com" in c for c in criteria)
    assert any("SINCE" in c for c in criteria)


async def test_no_sender_filter_searches_all_senders() -> None:
    clock = FakeClock(start=T0)
    client = _FakeImapClient({"INBOX": [_rfc822(sent_at=T0, sender="other@example.com")]})
    src = _source(client, clock, sender_filter=None)
    code = await src.fetch_code(sent_after=T0 - timedelta(seconds=1), timeout_s=60.0)
    assert code == "654321"
    _, criteria = client.search_calls[0]
    assert not any("FROM" in c for c in criteria)


async def test_checks_spam_mailbox_when_configured() -> None:
    clock = FakeClock(start=T0)
    client = _FakeImapClient(
        {"INBOX": [], "[Gmail]/Spam": [_rfc822(sent_at=T0)]},
    )
    src = _source(client, clock, mailboxes=("INBOX", "[Gmail]/Spam"))
    code = await src.fetch_code(sent_after=T0 - timedelta(seconds=1), timeout_s=60.0)
    assert code == "654321"


async def test_missing_mailbox_is_skipped_not_fatal() -> None:
    clock = FakeClock(start=T0)
    client = _FakeImapClient({"INBOX": [_rfc822(sent_at=T0)]})
    src = _source(client, clock, mailboxes=("[Gmail]/DoesNotExist", "INBOX"))
    code = await src.fetch_code(sent_after=T0 - timedelta(seconds=1), timeout_s=60.0)
    assert code == "654321"


async def test_message_without_code_is_ignored() -> None:
    clock = FakeClock(start=T0)
    codeless = _rfc822(sent_at=T0, body="Thanks for your reservation!")
    client = _FakeImapClient({"INBOX": [codeless]})
    src = _source(client, clock)
    with pytest.raises(OtpTimeoutError):
        await src.fetch_code(sent_after=T0 - timedelta(seconds=1), timeout_s=10.0)


async def test_naive_sent_after_is_rejected() -> None:
    clock = FakeClock(start=T0)
    src = _source(_FakeImapClient({"INBOX": []}), clock)
    with pytest.raises(ValueError, match="tz-aware"):
        await src.fetch_code(sent_after=datetime(2026, 7, 18, 10, 0, 0), timeout_s=5.0)


async def test_code_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:

    clock = FakeClock(start=T0)
    client = _FakeImapClient({"INBOX": [_rfc822(sent_at=T0)]})
    src = _source(client, clock)
    with caplog.at_level(logging.DEBUG):
        code = await src.fetch_code(sent_after=T0 - timedelta(seconds=1), timeout_s=60.0)
    assert code == "654321"
    assert "654321" not in caplog.text
