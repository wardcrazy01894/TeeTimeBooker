"""Cross-account blind-slot allocation (MULTIUSER_PLAN §5.4).

When several accounts want overlapping windows on the same course and date, their T0 blind bursts
must target DISJOINT grid slots. Otherwise account A's surplus POST can take account B's rank-0,
and A cancels it seconds later but B's POST has already been rejected. Pure functions with no I/O,
computed once at ~05:51 (pre-T0). The result is applied via
``MangroveBayAdapter.set_blind_allowlist`` (engine hook E2), so ``Orchestrator`` stays untouched.

Allocation is a SNAKE DRAFT over a ROTATING order: round 0 walks the order, each account taking its
highest-ranked not-yet-taken slot; odd rounds walk it reversed; stop at ``burst_size`` per account.
Accounts ranked beyond ``max_blind_rows`` (the §5.3 token cap) get an empty allowlist and run the
search race path. Accounts with disjoint windows are unaffected. The post-T0 fallback is NOT
allocated (first-come; a collision is a SlotGoneError -> next candidate).

STUB — implemented in MULTIUSER_PLAN MU-3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from ..core.models import SlotId, TeeTimeSlot
from .models import RowId

_MU3 = "MULTIUSER_PLAN.md MU-3"


@dataclass(frozen=True, slots=True)
class BlindAllocation:
    """``allowlists[row]`` is the set of slot ids that row's adapter may blind-POST (empty =
    search-only this drop). ``order`` is the draft order used, logged for fairness audits."""

    allowlists: Mapping[RowId, frozenset[SlotId]]
    order: tuple[RowId, ...]
    search_only: frozenset[RowId]


def draft_order(row_ids: Sequence[RowId], *, target_date: date) -> tuple[RowId, ...]:
    """Rows sorted by id, rotated by ``target_date.toordinal() % len(row_ids)``, so first pick
    rotates across dates (§13 Q4 may replace this with an operator-first priority)."""
    raise NotImplementedError(_MU3)


def allocate_blind_slots(
    ranked_candidates: Mapping[RowId, Sequence[TeeTimeSlot]],
    *,
    order: Sequence[RowId],
    burst_size: int,
    max_blind_rows: int,
) -> BlindAllocation:
    """Snake-draft disjoint allowlists. ``ranked_candidates[row]`` is that account's UNFILTERED
    ranked in-window list from ``synthesize_blind_slots`` (``max_count`` = grid size).

    Guarantees (each a named MU-3 test): allowlists pairwise disjoint; each row's first pick is its
    best still-available slot (distinct rank-0s whenever the grid holds >= N in-window slots);
    rows with disjoint windows receive exactly their own top-``burst_size``.
    """
    raise NotImplementedError(_MU3)
