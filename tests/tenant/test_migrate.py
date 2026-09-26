"""MU-16a: ``teetime.tenant.migrate`` — the ordered data-migration list the Manual
``teetime-migrate-<env>`` ACA job runs (MULTIUSER_PLAN §10.1/§10.2). v1 ships NO backfill."""

from __future__ import annotations

import pytest

from teetime.core.config import BookingCutoffConfig
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.migrate import MIGRATIONS, Migration, MigrationError, run_migrations
from teetime.tenant.store import TenantStore


def _store() -> InMemoryTenantStore:
    return InMemoryTenantStore(course_timezones={}, cutoff=BookingCutoffConfig())


def test_v1_ships_no_migration() -> None:
    assert MIGRATIONS == ()


class _Probe(InMemoryTenantStore):
    def __init__(self) -> None:
        super().__init__(course_timezones={}, cutoff=BookingCutoffConfig())
        self.initialized = 0

    async def initialize(self) -> None:
        self.initialized += 1


async def test_empty_list_verifies_connectivity_and_reports_nothing_applied() -> None:
    store = _Probe()
    report = await run_migrations(store)
    assert store.initialized == 1
    assert report.applied == ()


async def test_migrations_run_in_order_and_rerun_idempotently() -> None:
    seen: list[str] = []

    def step(name: str) -> Migration:
        async def apply(store: TenantStore) -> int:
            seen.append(name)
            return 0

        return Migration(name=name, apply=apply)

    migrations = (step("001-a"), step("002-b"))
    first = await run_migrations(_store(), migrations)
    second = await run_migrations(_store(), migrations)
    assert seen == ["001-a", "002-b", "001-a", "002-b"]
    assert first.applied == second.applied == (("001-a", 0), ("002-b", 0))


async def test_a_failing_migration_stops_the_list_and_names_itself() -> None:
    seen: list[str] = []

    async def boom(store: TenantStore) -> int:
        raise RuntimeError("secret-bearing message")

    async def later(store: TenantStore) -> int:
        seen.append("later")
        return 0

    with pytest.raises(MigrationError, match="001-boom") as info:
        await run_migrations(_store(), (Migration("001-boom", boom), Migration("002-later", later)))
    assert seen == []
    # Only the exception CLASS is carried (a message could echo document data).
    assert "secret-bearing" not in str(info.value)


async def test_duplicate_migration_names_are_refused() -> None:
    async def noop(store: TenantStore) -> int:
        return 0

    with pytest.raises(MigrationError, match="duplicate"):
        await run_migrations(_store(), (Migration("001", noop), Migration("001", noop)))
