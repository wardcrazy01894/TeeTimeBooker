"""Tenant-watcher helpers (MULTIUSER_PLAN §7, MU-10a). Pure decision functions + one adapter proxy.

No I/O, no store, no adapter calls in the decisions: every function takes plain data and returns
a decision. The runner wiring (MU-10b) is the only caller.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast
from zoneinfo import ZoneInfo

from ..core.adapter import (
    AdapterCapabilities,
    AuthStateReportable,
    BlindPostCapable,
    CourseAdapter,
    ReservationCacheRefreshable,
    ReservationSnapshotHealth,
)
from ..core.models import (
    MANAGED_BOOKING_TAG,
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
    achieved_rank,
    options_time_windows,
)

# Per-account reconcile cadence: log in when (hash(account_id) + run_index) % N == 0, i.e.
# ~hourly per account at a 10-min cron, spread across runs, no stored state (§7.1).
RECONCILE_EVERY_N_RUNS = 6
# Backstop: an account holding a BOOKED row is re-listed if its snapshot is older than this.
MAX_BOOKED_SNAPSHOT_AGE_S = 90 * 60
# Vanish inference needs this many consecutive TRUSTED snapshots without the reservation (§7.5),
# taken at least VANISH_MIN_SNAPSHOT_GAP apart (two logins in one run are ONE observation).
VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS = 2
VANISH_MIN_SNAPSHOT_GAP = timedelta(minutes=10)


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
    adapter): synthesized players sized to the party and the row's single window. ``holes=0``
    (any) is the only sensible value: ``RequestRow`` has no ``holes`` field, so there is nothing
    to filter on — and it keeps the pure decision permissive (a wasted login is harmless, a
    missed opportunity is not)."""
    return BookingRequest(
        request_id=row.request_id,
        target_dates=(row.target_date,),
        time_windows=options_time_windows(row.options),
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
    """The ``UpgradeOrchestrator`` rule over the row's ranked options (§16.2): a candidate in a
    BETTER-ranked option always beats the held tee time (the higher-tier leg); one in the SAME
    option must be STRICTLY closer to that option's midpoint (ties never upgrade: the
    cancel-before-book no-booking window is not worth an equal slot); a worse option never does.
    Ranks are resolved first-match in rank order, exactly as the engine ranks windows."""
    if row.booked_tee_time is None:
        return False
    held_slot = _held_slot(row, row.booked_tee_time)
    held_rank = achieved_rank(row.options, held_slot.tee_time.time())
    for candidate in ranked:
        rank = achieved_rank(row.options, candidate.tee_time.time())
        if rank is None:
            continue
        if held_rank is None or rank < held_rank:
            return True
        if rank == held_rank:
            (option,) = [o for o in row.options if o.rank == rank]
            window = TimeWindow(earliest=option.earliest, latest=option.latest)
            if midpoint_distance_minutes(candidate, window) < midpoint_distance_minutes(
                held_slot, window
            ):
                return True
    return False


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


class Ownership(StrEnum):
    """Who made a live reservation (§7.6). Only OWNED / ADOPTED_RECONCILE are the bot's."""

    OWNED = "owned"  # raw id ledgered held / held_extra
    ADOPTED_RECONCILE = "adopted_reconcile"  # needs_reconcile + exact recorded UNCERTAIN slot
    UNOWNED = "unowned"  # manual: never upgraded, never a reconcile-cancel candidate


def ownership_of(
    reservation: ExistingReservation,
    *,
    row: RequestRow,
    owned: Sequence[OwnedBooking],
    uncertain_tee_times: Sequence[datetime] = (),
) -> Ownership:
    """§7.6: OWNED iff the raw id is ledgered held / held_extra; ADOPTED_RECONCILE iff
    ``row.needs_reconcile`` and the tee time EXACTLY matches (same instant, same party) one of
    ``uncertain_tee_times`` — the slots the recorder logged as UNCERTAIN (§4.6; stricter than
    "in window"), which the runner passes along with ``row.booked_tee_time`` (the durable carrier
    of an UNCERTAIN slot across runs); else UNOWNED. Fail-safe: nothing passed -> UNOWNED.

    ``owned`` must already be scoped to the row's (account, date) — the ``list_owned_bookings
    (account_id, target_date=...)`` result — so a raw id from another account or date can never
    match; this function does not re-check either."""
    raw = reservation.confirmation_code.removeprefix(MANAGED_BOOKING_TAG)
    if any(o.raw_reservation_id == raw and o.state in _LIVE_LEDGER_STATES for o in owned):
        return Ownership.OWNED
    # Aware datetimes compare by INSTANT, so a UTC-stored slot matches a course-local one.
    exact = row.needs_reconcile and any(reservation.tee_time == t for t in uncertain_tee_times)
    if exact and reservation.party_size == row.party_size:
        return Ownership.ADOPTED_RECONCILE
    return Ownership.UNOWNED


def is_owned(
    reservation: ExistingReservation,
    *,
    row: RequestRow,
    owned: Sequence[OwnedBooking],
    uncertain_tee_times: Sequence[datetime] = (),
) -> bool:
    """Ownership predicate fed to ``WatchOrchestrator(reconcile_eligible=...)`` (engine hook E5)
    and to adoption (§7.6): ``ownership_of(...) is not Ownership.UNOWNED``. A dry-run
    environment passes ``lambda _: False`` instead (§7.8)."""
    return (
        ownership_of(reservation, row=row, owned=owned, uncertain_tee_times=uncertain_tee_times)
        is not Ownership.UNOWNED
    )


def upgrade_allowed(
    row: RequestRow,
    reservation: ExistingReservation,
    *,
    owned: Sequence[OwnedBooking],
    uncertain_tee_times: Sequence[datetime] = (),
) -> bool:
    """The ownership gate MU-10 MUST apply before ``_try_upgrade`` (E5 does not guard the
    upgrade, §7.6): True iff ``row`` is BOOKED and ``reservation`` is owned per
    ``ownership_of``. An unowned (manual) match never reaches the engine's upgrade."""
    return row.status is RowStatus.BOOKED and is_owned(
        reservation, row=row, owned=owned, uncertain_tee_times=uncertain_tee_times
    )


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
    """Pure (round-1 M2 exclusions + operator decision Q7). Only TRUSTED snapshots count (an
    untrusted one is neither a miss nor a sighting); they are ordered by ``observed_at`` here, so
    input order does not matter. BOT_CAUSED -> PENDING + needs_reconcile; EXTERNAL_CANCEL ->
    CANCELLED(external) + slot freed + email. Raises ``ValueError`` for a non-BOOKED row or a
    snapshot of another account; a BOOKED row with no raw id is NOT_YET (adoption resolves it)."""
    if row.status is not RowStatus.BOOKED:
        raise ValueError(f"classify_missing_booking needs a BOOKED row, got {row.status}")
    if any(s.course_account_id != row.course_account_id for s in snapshots):
        raise ValueError("snapshot belongs to another account")
    raw = row.booked_raw_id
    if raw is None:
        return MissingBookingVerdict.NOT_YET
    newest = _newest_trusted_miss(raw, snapshots)
    if newest is None:
        return MissingBookingVerdict.NOT_YET
    cancelled_by_us = any(
        o.raw_reservation_id == raw and o.state in _CANCELLED_BY_US for o in owned
    )
    replaced = any(
        e.raw_id != raw
        and e.party_size == row.party_size
        and _local(e.tee_time, row).date() == row.target_date
        for e in newest.entries
    )
    # M2 exclusions in §7.5 order: the marker / our own cancel first (the reconcile, not the
    # vanish path, decides what a replacement means then), then a same-(date, party) replacement.
    legs: tuple[tuple[bool, MissingBookingVerdict], ...] = (
        (row.upgrade_started_at is not None or cancelled_by_us, MissingBookingVerdict.BOT_CAUSED),
        (replaced, MissingBookingVerdict.ADOPT_REPLACEMENT),
    )
    return next((verdict for hit, verdict in legs if hit), MissingBookingVerdict.EXTERNAL_CANCEL)


# Ledger states that mean WE removed the reservation (§7.5 M2). ``cancelled_user`` is deliberately
# absent: a web cancel writes the row CANCELLED(user) in the same batch, so a BOOKED row can never
# carry one in a consistent store; if it did, EXTERNAL_CANCEL (no re-book) is the safe direction,
# whereas BOT_CAUSED would re-book what the user just cancelled.
_CANCELLED_BY_US: frozenset[BookingState] = frozenset(
    {BookingState.CANCELLED_UPGRADE, BookingState.CANCELLED_EXTRA}
)


def _newest_trusted_miss(
    raw: str, snapshots: Sequence[ReservationSnapshot]
) -> ReservationSnapshot | None:
    """The newest TRUSTED snapshot iff the last ``VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS`` trusted
    snapshots all miss ``raw`` and span at least ``VANISH_MIN_SNAPSHOT_GAP``; else None. Untrusted
    snapshots are skipped entirely (neither a miss nor a sighting)."""
    trusted = sorted((s for s in snapshots if s.trusted), key=lambda s: s.observed_at, reverse=True)
    recent = trusted[:VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS]
    if len(recent) < VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS:
        return None
    seen = any(e.raw_id == raw for s in recent for e in s.entries)
    too_close = recent[0].observed_at - recent[-1].observed_at < VANISH_MIN_SNAPSHOT_GAP
    return None if seen or too_close else recent[0]


class WatchAction(StrEnum):
    """What the tenant watcher is about to do, for the §7.8 dry-run gate."""

    LOGIN = "login"  # read-only authenticate + list_reservations
    PERSIST_SNAPSHOT = "persist_snapshot"
    ADOPT = "adopt"  # DB-only: a pending row becomes booked from a trusted snapshot
    BOOK = "book"  # the engine's own dry_run suppresses the POST
    UPGRADE = "upgrade"
    RECONCILE_CANCEL = "reconcile_cancel"
    MARK_CANCELLED_EXTERNAL = "mark_cancelled_external"


def dry_run_gate(*, dry_run: bool, action: WatchAction) -> bool:
    """§7.8 (SF2): True iff ``action`` may proceed. In a dry-run environment the watcher never
    reconcile-cancels, never upgrades and never writes booked -> cancelled(external); a
    read-only login, snapshot persistence and every DB-only action stay enabled."""
    return not (dry_run and action in _DRY_RUN_FORBIDDEN)


_DRY_RUN_FORBIDDEN: frozenset[WatchAction] = frozenset(
    {WatchAction.UPGRADE, WatchAction.RECONCILE_CANCEL, WatchAction.MARK_CANCELLED_EXTERNAL}
)


class SearchSnapshotAdapter:
    """``CourseAdapter`` proxy that serves ``search()`` from the run's shared group result and
    delegates everything else live to ``inner``. This base class IS the zero-capability variant;
    always build through ``make_search_snapshot_adapter``, which picks the variant that mirrors
    ``inner``.

    Capability fidelity (round-1 SF1): on Python >= 3.12 ``runtime_checkable`` ``isinstance`` uses
    ``inspect.getattr_static``, so ``__getattr__`` forwarding does NOT make
    ``isinstance(proxy, ReservationCacheRefreshable)`` true (verified on 3.14.7). The engine's
    reguard would then silently use the idempotent ``authenticate()`` and could double-book.
    The factory therefore returns a CONCRETE subclass per inner capability set, which DEFINES
    exactly ``inner``'s opt-in members (``refresh_reservations`` / ``is_authenticated`` /
    ``snapshot_trusted``, plus the ``BlindPostCapable`` pair the orchestrator only ``cast``s to)
    and nothing more. ``capabilities`` is forwarded unchanged. Pinned by
    ``test_snapshot_proxy_capabilities_mirror_inner`` (MU-10a).
    """

    course_id: CourseId
    capabilities: AdapterCapabilities

    def __init__(self, *, inner: CourseAdapter, slots: Sequence[TeeTimeSlot]) -> None:
        self._inner = inner
        self._slots: tuple[TeeTimeSlot, ...] = tuple(slots)
        self.course_id = inner.course_id
        self.capabilities = inner.capabilities

    async def authenticate(self, creds: CourseCredentials) -> None:
        await self._inner.authenticate(creds)

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        """The shared group result, as a FRESH list per call (the engine may mutate its copy).
        Never touches ``inner`` — the run's one search per group already happened."""
        return list(self._slots)

    async def prepare_book(
        self, slot: TeeTimeSlot | None, request: BookingRequest, *, count: int = 1
    ) -> None:
        await self._inner.prepare_book(slot, request, count=count)

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        return await self._inner.book(slot, request)

    async def list_reservations(self) -> list[ExistingReservation]:
        return await self._inner.list_reservations()

    async def cancel_reservation(self, confirmation_code: str) -> None:
        await self._inner.cancel_reservation(confirmation_code)

    async def aclose(self) -> None:
        await self._inner.aclose()


# --- opt-in member mixins: each DEFINES the member statically (no __getattr__) ------------------


class _RefreshableProxy:
    _inner: CourseAdapter

    async def refresh_reservations(self, creds: CourseCredentials) -> None:
        await cast(ReservationCacheRefreshable, self._inner).refresh_reservations(creds)


class _AuthStateProxy:
    _inner: CourseAdapter

    @property
    def is_authenticated(self) -> bool:
        return cast(AuthStateReportable, self._inner).is_authenticated


class _SnapshotHealthProxy:
    _inner: CourseAdapter

    @property
    def snapshot_trusted(self) -> bool:
        return cast(ReservationSnapshotHealth, self._inner).snapshot_trusted


class _BlindProxy:
    """The ``BlindPostCapable`` pair as pure pass-throughs. The orchestrator ``cast``s (never
    ``isinstance``-checks) for these, so a missing method fails late and silently — mirroring
    them keeps the proxy honest even though the watch path never calls them."""

    _inner: CourseAdapter

    def captcha_pool_size(self) -> int:
        return cast(BlindPostCapable, self._inner).captcha_pool_size()

    def synthesize_blind_slots(
        self, request: BookingRequest, target_date: date, *, max_count: int
    ) -> list[TeeTimeSlot]:
        return cast(BlindPostCapable, self._inner).synthesize_blind_slots(
            request, target_date, max_count=max_count
        )


# One concrete class per capability set. Suffix letters: R refresh_reservations,
# A is_authenticated, S snapshot_trusted, B the blind pair. The base is the empty set.
class _SnapR(_RefreshableProxy, SearchSnapshotAdapter): ...


class _SnapA(_AuthStateProxy, SearchSnapshotAdapter): ...


class _SnapS(_SnapshotHealthProxy, SearchSnapshotAdapter): ...


class _SnapB(_BlindProxy, SearchSnapshotAdapter): ...


class _SnapRA(_RefreshableProxy, _AuthStateProxy, SearchSnapshotAdapter): ...


class _SnapRS(_RefreshableProxy, _SnapshotHealthProxy, SearchSnapshotAdapter): ...


class _SnapRB(_RefreshableProxy, _BlindProxy, SearchSnapshotAdapter): ...


class _SnapAS(_AuthStateProxy, _SnapshotHealthProxy, SearchSnapshotAdapter): ...


class _SnapAB(_AuthStateProxy, _BlindProxy, SearchSnapshotAdapter): ...


class _SnapSB(_SnapshotHealthProxy, _BlindProxy, SearchSnapshotAdapter): ...


class _SnapRAS(_RefreshableProxy, _AuthStateProxy, _SnapshotHealthProxy, SearchSnapshotAdapter): ...


class _SnapRAB(_RefreshableProxy, _AuthStateProxy, _BlindProxy, SearchSnapshotAdapter): ...


class _SnapRSB(_RefreshableProxy, _SnapshotHealthProxy, _BlindProxy, SearchSnapshotAdapter): ...


class _SnapASB(_AuthStateProxy, _SnapshotHealthProxy, _BlindProxy, SearchSnapshotAdapter): ...


class _SnapRASB(
    _RefreshableProxy, _AuthStateProxy, _SnapshotHealthProxy, _BlindProxy, SearchSnapshotAdapter
): ...


# (refresh, auth, health, blind) -> the concrete class that defines exactly those members.
_CapabilityKey = tuple[bool, bool, bool, bool]


def _variant_key(cls: type[SearchSnapshotAdapter]) -> _CapabilityKey:
    return (
        issubclass(cls, _RefreshableProxy),
        issubclass(cls, _AuthStateProxy),
        issubclass(cls, _SnapshotHealthProxy),
        issubclass(cls, _BlindProxy),
    )


_VARIANTS: dict[_CapabilityKey, type[SearchSnapshotAdapter]] = {
    _variant_key(cls): cls
    for cls in (
        SearchSnapshotAdapter,
        _SnapR,
        _SnapA,
        _SnapS,
        _SnapB,
        _SnapRA,
        _SnapRS,
        _SnapRB,
        _SnapAS,
        _SnapAB,
        _SnapSB,
        _SnapRAS,
        _SnapRAB,
        _SnapRSB,
        _SnapASB,
        _SnapRASB,
    )
}
# 2 ** 4 capability sets: every combination of the four mixins must map to its own class.
_VARIANT_COUNT = 2 ** len(_variant_key(SearchSnapshotAdapter))
if len(_VARIANTS) != _VARIANT_COUNT:  # pragma: no cover - import-time guard vs a duplicated variant
    raise RuntimeError(
        f"SearchSnapshotAdapter variants must cover {_VARIANT_COUNT} sets, got {len(_VARIANTS)}"
    )

_MISSING = object()


def _defines(obj: object, name: str) -> bool:
    """``getattr_static`` presence — the same reading ``runtime_checkable`` makes on >= 3.12."""
    return inspect.getattr_static(obj, name, _MISSING) is not _MISSING


def make_search_snapshot_adapter(
    inner: CourseAdapter, *, slots: Sequence[TeeTimeSlot]
) -> SearchSnapshotAdapter:
    """Return a ``SearchSnapshotAdapter`` whose CONCRETE class mirrors ``inner``'s opt-in capability
    members (see the class docstring). ``inner`` is normally a ``tenant.recording`` recorder.

    The three Protocols are read with ``isinstance`` (``runtime_checkable``); the blind pair by
    static presence of BOTH methods rather than ``capabilities.blind_post`` — the contract is
    "define exactly what inner defines", and e.g. ``FakeAdapter`` carries the methods even when
    its flag is False."""
    key: _CapabilityKey = (
        isinstance(inner, ReservationCacheRefreshable),
        isinstance(inner, AuthStateReportable),
        isinstance(inner, ReservationSnapshotHealth),
        _defines(inner, "captcha_pool_size") and _defines(inner, "synthesize_blind_slots"),
    )
    return _VARIANTS[key](inner=inner, slots=slots)
