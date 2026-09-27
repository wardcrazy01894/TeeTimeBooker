"""MU-16b (MULTIUSER_PLAN §11 steps 6-7): adopt TOML-era reservations as OWNED bookings.

At the prod cutover the single-user TOML bot already holds the coming weekend's tee times. The
tenant watcher would adopt them UNOWNED (§7.6), which is safe but means the bot never upgrades
them. The operator instead confirms, on screen, that these reservations were made by the bot, and
the seed records them as ``adopted_owned`` ledger entries.

Two halves:

- ``plan_adoptions`` (pure): from the account's rows, a TRUSTED persisted snapshot and the
  ledger, decide per row what to record. A reservation is adopted only for the same course-local
  date and party size, and only when its tee time falls inside one of the row's options (the
  best-ranked option wins, then the earliest time). An untrusted snapshot plans nothing (§7.5).
- ``apply_adoptions``: writes each plan under the row's lease with the fingerprint taken at plan
  time (M5), so a row the watcher or booker changed in between is skipped, never overwritten.

Kinds:

- ``BOOK``: a PENDING row -> BOOKED on the reservation, ledgered ``adopted_owned``.
- ``LEDGER``: a BOOKED row whose reservation is live but not ledgered (the watcher adopted it
  unowned) -> the ledger entry only; the row is unchanged.
- ``REPOINT``: a BOOKED row whose reservation vanished while a new in-window one exists for the
  same date and party (the TOML watcher upgraded between seed and flip, SF7) -> BOOKED on the new
  one; the old ledger entry becomes ``cancelled_upgrade`` so its disappearance is never read as an
  external cancel.

The writes use ``Actor.WATCHER``: adoption is a watcher edge (pending -> booked, booked ->
booked), and the seed is a one-off, operator-confirmed run of that same adoption.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..core.clock import Clock
from ..core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingResult,
    SlotId,
    TeeTimeSlot,
)
from .models import (
    Actor,
    BookingSource,
    BookingState,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    ReservationSnapshot,
    RowFingerprint,
    RowId,
    RowStatus,
    SnapshotEntry,
    achieved_rank,
)
from .store import RowOutcome, TenantStore

log = logging.getLogger(__name__)

_LIVE = frozenset({BookingState.HELD, BookingState.HELD_EXTRA})
_LEASE = timedelta(seconds=60)
_HOLES = 18


class AdoptionKind(StrEnum):
    BOOK = "book"
    LEDGER = "ledger"
    REPOINT = "repoint"


@dataclass(frozen=True, slots=True)
class Adoption:
    row: RequestRow
    raw_id: str
    tee_time: datetime
    kind: AdoptionKind
    replaces: str | None = None  # REPOINT: the vanished reservation's raw id


@dataclass(frozen=True, slots=True)
class SeedReport:
    adopted: tuple[RowId, ...]
    failed: tuple[RowId, ...]


def _matches(row: RequestRow, snapshot: ReservationSnapshot) -> list[tuple[int, SnapshotEntry]]:
    """In-window reservations for the row's date and party, best rank first, then earliest."""
    zone = ZoneInfo(row.timezone)
    found: list[tuple[int, SnapshotEntry]] = []
    for entry in snapshot.entries:
        local = entry.tee_time.astimezone(zone)
        if local.date() != row.target_date or entry.party_size != row.party_size:
            continue
        rank = achieved_rank(row.options, local.time().replace(tzinfo=None))
        if rank is not None:
            found.append((rank, entry))
    return sorted(found, key=lambda m: (m[0], m[1].tee_time))


def plan_adoptions(
    rows: Sequence[RequestRow], snapshot: ReservationSnapshot, *, owned: Sequence[OwnedBooking]
) -> list[Adoption]:
    """What to record for each row (see the module docstring). Pure; plans nothing from an
    untrusted snapshot or for a row flagged ``needs_reconcile`` (its outcome is ambiguous and the
    watcher's reconcile path owns it)."""
    if not snapshot.trusted:
        return []
    live = {o.raw_reservation_id for o in owned if o.state in _LIVE}
    held_ids = {e.raw_id for e in snapshot.entries}
    plans: list[Adoption] = []
    for row in rows:
        if row.course_account_id != snapshot.course_account_id or row.needs_reconcile:
            continue
        matches = _matches(row, snapshot)
        if row.status is RowStatus.PENDING:
            free = [e for _, e in matches if e.raw_id not in live]
            if free:
                plans.append(Adoption(row, free[0].raw_id, free[0].tee_time, AdoptionKind.BOOK))
        elif row.status is RowStatus.BOOKED and row.booked_raw_id is not None:
            raw = row.booked_raw_id
            if raw in held_ids:
                entry = next((e for _, e in matches if e.raw_id == raw), None)
                if entry is not None and raw not in live:
                    plans.append(Adoption(row, raw, entry.tee_time, AdoptionKind.LEDGER))
            else:
                others = [e for _, e in matches if e.raw_id not in live]
                if others:
                    plans.append(
                        Adoption(
                            row,
                            others[0].raw_id,
                            others[0].tee_time,
                            AdoptionKind.REPOINT,
                            replaces=raw,
                        )
                    )
    return plans


def _booking(row: RequestRow, raw_id: str, tee: datetime) -> OwnedBooking:
    return OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=row.course_account_id,
        course_id=row.course_id,
        target_date=row.target_date,
        raw_reservation_id=raw_id,
        tee_time=tee,
        party_size=row.party_size,
        source=BookingSource.ADOPTED_OWNED,
        state=BookingState.HELD,
    )


def _result(row: RequestRow, raw_id: str, tee: datetime) -> BookingResult:
    slot = TeeTimeSlot(
        course_id=row.course_id,
        slot_id=SlotId(raw_id),
        tee_time=tee.astimezone(ZoneInfo(row.timezone)),
        holes=_HOLES,
        available_spots=row.party_size,
        price_per_player=Decimal("0"),
        cart_included=False,
    )
    return BookingResult(
        request_id=row.request_id,
        outcome=BookingOutcome.ALREADY_BOOKED,
        course_id=row.course_id,
        slot=slot,
        confirmation_code=f"{MANAGED_BOOKING_TAG}{raw_id}",
        booked_at=None,
        attempts=0,
    )


def _outcome(plan: Adoption, *, owner: str, now: datetime) -> RowOutcome:
    row = plan.row
    base = RowOutcome(
        row_id=row.id,
        course_account_id=row.course_account_id,
        target_date=row.target_date,
        actor=Actor.WATCHER,
        to_status=None,
        last_outcome=f"seed:adopted_owned_{plan.kind.value}",
        at=now,
        booking=_booking(row, plan.raw_id, plan.tee_time),
        release_lease_owner=owner,
    )
    if plan.kind is AdoptionKind.LEDGER:
        return base
    return RowOutcome(
        **{
            **{f: getattr(base, f) for f in base.__dataclass_fields__},
            "to_status": RowStatus.BOOKED,
            "result": _result(row, plan.raw_id, plan.tee_time),
            "cancelled_upgrade_raw_id": plan.replaces,
        }
    )


async def apply_adoptions(
    store: TenantStore, plans: Sequence[Adoption], *, clock: Clock, owner: str | None = None
) -> SeedReport:
    """Write each plan under the row's lease with its plan-time fingerprint. A row whose lease
    cannot be taken (another writer holds it, or it changed since the plan) is reported failed and
    left untouched; a refused write is reported failed. Never raises for one row."""
    owner = owner or f"seed:{uuid4().hex[:8]}"
    adopted: list[RowId] = []
    failed: list[RowId] = []
    for plan in plans:
        row = plan.row
        now = clock.now_utc()
        fingerprint = RowFingerprint(row.status, row.version, row.booked_raw_id)
        acquired = await store.acquire_row_lease(
            row.id, owner=owner, until=now + _LEASE, now=now, expected=fingerprint
        )
        if not acquired:
            log.warning("tenant-seed: row %s changed or is leased; not adopted", row.id)
            failed.append(row.id)
            continue
        try:
            await store.record_outcomes([_outcome(plan, owner=owner, now=now)])
        except Exception as exc:  # one refused row never stops the others
            log.error("tenant-seed: row %s: write refused (%s)", row.id, type(exc).__name__)
            await store.release_row_lease(row.id, owner=owner)
            failed.append(row.id)
            continue
        log.info("tenant-seed: row %s adopted %s (%s)", row.id, plan.raw_id, plan.kind.value)
        adopted.append(row.id)
    return SeedReport(adopted=tuple(adopted), failed=tuple(failed))
