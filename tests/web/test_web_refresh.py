"""MU-14: "Refresh from course" — an on-demand login behind a short TTL cache (MULTIUSER_PLAN §8.6).

Service-level, against a real ``InMemoryTenantStore`` + ``FakeClock``; the ForeUP adapter is
faked at the ``AdapterFactory`` boundary.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from teetime.core.clock import FakeClock
from teetime.core.redaction import redact_text
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountStatus,
    CourseAccount,
    ReservationSnapshot,
    SnapshotEntry,
    UserId,
)
from teetime.web import services
from teetime.web.services import ActionRefusedError, ProbeLimits, RateLimitedError, RefreshCache

from ..tenant.conformance import MB
from .account_builders import (
    KEYRING,
    PASSWORD,
    ProbeAdapter,
    ProbeFactory,
    reservation,
    stored_account,
)
from .conftest import T0, new_store

TEE = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)
TTL_S = 120


@pytest.fixture
def store() -> InMemoryTenantStore:
    return new_store()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


async def _account(store: InMemoryTenantStore) -> CourseAccount:
    account = stored_account(UserId(uuid4()))
    await store.upsert_account(account)
    return account


async def _refresh(
    store: InMemoryTenantStore,
    clock: FakeClock,
    factory: ProbeFactory,
    cache: RefreshCache,
    *,
    account: CourseAccount,
    user_id: UserId | None = None,
) -> ReservationSnapshot:
    return await services.refresh_account(
        store,
        user_id=account.user_id if user_id is None else user_id,
        account_id=account.id,
        keyring=KEYRING,
        adapter_factory=factory,
        clock=clock,
        cache=cache,
        limits=ProbeLimits(),
    )


def _live(adapter: ProbeAdapter) -> ProbeAdapter:
    adapter.set_existing_reservations([reservation("9001", TEE)])
    return adapter


async def test_refresh_persists_a_trusted_snapshot(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    account = await _account(store)
    factory = ProbeFactory(adapter=_live(ProbeAdapter()))
    snap = await _refresh(store, clock, factory, RefreshCache(ttl_s=TTL_S), account=account)
    expected = ReservationSnapshot(
        course_account_id=account.id,
        observed_at=T0,
        source="refresh",
        trusted=True,
        entries=(SnapshotEntry(raw_id="9001", tee_time=TEE, party_size=2),),
    )
    assert snap == expected
    assert await store.get_snapshot(account.id) == expected
    (call,) = factory.calls
    assert call["account"] == account and call["pool"] is None and call["dry_run"] is True
    assert factory.adapter.credentials == [(account.username, PASSWORD)]
    assert factory.adapter.closed == 1
    assert PASSWORD not in redact_text(PASSWORD)  # the decrypted plaintext is E7-registered


async def test_refresh_ttl_serves_cache(store: InMemoryTenantStore, clock: FakeClock) -> None:
    """Repeat clicks inside ``refresh_ttl_s`` are served from the in-process cache: no login."""
    account = await _account(store)
    factory = ProbeFactory(adapter=_live(ProbeAdapter()))
    cache = RefreshCache(ttl_s=TTL_S)
    first = await _refresh(store, clock, factory, cache, account=account)
    await clock.sleep(TTL_S - 1)
    again = await _refresh(store, clock, factory, cache, account=account)
    assert again == first
    assert factory.adapter.authenticate_call_count == 1
    await clock.sleep(2)  # past the TTL
    later = await _refresh(store, clock, factory, cache, account=account)
    assert factory.adapter.authenticate_call_count == 2
    assert later.observed_at == clock.now_utc()


async def test_refresh_cache_is_user_scoped(store: InMemoryTenantStore, clock: FakeClock) -> None:
    """A cached snapshot is never served to another user asking for that account id."""
    account = await _account(store)
    cache = RefreshCache(ttl_s=TTL_S)
    await _refresh(
        store, clock, ProbeFactory(adapter=_live(ProbeAdapter())), cache, account=account
    )
    with pytest.raises(services.WebNotFoundError):
        await _refresh(
            store, clock, ProbeFactory(), cache, account=account, user_id=UserId(uuid4())
        )


async def test_refresh_untrusted_snapshot_not_persisted(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    """§7.5 (b)/(c): a login whose reservation list can't be believed is neither persisted nor
    cached, and the previous (trusted) snapshot stays."""
    account = await _account(store)
    previous = ReservationSnapshot(
        course_account_id=account.id,
        observed_at=T0 - timedelta(minutes=30),
        source="watcher",
        trusted=True,
        entries=(SnapshotEntry(raw_id="9001", tee_time=TEE, party_size=2),),
    )
    await store.save_snapshot(previous)
    adapter = ProbeAdapter(trusted=False)  # logged in, cache empty
    factory = ProbeFactory(adapter=adapter)
    cache = RefreshCache(ttl_s=TTL_S)
    with pytest.raises(ActionRefusedError, match="couldn't read"):
        await _refresh(store, clock, factory, cache, account=account)
    assert await store.get_snapshot(account.id) == previous
    with pytest.raises(ActionRefusedError):
        await _refresh(store, clock, factory, cache, account=account)
    assert adapter.authenticate_call_count == 2  # nothing was cached


async def test_refresh_soft_login_failure_counts_and_persists_nothing(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    account = await _account(store)
    factory = ProbeFactory()
    factory.adapter.set_auth_soft_fail()
    with pytest.raises(ActionRefusedError, match="couldn't log in"):
        await _refresh(store, clock, factory, RefreshCache(ttl_s=TTL_S), account=account)
    assert await store.get_snapshot(account.id) is None
    stored = await store.get_account(account.id, user_id=account.user_id)
    assert stored is not None and stored.consecutive_soft_auth_failures == 1


async def test_refresh_hard_cap_per_account_per_hour(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    account = await _account(store)
    for _ in range(ProbeLimits().refreshes_per_account_per_hour):
        await store.record_login_probe(
            user_id=account.user_id,
            course_id=MB,
            username_hash=services.refresh_probe_hash(account.id),
            ok=True,
            at=T0,
        )
    factory = ProbeFactory()
    with pytest.raises(RateLimitedError):
        await _refresh(store, clock, factory, RefreshCache(ttl_s=TTL_S), account=account)
    assert factory.calls == []


async def test_refresh_records_a_probe_per_live_login(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    account = await _account(store)
    cache = RefreshCache(ttl_s=TTL_S)
    factory = ProbeFactory(adapter=_live(ProbeAdapter()))
    await _refresh(store, clock, factory, cache, account=account)
    await _refresh(store, clock, factory, cache, account=account)  # cached: not a login
    since = T0 - timedelta(hours=1)
    key = services.refresh_probe_hash(account.id)
    assert await store.count_login_probes(user_id=None, username_hash=key, since=since) == 1


async def test_refresh_refused_for_auth_failed_account(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    """``auth_failed`` stops every automatic AND on-demand login until re-verify (PLAN §12)."""
    account = await _account(store)
    await store.upsert_account(replace(account, status=AccountStatus.AUTH_FAILED))
    factory = ProbeFactory()
    with pytest.raises(ActionRefusedError, match="re-verify"):
        await _refresh(store, clock, factory, RefreshCache(ttl_s=TTL_S), account=account)
    assert factory.calls == []
