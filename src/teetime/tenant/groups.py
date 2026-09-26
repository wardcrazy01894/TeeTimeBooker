"""Group rules for ranked preferences (MULTIUSER_PLAN §16.3/§16.4, MU-R2). Pure: no I/O.

A group is one user's rows for one date across courses, sharing ``group_id``. The invariant is
at most one BOOKED, OWNED row per group, and it is the best achieved rank.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .models import RankedWindow, RequestRow, RowStatus, achieved_rank

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
