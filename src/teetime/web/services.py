"""Web service layer (MULTIUSER_PLAN §8.4-§8.6). Framework-free, so every behaviour is testable
with ``InMemoryTenantStore`` + ``FakeAdapter`` + ``FakeClock``. The web NEVER calls ``book()``
(booking is job-only, §9.5); it calls only ``authenticate``, ``list_reservations`` and
``cancel_reservation``.

STUB — implemented in MULTIUSER_PLAN MU-13 / MU-14.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..core.clock import Clock
from ..core.models import CourseId
from ..tenant.crypto import Keyring
from ..tenant.models import (
    CourseAccount,
    CourseAccountId,
    RequestRow,
    ReservationSnapshot,
    RowId,
    UserId,
)
from ..tenant.runner import AdapterFactory
from ..tenant.store import TenantStore

_MU13 = "MULTIUSER_PLAN.md MU-13"
_MU14 = "MULTIUSER_PLAN.md MU-14"


@dataclass(frozen=True, slots=True)
class DashboardRow:
    """What the dashboard renders per row: DB status + the account snapshot, with its age
    labelled, and a mismatch badge (§7.4)."""

    row: RequestRow
    snapshot_observed_at: datetime | None
    snapshot_trusted: bool
    mismatch: str | None  # "not_seen_at_course" | "manual_reservation" | None
    search_only_last_drop: bool


class CancelRefusedError(RuntimeError):
    """The cancel could not be performed safely (booker lease held, untrusted snapshot, not
    owned without confirmation). Nothing was cancelled."""


async def dashboard(store: TenantStore, *, user_id: UserId, clock: Clock) -> list[DashboardRow]:
    """Rows for the next 21 days + snapshot age. NEVER logs in to ForeUP (§8.6)."""
    raise NotImplementedError(_MU13)


async def connect_account(
    store: TenantStore,
    *,
    user_id: UserId,
    course_id: CourseId,
    username: str,
    password: str,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
) -> CourseAccount:
    """Rate-check -> live ``authenticate`` on a throwaway adapter -> require
    ``is_authenticated`` -> encrypt with AAD -> store (``provenance=user_supplied``). On failure
    nothing is stored and a generic error is shown (§8.4)."""
    raise NotImplementedError(_MU14)


async def refresh_account(
    store: TenantStore,
    *,
    user_id: UserId,
    account_id: CourseAccountId,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
) -> ReservationSnapshot:
    """Live login + list, persisted as a snapshot. Served from the in-process TTL cache inside
    ``refresh_ttl_s``; hard-capped per account per hour (§8.6)."""
    raise NotImplementedError(_MU14)


async def cancel_row(
    store: TenantStore,
    *,
    user_id: UserId,
    row_id: RowId,
    confirm_unowned: bool,
    keyring: Keyring,
    adapter_factory: AdapterFactory,
    clock: Clock,
) -> RequestRow:
    """The managed-cancel path (§8.5): row lease (60 s) -> decrypt -> authenticate (must be
    authenticated + trusted snapshot) -> confirm the raw id is live -> ``cancel_reservation`` ->
    ONE transaction (row CANCELLED(user), ledger cancelled_user, snapshot, audit) -> release ->
    email. Raises ``CancelRefusedError`` if unsafe; never cancels an unowned reservation without
    ``confirm_unowned``."""
    raise NotImplementedError(_MU14)
