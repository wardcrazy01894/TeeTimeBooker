"""Per-user notifications (MULTIUSER_PLAN §8.7).

The engine ``notifications.notifier.Notifier`` Protocol is UNCHANGED: the recipient is bound at
construction. In the booking runner each account's ``Orchestrator`` gets a ``BufferingNotifier``
(collect only, NO I/O, so nothing is sent during the race). After WRITE #2, the runner maps
results to ``UserEvent``s and sends them through a ``UserNotifier``. ``UserEvent`` (not
``BookingResult``) is the tenant contract because ``lost`` and ``cancelled(external)`` come from
the watcher, not from an engine terminal.

Backend: Azure Communication Services Email over REST (``tenant.acs_email``, HMAC-signed via
httpx, no SDK), with an Azure-managed sender domain (MU-11). This module is backend-neutral and
does NO network I/O itself: it renders plain-text mail and hands it to an ``EmailSender``.

Content is PII-minimal: the recipient's first name, the course, the date, the tee time, the
``TTB:`` confirmation and a reason. Never credentials, never another user's data. Every rendered
subject and body passes through ``core.redaction.redact_text`` as defence in depth, so a
registered secret literal (E7: keyring keys, decrypted passwords, the ACS key) or a stray email
address in a free-text ``detail`` is masked before it can reach a mailbox.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..core.models import BookingResult, CourseId
from ..core.redaction import redact_text
from .models import RowId, User, UserId

log = logging.getLogger(__name__)


class UserEventKind(StrEnum):
    BOOKED = "booked"
    UPGRADED = "upgraded"
    MISSED_DROP = "missed_drop"  # stays PENDING; the watcher keeps trying until cutoff
    LOST = "lost"  # frozen without a booking
    CANCELLED = "cancelled"  # by the user via the site
    CANCELLED_EXTERNAL = "cancelled_external"  # vanished from 2 trusted snapshots (§7.5)
    AUTH_FAILED = "auth_failed"
    NEEDS_RECONCILE = "needs_reconcile"  # UNCERTAIN outcome (§4.5) — operator only
    OPERATOR_SUMMARY = "operator_summary"


# Kinds that email the USER (§4.5). NEEDS_RECONCILE and OPERATOR_SUMMARY go to the operator only
# (as lines of the operator summary), so the user renderer refuses them.
USER_FACING_KINDS: frozenset[UserEventKind] = frozenset(
    {
        UserEventKind.BOOKED,
        UserEventKind.UPGRADED,
        UserEventKind.MISSED_DROP,
        UserEventKind.LOST,
        UserEventKind.CANCELLED,
        UserEventKind.CANCELLED_EXTERNAL,
        UserEventKind.AUTH_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class UserEvent:
    kind: UserEventKind
    user_id: UserId | None  # None for OPERATOR_SUMMARY (sent to OPERATOR-NOTIFY-EMAIL)
    row_id: RowId | None
    course_id: CourseId | None
    target_date: date | None
    tee_time: datetime | None
    confirmation: str | None
    detail: str
    at: datetime


@runtime_checkable
class UserNotifier(Protocol):
    """Deliver one ``UserEvent``. Failures are logged by the caller and never mask an outcome
    (the same rule as the engine notifier)."""

    async def send(self, event: UserEvent) -> None: ...


class BufferingNotifier:
    """Engine-``Notifier``-shaped collector: ``notify`` appends to ``results`` and does no I/O.

    One per account's ``Orchestrator`` in the booking runner, so nothing is sent near T0.
    After WRITE #2 the runner ``flush()``es it and maps each result to a ``UserEvent``."""

    def __init__(self) -> None:
        self._results: list[BookingResult] = []

    async def notify(self, result: BookingResult) -> None:
        self._results.append(result)

    @property
    def results(self) -> tuple[BookingResult, ...]:
        return tuple(self._results)

    def flush(self) -> tuple[BookingResult, ...]:
        """Hand over every buffered result (in arrival order) and empty the buffer."""
        drained = tuple(self._results)
        self._results.clear()
        return drained


# --- email transport contract (backend-neutral) --------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    body: str  # plain text


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    """Outcome of one send. A sender RETURNS this rather than raising, so a mail failure can
    never mask a booking outcome; the caller decides what a failure means (§4.5)."""

    ok: bool
    status: str  # backend status, e.g. "Succeeded" / "Failed" / "HTTP 401" / "timeout"
    operation_id: str | None = None
    error: str | None = None


@runtime_checkable
class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> EmailSendResult: ...


class FakeEmailSender:
    """In-memory ``EmailSender`` for tests: records every message; ``fail=True`` makes every
    send return a failed result (it never raises)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> EmailSendResult:
        self.sent.append(message)
        if self.fail:
            return EmailSendResult(ok=False, status="Failed", error="fake failure")
        return EmailSendResult(ok=True, status="Succeeded", operation_id=f"fake-{len(self.sent)}")


# --- rendering (pure) ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    body: str


def first_name(display_name: str) -> str:
    """The first whitespace token of a display name; "there" when blank (``Hi there,``)."""
    parts = display_name.split()
    return parts[0] if parts else "there"


_USER_TEMPLATES: Mapping[UserEventKind, tuple[str, str]] = {
    UserEventKind.BOOKED: ("Booked: {course} {when}", "You're booked at {course} on {when}."),
    UserEventKind.UPGRADED: (
        "Upgraded: {course} {when}",
        "We moved your booking at {course} to a better tee time: {when}.",
    ),
    UserEventKind.MISSED_DROP: (
        "Missed the drop: {course} {day}",
        "We didn't get a tee time at {course} for {day} when the tee sheet opened. "
        "We're watching for cancellations and will book one if it opens before the cutoff.",
    ),
    UserEventKind.LOST: (
        "No tee time: {course} {day}",
        "We couldn't find a tee time at {course} for {day} before the booking cutoff.",
    ),
    UserEventKind.CANCELLED: (
        "Cancelled: {course} {day}",
        "Your tee time at {course} on {when} was cancelled as you asked.",
    ),
    UserEventKind.CANCELLED_EXTERNAL: (
        "Cancelled at the course: {course} {day}",
        "Your tee time at {course} on {when} is no longer on your course account, so it was "
        "cancelled outside this site. We won't re-book it; re-request the date if you still "
        "want to play.",
    ),
    UserEventKind.AUTH_FAILED: (
        "Action needed: sign-in failed at {course}",
        "We couldn't sign in to your {course} account. We've paused booking for it until you "
        "re-enter your course password on the site.",
    ),
}


def _day(d: date | None) -> str:
    return f"{d:%a %b} {d.day}" if d is not None else "an upcoming date"


def _time(t: datetime) -> str:
    return f"{t:%I:%M %p}".lstrip("0")


def _when(event: UserEvent) -> str:
    day = _day(event.tee_time.date() if event.tee_time else event.target_date)
    return f"{day} at {_time(event.tee_time)}" if event.tee_time else day


def _course(event: UserEvent, course_label: str | None) -> str:
    if course_label:
        return course_label
    return str(event.course_id) if event.course_id is not None else "your course"


def _redacted(subject: str, body: str) -> RenderedEmail:
    return RenderedEmail(subject=redact_text(subject), body=redact_text(body))


def render_user_event(
    event: UserEvent, *, first_name: str, course_label: str | None = None
) -> RenderedEmail:
    """Plain-text subject/body for one user-facing event. Operator-only kinds raise."""
    if event.kind not in USER_FACING_KINDS:
        raise ValueError(f"{event.kind} is operator-only; render it via render_operator_summary")
    subject_t, lead_t = _USER_TEMPLATES[event.kind]
    fields = {"course": _course(event, course_label), "when": _when(event)}
    fields["day"] = _day(event.target_date)
    lines = [f"Hi {first_name},", "", lead_t.format(**fields)]
    if event.confirmation and event.kind in {UserEventKind.BOOKED, UserEventKind.UPGRADED}:
        lines.append(f"Confirmation: {event.confirmation}")
    if event.detail:
        lines += ["", f"Details: {event.detail}"]
    lines += ["", "— TeeTimeBooker"]
    return _redacted(f"[TeeTimeBooker] {subject_t.format(**fields)}", "\n".join(lines))


def _summary_line(event: UserEvent) -> str:
    user = str(event.user_id)[:8] if event.user_id is not None else "-"
    when = _when(event) if (event.tee_time or event.target_date) else "-"
    parts = [event.kind.value, f"user={user}", f"course={event.course_id or '-'}", when]
    if event.confirmation:
        parts.append(event.confirmation)
    if event.detail:
        parts.append(event.detail)
    return "- " + " | ".join(parts)


def render_operator_summary(
    events: Sequence[UserEvent], *, exit_code: int, at: datetime
) -> RenderedEmail:
    """One email per booking-runner execution: exit status + one line per event. Users are
    referenced by a short id prefix (never their email)."""
    status = "OK" if exit_code == 0 else "FAILED"
    stamp = f"{at:%Y-%m-%d %H:%M %Z}".strip()
    lines = [f"Run at {stamp}: exit {exit_code} ({status}).", f"{len(events)} event(s):"]
    lines += [_summary_line(e) for e in events] or ["- none"]
    return _redacted(f"[TeeTimeBooker] run {status} (exit {exit_code})", "\n".join(lines))


# --- delivery ----------------------------------------------------------------------------------

# Exit code when the run itself was clean but the operator summary could not be delivered (§4.5).
EXIT_OPERATOR_NOTIFY_FAILED = 1


async def _safe_send(sender: EmailSender, message: EmailMessage) -> EmailSendResult:
    try:
        return await sender.send(message)
    except Exception as exc:  # a sender bug must never mask a booking outcome
        return EmailSendResult(ok=False, status="error", error=type(exc).__name__)


async def deliver_operator_summary(
    sender: EmailSender,
    *,
    to: str,
    events: Sequence[UserEvent],
    exit_code: int,
    at: datetime,
) -> int:
    """Send the operator summary when there was anything to report (events, or a non-zero
    exit) and return the run's FINAL exit code: a failed send turns a clean exit into
    ``EXIT_OPERATOR_NOTIFY_FAILED`` (§4.5 SF6 — otherwise a broken ACS setup would make every
    miss invisible, since misses exit 0). An already non-zero code is kept."""
    if not events and exit_code == 0:
        return 0
    rendered = render_operator_summary(events, exit_code=exit_code, at=at)
    result = await _safe_send(
        sender, EmailMessage(to=to, subject=rendered.subject, body=rendered.body)
    )
    if result.ok:
        return exit_code
    log.error(
        "operator summary NOT delivered (status=%s error=%s); exit forced non-zero",
        result.status,
        result.error,
    )
    return exit_code or EXIT_OPERATOR_NOTIFY_FAILED


class EmailUserNotifier:
    """``UserNotifier`` bound to ONE user at construction (the engine-notifier rule). Refuses an
    event for any other user, so one user's data can never be mailed to another. A failed send
    is logged and swallowed — it never masks the outcome it reports."""

    def __init__(
        self,
        sender: EmailSender,
        *,
        user: User,
        course_labels: Mapping[CourseId, str] | None = None,
    ) -> None:
        self._sender = sender
        self._user = user
        self._labels = dict(course_labels or {})

    async def send(self, event: UserEvent) -> None:
        if event.user_id != self._user.id:
            raise ValueError("refusing to mail an event that belongs to another user")
        label = self._labels.get(event.course_id) if event.course_id is not None else None
        rendered = render_user_event(
            event, first_name=first_name(self._user.display_name), course_label=label
        )
        message = EmailMessage(to=self._user.email, subject=rendered.subject, body=rendered.body)
        result = await _safe_send(self._sender, message)
        if not result.ok:
            log.warning(
                "user notification %s not delivered (status=%s error=%s)",
                event.kind.value,
                result.status,
                result.error,
            )
