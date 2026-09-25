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

Implemented in MULTIUSER_PLAN MU-3 (``tests/test_allocation.py``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from ..core.models import SlotId, TeeTimeSlot
from .models import RowId


@dataclass(frozen=True, slots=True)
class BlindAllocation:
    """``allowlists[row]`` is the set of slot ids that row's adapter may blind-POST (empty =
    search-only this drop). ``order`` is the draft order used, logged for fairness audits.
    ``search_only`` names every row whose allowlist is EMPTY — over the ``max_blind_rows`` cap
    (§5.3) OR drafted nothing because the shared grid was exhausted (§5.4 b) — i.e. every row
    the runner must run on the search race path (``blind_post_max_count=0``, pool demand k=0)."""

    allowlists: Mapping[RowId, frozenset[SlotId]]
    order: tuple[RowId, ...]
    search_only: frozenset[RowId]


def draft_order(row_ids: Sequence[RowId], *, target_date: date) -> tuple[RowId, ...]:
    """Rows sorted by id, rotated by ``target_date.toordinal() % len(row_ids)``, so first pick
    rotates across dates (§13 Q4 may replace this with an operator-first priority)."""
    ordered = sorted(row_ids)
    if not ordered:
        return ()
    k = target_date.toordinal() % len(ordered)
    return tuple(ordered[k:] + ordered[:k])


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

    ``order`` must be a permutation of ``ranked_candidates``' keys (normally ``draft_order``);
    only its first ``max_blind_rows`` entries draft — the rest are search-only. Raises
    ``ValueError`` on an inconsistent ``order`` or a negative ``burst_size``/``max_blind_rows``
    (a runner bug, and at 05:51 the loud failure is the safe one: no row is silently starved).
    """
    if burst_size < 0:
        raise ValueError(f"burst_size must be >= 0, got {burst_size}")
    if max_blind_rows < 0:
        raise ValueError(f"max_blind_rows must be >= 0, got {max_blind_rows}")
    if len(set(order)) != len(order) or set(order) != set(ranked_candidates):
        raise ValueError(
            "order must be a permutation of ranked_candidates' rows "
            f"(order={list(order)}, rows={list(ranked_candidates)})"
        )

    drafting = list(order[:max_blind_rows])
    picks: dict[RowId, list[SlotId]] = {row: [] for row in order}
    taken: set[SlotId] = set()
    round_index = 0
    while True:
        walk = drafting if round_index % 2 == 0 else drafting[::-1]
        progressed = False
        for row in walk:
            if len(picks[row]) >= burst_size:
                continue
            # Highest-ranked slot this row wants that nobody has drafted yet.
            best = next((s.slot_id for s in ranked_candidates[row] if s.slot_id not in taken), None)
            if best is None:
                continue
            taken.add(best)
            picks[row].append(best)
            progressed = True
        if not progressed:
            # Every drafting row is either full or exhausted: the draft is complete.
            break
        round_index += 1

    allowlists = {row: frozenset(ids) for row, ids in picks.items()}
    return BlindAllocation(
        allowlists=allowlists,
        order=tuple(order),
        search_only=frozenset(row for row, ids in allowlists.items() if not ids),
    )
