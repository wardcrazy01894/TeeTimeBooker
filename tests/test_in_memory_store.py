"""Tests for InMemoryStore — partial M2.T1 deliverable.

Behavioral parity with future SqliteStore (M3): same Protocol, same semantics
for single-process usage. Tests here pin the contract so M3 can copy them.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from teetime.core.models import (
    BookingOutcome,
    BookingResult,
    CourseId,
    RequestId,
)
from teetime.persistence.in_memory_store import InMemoryStore
from teetime.persistence.store import BookingStore, ConcurrentRunError


def _rid() -> RequestId:
    return RequestId(uuid4())


def _result(rid: RequestId, outcome: BookingOutcome = BookingOutcome.BOOKED) -> BookingResult:
    return BookingResult(
        request_id=rid,
        outcome=outcome,
        course_id=CourseId("foreup:mangrove_bay"),
        slot=None,
        confirmation_code="TEST-CONF-1",
        booked_at=datetime(2026, 5, 6, 10, 0, 1, tzinfo=UTC),
        attempts=1,
    )


def test_in_memory_store_satisfies_protocol() -> None:
    assert isinstance(InMemoryStore(), BookingStore)


async def test_get_terminal_returns_none_when_empty() -> None:
    store = InMemoryStore()
    await store.initialize()
    assert await store.get_terminal(_rid(), date(2026, 5, 13)) is None


async def test_record_then_get_terminal_round_trips() -> None:
    store = InMemoryStore()
    await store.initialize()
    rid = _rid()
    d = date(2026, 5, 13)
    res = _result(rid)
    await store.record_terminal(res, d)
    got = await store.get_terminal(rid, d)
    assert got == res


async def test_record_terminal_idempotent_on_same_outcome() -> None:
    """Re-writing the same terminal for the same key is a no-op (replays are OK)."""
    store = InMemoryStore()
    await store.initialize()
    rid = _rid()
    d = date(2026, 5, 13)
    res = _result(rid, BookingOutcome.BOOKED)
    await store.record_terminal(res, d)
    await store.record_terminal(res, d)  # must not raise


async def test_record_terminal_rejects_conflicting_outcome() -> None:
    """A BOOKED then a different outcome for the same (rid, date) MUST raise —
    this is the v0 layer that catches in-process double-book attempts."""
    store = InMemoryStore()
    await store.initialize()
    rid = _rid()
    d = date(2026, 5, 13)
    await store.record_terminal(_result(rid, BookingOutcome.BOOKED), d)
    with pytest.raises(ValueError, match="conflicting"):
        await store.record_terminal(_result(rid, BookingOutcome.NO_INVENTORY), d)


async def test_record_terminal_does_not_collide_across_dates() -> None:
    """(RequestId, date) is the composite key per PLAN §13.1 — different dates
    are independent records even for the same RequestId."""
    store = InMemoryStore()
    await store.initialize()
    rid = _rid()
    d1 = date(2026, 5, 13)
    d2 = date(2026, 5, 14)
    await store.record_terminal(_result(rid, BookingOutcome.BOOKED), d1)
    await store.record_terminal(_result(rid, BookingOutcome.NO_INVENTORY), d2)
    assert (await store.get_terminal(rid, d1)).outcome == BookingOutcome.BOOKED  # type: ignore[union-attr]
    assert (await store.get_terminal(rid, d2)).outcome == BookingOutcome.NO_INVENTORY  # type: ignore[union-attr]


async def test_append_attempt_records_event() -> None:
    store = InMemoryStore()
    rid = _rid()
    await store.initialize()
    await store.append_attempt(
        rid,
        attempt=1,
        event="SEARCH_START",
        payload={"course_id": "c1"},
        at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC),
    )
    # InMemoryStore exposes the list for test inspection (real store reads via SQL).
    assert len(store._attempts) == 1
    assert store._attempts[0][2] == "SEARCH_START"


async def test_request_lock_holds_for_duration() -> None:
    store = InMemoryStore()
    await store.initialize()
    rid = _rid()
    async with store.request_lock(rid):
        # While holding, second acquire raises ConcurrentRunError fast.
        with pytest.raises(ConcurrentRunError):
            async with store.request_lock(rid):
                pass


async def test_request_lock_releases_after_with_block() -> None:
    store = InMemoryStore()
    await store.initialize()
    rid = _rid()
    async with store.request_lock(rid):
        pass
    # Re-acquire after release must succeed.
    async with store.request_lock(rid):
        pass


async def test_request_lock_distinct_request_ids_dont_block() -> None:
    store = InMemoryStore()
    await store.initialize()
    rid1, rid2 = _rid(), _rid()
    async with store.request_lock(rid1), store.request_lock(rid2):
        pass


async def test_request_lock_releases_on_exception() -> None:
    store = InMemoryStore()
    await store.initialize()
    rid = _rid()

    async def raises() -> None:
        async with store.request_lock(rid):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await raises()
    # Lock must be free again.
    async with store.request_lock(rid):
        pass


async def test_session_cache_round_trip_and_expiry() -> None:
    store = InMemoryStore()
    await store.initialize()
    cid = CourseId("foreup:mangrove_bay")
    future = datetime(2099, 1, 1, tzinfo=UTC)
    past = datetime(2000, 1, 1, tzinfo=UTC)

    await store.cache_session(cid, b"session-bytes", expires_at=future)
    assert await store.load_session(cid) == b"session-bytes"

    await store.cache_session(cid, b"expired-bytes", expires_at=past)
    assert await store.load_session(cid) is None


_ = asyncio  # silence "imported but unused" if Python keeps optimizing
