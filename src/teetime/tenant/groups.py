"""Group rules for ranked preferences (MULTIUSER_PLAN §16.3/§16.4, MU-R2). Pure: no I/O.

A group is one user's rows for one date across courses, sharing ``group_id``. The invariant is
at most one BOOKED, OWNED row per group, and it is the best achieved rank.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from ..core.clock import Clock
from .models import (
    GROUP_DOWNGRADE_REASON,
    Actor,
    BookingState,
    CourseAccountId,
    OwnedBooking,
    RankedWindow,
    RequestRow,
    RowFingerprint,
    RowId,
    RowStatus,
    achieved_rank,
)
from .store import RowOutcome

log = logging.getLogger(__name__)

# A booked row whose tee time sits in none of its options (e.g. an adopted manual booking)
# ranks below every real option.
_UNRANKED = 1_000_000


def booked_rank(row: RequestRow) -> int | None:
    """The rank a BOOKED row achieved: first match of its COURSE-LOCAL tee time over its options
    in rank order (§16.2, review round 1 MF4). Computed, not stored: a booked row's options never
    change (rule edits never touch BOOKED rows), so this is stable."""
    if row.status is not RowStatus.BOOKED or row.booked_tee_time is None:
        return None
    local = row.booked_tee_time.astimezone(ZoneInfo(row.timezone))
    return achieved_rank(row.options, local.time())


def _same_group(a: RequestRow, b: RequestRow) -> bool:
    return a.group_id is not None and a.group_id == b.group_id and a.target_date == b.target_date


def _rank_key(row: RequestRow) -> tuple[int, str]:
    rank = booked_rank(row)
    return (_UNRANKED if rank is None else rank, str(row.id))


def group_floor(row: RequestRow, rows: Iterable[RequestRow]) -> tuple[RankedWindow, ...]:
    """The options of ``row`` still worth attempting (§16.3): only those ranked strictly better
    than the best BOOKED row of the same group at ANOTHER account. Empty means do not attempt
    this row at all. A row outside any group keeps all its options."""
    best: int | None = None
    for other in rows:
        if other.id == row.id or other.course_account_id == row.course_account_id:
            continue
        if not _same_group(row, other) or other.status is not RowStatus.BOOKED:
            continue
        rank = booked_rank(other)
        effective = _UNRANKED if rank is None else rank
        best = effective if best is None else min(best, effective)
    if best is None:
        return row.options
    return tuple(o for o in row.options if o.rank < best)


@dataclass(frozen=True, slots=True)
class CollapsePlan:
    """What the §16.4 collapse does for one group: keep one booking, cancel the OWNED worse
    ones, and leave (and tell the user about) the MANUAL worse ones."""

    keep: RequestRow | None
    cancel: tuple[RequestRow, ...]
    leave_manual: tuple[RequestRow, ...]


def plan_collapse(
    group: Sequence[RequestRow], *, owned: Callable[[RequestRow], bool]
) -> CollapsePlan:
    """Keep the BOOKED row with the best achieved rank, owned or not; every other BOOKED row is
    cancelled if the bot owns it and left alone if it is manual (never auto-cancelled, §7.6)."""
    booked = sorted((r for r in group if r.status is RowStatus.BOOKED), key=_rank_key)
    if not booked:
        return CollapsePlan(keep=None, cancel=(), leave_manual=())
    keep, *rest = booked
    return CollapsePlan(
        keep=keep,
        cancel=tuple(r for r in rest if owned(r)),
        leave_manual=tuple(r for r in rest if not owned(r)),
    )


class CollapseStore(Protocol):
    """The ``TenantStore`` calls the collapse makes."""

    async def list_owned_bookings(
        self, account_id: CourseAccountId, *, target_date: date
    ) -> list[OwnedBooking]: ...

    async def acquire_row_lease(
        self,
        row_id: RowId,
        *,
        owner: str,
        until: datetime,
        now: datetime,
        expected: RowFingerprint | None,
    ) -> bool: ...

    async def set_upgrade_marker(
        self, row_id: RowId, *, owner: str, at: datetime, expected: RowFingerprint
    ) -> bool: ...

    async def record_outcomes(self, outcomes: Sequence[RowOutcome]) -> None: ...

    async def release_row_lease(self, row_id: RowId, *, owner: str) -> None: ...


class _Cancels(Protocol):
    async def cancel_reservation(self, confirmation_code: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CollapseReport:
    downgraded: tuple[RowId, ...] = ()
    manual: tuple[RowId, ...] = ()  # left in place and reported (never auto-cancelled)
    skipped: tuple[RowId, ...] = ()  # lease or marker not obtained, or dry-run
    failed: tuple[RowId, ...] = ()  # cancel or write failed: retried by the next watcher run


_HELD = frozenset({BookingState.HELD, BookingState.HELD_EXTRA})


async def collapse_group(
    group: Sequence[RequestRow],
    *,
    store: CollapseStore,
    adapters: Mapping[CourseAccountId, _Cancels],
    actor: Actor,
    owner: str,
    clock: Clock,
    lease_seconds: int = 120,
    dry_run: bool = False,
) -> CollapseReport:
    """§16.4 collapse of ONE group: keep the best booking (``plan_collapse``) and, for every
    worse OWNED booking: lease (fingerprinted) -> upgrade marker -> ``cancel_reservation`` -> ONE
    ``record_outcomes`` (booked -> pending ``group_downgrade``, ledger ``cancelled_group``, marker
    cleared, lease released). The marker is set BEFORE the course call so a crash after the cancel
    lands reads as ``BOT_CAUSED`` on the next run (§7.5), never an external cancel. A failure logs
    CRITICAL and leaves the row BOOKED for the next watcher run to retry. Never raises for one
    row's failure; dry-run cancels nothing (§7.8)."""
    ledgers: dict[tuple[CourseAccountId, date], list[OwnedBooking]] = {}

    async def entry_for(row: RequestRow) -> OwnedBooking | None:
        key = (row.course_account_id, row.target_date)
        if key not in ledgers:
            ledgers[key] = await store.list_owned_bookings(
                row.course_account_id, target_date=row.target_date
            )
        return next(
            (
                e
                for e in ledgers[key]
                if e.raw_reservation_id == row.booked_raw_id and e.state in _HELD
            ),
            None,
        )

    held: dict[RowId, OwnedBooking] = {}
    for row in group:
        if row.status is RowStatus.BOOKED:
            found = await entry_for(row)
            if found is not None:
                held[row.id] = found
    plan = plan_collapse(group, owned=lambda r: r.id in held)
    downgraded: list[RowId] = []
    skipped: list[RowId] = []
    failed: list[RowId] = []
    for row in plan.leave_manual:
        log.warning(
            "group %s: row %s holds a MANUAL worse booking; left alone", row.group_id, row.id
        )
    for row in plan.cancel:
        if dry_run:
            log.info("group %s: dry-run, would cancel row %s's booking", row.group_id, row.id)
            skipped.append(row.id)
            continue
        outcome = await _downgrade(
            row,
            held[row.id],
            store=store,
            adapter=adapters.get(row.course_account_id),
            actor=actor,
            owner=owner,
            clock=clock,
            lease_seconds=lease_seconds,
        )
        {"downgraded": downgraded, "skipped": skipped, "failed": failed}[outcome].append(row.id)
    return CollapseReport(
        downgraded=tuple(downgraded),
        manual=tuple(r.id for r in plan.leave_manual),
        skipped=tuple(skipped),
        failed=tuple(failed),
    )


async def _downgrade(
    row: RequestRow,
    entry: OwnedBooking,
    *,
    store: CollapseStore,
    adapter: _Cancels | None,
    actor: Actor,
    owner: str,
    clock: Clock,
    lease_seconds: int,
) -> str:
    raw = row.booked_raw_id
    if adapter is None or raw is None:
        log.critical(
            "group %s: no adapter/raw id for row %s; cannot collapse", row.group_id, row.id
        )
        return "failed"
    now = clock.now_utc()
    expected = RowFingerprint(status=row.status, version=row.version, booked_raw_id=raw)
    if not await store.acquire_row_lease(
        row.id,
        owner=owner,
        until=now + timedelta(seconds=lease_seconds),
        now=now,
        expected=expected,
    ):
        log.info("group %s: row %s is leased or changed; the watcher retries", row.group_id, row.id)
        return "skipped"
    try:
        if not await store.set_upgrade_marker(row.id, owner=owner, at=now, expected=expected):
            return "skipped"
        try:
            await adapter.cancel_reservation(raw)
        except Exception as exc:
            log.critical(
                "group %s: cancelling row %s's worse booking failed (%s); retried next run",
                row.group_id,
                row.id,
                type(exc).__name__,
            )
            return "failed"
        try:
            await store.record_outcomes(
                [
                    RowOutcome(
                        row_id=row.id,
                        course_account_id=row.course_account_id,
                        target_date=row.target_date,
                        actor=actor,
                        to_status=RowStatus.PENDING,
                        status_reason=GROUP_DOWNGRADE_REASON,
                        last_outcome="group_downgrade",
                        at=clock.now_utc(),
                        cancelled_extras=(replace(entry, state=BookingState.CANCELLED_GROUP),),
                        clear_upgrade_marker=True,
                        release_lease_owner=owner,
                    )
                ]
            )
        except Exception as exc:
            # The course cancel landed; the marker makes the next run treat the vanish as ours.
            log.critical(
                "group %s: row %s cancelled at the course but the write failed (%s)",
                row.group_id,
                row.id,
                type(exc).__name__,
            )
            return "failed"
        log.info("group %s: row %s downgraded (kept a better-ranked booking)", row.group_id, row.id)
        return "downgraded"
    finally:
        await store.release_row_lease(row.id, owner=owner)
