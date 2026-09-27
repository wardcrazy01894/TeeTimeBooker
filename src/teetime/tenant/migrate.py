"""Tenant data migrations (MULTIUSER_PLAN §10.1/§10.2, MU-16a) — what ``teetime tenant-migrate``
and the Manual ``teetime-migrate-<env>`` ACA job run.

Schema evolution is expand/contract: readers accept ``schemaVersion`` N and N-1 and writers
write N (``tenant/cosmos/documents.py``), so most changes need no migration at all. A data
BACKFILL, when one is needed, is appended to ``MIGRATIONS`` as a named step whose ``apply`` is
IDEMPOTENT (safe to run on every deploy, returns how many documents it changed) — there is no
applied-migrations ledger to consult, so re-running is the normal case, not the exception.

v1 ships NO backfill: the job connects (``TenantStore.initialize`` — a point read per container,
which proves reachability and the data-plane role assignment), runs the empty list, logs what it
did and exits 0.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..core.clock import Clock, RealClock
from .retry import retry_transient
from .store import TenantStore

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """A migration failed (or the list is malformed). Carries the step name + exception class
    only, never the message (it could echo document data)."""


@dataclass(frozen=True, slots=True)
class Migration:
    name: str  # ordered, unique, e.g. "001-backfill-options"
    apply: Callable[[TenantStore], Awaitable[int]]  # idempotent; returns documents changed


@dataclass(frozen=True, slots=True)
class MigrationReport:
    applied: tuple[tuple[str, int], ...]  # (name, documents changed), in run order


# The ordered migration list. Append only; never reorder or rename a shipped step.
MIGRATIONS: tuple[Migration, ...] = ()


async def run_migrations(
    store: TenantStore,
    migrations: Sequence[Migration] = MIGRATIONS,
    *,
    clock: Clock | None = None,
) -> MigrationReport:
    """Connect, then run every migration in order. The first failure stops the list and raises
    ``MigrationError`` (the job exits non-zero; the next deploy re-runs from the start).
    ``initialize`` is a pure read, so a transient failure is replayed (``tenant/retry.py``);
    a migration step is NOT (the whole job is the retry unit: every step is idempotent)."""
    names = [m.name for m in migrations]
    if len(set(names)) != len(names):
        raise MigrationError(f"duplicate migration names in {names}")
    await retry_transient(
        store.initialize, label="tenant-migrate: initialize", clock=clock or RealClock()
    )
    applied: list[tuple[str, int]] = []
    for migration in migrations:
        try:
            changed = await migration.apply(store)
        except Exception as exc:
            raise MigrationError(
                f"migration {migration.name} failed ({type(exc).__name__})"
            ) from None
        log.info("tenant-migrate: %s changed %d document(s)", migration.name, changed)
        applied.append((migration.name, changed))
    log.info("tenant-migrate: %d migration(s) run", len(applied))
    return MigrationReport(applied=tuple(applied))
