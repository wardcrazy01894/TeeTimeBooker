"""MU-11: per-user notifications — buffering, rendering, operator summary (MULTIUSER_PLAN §8.7).

Nothing here does network I/O: ``BufferingNotifier`` is the in-race collector, the renderer is
pure, and delivery goes through an ``EmailSender`` (``FakeEmailSender`` in tests; the ACS client
is pinned separately in ``test_acs_email.py``).
"""

from __future__ import annotations

import ast
import inspect
import logging
import random
from dataclasses import replace
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
from teetime.tenant.golf_quips import GOLF_QUIPS, MISS_QUIPS
from teetime.tenant.models import RowId, User, UserId, UserRole, UserStatus
from teetime.tenant.notify import (
    OPERATOR_COPY_KINDS,
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
    deliver_operator_booking_notice,
    deliver_operator_summary,
    first_name,
    render_invitation,
    render_operator_booking_notice,
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
    assert str(USER_ID) not in email.body


def test_booked_email_hides_internal_ids_and_details() -> None:
    """The confirmation id is ours (and ForeUP's internal teetime id), not something the golfer
    ever sees at the course; the engine detail ("watcher", "") is jargon."""
    for kind in (UserEventKind.BOOKED, UserEventKind.UPGRADED):
        email = render_user_event(_event(kind, detail="watcher"), first_name="Turk")
        assert "TTB" not in email.body
        assert "123456" not in email.body
        assert "Confirmation" not in email.body
        assert "watcher" not in email.body
        assert "Details" not in email.body


def test_booked_email_lays_out_the_tee_time() -> None:
    email = render_user_event(
        _event(UserEventKind.BOOKED), first_name="Turk", course_label="Mangrove Bay"
    )
    assert "You're booked at Mangrove Bay on Sat Oct 10 at 8:12 AM." in email.body
    assert "Course:    Mangrove Bay" in email.body
    assert "Date:      Saturday, October 10" in email.body
    assert "Tee time:  8:12 AM" in email.body


def test_booked_email_signs_off_with_a_random_golf_quip() -> None:
    seen = set()
    for seed in range(40):
        email = render_user_event(
            _event(UserEventKind.BOOKED), first_name="Turk", rng=random.Random(seed)
        )
        lines = email.body.splitlines()
        assert lines[-1] == "— TeeTimeBooker"
        quip = lines[-3]
        assert quip in GOLF_QUIPS
        seen.add(quip)
    assert len(seen) > 10  # really picked at random, not a fixed line


def test_golf_quips_are_fifty_distinct_one_liners() -> None:
    assert len(GOLF_QUIPS) >= 50
    assert len(set(GOLF_QUIPS)) == len(GOLF_QUIPS)
    assert all(q.strip() == q and "\n" not in q and q for q in GOLF_QUIPS)
    assert any("screen door" in q for q in GOLF_QUIPS)


def test_cancelled_email_closes_with_come_back_soon_not_a_quip() -> None:
    email = render_user_event(
        _event(UserEventKind.CANCELLED, detail="cancelled from the site"),
        first_name="Turk",
        course_label="Mangrove Bay",
        rng=random.Random(0),
    )
    lines = email.body.splitlines()
    assert lines[-3:] == ["Hope to see you back on the course soon.", "", "— TeeTimeBooker"]
    assert not any(q in email.body for q in GOLF_QUIPS)
    assert "Details" not in email.body  # "as you asked" already says it
    assert "TTB" not in email.body


def test_only_a_booking_gets_a_quip() -> None:
    for kind in USER_FACING_KINDS - {UserEventKind.BOOKED, UserEventKind.UPGRADED}:
        body = render_user_event(_event(kind), first_name="Turk", rng=random.Random(0)).body
        assert not any(q in body for q in GOLF_QUIPS), kind


# --- the miss email (operator request 2026-10-02) ------------------------------------------

CUTOFF_LOCAL = datetime(2026, 10, 9, 16, 0, tzinfo=ZoneInfo("America/New_York"))


def _miss(detail: str = "no_inventory", **kw: object) -> UserEvent:
    base = {
        "window": (time(8, 45), time(10, 0)),
        "party_size": 4,
        "extra_options": 0,
        "cutoff_local": CUTOFF_LOCAL,
    }
    base.update(kw)
    return replace(_event(UserEventKind.MISSED_DROP, detail=detail), tee_time=None, **base)  # type: ignore[arg-type]


def test_miss_email_reads_like_a_person_wrote_it() -> None:
    """The 06:00 run could not book the release-day date. Say so plainly, say why it usually
    happens (an event blocks the sheet), promise only what the watcher does (tries until the
    cutoff), and set expectations: possible, unlikely. No engine words."""
    email = render_user_event(
        _miss(), first_name="Alex", course_label="Mangrove Bay", rng=random.Random(1)
    )
    assert email.subject == "[TeeTimeBooker] No tee time yet: Mangrove Bay Sat Oct 10"
    body = email.body
    assert body.startswith("Hi Alex,\n\n")
    assert (
        "Unfortunately we couldn't get you a tee time at Mangrove Bay for Saturday, October 10 "
        "when the tee sheet opened this morning." in body
    )
    assert "  Course:     Mangrove Bay" in body
    assert "  Date:       Saturday, October 10" in body
    assert "  You asked:  8:45 AM to 10:00 AM, 4 players" in body
    assert "blocked the morning for an event or an outing" in body
    assert "before 4 PM on Friday, October 9" in body
    assert "not something to count on" in body and "make other plans" in body
    assert "no_inventory" not in body and "Details" not in body
    lines = body.splitlines()
    assert lines[-1] == "— Spicy's helper"
    assert lines[-3] in MISS_QUIPS


def test_miss_email_quip_is_random_and_consoling() -> None:
    seen = {
        render_user_event(_miss(), first_name="Alex", rng=random.Random(s)).body.splitlines()[-3]
        for s in range(40)
    }
    assert seen <= set(MISS_QUIPS) and len(seen) > 6
    assert not set(MISS_QUIPS) & set(GOLF_QUIPS)  # a miss never gets a "shoot 'em low"


def test_miss_email_owns_a_service_error() -> None:
    """A CAPTCHA / booking-service failure is our fault, not the course's: say so, name no
    exception class, and still promise the watcher."""
    body = render_user_event(
        _miss("booking service error (CaptchaError)"), first_name="Alex", course_label="MB"
    ).body
    assert "This one was on our side" in body and "The operator has been notified" in body
    assert "event or an outing" not in body
    assert "CaptchaError" not in body
    assert "keep watching for a cancellation" in body


def test_miss_email_lists_the_best_option_and_counts_the_rest() -> None:
    body = render_user_event(_miss(extra_options=2, party_size=1), first_name="Alex").body
    assert "  You asked:  8:45 AM to 10:00 AM, 1 player (+2 more options)" in body
    body = render_user_event(_miss(extra_options=1), first_name="Alex").body
    assert "4 players (+1 more option)" in body


def test_miss_email_never_renders_the_engine_detail() -> None:
    """The detail is engine jargon and may carry a secret or a stray address (test_email_has_no_
    secret passes trivially for this kind now); the miss email must not render it at all."""
    leaky = f"no_inventory pw={PASSWORD} cc {OTHER_USER_EMAIL}"
    body = render_user_event(_miss(leaky), first_name="Alex").body
    assert PASSWORD not in body and OTHER_USER_EMAIL not in body and "no_inventory" not in body


def test_miss_email_without_row_facts_still_reads_well() -> None:
    body = render_user_event(
        _miss(window=None, party_size=None, cutoff_local=None), first_name="Alex"
    ).body
    assert "You asked" not in body
    assert "before the booking cutoff" in body


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


def test_operator_summary_labels_an_unconfirmed_booking() -> None:
    row = SummaryRow(
        row_id=ALEX_ROW,
        user_id=ALEX_ID,
        user_name="Alex Lancaster",
        course_id=REAL_MB,
        target_date=MON,
        party_size=4,
        windows=((time(8, 0), time(9, 0)),),
        attempts=(SummaryAttempt(_tee(8, 30), T0_ET, AttemptResult.UNCONFIRMED),),
    )
    email = render_operator_summary(_summary((row,), (_booked_alex(),)), exit_code=0, at=AT)
    assert "booked → unconfirmed (no id; the watcher adopts it)" in email.body


# --- invitation (operator request 2026-09-29; wording approved by the operator) -----------------


def test_render_invitation_is_the_approved_text() -> None:
    email = render_invitation("friend@example.com", site_url="https://spicyteetimebooker.com")
    assert email.subject == "You're invited to Spicy's Tee Time Booker!"
    assert email.body == (
        "Hi,\n"
        "\n"
        "Spicy Al has invited you to Spicy's Tee Time Booker. It books golf tee times for you "
        "the moment the course opens them, so you don't have to be up at 6 AM to grab a good "
        "slot.\n"
        "\n"
        "To get started:\n"
        "\n"
        "  1. Go to https://spicyteetimebooker.com\n"
        "  2. Sign in with Google using this email address (friend@example.com). The invite "
        "only works with this exact address.\n"
        "  3. Connect your course login, then pick the days and times you'd like to play.\n"
        "\n"
        "If you weren't expecting this, you can ignore this email.\n"
        "\n"
        "See you on the first tee!\n"
        "\n"
        "— Spicy's Tee Time Booker"
    )


def test_render_invitation_shows_the_address_unredacted() -> None:
    """The one email whose job is to show an address: the redaction filter would mask it."""
    body = render_invitation("Pal@Example.com", site_url="https://x.test").body
    assert "(Pal@Example.com)" in body and "redacted" not in body


async def test_a_sender_exception_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scan 2026-09-30: _safe_send kept the class name only, so a sender bug was undiagnosable."""

    class Exploding:
        async def send(self, message: EmailMessage) -> EmailSendResult:
            raise RuntimeError("boom")

    caplog.set_level(logging.WARNING, logger="teetime.tenant.notify")
    summary = RunSummary(events=(_event(UserEventKind.BOOKED),))
    await deliver_operator_summary(Exploding(), to=OPS, summary=summary, exit_code=0, at=AT)
    (rec,) = [r for r in caplog.records if "email sender raised" in r.getMessage()]
    assert rec.exc_info is not None and "RuntimeError" in rec.getMessage()


def test_a_too_early_rejection_reads_as_such_in_the_operator_summary() -> None:
    """2026-09-29/30: the -500 ms POST was refused before ForeUP's release and the email said
    "rejected: reason unknown". It names the cause now."""
    ms = timedelta(milliseconds=1)
    row = replace(
        _alex_row(),
        attempts=(
            SummaryAttempt(_tee(8, 30), T0_ET - 400 * ms, AttemptResult.REJECTED, "too_early"),
            SummaryAttempt(_tee(8, 37), T0_ET - 250 * ms, AttemptResult.KEPT),
        ),
    )
    body = render_operator_summary(_summary((row,), (_booked_alex(),)), exit_code=0, at=AT).body
    (line,) = [ln for ln in body.splitlines() if "8:30 AM" in ln and "sent 0.40 s early" in ln]
    assert "rejected: too early (before the booking window opened)" in line
    assert "reason unknown" not in body


# --- operator copy of every booking (operator request 2026-10-01) -------------------------------


def test_operator_booking_notice_names_who_what_and_when() -> None:
    """The operator wants to know EVERY time someone gets a tee time, not only at the 06:00 run:
    one short email per watcher booking, by display name, course, date and tee time, tagged with
    the environment so dev and prod never read alike."""
    event = _event(UserEventKind.BOOKED, detail="watcher")
    mail = render_operator_booking_notice(
        event, user_name="Andrew Golfer", course_label="Mangrove Bay", environment="prod"
    )
    assert (
        mail.subject
        == "[TeeTimeBooker · PROD] Booked: Andrew Golfer · Mangrove Bay Sat Oct 10 at 8:12 AM"
    )
    assert "Andrew Golfer" in mail.body
    assert "Mangrove Bay" in mail.body
    assert "Saturday, October 10" in mail.body
    assert "8:12 AM" in mail.body
    assert "watcher" in mail.body  # how it got booked (a check between drops)
    assert str(MB) not in mail.subject + mail.body


def test_operator_booking_notice_for_an_upgrade_says_so_and_tolerates_no_name() -> None:
    event = _event(UserEventKind.UPGRADED, detail="watcher")
    mail = render_operator_booking_notice(
        event, user_name=None, course_label="Mangrove Bay", environment=None
    )
    assert mail.subject.startswith("[TeeTimeBooker] Upgraded: user 11111111 ·")
    assert "moved to a better tee time" in mail.body


def test_operator_booking_notice_for_an_event_with_no_user_says_someone() -> None:
    mail = render_operator_booking_notice(
        _event(UserEventKind.BOOKED, user_id=None),
        user_name=None,
        course_label="MB",
        environment=None,
    )
    assert mail.subject.startswith("[TeeTimeBooker] Booked: Someone ·")


async def test_deliver_operator_booking_notice_never_raises() -> None:
    bad = _event(UserEventKind.CANCELLED)  # not a booking: the renderer refuses it
    result = await deliver_operator_booking_notice(
        FakeEmailSender(), to=OPS, event=bad, user_name=None, course_label=None, environment=None
    )
    assert result.ok is False and result.error == "ValueError"
    sender = FakeEmailSender()
    ok = await deliver_operator_booking_notice(
        sender,
        to=OPS,
        event=_event(UserEventKind.BOOKED),
        user_name="Turk",
        course_label="MB",
        environment="prod",
    )
    assert ok.ok and [m.to for m in sender.sent] == [OPS]


def test_operator_booking_notice_refuses_a_non_booking_kind() -> None:
    with pytest.raises(ValueError, match="booking"):
        render_operator_booking_notice(
            _event(UserEventKind.CANCELLED), user_name="x", course_label="MB", environment=None
        )


def test_operator_copy_kinds_are_exactly_the_bookings() -> None:
    assert frozenset({UserEventKind.BOOKED, UserEventKind.UPGRADED}) == OPERATOR_COPY_KINDS


# --- dry run: the operator hears from a dry-run environment only when it FAILS (2026-10-04) -----


async def test_dry_run_operator_summary_goes_out_only_on_a_systemic_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dev (permanent dry run) mailed its daily "Checked N requests" summary and, after
    2026-10-03, every user email re-addressed to the operator. The operator wants NO mail from
    dev unless something is actually broken (2026-10-04): a clean dry run (exit 0) sends nothing,
    however many rows or events it has; a non-zero exit still sends the summary (§4.5 SF6: that
    IS the failure channel). A live run is unchanged: a clean run with rows still reports."""
    dry = _row_event(UserEventKind.DRY_RUN, detail="dry run: nothing booked")
    summary = _summary((_alex_row(),), (dry,), environment="dev", dry_run=True)
    sender = FakeEmailSender(fail=True)  # nothing may even be attempted
    with caplog.at_level(logging.INFO):
        code = await deliver_operator_summary(sender, to=OPS, summary=summary, exit_code=0, at=AT)
    assert code == 0
    assert sender.sent == []
    assert "dry run" in caplog.text and "not sent" in caplog.text

    sender = FakeEmailSender()
    code = await deliver_operator_summary(sender, to=OPS, summary=summary, exit_code=2, at=AT)
    assert code == 2
    (failed,) = sender.sent
    assert "FAILED" in failed.subject and "dry run" in failed.subject

    live = _summary((_alex_row(),), (_booked_alex(),))
    assert await deliver_operator_summary(sender, to=OPS, summary=live, exit_code=0, at=AT) == 0
    assert len(sender.sent) == 2
