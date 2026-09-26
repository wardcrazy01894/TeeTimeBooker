"""MU-9c: ``LeasedBookingStore`` maps the engine's ``request_lock`` onto the durable row lease
(MULTIUSER_PLAN §3.5, round-1 M5).

The engine (``core/``) is unmodified: it sees a plain ``BookingStore`` whose ``request_lock``
raises ``ConcurrentRunError`` when the row is leased by someone else OR moved since it was read,
so its existing defer handling serializes the watcher / web against the booker across PROCESSES.
Rows are seeded through the ``TenantStore`` Protocol (never by poking dicts).
"""

from __future__ import annotations

from datetime import UTC, datetime

from teetime.core.clock import FakeClock
from teetime.core.models import BookingOutcome, BookingResult, CourseId
from teetime.persistence.in_memory_store import InMemoryStore
from teetime.persistence.store import BookingStore
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RequestRow, RowFingerprint
from teetime.tenant.store import LeasedBookingStore

from .watch_runner_builders import TARGET, WATCH_NOW, new_store, seed

OWNER = "watch-exec-1"
LEASE_S = 300.0


def _fingerprint(row: RequestRow) -> RowFingerprint:
    return RowFingerprint(row.status, row.version, row.booked_raw_id)


def _leased(
    tenant: InMemoryTenantStore,
    row: RequestRow,
    *,
    inner: InMemoryStore | None = None,
    clock: FakeClock | None = None,
    owner: str = OWNER,
) -> LeasedBookingStore:
    return LeasedBookingStore(
        inner=inner or InMemoryStore(),
        tenant=tenant,
        owner=owner,
        lease_seconds=LEASE_S,
        clock=clock or FakeClock(start=WATCH_NOW),
        row_for_request={row.request_id: (row.id, _fingerprint(row))},
    )


async def test_leased_store_is_booking_store() -> None:
    tenant = new_store()
    s = await seed(tenant, n=1)

    assert isinstance(_leased(tenant, s.row), BookingStore)


async def test_delegates_terminals_and_attempts_to_inner() -> None:
    tenant = new_store()
    s = await seed(tenant, n=1)
    inner = InMemoryStore()
    store = _leased(tenant, s.row, inner=inner)
    rid = s.row.request_id
    result = BookingResult(
        request_id=rid,
        outcome=BookingOutcome.NO_INVENTORY,
        course_id=None,
        slot=None,
        confirmation_code=None,
        booked_at=None,
        attempts=1,
    )

    await store.initialize()
    await store.record_terminal(result, TARGET)
    assert await inner.get_terminal(rid, TARGET) == result
    assert await store.get_terminal(rid, TARGET) == result
    await store.delete_terminal(rid, TARGET)
    assert await inner.get_terminal(rid, TARGET) is None

    await store.append_attempt(rid, 1, "search", {"card_number": "4111111111111111"}, WATCH_NOW)
    ((_, attempt, event, payload, _at),) = inner._attempts
    assert (attempt, event) == (1, "search")
    assert "4111111111111111" not in repr(payload)  # the inner store's redaction still applies

    course = CourseId("fake:mb")
    far = datetime(2099, 1, 1, tzinfo=UTC)
    await store.cache_session(course, b"blob", far)
    assert await store.load_session(course) == b"blob"
    assert await inner.load_session(course) == b"blob"
