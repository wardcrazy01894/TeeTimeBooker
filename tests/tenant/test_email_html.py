"""The HTML part every ACS email carries beside its plain text (2026-10-23).

The plain-text cards align their values with spaces, which only lines up in a monospace font;
Gmail shows plain text in a proportional one, so "Date:" sat two spaces left of "Course:" and
"Tee time:" half a space right of it. The HTML part lays the same words out as a table. These
tests pin that the words never change, that only card-shaped and column-shaped lines become
tables, and that the HTML sets no colour or font of its own (clients theme it, dark mode
included).
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from html.parser import HTMLParser
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from teetime.courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID
from teetime.tenant.email_html import html_from_text
from teetime.tenant.models import RowId, User, UserId, UserRole, UserStatus
from teetime.tenant.notify import (
    USER_FACING_KINDS,
    AttemptResult,
    EmailMessage,
    RunSummary,
    SummaryAttempt,
    SummaryRow,
    UserEvent,
    UserEventKind,
    render_invitation,
    render_operator_booking_notice,
    render_operator_summary,
    render_user_event,
)
from teetime.web.feedback import render_feedback

NY = ZoneInfo("America/New_York")
TEE = datetime(2026, 10, 10, 8, 12, tzinfo=NY)
USER_ID = UserId(UUID("11111111-2222-3333-4444-555555555555"))
ROW_ID = RowId("row-1")

EVENT = UserEvent(
    kind=UserEventKind.BOOKED,
    user_id=USER_ID,
    row_id=ROW_ID,
    course_id=MANGROVE_BAY_COURSE_ID,
    target_date=date(2026, 10, 10),
    tee_time=TEE,
    confirmation="TTB:123456",
    detail="watcher",
    at=datetime(2026, 10, 9, 7, 0, tzinfo=NY),
    windows=((time(7, 0), time(8, 0)), (time(8, 0), time(9, 0))),
    party_size=4,
    max_price=Decimal("60.00"),
    cutoff_local=datetime(2026, 10, 9, 16, 0, tzinfo=NY),
    previous_tee_time=TEE.replace(hour=9, minute=30),
    course_timezone="America/New_York",
)


class _Extract(HTMLParser):
    """The HTML's words: tag text in document order, cells joined by one space, rows and
    block elements by a newline."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "tr", "p", "div", "table"}:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _words(markup: str) -> list[str]:
    parser = _Extract()
    parser.feed(markup)
    return "".join(parser.parts).split()


def _cells(markup: str) -> list[list[str]]:
    """Every table row's cell texts, stripped."""

    class Rows(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.rows: list[list[str]] = []
            self._cell: list[str] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "tr":
                self.rows.append([])
            elif tag == "td":
                self._cell = []

        def handle_endtag(self, tag: str) -> None:
            if tag == "td" and self._cell is not None:
                self.rows[-1].append("".join(self._cell).strip())
                self._cell = None

        def handle_data(self, data: str) -> None:
            if self._cell is not None:
                self._cell.append(data)

    parser = Rows()
    parser.feed(markup)
    return parser.rows


# --- the structure ------------------------------------------------------------------------------


def test_a_card_block_becomes_a_two_column_table() -> None:
    body = (
        "Hi Turk,\n\nYou're booked.\n\n"
        "  Course:    Mangrove Bay\n"
        "  Date:      Saturday, October 10\n"
        "  Tee time:  8:12 AM (was 9:30 AM)\n\n"
        "— TeeTimeBooker"
    )
    markup = html_from_text(body)
    assert _cells(markup) == [
        ["Course:", "Mangrove Bay"],
        ["Date:", "Saturday, October 10"],
        ["Tee time:", "8:12 AM (was 9:30 AM)"],
    ]
    # The label never wraps away from its value; the value may wrap.
    assert markup.count("white-space:nowrap") == 3


def test_a_continuation_line_is_a_row_with_an_empty_label() -> None:
    body = (
        "  Windows:    1. 7:00-8:00 AM\n"
        "              2. 8:00-9:00 AM\n"
        "              3. 9:00-10:00 AM\n"
        "  How:        watcher"
    )
    assert _cells(html_from_text(body)) == [
        ["Windows:", "1. 7:00-8:00 AM"],
        ["", "2. 8:00-9:00 AM"],
        ["", "3. 9:00-10:00 AM"],
        ["How:", "watcher"],
    ]


def test_a_summary_row_block_is_text_not_a_table() -> None:
    """The operator summary's row lines have a colon followed by ONE space (a person's name,
    then the request), and the Attempts heading ends in a colon: none of that is a card."""
    body = (
        "BOOKED (1)\n"
        "  Brad: Mangrove Bay, Sat Oct 10 at 8:12 AM, 4 players\n"
        "    Windows 7:00-8:00 AM, 8:00-9:00 AM · confirmation 123456\n"
        "    Attempts (2 sent around 6:00:00 AM):"
    )
    markup = html_from_text(body)
    assert "<table" not in markup
    assert "<td" not in markup
    assert _words(markup) == body.split()


def test_attempt_lines_become_a_three_column_table() -> None:
    body = (
        "  Brad: Mangrove Bay, Sat Oct 10 at 8:12 AM, 4 players\n"
        "    Attempts (2 sent around 6:00:00 AM):\n"
        "      7:04 AM   sent 0.37 s early     rejected: too early (before the booking window opened)\n"
        "      8:12 AM   sent on time          booked → kept"
    )
    assert _cells(html_from_text(body)) == [
        ["7:04 AM", "sent 0.37 s early", "rejected: too early (before the booking window opened)"],
        ["8:12 AM", "sent on time", "booked → kept"],
    ]
    assert _words(html_from_text(body)) == body.split()


def test_blank_lines_separate_blocks_and_line_breaks_survive() -> None:
    markup = html_from_text("Hi Turk,\n\nLine one\nLine two\n\n— TeeTimeBooker")
    assert markup.count("<p") == 3
    assert "Line one<br>Line two" in markup


def test_leading_indentation_survives_in_text_blocks() -> None:
    """The run summary's hierarchy is its indentation (2, 4, 6 spaces); a browser collapses
    plain spaces, so they go out as non-breaking ones."""
    markup = html_from_text("OTHER (1)\n  user 1234: dry_run\n    detail")
    assert "&nbsp;&nbsp;user 1234: dry_run<br>&nbsp;&nbsp;&nbsp;&nbsp;detail" in markup


def test_markup_in_the_text_is_escaped() -> None:
    """The feedback form's email carries a person's own words."""
    markup = html_from_text("  Name:   <b>Turk</b> & co\n\n<script>alert(1)</script>")
    assert "<b>" not in markup and "<script>" not in markup
    assert "&lt;b&gt;Turk&lt;/b&gt; &amp; co" in markup
    assert _words(markup) == ["Name:", "<b>Turk</b>", "&", "co", "<script>alert(1)</script>"]


def test_a_url_becomes_a_link_without_its_trailing_punctuation() -> None:
    markup = html_from_text("  1. Go to https://example.com/a?b=1&c=2.\nThen (https://x.io).")
    assert (
        '<a href="https://example.com/a?b=1&amp;c=2">https://example.com/a?b=1&amp;c=2</a>.'
        in markup
    )
    assert '(<a href="https://x.io">https://x.io</a>).' in markup


def test_a_balanced_parenthesis_stays_inside_the_link() -> None:
    """A pasted Wikipedia or Maps link ends in ``)``: only an UNbalanced one is punctuation."""
    markup = html_from_text("see https://a.b/c_(x) and (https://a.b/d_(y)).")
    assert '<a href="https://a.b/c_(x)">https://a.b/c_(x)</a> and' in markup
    assert '(<a href="https://a.b/d_(y)">https://a.b/d_(y)</a>).' in markup


def test_runs_of_spaces_inside_a_text_line_survive() -> None:
    """A pasted log in the feedback form keeps its columns (a browser collapses plain runs);
    the last space of a run stays breakable so a long line can still wrap."""
    markup = html_from_text("a    b  c d")
    assert "a&nbsp;&nbsp;&nbsp; b&nbsp; c d" in markup


def test_windows_line_endings_are_plain_line_breaks() -> None:
    """A browser textarea submits CRLF; a stray ``\r`` must not reach a cell or a paragraph."""
    markup = html_from_text("Hi,\r\n\r\n  Course:    Mangrove Bay\r\n  Date:      Sat\r\n\r\nBye")
    assert "\r" not in markup
    assert _cells(markup) == [["Course:", "Mangrove Bay"], ["Date:", "Sat"]]
    assert markup.count("<p") == 2


def test_no_colour_or_font_of_its_own() -> None:
    """Dark mode and the client's own font are the client's business: style the layout only."""
    markup = html_from_text("Hi,\n\n  Course:    Mangrove Bay\n  Date:      Saturday, October 10")
    for forbidden in ("color", "background", "font-family", "font-size", "<style", "class="):
        assert forbidden not in markup


def test_an_empty_body_is_an_empty_document_body() -> None:
    assert _words(html_from_text("")) == []


def test_the_document_declares_utf8() -> None:
    """The em dash and the ✗ / → glyphs must survive a client that ignores the transport charset."""
    assert '<meta charset="utf-8">' in html_from_text("— TeeTimeBooker")


# --- every email the site sends keeps its words ------------------------------------------------


def _samples() -> list[tuple[str, str]]:
    rng = random.Random(0)
    out: list[tuple[str, str]] = []
    for kind in sorted(USER_FACING_KINDS):
        event = replace(
            EVENT, kind=kind, tee_time=None if kind is UserEventKind.MISSED_DROP else TEE
        )
        rendered = render_user_event(event, first_name="Turk", course_label="Mangrove Bay", rng=rng)
        out.append((f"user {kind}", rendered.body))
    for kind in (UserEventKind.BOOKED, UserEventKind.UPGRADED):
        rendered = render_operator_booking_notice(
            replace(EVENT, kind=kind),
            user_name="Brad",
            course_label="Mangrove Bay",
            environment="prod",
        )
        out.append((f"operator {kind}", rendered.body))
    out.append(("invitation", render_invitation("turk@example.com", site_url="https://x.io/").body))
    row = SummaryRow(
        row_id=ROW_ID,
        user_id=USER_ID,
        user_name="Brad",
        course_id=MANGROVE_BAY_COURSE_ID,
        target_date=date(2026, 10, 10),
        party_size=4,
        windows=((time(7, 0), time(8, 0)),),
        attempts=(
            SummaryAttempt(
                TEE,
                TEE.replace(hour=6, minute=0, second=0, microsecond=631000),
                AttemptResult.REJECTED,
                "too_early",
            ),
            SummaryAttempt(
                TEE, TEE.replace(hour=6, minute=0, second=0, microsecond=1000), AttemptResult.KEPT
            ),
        ),
    )
    summary = RunSummary(
        rows=(row,),
        events=(
            EVENT,
            replace(
                EVENT, kind=UserEventKind.OPERATOR_SUMMARY, row_id=None, detail="CAPTCHA pool dry"
            ),
        ),
        environment="prod",
        release_at=TEE.replace(hour=6, minute=0, second=0, microsecond=0),
        rows_loaded=1,
        rows_claimed=1,
        captcha_demanded=2,
        captcha_solved=2,
    )
    out.append(("summary", render_operator_summary(summary, exit_code=0, at=EVENT.at).body))
    out.append(("feedback", _feedback().body))
    return out


def _feedback() -> EmailMessage:
    """The email the feedback form mails the operator: a person's own words."""
    user = User(
        id=USER_ID,
        oauth_provider="google",
        oauth_subject="sub-1",
        email="turk@example.com",
        display_name="Turk <b>",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    return render_feedback(
        "bug",
        user=user,
        page="/dates",
        message="The 2nd row  lost  its time.\r\nSee https://en.wikipedia.org/wiki/Golf_(sport).",
    )


@pytest.mark.parametrize(("name", "body"), _samples(), ids=[n for n, _ in _samples()])
def test_the_html_part_says_exactly_what_the_text_says(name: str, body: str) -> None:
    markup = html_from_text(body)
    assert _words(markup) == body.split()


def test_the_feedback_email_escapes_the_persons_words_and_links_their_url() -> None:
    markup = html_from_text(_feedback().body)
    assert "<b>" not in markup and "Turk &lt;b&gt;" in markup
    assert (
        '<a href="https://en.wikipedia.org/wiki/Golf_(sport)">'
        "https://en.wikipedia.org/wiki/Golf_(sport)</a>." in markup
    )
    assert "<table" not in markup


def test_every_card_in_every_email_is_a_table() -> None:
    by_name = dict(_samples())
    assert _cells(html_from_text(by_name["user booked"]))[:3] == [
        ["Course:", "Mangrove Bay"],
        ["Date:", "Saturday, October 10"],
        ["Tee time:", "8:12 AM (was 9:30 AM)"],
    ]
    assert _cells(html_from_text(by_name["user missed_drop"]))[0] == ["Course:", "Mangrove Bay"]
    operator = _cells(html_from_text(by_name["operator booked"]))
    assert [c[0] for c in operator] == [
        "Course:",
        "Date:",
        "Tee time:",
        "Asked for:",
        "Windows:",
        "",
        "How:",
        "Id:",
        "Cutoff:",
    ]
    summary = _cells(html_from_text(by_name["summary"]))
    assert len(summary) == 2 and summary[0][2] == "booked → kept"  # sent first, listed first
