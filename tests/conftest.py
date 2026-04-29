"""Shared pytest fixtures. PLAN.md §11.

Each fixture owns one collaborator the orchestrator depends on. Tests pull
the ones they need; nothing else is implicit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from teetime.core.clock import Clock, FakeClock
from teetime.core.models import CourseId
from teetime.dev.fake_adapter import FakeAdapter
from teetime.notifications.notifier import NoopNotifier, Notifier
from teetime.persistence.in_memory_store import InMemoryStore
from teetime.persistence.store import BookingStore


@pytest.fixture
def t0_utc() -> datetime:
    """Canonical T0 for race-window tests. 2026-05-06 06:00 EDT == 10:00 UTC.
    Date deliberately inside EDT to avoid DST-edge ambiguity."""
    return datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def fake_clock(t0_utc: datetime) -> Clock:
    """A FakeClock anchored 2 s before the canonical T0."""
    return FakeClock(start=t0_utc - timedelta(seconds=2))


@pytest.fixture
def in_memory_store() -> BookingStore:
    """A fresh InMemoryStore. Behavioral parity with SqliteStore for tests."""
    return InMemoryStore()


@pytest.fixture
def noop_notifier() -> Notifier:
    """Silent notifier — for tests that don't care about delivery."""
    return NoopNotifier()


@pytest.fixture
def fake_adapter() -> FakeAdapter:
    """Default-happy-path scriptable adapter. Tests override via set_*()."""
    return FakeAdapter(course_id=CourseId("fake:course"))
