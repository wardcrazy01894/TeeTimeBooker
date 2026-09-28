"""MU-11: per-user notifications — buffering, rendering, operator summary (MULTIUSER_PLAN §8.7).

Nothing here does network I/O: ``BufferingNotifier`` is the in-race collector, the renderer is
pure, and delivery goes through an ``EmailSender`` (``FakeEmailSender`` in tests; the ACS client
is pinned separately in ``test_acs_email.py``).
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import respx

from teetime.core.models import BookingOutcome, BookingResult, CourseId, RequestId
from teetime.core.redaction import register_secret_literals
from teetime.courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID
from teetime.notifications.notifier import Notifier
from teetime.tenant import notify
from teetime.tenant.models import RowId, User, UserId, UserRole, UserStatus
from teetime.tenant.notify import (
    USER_FACING_KINDS,
    AttemptResult,
    BufferingNotifier,
    EmailMessage,
    EmailSendResult,
    EmailUserNotifier,
    FakeEmailSender,
    RunSummary,
    SummaryAttempt,
    SummaryRow,
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
            RunSummary(events=tuple(_event(k, detail=leaky) for k in UserEventKind)),
            exit_code=1,
            at=AT,
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


# --- operator summary (one email per booking run) -----------------------------------------------

ET = ZoneInfo("America/New_York")
REAL_MB = MANGROVE_BAY_COURSE_ID
MON = date(2026, 10, 5)
T0_ET = datetime(2026, 9, 28, 6, 0, tzinfo=ET)
ALEX_ID = UserId(UUID("d16e0d4b-0000-4000-8000-000000000001"))
TURK_ID = UserId(UUID("7ae1c2d3-0000-4000-8000-000000000002"))
ALEX_ROW = RowId(UUID("00000000-0000-4000-8000-00000000000a"))
TURK_ROW = RowId(UUID("00000000-0000-4000-8000-00000000000b"))


def _tee(hour: int, minute: int, day: date = MON) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def _row_event(
    kind: UserEventKind,
    *,
    row_id: RowId = ALEX_ROW,
    user_id: UserId = ALEX_ID,
    tee: datetime | None = None,
    confirmation: str | None = None,
    detail: str = "",
) -> UserEvent:
    return UserEvent(
        kind=kind,
        user_id=user_id,
        row_id=row_id,
        course_id=REAL_MB,
        target_date=MON,
        tee_time=tee,
        confirmation=confirmation,
        detail=detail,
        at=AT,
    )


def _alex_row(*, name: str | None = "Alex Lancaster") -> SummaryRow:
    """This morning's run (2026-09-28): 3 staggered POSTs, two booked, one daily-limit."""
    ms = timedelta(milliseconds=1)
    return SummaryRow(
        row_id=ALEX_ROW,
        user_id=ALEX_ID,
        user_name=name,
        course_id=REAL_MB,
        target_date=MON,
        party_size=4,
        windows=((time(8, 0), time(9, 0)),),
        attempts=(
            SummaryAttempt(_tee(8, 30), T0_ET - 498 * ms, AttemptResult.KEPT),
            SummaryAttempt(_tee(8, 37), T0_ET - 250 * ms, AttemptResult.CANCELLED_EXTRA),
            SummaryAttempt(_tee(8, 22), T0_ET + 1 * ms, AttemptResult.REJECTED, "daily_limit"),
        ),
    )


def _turk_row() -> SummaryRow:
    return SummaryRow(
        row_id=TURK_ROW,
        user_id=TURK_ID,
        user_name="Turk Golfer",
        course_id=REAL_MB,
        target_date=MON,
        party_size=2,
        windows=((time(7, 0), time(8, 0)), (time(11, 30), time(12, 30))),
        attempts=(
            SummaryAttempt(
                _tee(7, 30),
                T0_ET - timedelta(milliseconds=500),
                AttemptResult.REJECTED,
                "unavailable",
            ),
        ),
    )


def _booked_alex() -> UserEvent:
    return _row_event(
        UserEventKind.BOOKED, tee=_tee(8, 30), confirmation="TTB:TTID_0928060000ggnxe"
    )


def _summary(
    rows: tuple[SummaryRow, ...], events: tuple[UserEvent, ...], **kw: object
) -> RunSummary:
    base: dict[str, object] = {
        "rows": rows,
        "events": events,
        "environment": "prod",
        "dry_run": False,
        "release_at": T0_ET,
        "rows_loaded": len(rows),
        "rows_claimed": len(rows),
        "captcha_demanded": 5,
        "captcha_solved": 3,
    }
    base.update(kw)
    return RunSummary(**base)  # type: ignore[arg-type]


def test_operator_summary_clean_booking_reads_like_a_report() -> None:
    email = render_operator_summary(_summary((_alex_row(),), (_booked_alex(),)), exit_code=0, at=AT)
    assert email.subject == "[TeeTimeBooker · PROD] Booked 1 of 1 · Mangrove Bay · Mon Oct 5"
    body = email.body
    assert body.startswith("Booking run: Mon Sep 28, 6:00 AM EDT (prod, live) · OK · exit 0")
    assert "PROBLEMS" not in body and "⚠" not in body
    assert "BOOKED (1)" in body
    assert "Alex Lancaster: Mangrove Bay, Mon Oct 5 at 8:30 AM, 4 players" in body
    assert "Window 8:00-9:00 AM · confirmation TTID_0928060000ggnxe" in body
    assert "Attempts (3 sent around 6:00:00 AM):" in body
    lines = body.splitlines()
    assert any(
        "8:30 AM" in ln and "sent 0.50 s early" in ln and "booked → kept" in ln for ln in lines
    )
    assert any(
        "8:37 AM" in ln and "sent 0.25 s early" in ln and "booked → cancelled (extra)" in ln
        for ln in lines
    )
    assert any(
        "8:22 AM" in ln and "sent on time" in ln and "rejected: one-per-day limit" in ln
        for ln in lines
    )
    assert "1 request loaded / 1 claimed" in body
    assert "CAPTCHAs: 3 of 5 solved in the first wave" in body
    assert "user emails: 1" in body
    assert "foreup:" not in body + email.subject  # never a raw course id
    assert str(ALEX_ID) not in body


def test_operator_summary_puts_problems_first_and_in_the_subject() -> None:
    missed = _row_event(
        UserEventKind.MISSED_DROP, row_id=TURK_ROW, user_id=TURK_ID, detail="no_inventory"
    )
    email = render_operator_summary(
        _summary((_alex_row(), _turk_row()), (_booked_alex(), missed)), exit_code=0, at=AT
    )
    assert email.subject == (
        "[TeeTimeBooker · PROD] ⚠ 1 problem · Booked 1 of 2 · Mangrove Bay · Mon Oct 5"
    )
    body = email.body
    assert "· ⚠ 1 PROBLEM ·" in body.splitlines()[0]
    problems_at, booked_at = body.index("⚠ PROBLEMS (1)"), body.index("BOOKED (1)")
    assert problems_at < booked_at
    problem_block = body[problems_at:booked_at]
    assert "Turk Golfer: Mangrove Bay, Mon Oct 5, 2 players" in problem_block
    assert "missed the drop (no_inventory)" in problem_block
    assert "Windows 7:00-8:00 AM, 11:30 AM-12:30 PM" in problem_block
    assert "rejected: time not available" in problem_block
    assert "Alex Lancaster" not in problem_block


def test_operator_summary_failed_exit_is_loud_and_run_lines_come_first() -> None:
    systemic = UserEvent(
        kind=UserEventKind.OPERATOR_SUMMARY,
        user_id=None,
        row_id=None,
        course_id=None,
        target_date=None,
        tee_time=None,
        confirmation=None,
        detail="systemic: prepare: RuntimeError",
        at=AT,
    )
    email = render_operator_summary(_summary((), (systemic,)), exit_code=1, at=AT)
    assert email.subject.startswith("[TeeTimeBooker · PROD] ❌ RUN FAILED (exit 1) · ")
    assert "1 problem" in email.subject
    first = email.body.splitlines()[0]
    assert "FAILED" in first and "exit 1" in first
    assert "Run: systemic: prepare: RuntimeError" in email.body


def test_operator_summary_uncertain_and_held_extra_are_problems() -> None:
    held = _row_event(
        UserEventKind.OPERATOR_SUMMARY, detail="1 surplus reservation(s) still held (held_extra)"
    )
    uncertain = _row_event(UserEventKind.NEEDS_RECONCILE, detail="uncertain (ReadTimeout)")
    email = render_operator_summary(
        _summary((_alex_row(),), (_booked_alex(), held, uncertain)), exit_code=1, at=AT
    )
    assert "⚠ 2 problems" not in email.subject  # one ROW with problems is one problem
    assert "1 problem" in email.subject
    assert "BOOKED (" not in email.body  # a booked row WITH a problem is listed once, up top
    block = email.body[email.body.index("PROBLEMS") : email.body.index("\nRun: ")]
    assert "Alex Lancaster: Mangrove Bay, Mon Oct 5 at 8:30 AM, 4 players" in block
    assert "outcome UNCERTAIN (uncertain (ReadTimeout)); the watcher reconciles it" in block
    assert "1 surplus reservation(s) still held (held_extra)" in block


def test_operator_summary_dry_run_is_labelled_and_not_a_problem() -> None:
    dry = _row_event(UserEventKind.DRY_RUN, detail="dry run: nothing booked")
    email = render_operator_summary(
        _summary((_alex_row(),), (dry,), environment="dev", dry_run=True), exit_code=0, at=AT
    )
    assert email.subject == (
        "[TeeTimeBooker · DEV · dry run] Checked 1 request · Mangrove Bay · Mon Oct 5"
    )
    assert "(dev, dry run)" in email.body.splitlines()[0]
    assert "⚠" not in email.subject + email.body
    assert "DRY RUN (1)" in email.body


def test_operator_summary_falls_back_to_a_short_user_id_without_a_name() -> None:
    email = render_operator_summary(
        _summary((_alex_row(name=None),), (_booked_alex(),), environment=None),
        exit_code=0,
        at=AT,
    )
    assert "user d16e0d4b: Mangrove Bay" in email.body
    assert email.subject.startswith("[TeeTimeBooker] Booked 1 of 1")


# --- delivery ----------------------------------------------------------------------------------


async def test_operator_summary_on_nonzero() -> None:
    """§4.5/§8.7: a non-zero run ALWAYS emails the operator, even with no rows."""
    sender = FakeEmailSender()
    code = await deliver_operator_summary(sender, to=OPS, summary=RunSummary(), exit_code=2, at=AT)
    assert code == 2
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert msg.to == OPS
    assert "FAILED" in msg.subject
    assert "exit 2" in msg.body


async def test_operator_summary_skipped_when_idle_and_clean() -> None:
    sender = FakeEmailSender()
    code = await deliver_operator_summary(sender, to=OPS, summary=RunSummary(), exit_code=0, at=AT)
    assert code == 0
    assert sender.sent == []


async def test_operator_summary_send_failure_makes_exit_nonzero() -> None:
    """§4.5 SF6: a broken mail setup must not make every miss invisible."""
    summary = RunSummary(events=(_event(UserEventKind.MISSED_DROP),))
    failing = FakeEmailSender(fail=True)
    code = await deliver_operator_summary(failing, to=OPS, summary=summary, exit_code=0, at=AT)
    assert code != 0
    # An already-non-zero code is kept (not overwritten).
    code = await deliver_operator_summary(failing, to=OPS, summary=summary, exit_code=3, at=AT)
    assert code == 3


async def test_operator_summary_sender_exception_is_a_failure_not_a_raise() -> None:
    class Exploding:
        async def send(self, message: EmailMessage) -> EmailSendResult:
            raise RuntimeError("boom")

    summary = RunSummary(events=(_event(UserEventKind.BOOKED),))
    code = await deliver_operator_summary(Exploding(), to=OPS, summary=summary, exit_code=0, at=AT)
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


def test_buffering_notifier_satisfies_engine_notifier_protocol() -> None:
    """The per-account engine notifier during the race: it must structurally satisfy the
    engine's runtime_checkable ``Notifier`` Protocol so MU-9a can inject it unchanged."""
    assert isinstance(BufferingNotifier(), Notifier)
