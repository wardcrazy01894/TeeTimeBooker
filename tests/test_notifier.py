"""Tests for ConsoleNotifier and NoopNotifier (M4.T2 — partial M4)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from uuid import uuid4

from teetime.core.models import (
    BookingOutcome,
    BookingResult,
    CourseId,
    RequestId,
)
from teetime.notifications.notifier import ConsoleNotifier, NoopNotifier, Notifier


def _result(outcome: BookingOutcome = BookingOutcome.BOOKED) -> BookingResult:
    return BookingResult(
        request_id=RequestId(uuid4()),
        outcome=outcome,
        course_id=CourseId("foreup:mangrove_bay"),
        slot=None,
        confirmation_code="MB-12345",
        booked_at=datetime(2026, 5, 6, 10, 0, 1, tzinfo=UTC),
        attempts=1,
    )


def test_noop_notifier_satisfies_protocol() -> None:
    assert isinstance(NoopNotifier(), Notifier)


def test_console_notifier_satisfies_protocol() -> None:
    assert isinstance(ConsoleNotifier(stream=io.StringIO()), Notifier)


async def test_noop_notifier_succeeds_silently() -> None:
    """NoopNotifier is the test/dry-run sink: it must succeed without I/O."""
    n = NoopNotifier()
    await n.notify(_result())  # MUST NOT raise


async def test_console_notifier_writes_outcome_and_confirmation() -> None:
    buf = io.StringIO()
    n = ConsoleNotifier(stream=buf)
    await n.notify(_result(BookingOutcome.BOOKED))
    out = buf.getvalue()
    assert "booked" in out.lower()
    assert "MB-12345" in out
    assert "foreup:mangrove_bay" in out


async def test_console_notifier_writes_failure_outcome() -> None:
    buf = io.StringIO()
    n = ConsoleNotifier(stream=buf)
    await n.notify(_result(BookingOutcome.NO_INVENTORY))
    out = buf.getvalue()
    assert "no_inventory" in out.lower()
