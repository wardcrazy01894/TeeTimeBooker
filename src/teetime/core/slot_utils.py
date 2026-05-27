"""Shared slot filtering and ranking utilities.

Extracted so Orchestrator and WatchOrchestrator can reuse the same logic
without coupling to each other.  Both use identical filtering criteria
(spots, holes, price, time-window membership) and Feature 3's ascending
tee_time sort.

This module has no imports beyond models and stdlib — keep it that way.
"""

from __future__ import annotations

from .models import BookingRequest, TeeTimeSlot, TimeWindow


def rank_slots_for_request(
    slots: list[TeeTimeSlot],
    request: BookingRequest,
) -> list[TeeTimeSlot]:
    """Filter `slots` to those matching `request` criteria and sort ascending
    by tee_time (earliest first — Feature 3).

    Filtering criteria:
    - available_spots >= len(request.players)
    - slot.holes matches request.holes (0 means any)
    - slot.price_per_player <= request.max_price_per_player (if set)
    - slot.tee_time falls within one of request.time_windows

    Returns:
        A new list, sorted ascending by tee_time. Empty = no matching inventory.
    """
    candidates: list[TeeTimeSlot] = []
    for s in slots:
        if s.available_spots < len(request.players):
            continue
        if request.holes not in (s.holes, 0):
            continue
        cap = request.max_price_per_player
        if cap is not None and s.price_per_player > cap:
            continue
        if _matching_window(s, request) is None:
            continue
        candidates.append(s)
    # Feature 3: prefer the earliest available slot.
    candidates.sort(key=lambda s: s.tee_time)
    return candidates


def _matching_window(slot: TeeTimeSlot, request: BookingRequest) -> TimeWindow | None:
    """Return the first time window that contains `slot.tee_time`, or None."""
    local = slot.tee_time
    for w in request.time_windows:
        if w.earliest <= local.time() <= w.latest:
            return w
    return None
