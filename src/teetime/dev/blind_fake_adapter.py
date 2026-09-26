"""Blind-capable ``FakeAdapter`` VARIANT with the MU-3 allowlist hook (MULTIUSER_PLAN §4.6).

``FakeAdapter(supports_blind_post=True)`` already exposes the two ``BlindPostCapable`` members,
but not ``set_blind_allowlist`` / ``blind_allowlist`` — the engine hook E2 that only
``MangroveBayAdapter`` carries and that the tenant runner sets on every blind-capable adapter
before T0. The recorder's blind-capable end-to-end test must drive a full race-path
``Orchestrator.run`` through EVERY member the recorder passes through, so this subclass adds the
hook with Mangrove Bay's exact semantics (filter the RANKED candidates BEFORE truncation;
``frozenset()`` = search-only; ``None`` = unfiltered). ``FakeAdapter``'s defaults are untouched.

Tests / local demo only — nothing on the production path imports this module.
"""

from __future__ import annotations

from datetime import date

from ..core.models import BookingRequest, CourseId, SlotId, TeeTimeSlot
from ..core.slot_utils import rank_slots_for_request
from .fake_adapter import FakeAdapter


class BlindFakeAdapter(FakeAdapter):
    """``FakeAdapter`` that is blind-capable by construction and honours a blind allowlist."""

    def __init__(self, *, course_id: CourseId) -> None:
        super().__init__(course_id=course_id, supports_blind_post=True)
        self._blind_allowlist: frozenset[SlotId] | None = None

    @property
    def blind_allowlist(self) -> frozenset[SlotId] | None:
        """The allowlist currently applied by synthesize_blind_slots (None = unfiltered)."""
        return self._blind_allowlist

    def set_blind_allowlist(self, allowlist: frozenset[SlotId] | None) -> None:
        """Restrict synthesize_blind_slots to ``allowlist`` — same contract as
        ``MangroveBayAdapter.set_blind_allowlist`` (engine hook E2). Pure state, no I/O."""
        self._blind_allowlist = allowlist

    def synthesize_blind_slots(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        max_count: int,
    ) -> list[TeeTimeSlot]:
        """Scripted slots, RANKED, allowlist-filtered BEFORE truncation (mirrors Mangrove Bay)."""
        self.synthesize_blind_slots_call_count += 1
        candidates = (
            list(self._blind_slots)
            if self._blind_slots is not None
            else [self._default_slot(request)]
        )
        # Ranked with the SAME ordering the orchestrator's keep-best uses (the contract every
        # BlindPostCapable adapter carries; FakeAdapter's own variant leaves list order as-is).
        ranked = rank_slots_for_request(candidates, request)
        allowlist = self._blind_allowlist
        allowed = ranked if allowlist is None else [s for s in ranked if s.slot_id in allowlist]
        return allowed[:max_count]
