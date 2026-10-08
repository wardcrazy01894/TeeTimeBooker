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

Content is PII-minimal: the recipient's first name, the course, the date, the tee time and a
reason. A booking email carries no confirmation id (it is internal, never the golfer's) and
signs off with a random ``golf_quips`` line. Never credentials, never another user's data. Every
rendered subject and body passes through ``core.redaction.redact_text`` as defence in depth, so a
registered secret literal (E7: keyring keys, decrypted passwords, the ACS key) or a stray email
address in a free-text ``detail`` is masked before it can reach a mailbox.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from ..core.models import BookingResult, CourseId
from ..core.redaction import redact_text
from ..courses.names import course_display_name
from .golf_quips import GOLF_QUIPS, MISS_QUIPS
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
    # §16.4: the user holds two tee times for one date and the bot could not (manual booking) or
    # did not yet (a failed cancel it keeps retrying) cancel the worse one. Sent once per row.
    DOUBLE_HELD = "double_held"
    NEEDS_RECONCILE = "needs_reconcile"  # UNCERTAIN outcome (§4.5) — operator only
    DRY_RUN = "dry_run"  # a dry run: nothing POSTed. Operator only, and NOT a problem
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
        UserEventKind.DOUBLE_HELD,
    }
)

# Kinds that ALSO mail the operator one short notice per event (operator request 2026-10-01: "I
# want an email every time someone gets a tee time", not only the 06:00 run's summary). The
# watcher's ``StoreUserNotifier`` sends it; the booker does not (its run summary already lists
# every booking).
OPERATOR_COPY_KINDS: frozenset[UserEventKind] = frozenset(
    {UserEventKind.BOOKED, UserEventKind.UPGRADED}
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
    # What the person asked for: the row's ranked windows (best first), the party, the price
    # cap, and the booking cutoff as an aware instant in the COURSE timezone ("before 4 PM on
    # Friday …"). The miss email's card uses the first window (operator request 2026-10-02); the
    # operator's booking notice lays all of it out (operator request 2026-10-06, after a watcher
    # booking's notice named only the date). ``previous_tee_time`` is an upgrade's old tee time;
    # ``course_timezone`` puts the notice's send time on the course's clock. All optional: the
    # booker and the watcher fill them, and every email reads fine without them.
    windows: tuple[tuple[time, time], ...] = ()
    party_size: int | None = None
    max_price: Decimal | None = None
    cutoff_local: datetime | None = None
    previous_tee_time: datetime | None = None
    course_timezone: str | None = None


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
        # Operator request 2026-10-08: an upgrade is good news, not a notice.
        "Good news! We got you a tee time closer to what you asked for.",
    ),
    UserEventKind.MISSED_DROP: (
        "No tee time yet: {course} {day}",
        "Unfortunately we couldn't get you a tee time at {course} for {long_day} when the tee "
        "sheet opened this morning.",
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
    UserEventKind.DOUBLE_HELD: (
        "Two tee times on {day}",
        "You're holding two tee times on {day}, one of them at {course} ({when}), and we couldn't "
        "cancel the one you ranked lower. If it was booked by hand we never cancel it for you; "
        "otherwise we'll keep trying. Cancel whichever you don't want at the course.",
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


def _clock(t: time) -> str:
    return f"{t:%I:%M %p}".lstrip("0")


# The booker's MISSED_DROP detail for a CAPTCHA / OTP / booking-service failure starts with this
# (runner._row_events); everything else is "the course had nothing".
_SERVICE_ERROR_DETAIL = "booking service error"

_MISS_CAUSE_COURSE = (
    "When this happens it is almost always because the course has blocked the morning for an "
    "event or an outing, so there was nothing to book. Now and then the sheet simply sold out "
    "faster than we could get in."
)
_MISS_CAUSE_OURS = (
    "This one was on our side: a problem with our booking service, not the course. The operator "
    "has been notified."
)


def _miss_card(event: UserEvent, course: str) -> list[str]:
    card = [f"  Course:     {course}"]
    if event.target_date is not None:
        card.append(f"  Date:       {event.target_date:%A, %B} {event.target_date.day}")
    if event.windows and event.party_size is not None:
        lo, hi = event.windows[0]
        n = len(event.windows) - 1
        more = f" (+{n} more option{'s' if n != 1 else ''})" if n else ""
        card.append(
            f"  You asked:  {_clock(lo)} to {_clock(hi)}, {_players(event.party_size)}{more}"
        )
    return card


def _players(n: int) -> str:
    return "1 player" if n == 1 else f"{n} players"


def _deadline_text(cutoff_local: datetime) -> str:
    """``4 PM on Friday, October 9`` (the course's clock; ``:00`` dropped)."""
    c = cutoff_local
    return f"{_clock(c.time()).replace(':00 ', ' ')} on {c:%A, %B} {c.day}"


def _miss_lines(event: UserEvent, course: str, rng: random.Random | None) -> list[str]:
    """The miss email after the lead sentence (operator request 2026-10-02): the card, why it
    usually happens, what the watcher will do until the cutoff, honest odds, a consolation."""
    cause = (
        _MISS_CAUSE_OURS if event.detail.startswith(_SERVICE_ERROR_DETAIL) else _MISS_CAUSE_COURSE
    )
    deadline = (
        _deadline_text(event.cutoff_local)
        if event.cutoff_local is not None
        else "the booking cutoff"
    )
    return [
        "",
        *_miss_card(event, course),
        "",
        cause,
        "",
        "We'll keep watching for a cancellation and will book one for you if a time opens up "
        f"before {deadline}. That does happen, but it's not something to count on, so you may "
        "want to make other plans for that morning.",
        "",
        (rng or random).choice(MISS_QUIPS),
        "",
        "— Spicy's helper",
    ]


def _redacted(subject: str, body: str) -> RenderedEmail:
    return RenderedEmail(subject=redact_text(subject), body=redact_text(body))


_BOOKING_KINDS = frozenset({UserEventKind.BOOKED, UserEventKind.UPGRADED})


def _booking_card(event: UserEvent, course: str) -> list[str]:
    """The tee time laid out as a small aligned block (plain text, so spaces align it); an
    upgrade's old tee time follows the new one as ``(was 9:30 AM)``."""
    day = event.tee_time.date() if event.tee_time else event.target_date
    card = [f"  Course:    {course}"]
    if day is not None:
        card.append(f"  Date:      {day:%A, %B} {day.day}")
    if event.tee_time is not None:
        was = f" (was {_time(event.previous_tee_time)})" if event.previous_tee_time else ""
        card.append(f"  Tee time:  {_time(event.tee_time)}{was}")
    return card


def _keeps_checking_line(event: UserEvent) -> list[str]:
    """What the watcher does next (operator request 2026-10-08): it keeps upgrading the booking
    until the person's cutoff, so say so, in their own deadline's words. Nothing when the event
    carries no cutoff (an old row, a test event): never an empty promise."""
    if event.cutoff_local is None:
        return []
    better = "an even better" if event.kind is UserEventKind.UPGRADED else "a better"
    return [
        "",
        f"We'll keep checking for {better} time until {_deadline_text(event.cutoff_local)}.",
    ]


def render_user_event(
    event: UserEvent,
    *,
    first_name: str,
    course_label: str | None = None,
    rng: random.Random | None = None,
) -> RenderedEmail:
    """Plain-text subject/body for one user-facing event. Operator-only kinds raise. A booking
    (BOOKED / UPGRADED) signs off with a golf one-liner picked by ``rng`` (the module RNG when
    None)."""
    if event.kind not in USER_FACING_KINDS:
        raise ValueError(f"{event.kind} is operator-only; render it via render_operator_summary")
    subject_t, lead_t = _USER_TEMPLATES[event.kind]
    fields = {"course": _course(event, course_label), "when": _when(event)}
    fields["day"] = _day(event.target_date)
    d = event.target_date
    fields["long_day"] = f"{d:%A, %B} {d.day}" if d is not None else "an upcoming date"
    lines = [f"Hi {first_name},", "", lead_t.format(**fields)]
    if event.kind is UserEventKind.MISSED_DROP:
        lines += _miss_lines(event, fields["course"], rng)
        return _redacted(f"[TeeTimeBooker] {subject_t.format(**fields)}", "\n".join(lines))
    if event.kind in _BOOKING_KINDS:
        # The golfer's view only: no confirmation id (ours, and ForeUP's internal teetime id,
        # never shown to them by the course) and no engine detail ("watcher").
        lines += ["", *_booking_card(event, fields["course"])]
        lines += _keeps_checking_line(event)
        lines += ["", (rng or random).choice(GOLF_QUIPS)]
    elif event.kind is UserEventKind.CANCELLED:
        lines += ["", "Hope to see you back on the course soon."]
    elif event.detail:
        lines += ["", f"Details: {event.detail}"]
    lines += ["", "— TeeTimeBooker"]
    return _redacted(f"[TeeTimeBooker] {subject_t.format(**fields)}", "\n".join(lines))


_OPERATOR_NOTICE_LEAD: Mapping[UserEventKind, str] = {
    UserEventKind.BOOKED: "{who} got a tee time at {course}.",
    UserEventKind.UPGRADED: (
        "{who}'s tee time at {course} was moved to a better tee time: {now}{was}."
    ),
}

_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}


def _ordinal(n: int) -> str:
    return _ORDINALS.get(n, f"{n}th")


def _choice_text(windows: tuple[tuple[time, time], ...], tee_time: datetime) -> str:
    """Which ranked window the tee time landed in, by the engine's first-match rule
    (``models.achieved_rank``): nothing with no windows known or a single window hit (nothing
    to rank); a tee time no window contains is flagged even then. ``windows`` is the row's
    options in rank order (``validate_options`` pins ascending ranks), and the choice is numbered
    by POSITION on purpose: a row's ranks may skip numbers (they span the person's courses), and
    the notice says "2nd choice" meaning the second window asked for at this course."""
    if not windows:
        return ""
    t = tee_time.time()
    for position, (lo, hi) in enumerate(windows, start=1):
        if lo <= t <= hi:
            return f" ({_ordinal(position)} choice)" if len(windows) > 1 else ""
    return " (outside every window)"


def _operator_context(event: UserEvent) -> list[str]:
    """The request behind the booking, as card lines; nothing for an event that carries none."""
    lines: list[str] = []
    if event.party_size is not None:
        cap = f", up to ${event.max_price:.2f} each" if event.max_price is not None else ""
        lines.append(f"  Asked for:  {_players(event.party_size)}{cap}")
    if len(event.windows) == 1:
        lines.append(f"  Windows:    {_window_span(*event.windows[0])}")
    elif event.windows:
        lines.append(f"  Windows:    1. {_window_span(*event.windows[0])}")
        lines += [
            f"              {i}. {_window_span(lo, hi)}"
            for i, (lo, hi) in enumerate(event.windows[1:], start=2)
        ]
    return lines


def _window_span(lo: time, hi: time) -> str:
    """``7:00-8:00 AM`` (one meridiem) or ``11:30 AM-1:00 PM``, as the run summary prints it."""
    if f"{lo:%p}" == f"{hi:%p}":
        return f"{_time_of_day(lo, ampm=False)}-{_time_of_day(hi)}"
    return f"{_time_of_day(lo)}-{_time_of_day(hi)}"


def render_operator_booking_notice(
    event: UserEvent,
    *,
    user_name: str | None,
    course_label: str | None,
    environment: str | None,
) -> RenderedEmail:
    """One operator email for a booking made OUTSIDE the release run (the watcher found a free
    time, or upgraded one), with everything the operator would otherwise look up (2026-10-06):
    who (display name, else a short user id), course, date, the tee time and which ranked
    choice it landed in (an upgrade also names the old tee time), what the person asked for
    (party, price cap, every window), how (``event.detail``), the course's reservation id, the
    cutoff until which the watcher keeps upgrading, and the send time on the course's clock.
    Context the event does not carry is left out, never printed empty. Tagged with the
    environment like the run summary. Refuses any kind outside ``OPERATOR_COPY_KINDS``."""
    if event.kind not in OPERATOR_COPY_KINDS:
        raise ValueError(f"{event.kind} is not a booking; only {sorted(OPERATOR_COPY_KINDS)}")
    who = _who(event.user_id, user_name) if (user_name or event.user_id) else "Someone"
    course = _course(event, course_label)
    tag = "TeeTimeBooker" + (f" · {environment.upper()}" if environment else "")
    verb = "Booked" if event.kind is UserEventKind.BOOKED else "Upgraded"
    subject = f"[{tag}] {verb}: {who} · {course} {_when(event)}"
    now = _time(event.tee_time) if event.tee_time is not None else _day(event.target_date)
    was = f" (was {_time(event.previous_tee_time)})" if event.previous_tee_time else ""
    day = event.tee_time.date() if event.tee_time else event.target_date
    lines = [
        _OPERATOR_NOTICE_LEAD[event.kind].format(who=who, course=course, now=now, was=was),
        "",
        f"  Course:     {course}",
    ]
    if day is not None:
        lines.append(f"  Date:       {day:%A, %B} {day.day}")
    if event.tee_time is not None:
        choice = _choice_text(event.windows, event.tee_time)
        old = f", was {_time(event.previous_tee_time)}" if event.previous_tee_time else ""
        lines.append(f"  Tee time:   {_time(event.tee_time)}{choice}{old}")
    lines += _operator_context(event)
    if event.detail:
        lines.append(f"  How:        {event.detail}")
    if event.confirmation:
        lines.append(f"  Id:         {event.confirmation.removeprefix('TTB:')}")
    if event.cutoff_local is not None:
        lines.append(
            f"  Cutoff:     {_deadline_text(event.cutoff_local)} "
            "(the watcher keeps upgrading until then)"
        )
    at = event.at.astimezone(ZoneInfo(event.course_timezone)) if event.course_timezone else event.at
    lines += ["", f"Sent {at:%a %b} {at.day}, {_time(at)} {at:%Z}".rstrip()]
    return _redacted(subject, "\n".join(lines))


# --- invitation (operator request 2026-09-29; wording approved by the operator) ---------------

SITE_NAME = "Spicy's Tee Time Booker"
INVITER_NAME = "Spicy Al"


def render_invitation(email: str, *, site_url: str) -> RenderedEmail:
    """The invite email sent from ``/admin/users``. NOT passed through ``redact_text``: its whole
    point is to show the invitee their own address (redaction masks every email), and every part
    of it is fixed text, that operator-entered address or the configured site URL."""
    body = [
        "Hi,",
        "",
        f"{INVITER_NAME} has invited you to {SITE_NAME}. It books golf tee times for you the "
        "moment the course opens them, so you don't have to be up at 6 AM to grab a good slot.",
        "",
        "To get started:",
        "",
        f"  1. Go to {site_url}",
        f"  2. Sign in with Google using this email address ({email}). The invite only works "
        "with this exact address.",
        "  3. Connect your course login, then pick the days and times you'd like to play.",
        "",
        "If you weren't expecting this, you can ignore this email.",
        "",
        "See you on the first tee!",
        "",
        f"— {SITE_NAME}",
    ]
    return RenderedEmail(subject=f"You're invited to {SITE_NAME}!", body="\n".join(body))


# --- operator summary (one email per booking run) ----------------------------------------------


class AttemptResult(StrEnum):
    """What became of one book POST, as the operator summary lists it."""

    KEPT = "kept"
    CANCELLED_EXTRA = "cancelled_extra"  # booked, then cancelled in-run as a surplus
    HELD_EXTRA = "held_extra"  # booked surplus whose cancel FAILED (the watcher collapses it)
    UNCONFIRMED = "unconfirmed"  # BOOKED with no confirmation id: not owned, needs_reconcile
    REJECTED = "rejected"  # SlotGoneError: nothing created
    UNCERTAIN = "uncertain"  # the POST may have landed


@dataclass(frozen=True, slots=True)
class SummaryAttempt:
    tee_time: datetime
    sent_at: datetime
    result: AttemptResult
    reason: str | None = None  # SlotGoneError.reason, or the UNCERTAIN exception class name


@dataclass(frozen=True, slots=True)
class SummaryRow:
    """One claimed request row as the operator sees it. ``user_name`` is the display name (the
    summary goes to the operator only), None when the lookup failed."""

    row_id: RowId
    user_id: UserId
    user_name: str | None
    course_id: CourseId
    target_date: date
    party_size: int
    windows: tuple[tuple[time, time], ...]
    attempts: tuple[SummaryAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Everything the operator summary renders. ``events`` holds every ``UserEvent`` of the run:
    a row's events are shown under its ``SummaryRow`` (by ``row_id``); the rest are run-level
    lines. ``release_at`` is the release instant (T0) in the course timezone."""

    rows: tuple[SummaryRow, ...] = ()
    events: tuple[UserEvent, ...] = ()
    environment: str | None = None
    dry_run: bool = False
    release_at: datetime | None = None
    rows_loaded: int = 0
    rows_claimed: int = 0
    captcha_demanded: int | None = None
    captcha_solved: int | None = None


# Kinds that put a row (or the run) in the PROBLEMS section at the top, and in the subject.
PROBLEM_KINDS: frozenset[UserEventKind] = frozenset(
    {
        UserEventKind.MISSED_DROP,
        UserEventKind.LOST,
        UserEventKind.AUTH_FAILED,
        UserEventKind.NEEDS_RECONCILE,
        UserEventKind.DOUBLE_HELD,
        UserEventKind.CANCELLED_EXTERNAL,
        UserEventKind.OPERATOR_SUMMARY,
    }
)
_BOOKED_KINDS = frozenset({UserEventKind.BOOKED, UserEventKind.UPGRADED})

_PROBLEM_TEXT: Mapping[UserEventKind, str] = {
    UserEventKind.MISSED_DROP: "missed the drop{d}; the watcher keeps trying until the cutoff",
    UserEventKind.LOST: "no tee time before the cutoff{d}",
    UserEventKind.AUTH_FAILED: "course login failed{d}; booking paused until the password is "
    "re-entered",
    UserEventKind.NEEDS_RECONCILE: "outcome UNCERTAIN{d}; the watcher reconciles it",
    UserEventKind.DOUBLE_HELD: "holds two tee times for this date{d}",
    UserEventKind.CANCELLED_EXTERNAL: "cancelled outside the site{d}",
    UserEventKind.OPERATOR_SUMMARY: "{detail}",
}

_REJECTION_TEXT: Mapping[str, str] = {
    "daily_limit": "one-per-day limit",
    "unavailable": "time not available",
    "too_early": "too early (before the booking window opened)",
    "conflict": "conflict (409)",
}

_ATTEMPT_TEXT: Mapping[AttemptResult, str] = {
    AttemptResult.KEPT: "booked → kept",
    AttemptResult.CANCELLED_EXTRA: "booked → cancelled (extra)",
    AttemptResult.HELD_EXTRA: "booked → extra STILL HELD (cancel failed)",
    AttemptResult.UNCONFIRMED: "booked → unconfirmed (no id; the watcher adopts it)",
}


# A POST within this many ms of T0 reads "on time" (the clock itself is NTP-corrected to ~ms).
_ON_TIME_MS = 5.0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _who(user_id: UserId | None, name: str | None) -> str:
    if name:
        return name
    return f"user {str(user_id)[:8]}" if user_id is not None else "Run"


def _window_text(windows: tuple[tuple[time, time], ...]) -> str:
    label = "Window" if len(windows) == 1 else "Windows"
    return f"{label} " + ", ".join(_window_span(lo, hi) for lo, hi in windows)


def _time_of_day(t: time, *, ampm: bool = True) -> str:
    text = f"{t:%I:%M}".lstrip("0")
    return f"{text} {t:%p}" if ampm else text


def _offset_text(sent_at: datetime, release_at: datetime | None) -> str:
    if release_at is None:
        return f"sent {sent_at:%H:%M:%S}"
    ms = (sent_at - release_at).total_seconds() * 1000.0
    if abs(ms) < _ON_TIME_MS:
        return "sent on time"
    return f"sent {abs(ms) / 1000:.2f} s {'early' if ms < 0 else 'late'}"


def _attempt_result_text(a: SummaryAttempt) -> str:
    if a.result is AttemptResult.REJECTED:
        reason = _REJECTION_TEXT.get(a.reason or "", "reason unknown")
        return f"rejected: {reason}"
    if a.result is AttemptResult.UNCERTAIN:
        return f"UNCERTAIN ({a.reason or 'unknown'})"
    return _ATTEMPT_TEXT[a.result]


def _problem_text(event: UserEvent) -> str:
    template = _PROBLEM_TEXT.get(event.kind, "{detail}")
    return template.format(d=f" ({event.detail})" if event.detail else "", detail=event.detail)


def _row_block(row: SummaryRow, events: Sequence[UserEvent], summary: RunSummary) -> list[str]:
    booked = next((e for e in events if e.kind in _BOOKED_KINDS), None)
    when = _day(row.target_date)
    if booked is not None and booked.tee_time is not None:
        when = f"{when} at {_time(booked.tee_time)}"
    course = course_display_name(row.course_id)
    lines = [
        f"  {_who(row.user_id, row.user_name)}: {course}, {when}, "
        f"{_plural(row.party_size, 'player')}"
    ]
    detail = _window_text(row.windows) if row.windows else ""
    if booked is not None and booked.confirmation:
        conf = booked.confirmation.removeprefix("TTB:")
        detail = f"{detail} · confirmation {conf}" if detail else f"confirmation {conf}"
    if booked is not None and booked.kind is UserEventKind.UPGRADED:
        detail += " · upgraded"
    if detail:
        lines.append(f"    {detail}")
    lines += [f"    ✗ {_problem_text(e)}" for e in events if e.kind in PROBLEM_KINDS]
    if row.attempts:
        ordered = sorted(row.attempts, key=lambda a: a.sent_at)
        around = ""
        if summary.release_at is not None:
            around = f" sent around {summary.release_at:%I:%M:%S %p}".replace(" 0", " ", 1)
        lines.append(f"    Attempts ({len(ordered)}{around}):")
        lines += [
            f"      {_time(a.tee_time):<9} {_offset_text(a.sent_at, summary.release_at):<20} "
            f"{_attempt_result_text(a)}"
            for a in ordered
        ]
    return lines


@dataclass(frozen=True, slots=True)
class _Sorted:
    """A run's rows and events sorted into the summary's sections."""

    by_row: dict[RowId, list[UserEvent]]
    problem_rows: list[SummaryRow]
    booked_rows: list[SummaryRow]
    dry_rows: list[SummaryRow]
    other_rows: list[SummaryRow]
    loose_problems: list[UserEvent]  # run-level (no row) problem lines
    loose_other: list[UserEvent]
    booked_total: int

    @property
    def n_problems(self) -> int:
        return len(self.problem_rows) + len(self.loose_problems)


def _sort(summary: RunSummary) -> _Sorted:
    by_row: dict[RowId, list[UserEvent]] = {}
    row_ids = {r.row_id for r in summary.rows}
    loose: list[UserEvent] = []
    for e in summary.events:
        if e.row_id is not None and e.row_id in row_ids:
            by_row.setdefault(e.row_id, []).append(e)
        else:
            loose.append(e)

    def kinds(row: SummaryRow) -> set[UserEventKind]:
        return {e.kind for e in by_row.get(row.row_id, [])}

    problem, booked, dry, other = [], [], [], []
    for r in summary.rows:
        k = kinds(r)
        if k & PROBLEM_KINDS:
            problem.append(r)
        elif k & _BOOKED_KINDS:
            booked.append(r)
        elif UserEventKind.DRY_RUN in k:
            dry.append(r)
        else:
            other.append(r)
    return _Sorted(
        by_row=by_row,
        problem_rows=problem,
        booked_rows=booked,
        dry_rows=dry,
        other_rows=other,
        loose_problems=[e for e in loose if e.kind in PROBLEM_KINDS],
        loose_other=[e for e in loose if e.kind not in PROBLEM_KINDS],
        booked_total=sum(1 for r in summary.rows if kinds(r) & _BOOKED_KINDS),
    )


def _summary_subject(summary: RunSummary, parts: _Sorted, *, exit_code: int) -> str:
    """Environment tag, then failures FIRST, then the result and what it was about."""
    tag = "TeeTimeBooker"
    if summary.environment:
        tag += f" · {summary.environment.upper()}"
    if summary.dry_run:
        tag += " · dry run"
    bits: list[str] = []
    if exit_code != 0:
        bits.append(f"❌ RUN FAILED (exit {exit_code})")
    if parts.n_problems:
        bits.append(f"⚠ {_plural(parts.n_problems, 'problem')}")
    if summary.rows:
        n = len(summary.rows)
        bits.append(
            f"Checked {_plural(n, 'request')}"
            if summary.dry_run
            else f"Booked {parts.booked_total} of {n}"
        )
        bits.append(
            ", ".join(dict.fromkeys(course_display_name(r.course_id) for r in summary.rows))
        )
        bits.append(", ".join(_day(d) for d in sorted({r.target_date for r in summary.rows})))
    elif not bits:
        bits.append("nothing to book")
    return f"[{tag}] " + " · ".join(bits)


def _summary_header(summary: RunSummary, parts: _Sorted, *, exit_code: int, at: datetime) -> str:
    when = summary.release_at or at
    stamp = f"{when:%a %b} {when.day}, {_time(when)} {when:%Z}".strip()
    mode = [summary.environment] if summary.environment else []
    mode.append("dry run" if summary.dry_run else "live")
    status: list[str] = []
    if exit_code != 0:
        status.append("❌ FAILED")
    if parts.n_problems:
        status.append(f"⚠ {_plural(parts.n_problems, 'PROBLEM')}")
    return (
        f"Booking run: {stamp} ({', '.join(mode)}) · {' · '.join(status) or 'OK'} · "
        f"exit {exit_code}"
    )


def _summary_stats(summary: RunSummary) -> str:
    stats = [f"{_plural(summary.rows_loaded, 'request')} loaded / {summary.rows_claimed} claimed"]
    if summary.captcha_demanded is not None and summary.captcha_solved is not None:
        stats.append(
            f"CAPTCHAs: {summary.captcha_solved} of {summary.captcha_demanded} solved in the "
            "first wave"
        )
    user_emails = sum(1 for e in summary.events if e.kind in USER_FACING_KINDS)
    stats.append(f"user emails: {user_emails}")
    return "Run: " + " · ".join(stats)


def render_operator_summary(summary: RunSummary, *, exit_code: int, at: datetime) -> RenderedEmail:
    """One email per booking run. Problems (any row with a ``PROBLEM_KINDS`` event, and every
    run-level line) come FIRST and are counted in the subject; then the booked rows with each
    POST of the burst; then dry-run and other rows; then the run statistics. Names people and
    courses (never a raw course id or an email address)."""
    parts = _sort(summary)
    lines = [_summary_header(summary, parts, exit_code=exit_code, at=at)]

    def section(title: str, rows: Sequence[SummaryRow], extra: Sequence[UserEvent] = ()) -> None:
        if not rows and not extra:
            return
        lines.extend(["", f"{title} ({len(rows) + len(extra)})"])
        for e in extra:
            text = _problem_text(e) if e.kind in PROBLEM_KINDS else e.kind.value
            lines.append(f"  {_who(e.user_id, None)}: {text}")
        for r in rows:
            lines.extend(_row_block(r, parts.by_row.get(r.row_id, []), summary))

    section("⚠ PROBLEMS", parts.problem_rows, parts.loose_problems)
    section("BOOKED", parts.booked_rows)
    section("DRY RUN", parts.dry_rows)
    section("OTHER", parts.other_rows, parts.loose_other)
    if not summary.rows and not summary.events:
        lines += ["", "No requests for this release."]
    lines += ["", _summary_stats(summary)]
    return _redacted(_summary_subject(summary, parts, exit_code=exit_code), "\n".join(lines))


# --- delivery ----------------------------------------------------------------------------------

# Exit code when the run itself was clean but the operator summary could not be delivered (§4.5).
EXIT_OPERATOR_NOTIFY_FAILED = 1


async def _safe_send(sender: EmailSender, message: EmailMessage) -> EmailSendResult:
    try:
        return await sender.send(message)
    except Exception as exc:  # a sender bug must never mask a booking outcome
        log.warning("email sender raised %s", type(exc).__name__, exc_info=True)
        return EmailSendResult(ok=False, status="error", error=type(exc).__name__)


async def deliver_operator_summary(
    sender: EmailSender,
    *,
    to: str,
    summary: RunSummary,
    exit_code: int,
    at: datetime,
) -> int:
    """Send the operator summary when there was anything to report (rows, events, or a non-zero
    exit) and return the run's FINAL exit code: a failed send turns a clean exit into
    ``EXIT_OPERATOR_NOTIFY_FAILED`` (§4.5 SF6 — otherwise a broken ACS setup would make every
    miss invisible, since misses exit 0). An already non-zero code is kept.

    A DRY RUN (dev, permanently) reports only a systemic failure: a clean dry run cannot have
    booked or missed anything real, and the operator wants no mail from dev unless something is
    broken (2026-10-04). Its non-zero exit still sends the summary: that IS the failure channel."""
    if not summary.rows and not summary.events and exit_code == 0:
        return 0
    if summary.dry_run and exit_code == 0:
        log.info(
            "dry run: operator summary not sent (clean exit; %d row(s), %d event(s))",
            len(summary.rows),
            len(summary.events),
        )
        return 0
    rendered = render_operator_summary(summary, exit_code=exit_code, at=at)
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


async def deliver_operator_booking_notice(
    sender: EmailSender,
    *,
    to: str,
    event: UserEvent,
    user_name: str | None,
    course_label: str | None,
    environment: str | None,
) -> EmailSendResult:
    """Render + send one operator booking notice. NEVER raises (a render bug or a sender raise is
    an ``ok=False`` result, logged), so the caller's own outcome and the user's email stand."""
    try:
        rendered = render_operator_booking_notice(
            event, user_name=user_name, course_label=course_label, environment=environment
        )
    except Exception as exc:
        log.warning("operator booking notice not rendered (%s)", type(exc).__name__, exc_info=True)
        return EmailSendResult(ok=False, status="error", error=type(exc).__name__)
    result = await _safe_send(
        sender, EmailMessage(to=to, subject=rendered.subject, body=rendered.body)
    )
    if not result.ok:
        log.warning(
            "operator copy of %s for row %s not delivered (status=%s error=%s)",
            event.kind.value,
            event.row_id,
            result.status,
            result.error,
        )
    return result


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
