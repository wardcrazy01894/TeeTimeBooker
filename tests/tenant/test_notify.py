"""MU-11: per-user notifications — buffering, rendering, operator summary (MULTIUSER_PLAN §8.7).

Nothing here does network I/O: ``BufferingNotifier`` is the in-race collector, the renderer is
pure, and delivery goes through an ``EmailSender`` (``FakeEmailSender`` in tests; the ACS client
is pinned separately in ``test_acs_email.py``).
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import respx

from teetime.core.models import BookingOutcome, BookingResult, CourseId, RequestId
from teetime.core.redaction import register_secret_literals
from teetime.tenant import notify
from teetime.tenant.models import User, UserId, UserRole, UserStatus
from teetime.tenant.notify import (
    USER_FACING_KINDS,
    BufferingNotifier,
    EmailMessage,
    EmailSendResult,
    EmailUserNotifier,
    FakeEmailSender,
    UserEvent,
    UserEventKind,
    UserNotifier,
    deliver_operator_summary,
    first_name,
    render_operator_summary,
    render_user_event,
)

MB = CourseId("foreup:19671:2149")
AT = datetime(2026, 10, 3, 10, 0, 5, tzinfo=UTC)


def _result(outcome: BookingOutcome = BookingOutcome.BOOKED) -> BookingResult:
    return BookingResult(
        request_id=RequestId("req-1"),
        outcome=outcome,
        course_id=MB,
        slot=None,
        confirmation_code="TTB:123456",
        booked_at=AT,
        attempts=1,
    )


async def test_buffering_notifier_no_io() -> None:
    buf = BufferingNotifier()
    first, second = _result(), _result(BookingOutcome.NO_INVENTORY)
    # Any HTTP request under this router is an error (nothing is mocked).
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        await buf.notify(first)
        await buf.notify(second)
        assert router.calls.call_count == 0
    assert buf.results == (first, second)
    # flush() hands the buffered results over after the race, and empties the buffer.
    assert buf.flush() == (first, second)
    assert buf.results == ()
    assert buf.flush() == ()


def test_notify_module_imports_no_network_library() -> None:
    """Structural half of "no I/O": the module cannot reach the network at all."""
    tree = ast.parse(inspect.getsource(notify))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not imported & {"httpx", "socket", "smtplib", "urllib", "requests", "aiohttp"}


# --- rendering ---------------------------------------------------------------------------------

TARGET = date(2026, 10, 10)
TEE = datetime(2026, 10, 10, 8, 12, tzinfo=ZoneInfo("America/New_York"))
USER_ID = UserId(UUID("11111111-2222-3333-4444-555555555555"))
OTHER_USER_EMAIL = "other.golfer@example.com"
PASSWORD = "Hunter2-correct-horse-battery"
ACS_KEY = "c2VjcmV0LWFjcy1hY2Nlc3Mta2V5LWJhc2U2NA=="
OPS = "ops@example.com"


def _event(kind: UserEventKind, *, detail: str = "", user_id: UserId | None = USER_ID) -> UserEvent:
    return UserEvent(
        kind=kind,
        user_id=user_id,
        row_id=None,
        course_id=MB,
        target_date=TARGET,
        tee_time=TEE,
        confirmation="TTB:123456",
        detail=detail,
        at=AT,
    )


def _user(user_id: UserId = USER_ID, email: str = "turk@example.com") -> User:
    return User(
        id=user_id,
        oauth_provider="google",
        oauth_subject="sub-1",
        email=email,
        display_name="Turk Golfer",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


def test_render_booked_is_pii_minimal() -> None:
    email = render_user_event(
        _event(UserEventKind.BOOKED), first_name="Turk", course_label="Mangrove Bay"
    )
    assert "Mangrove Bay" in email.subject
    assert "Hi Turk," in email.body
    assert "Mangrove Bay" in email.body
    assert "Sat Oct 10" in email.body
    assert "8:12 AM" in email.body
    assert "TTB:123456" in email.body
    assert str(USER_ID) not in email.body


def test_render_falls_back_to_course_id_without_label() -> None:
    email = render_user_event(_event(UserEventKind.LOST), first_name="Turk")
    assert str(MB) in email.body


def test_first_name_takes_first_token() -> None:
    assert first_name("Turk Golfer") == "Turk"
    assert first_name("  ") == "there"


def test_render_refuses_operator_only_kinds() -> None:
    for kind in (UserEventKind.OPERATOR_SUMMARY, UserEventKind.NEEDS_RECONCILE):
        with pytest.raises(ValueError, match="operator"):
            render_user_event(_event(kind), first_name="Turk")


def test_email_has_no_secret() -> None:
    register_secret_literals([PASSWORD, ACS_KEY])
    leaky = f"login failed pw={PASSWORD} key={ACS_KEY} cc {OTHER_USER_EMAIL}"
    rendered = [
        render_user_event(_event(kind, detail=leaky), first_name="Turk", course_label="MB")
        for kind in USER_FACING_KINDS
    ]
    operator_kinds = [k for k in UserEventKind if k not in USER_FACING_KINDS]
    rendered.append(
        render_operator_summary(
            [_event(k, detail=leaky) for k in UserEventKind], exit_code=1, at=AT
        )
    )
    # every kind reaches some renderer: user-facing ones individually, the rest via the summary
    assert set(USER_FACING_KINDS) | set(operator_kinds) == set(UserEventKind)
    for email in rendered:
        text = email.subject + "\n" + email.body
        assert PASSWORD not in text
        assert ACS_KEY not in text
        assert OTHER_USER_EMAIL not in text


def test_user_facing_kinds_cover_brief_events() -> None:
    assert {
        UserEventKind.BOOKED,
        UserEventKind.LOST,
        UserEventKind.CANCELLED_EXTERNAL,
        UserEventKind.AUTH_FAILED,
    } <= USER_FACING_KINDS
    assert UserEventKind.NEEDS_RECONCILE not in USER_FACING_KINDS
    assert UserEventKind.OPERATOR_SUMMARY not in USER_FACING_KINDS


def test_operator_summary_lists_each_event() -> None:
    email = render_operator_summary(
        [_event(UserEventKind.BOOKED), _event(UserEventKind.NEEDS_RECONCILE)], exit_code=0, at=AT
    )
    assert "FAILED" not in email.subject
    assert "booked" in email.body
    assert "needs_reconcile" in email.body
    assert "11111111" in email.body  # short user ref for the operator
    assert "turk@example.com" not in email.body


# --- delivery ----------------------------------------------------------------------------------


async def test_operator_summary_on_nonzero() -> None:
    """§4.5/§8.7: a non-zero run ALWAYS emails the operator, even with no rows."""
    sender = FakeEmailSender()
    code = await deliver_operator_summary(sender, to=OPS, events=[], exit_code=2, at=AT)
    assert code == 2
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert msg.to == OPS
    assert "FAILED" in msg.subject
    assert "exit 2" in msg.body


async def test_operator_summary_skipped_when_idle_and_clean() -> None:
    sender = FakeEmailSender()
    code = await deliver_operator_summary(sender, to=OPS, events=[], exit_code=0, at=AT)
    assert code == 0
    assert sender.sent == []


async def test_operator_summary_send_failure_makes_exit_nonzero() -> None:
    """§4.5 SF6: a broken mail setup must not make every miss invisible."""
    events = [_event(UserEventKind.MISSED_DROP)]
    failing = FakeEmailSender(fail=True)
    code = await deliver_operator_summary(failing, to=OPS, events=events, exit_code=0, at=AT)
    assert code != 0
    # An already-non-zero code is kept (not overwritten).
    code = await deliver_operator_summary(failing, to=OPS, events=events, exit_code=3, at=AT)
    assert code == 3


async def test_operator_summary_sender_exception_is_a_failure_not_a_raise() -> None:
    class Exploding:
        async def send(self, message: EmailMessage) -> EmailSendResult:
            raise RuntimeError("boom")

    events = [_event(UserEventKind.BOOKED)]
    code = await deliver_operator_summary(Exploding(), to=OPS, events=events, exit_code=0, at=AT)
    assert code != 0


async def test_email_user_notifier_sends_to_bound_user() -> None:
    sender = FakeEmailSender()
    notifier = EmailUserNotifier(sender, user=_user(), course_labels={MB: "Mangrove Bay"})
    assert isinstance(notifier, UserNotifier)
    await notifier.send(_event(UserEventKind.BOOKED))
    assert [m.to for m in sender.sent] == ["turk@example.com"]
    assert "Mangrove Bay" in sender.sent[0].subject
    assert "Hi Turk," in sender.sent[0].body


async def test_email_user_notifier_refuses_other_users_event() -> None:
    sender = FakeEmailSender()
    notifier = EmailUserNotifier(sender, user=_user())
    other = UserId(UUID("99999999-2222-3333-4444-555555555555"))
    with pytest.raises(ValueError, match="another user"):
        await notifier.send(_event(UserEventKind.BOOKED, user_id=other))
    assert sender.sent == []


async def test_email_user_notifier_failure_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    notifier = EmailUserNotifier(FakeEmailSender(fail=True), user=_user())
    await notifier.send(_event(UserEventKind.BOOKED))  # must not raise
    assert "not delivered" in caplog.text
