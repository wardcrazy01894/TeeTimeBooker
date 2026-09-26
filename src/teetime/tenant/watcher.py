"""Tenant-watcher helpers (MULTIUSER_PLAN §7). Pure decision functions + one adapter proxy.

STUB — implemented in MULTIUSER_PLAN MU-10a (pure decisions + proxy factory).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from ..core.adapter import AdapterCapabilities, CourseAdapter
from ..core.models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    Player,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from ..core.slot_utils import midpoint_distance_minutes, rank_slots_for_request
from .models import (
    BookingState,
    EventRow,
    OwnedBooking,
    RequestRow,
    ReservationSnapshot,
    RowStatus,
)

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
    member windows; each row re-ranks with its own window afterwards. Input order is preserved
    inside each group; PENDING and BOOKED rows share a group (one search serves both)."""
    groups: dict[SearchGroupKey, list[EventRow]] = {}
    for event_row in rows:
        r = event_row.row
        key = SearchGroupKey(r.course_id, r.target_date, r.party_size)
        groups.setdefault(key, []).append(event_row)
    return groups


# --- pure helpers shared by the decisions ------------------------------------------------


_LIVE_LEDGER_STATES: frozenset[BookingState] = frozenset(
    {BookingState.HELD, BookingState.HELD_EXTRA}
)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be tz-aware")


def _local(value: datetime, row: RequestRow) -> datetime:
    """``value`` in the row's COURSE timezone: ``slot_utils`` reads ``.time()`` directly, so a
    stored UTC instant must be converted before any wall-clock comparison."""
    return value.astimezone(ZoneInfo(row.timezone))


def _ranking_request(row: RequestRow) -> BookingRequest:
    """The row as a ``BookingRequest`` for ``rank_slots_for_request`` ONLY (never sent to an
    adapter): synthesized players sized to the party, the row's single window, holes=0 (any) so
    the pure decision is permissive — a wasted login is harmless, a missed opportunity is not."""
    return BookingRequest(
        request_id=row.request_id,
        target_dates=(row.target_date,),
        time_windows=(TimeWindow(earliest=row.window_earliest, latest=row.window_latest),),
        players=tuple(
            Player(first_name=f"p{i}", last_name="tenant", email="") for i in range(row.party_size)
        ),
        course_preferences=(row.course_id,),
        holes=0,
    )


def _row_booking_owned(row: RequestRow, owned: Sequence[OwnedBooking]) -> bool:
    """§7.6: the row's held reservation is OURS iff its raw id is ledgered held / held_extra."""
    return row.booked_raw_id is not None and any(
        o.raw_reservation_id == row.booked_raw_id and o.state in _LIVE_LEDGER_STATES for o in owned
    )


def _lease_is_stale(row: RequestRow, *, now: datetime) -> bool:
    """An EXPIRED but never-released lease: the holder died mid-act, so its outcome may never
    have been written. An unexpired lease means another process is acting (no login)."""
    return (
        row.lease_owner is not None
        and row.lease_expires_at is not None
        and row.lease_expires_at <= now
    )


def _held_slot(row: RequestRow, tee_time: datetime) -> TeeTimeSlot:
    """A synthesized slot for the held booking, for the midpoint comparison only (mirrors
    ``WatchOrchestrator._synthesize_managed_booking``)."""
    return TeeTimeSlot(
        course_id=row.course_id,
        slot_id=SlotId(row.booked_raw_id or "held"),
        tee_time=_local(tee_time, row),
        holes=18,
        available_spots=row.party_size,
        price_per_player=Decimal("0"),
        cart_included=False,
    )


def _has_upgrade_candidate(row: RequestRow, ranked: Sequence[TeeTimeSlot]) -> bool:
    """The ``UpgradeOrchestrator`` within-window rule: a candidate STRICTLY closer to the row
    window's midpoint than the held tee time. Ties never upgrade (the cancel-before-book
    no-booking window is not worth an equal slot). Tenant rows carry ONE window, so the
    higher-tier leg of that rule has nothing to compare."""
    if row.booked_tee_time is None:
        return False
    window = TimeWindow(earliest=row.window_earliest, latest=row.window_latest)
    held = midpoint_distance_minutes(_held_slot(row, row.booked_tee_time), window)
    return any(midpoint_distance_minutes(c, window) < held for c in ranked)


def needs_login(
    row: EventRow,
    *,
    group_slots: Sequence[TeeTimeSlot],
    snapshot: ReservationSnapshot | None,
    run_index: int,
    now: datetime,
    owned: Sequence[OwnedBooking] = (),
    cadence: int = RECONCILE_EVERY_N_RUNS,
) -> LoginReason | None:
    """Why this row's account must log in this run, or None (no ForeUP login). Pure; §7.1 step 3
    lists the reasons in priority order. ``owned`` is the row's (account, date) ledger (an
    upgrade candidate counts only for an OWNED booking, §7.6); ``cadence`` is the reconcile
    period in runs (``RECONCILE_EVERY_N_RUNS``).

    Order (first match wins): BOOKABLE_SLOT (pending + an in-window bookable slot),
    UPGRADE_CANDIDATE (booked + OWNED + a strictly-better slot), NEEDS_RECONCILE,
    STALE_BOOKER_LEASE (an expired, unreleased lease), CADENCE
    (``(account_id.int + run_index) % cadence == 0`` — the UUID integer, never ``hash()``, which
    is salted per process), STALE_SNAPSHOT (a booked row whose account snapshot is missing or
    older than ``MAX_BOOKED_SNAPSHOT_AGE_S``)."""
    _require_aware(now, "now")
    if cadence < 1:
        raise ValueError(f"cadence must be >= 1, got {cadence}")
    r, acct = row.row, row.account
    if snapshot is not None and snapshot.course_account_id != acct.id:
        raise ValueError("snapshot belongs to another account")
    ranked = rank_slots_for_request(list(group_slots), _ranking_request(r))
    booked = r.status is RowStatus.BOOKED
    snapshot_stale = snapshot is None or now - snapshot.observed_at > timedelta(
        seconds=MAX_BOOKED_SNAPSHOT_AGE_S
    )
    # §7.1 step 3, in priority order; the first true condition names the reason.
    legs: tuple[tuple[bool, LoginReason], ...] = (
        (r.status is RowStatus.PENDING and bool(ranked), LoginReason.BOOKABLE_SLOT),
        (
            booked and _row_booking_owned(r, owned) and _has_upgrade_candidate(r, ranked),
            LoginReason.UPGRADE_CANDIDATE,
        ),
        (r.needs_reconcile, LoginReason.NEEDS_RECONCILE),
        (_lease_is_stale(r, now=now), LoginReason.STALE_BOOKER_LEASE),
        ((acct.id.int + run_index) % cadence == 0, LoginReason.CADENCE),
        (booked and snapshot_stale, LoginReason.STALE_SNAPSHOT),
    )
    return next((reason for hit, reason in legs if hit), None)


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
