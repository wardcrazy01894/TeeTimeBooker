"""Tenant-watcher helpers (MULTIUSER_PLAN §7). Pure decision functions + one adapter proxy.

STUB — implemented in MULTIUSER_PLAN MU-10a (pure decisions + proxy factory).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from ..core.adapter import AdapterCapabilities, CourseAdapter
from ..core.models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
)
from .models import EventRow, OwnedBooking, RequestRow, ReservationSnapshot

_MU10 = "MULTIUSER_PLAN.md MU-10a"

# Per-account reconcile cadence: log in when (hash(account_id) + run_index) % N == 0, i.e.
# ~hourly per account at a 10-min cron, spread across runs, no stored state (§7.1).
RECONCILE_EVERY_N_RUNS = 6
# Backstop: an account holding a BOOKED row is re-listed if its snapshot is older than this.
MAX_BOOKED_SNAPSHOT_AGE_S = 90 * 60
# Vanish inference needs this many consecutive TRUSTED snapshots without the reservation (§7.5).
VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS = 2


@dataclass(frozen=True, slots=True)
class SearchGroupKey:
    """One shared ``search()`` per group (§7.2). party_size is part of the key because ForeUP
    hides slots with fewer open spots than the requested players."""

    course_id: CourseId
    target_date: date
    party_size: int


class LoginReason(StrEnum):
    BOOKABLE_SLOT = "bookable_slot"
    UPGRADE_CANDIDATE = "upgrade_candidate"
    NEEDS_RECONCILE = "needs_reconcile"
    STALE_BOOKER_LEASE = "stale_booker_lease"
    CADENCE = "cadence"
    STALE_SNAPSHOT = "stale_snapshot"


def group_rows_for_search(rows: Sequence[EventRow]) -> dict[SearchGroupKey, list[EventRow]]:
    """Group rows by (course, date, party_size). The group's search request uses the UNION of
    member windows; each row re-ranks with its own window afterwards."""
    raise NotImplementedError(_MU10)


def needs_login(
    row: EventRow,
    *,
    group_slots: Sequence[TeeTimeSlot],
    snapshot: ReservationSnapshot | None,
    run_index: int,
    now: datetime,
) -> LoginReason | None:
    """Why this row's account must log in this run, or None (no ForeUP login). Pure; §7.1 step 3
    lists the reasons in priority order."""
    raise NotImplementedError(_MU10)


def is_owned(
    reservation: ExistingReservation,
    *,
    row: RequestRow,
    owned: Sequence[OwnedBooking],
) -> bool:
    """Ownership predicate fed to ``WatchOrchestrator(reconcile_eligible=...)`` (engine hook E5)
    and to adoption (§7.6): True iff the raw id is in the ledger (states held / held_extra), OR
    ``row.needs_reconcile`` and the tee time EXACTLY matches a slot the recorder logged as
    UNCERTAIN (§4.6; stricter than "in window"). A dry-run environment passes
    ``lambda _: False`` instead (§7.8)."""
    raise NotImplementedError(_MU10)


class MissingBookingVerdict(StrEnum):
    """Meaning of a BOOKED row's reservation missing from the latest TRUSTED snapshot (§7.5)."""

    NOT_YET = "not_yet"  # fewer than VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS trusted misses
    BOT_CAUSED = "bot_caused"  # upgrade marker set, or ledgered cancelled_upgrade/extra
    ADOPT_REPLACEMENT = "adopt_replacement"  # a same-(date, party) reservation exists
    EXTERNAL_CANCEL = "external_cancel"  # the expected common case: mark, notify, never re-book


def classify_missing_booking(
    row: RequestRow,
    *,
    snapshots: Sequence[ReservationSnapshot],
    owned: Sequence[OwnedBooking],
) -> MissingBookingVerdict:
    """Pure (round-1 M2 exclusions + operator decision Q7). ``snapshots`` are newest-first; only
    trusted ones count. BOT_CAUSED -> PENDING + needs_reconcile; EXTERNAL_CANCEL ->
    CANCELLED(external) + slot freed + email."""
    raise NotImplementedError(_MU10)


class SearchSnapshotAdapter:
    """``CourseAdapter`` proxy that serves ``search()`` from the run's shared group result and
    delegates everything else live to ``inner``. Base class only: build it with
    ``make_search_snapshot_adapter``.

    Capability fidelity (round-1 SF1): on Python >= 3.12 ``runtime_checkable`` ``isinstance`` uses
    ``inspect.getattr_static``, so ``__getattr__`` forwarding does NOT make
    ``isinstance(proxy, ReservationCacheRefreshable)`` true (verified on 3.14.7). The engine's
    reguard would then silently use the idempotent ``authenticate()`` and could double-book.
    The factory therefore returns a CONCRETE subclass per inner capability set, which DEFINES
    exactly ``inner``'s opt-in members (``refresh_reservations`` / ``is_authenticated`` /
    ``snapshot_trusted``) and nothing more. ``capabilities`` is forwarded unchanged. Pinned by
    ``test_snapshot_proxy_capabilities_mirror_inner`` (MU-10).
    """

    course_id: CourseId
    capabilities: AdapterCapabilities

    def __init__(self, *, inner: CourseAdapter, slots: Sequence[TeeTimeSlot]) -> None:
        raise NotImplementedError(_MU10)

    async def authenticate(self, creds: CourseCredentials) -> None:
        raise NotImplementedError(_MU10)

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        raise NotImplementedError(_MU10)

    async def prepare_book(
        self, slot: TeeTimeSlot | None, request: BookingRequest, *, count: int = 1
    ) -> None:
        raise NotImplementedError(_MU10)

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        raise NotImplementedError(_MU10)

    async def list_reservations(self) -> list[ExistingReservation]:
        raise NotImplementedError(_MU10)

    async def cancel_reservation(self, confirmation_code: str) -> None:
        raise NotImplementedError(_MU10)

    async def aclose(self) -> None:
        raise NotImplementedError(_MU10)


def make_search_snapshot_adapter(
    inner: CourseAdapter, *, slots: Sequence[TeeTimeSlot]
) -> SearchSnapshotAdapter:
    """Return a ``SearchSnapshotAdapter`` whose CONCRETE class mirrors ``inner``'s opt-in capability
    members (see the class docstring). ``inner`` is normally a ``tenant.recording`` recorder."""
    raise NotImplementedError(_MU10)
