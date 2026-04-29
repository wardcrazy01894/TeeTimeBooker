"""Shared pytest fixtures for the test suite.

Stubs only — bodies will be filled in alongside the milestone that owns each
fixture (mostly M2.T1). Listing them here NOW prevents the M2 implementer
from having to invent fixture names mid-task and keeps the cross-milestone
test surface coherent. See PLAN.md §11.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from teetime.core.clock import Clock
    from teetime.notifications.notifier import Notifier
    from teetime.persistence.store import BookingStore


@pytest.fixture
def fake_clock() -> Clock:
    """A controllable Clock for deterministic race-window tests.

    Real impl in M1.T1: a Clock whose `now_utc()` returns a settable internal
    `_now` and whose `sleep(s)` advances `_now` by `s` seconds without real
    I/O. Tests for `busy_wait_until` use this to assert ±50 ms targeting
    accuracy without waiting 7 days.
    """
    raise NotImplementedError("M1.T1: implement FakeClock")


@pytest.fixture
def in_memory_store() -> BookingStore:
    """An InMemoryStore (M2.T1). Use for orchestrator unit tests; SqliteStore
    is reserved for its own M3 tests.
    """
    raise NotImplementedError("M2.T1: instantiate InMemoryStore")


@pytest.fixture
def noop_notifier() -> Notifier:
    """A Notifier whose `notify()` records calls but does nothing else (M4.T2)."""
    raise NotImplementedError("M4.T2: implement NoopNotifier in tests")


@pytest.fixture
async def fake_adapter() -> AsyncIterator[object]:
    """A scriptable CourseAdapter for orchestrator tests (M2.T1).

    Concrete shape: a class exposing setters like `set_search_response(slots)` and
    `set_book_outcome(outcome)` so tests can drive specific code paths through
    the orchestrator without hitting the network. SHOULD also support recording
    that `list_reservations()` was called (for §9 reconciliation tests).
    """
    raise NotImplementedError("M2.T1: implement scriptable FakeAdapter")
    yield  # pragma: no cover


@pytest.fixture
def t0_utc() -> datetime:
    """A canonical T0 used by race-window tests. Tied to a date safely inside
    EDT to avoid DST-edge ambiguity. M1.T1 freezes this to a specific value."""
    return datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
