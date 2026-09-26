"""MU-9c: ``LeasedBookingStore`` maps the engine's ``request_lock`` onto the durable row lease
(MULTIUSER_PLAN §3.5, round-1 M5).

The engine (``core/``) is unmodified: it sees a plain ``BookingStore`` whose ``request_lock``
raises ``ConcurrentRunError`` when the row is leased by someone else OR moved since it was read,
so its existing defer handling serializes the watcher / web against the booker across PROCESSES.
Rows are seeded through the ``TenantStore`` Protocol (never by poking dicts).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from teetime.core.clock import FakeClock
from teetime.core.config import OneBookingPolicyConfig, SchedulerConfig
from teetime.core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseId,
    ExistingReservation,
    Player,
    TimeWindow,
)
from teetime.core.upgrade_orchestrator import UpgradeOrchestrator
from teetime.dev.fake_adapter import FakeAdapter
from teetime.notifications.notifier import NoopNotifier
from teetime.persistence.in_memory_store import InMemoryStore
from teetime.persistence.store import BookingStore, ConcurrentRunError
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import Actor, RequestRow, RowFingerprint, RowId, RowStatus
from teetime.tenant.store import LeasedBookingStore, TenantStore

from .watch_runner_builders import (
    BETTER,
    HELD_EARLY,
    MB,
    TARGET,
    WATCH_NOW,
    WINDOW,
    Seeded,
    new_store,
    seed,
    stored_row,
)

OWNER = "watch-exec-1"
LEASE_S = 300.0


def _fingerprint(row: RequestRow) -> RowFingerprint:
    return RowFingerprint(row.status, row.version, row.booked_raw_id)


def _leased(
    tenant: TenantStore,
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


# --- request_lock -> durable row lease (§3.5, M5) --------------------------------------------


async def _row(tenant: InMemoryTenantStore, s: Seeded) -> RequestRow:
    return await stored_row(tenant, s)


async def _skip(tenant: InMemoryTenantStore, s: Seeded, *, unskip: bool = False) -> None:
    targets = [RowStatus.SKIPPED, RowStatus.PENDING] if unskip else [RowStatus.SKIPPED]
    for to in targets:
        await tenant.transition_row(
            s.row.id, user_id=s.user.id, to=to, actor=Actor.WEB, reason=None, now=WATCH_NOW
        )


async def test_request_lock_takes_row_lease_and_releases() -> None:
    tenant = new_store()
    s = await seed(tenant, n=1)
    store = _leased(tenant, s.row)

    async with store.request_lock(s.row.request_id):
        held = await _row(tenant, s)
        assert held.lease_owner == OWNER
        assert held.lease_expires_at == WATCH_NOW + timedelta(seconds=LEASE_S)

    released = await _row(tenant, s)
    assert (released.lease_owner, released.lease_expires_at) == (None, None)
    assert released.version == s.row.version  # a lease write never bumps the domain version

    # The fingerprint stays valid across our own acquire/release: a second lock (Gate-3 reconcile
    # then upgrade, in one watch run) acquires again.
    async with store.request_lock(s.row.request_id):
        assert (await _row(tenant, s)).lease_owner == OWNER


async def test_request_lock_changed_fingerprint_raises_concurrent_run() -> None:
    """Read, then the user skips + unskips (still PENDING, version bumped) before the engine locks:
    the fingerprinted acquire refuses and the engine defers (M5)."""
    tenant = new_store()
    s = await seed(tenant, n=1)
    inner = InMemoryStore()
    store = _leased(tenant, s.row, inner=inner)
    await _skip(tenant, s, unskip=True)

    with pytest.raises(ConcurrentRunError):
        async with store.request_lock(s.row.request_id):
            pytest.fail("body must not run on a moved row")

    assert (await _row(tenant, s)).lease_owner is None
    # The in-process lock was released too: the refusal is not sticky inside the process.
    async with inner.request_lock(s.row.request_id):
        pass


async def test_request_lock_foreign_live_lease_raises_concurrent_run() -> None:
    tenant = new_store()
    s = await seed(tenant, n=1)
    store = _leased(tenant, s.row)
    booker_until = WATCH_NOW + timedelta(seconds=1200)
    assert await tenant.claim_rows([s.row.id], owner="booker", until=booker_until, now=WATCH_NOW)

    with pytest.raises(ConcurrentRunError):
        async with store.request_lock(s.row.request_id):
            pytest.fail("body must not run under a foreign lease")

    row = await _row(tenant, s)
    assert (row.lease_owner, row.lease_expires_at) == ("booker", booker_until)


async def test_request_lock_takes_over_an_expired_foreign_lease() -> None:
    tenant = new_store()
    s = await seed(tenant, n=1)
    stale = WATCH_NOW - timedelta(seconds=1)
    assert await tenant.claim_rows(
        [s.row.id], owner="booker", until=stale, now=WATCH_NOW - timedelta(minutes=20)
    )
    store = _leased(tenant, s.row)

    async with store.request_lock(s.row.request_id):
        assert (await _row(tenant, s)).lease_owner == OWNER


async def test_request_lock_releases_on_exception_and_cancellation() -> None:
    tenant = new_store()
    s = await seed(tenant, n=1)
    store = _leased(tenant, s.row)
    rid = s.row.request_id

    with pytest.raises(RuntimeError, match="boom"):
        async with store.request_lock(rid):
            raise RuntimeError("boom")
    assert (await _row(tenant, s)).lease_owner is None

    entered = asyncio.Event()

    async def _hold() -> None:
        async with store.request_lock(rid):
            entered.set()
            await asyncio.Event().wait()  # parked until cancelled

    task = asyncio.create_task(_hold())
    await entered.wait()
    assert (await _row(tenant, s)).lease_owner == OWNER
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await _row(tenant, s)).lease_owner is None
    async with store.request_lock(rid):  # nothing left held in-process either
        pass


async def test_request_lock_is_not_reentrant_like_in_memory_store() -> None:
    """A nested acquire of the same RequestId raises (``InMemoryStore`` semantics) instead of
    silently re-entering, and it does NOT release the outer holder's lease."""
    tenant = new_store()
    s = await seed(tenant, n=1)
    store = _leased(tenant, s.row)
    rid = s.row.request_id

    async with store.request_lock(rid):
        with pytest.raises(ConcurrentRunError):
            async with store.request_lock(rid):
                pytest.fail("nested acquire must not enter")
        assert (await _row(tenant, s)).lease_owner == OWNER
    assert (await _row(tenant, s)).lease_owner is None


async def test_request_lock_unregistered_request_raises_concurrent_run() -> None:
    """No row registered for the RequestId: there is nothing to lease, so the engine defers
    rather than act without cross-process exclusion."""
    tenant = new_store()
    s = await seed(tenant, n=1)
    other = await seed(tenant, n=2)
    store = _leased(tenant, s.row)

    with pytest.raises(ConcurrentRunError):
        async with store.request_lock(other.row.request_id):
            pytest.fail("unregistered request must not enter")
    assert (await _row(tenant, other)).lease_owner is None


async def test_leased_store_release_only_by_owner() -> None:
    """Our lease expired while held and another owner took the row: our exit leaves theirs."""
    tenant = new_store()
    s = await seed(tenant, n=1)
    store = _leased(tenant, s.row)
    later = WATCH_NOW + timedelta(seconds=LEASE_S + 1)
    web_until = later + timedelta(seconds=60)

    async with store.request_lock(s.row.request_id):
        assert await tenant.acquire_row_lease(
            s.row.id, owner="web", until=web_until, now=later, expected=None
        )

    assert (await _row(tenant, s)).lease_owner == "web"


class _FailingRelease:
    """A TenantStore whose release fails (a DB blip); everything else is the real store."""

    def __init__(self, inner: InMemoryTenantStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def release_row_lease(self, row_id: RowId, *, owner: str) -> None:
        raise RuntimeError("db down")


async def test_request_lock_release_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed release never masks the body's outcome: the lease simply expires."""
    tenant = new_store()
    s = await seed(tenant, n=1)
    store = _leased(_FailingRelease(tenant), s.row)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        async with store.request_lock(s.row.request_id):
            pass

    assert "release" in caplog.text
    assert (await _row(tenant, s)).lease_owner == OWNER  # left to expire


# --- the UNMODIFIED UpgradeOrchestrator over a LeasedBookingStore ----------------------------


def _upgrade_setup(
    tenant: InMemoryTenantStore, s: Seeded
) -> tuple[UpgradeOrchestrator, FakeAdapter, BookingRequest, BookingResult]:
    fake = FakeAdapter(course_id=MB)
    fake.set_existing_reservations(
        [
            ExistingReservation(
                course_id=MB, confirmation_code="raw-1", tee_time=HELD_EARLY.tee_time, party_size=2
            )
        ]
    )
    fake.set_search_response([BETTER])
    request = BookingRequest(
        request_id=s.row.request_id,
        target_dates=(TARGET,),
        time_windows=(TimeWindow(earliest=WINDOW[0], latest=WINDOW[1]),),
        players=(Player(first_name="A", last_name="B", email="a@b.test"),) * 2,
        course_preferences=(MB,),
    )
    current = BookingResult(
        request_id=s.row.request_id,
        outcome=BookingOutcome.BOOKED,
        course_id=MB,
        slot=HELD_EARLY,
        confirmation_code=f"{MANAGED_BOOKING_TAG}raw-1",
        booked_at=WATCH_NOW,
        attempts=1,
    )
    clock = FakeClock(start=WATCH_NOW)
    orc = UpgradeOrchestrator(
        adapters={MB: fake},
        store=_leased(tenant, s.row, clock=clock),
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=SchedulerConfig(),
        policy=OneBookingPolicyConfig(enabled=True),
    )
    return orc, fake, request, current


async def test_upgrade_orchestrator_upgrades_when_row_unchanged() -> None:
    """Control for the defer test: the same setup with no move DOES cancel + book."""
    tenant = new_store()
    s = await seed(tenant, n=1)
    orc, fake, request, current = _upgrade_setup(tenant, s)

    result = await orc.maybe_upgrade(request, TARGET, current)

    assert result is not None
    assert result.slot == BETTER
    assert (fake.cancel_call_count, fake.book_call_count) == (1, 1)
    assert (await _row(tenant, s)).lease_owner is None


async def test_upgrade_orchestrator_defers_when_row_moved() -> None:
    """The user skips the row between the runner's read and the engine's lock: the lease
    acquire refuses on the fingerprint, ``maybe_upgrade`` raises ``ConcurrentRunError`` (the
    watch orchestrator's existing defer), and nothing is cancelled or booked."""
    tenant = new_store()
    s = await seed(tenant, n=1)
    orc, fake, request, current = _upgrade_setup(tenant, s)
    await _skip(tenant, s)

    with pytest.raises(ConcurrentRunError):
        await orc.maybe_upgrade(request, TARGET, current)

    assert (fake.cancel_call_count, fake.book_call_count) == (0, 0)
    row = await _row(tenant, s)
    assert (row.status, row.lease_owner) == (RowStatus.SKIPPED, None)
