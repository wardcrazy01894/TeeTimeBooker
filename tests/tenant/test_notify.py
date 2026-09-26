"""MU-11: per-user notifications — buffering, rendering, operator summary (MULTIUSER_PLAN §8.7).

Nothing here does network I/O: ``BufferingNotifier`` is the in-race collector, the renderer is
pure, and delivery goes through an ``EmailSender`` (``FakeEmailSender`` in tests; the ACS client
is pinned separately in ``test_acs_email.py``).
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

import respx

from teetime.core.models import BookingOutcome, BookingResult, CourseId, RequestId
from teetime.tenant import notify
from teetime.tenant.notify import BufferingNotifier

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
