"""Shared slot filtering and ranking utilities.

Extracted so Orchestrator and WatchOrchestrator can reuse the same logic
without coupling to each other. Both use identical filtering criteria
(spots, holes, price, time-window membership) and the midpoint-distance sort.

Sort key: for each candidate slot, find the time window it belongs to, compute
that window's midpoint, and measure |slot.tee_time - midpoint| in minutes
(ascending). Equidistant slots are broken by ascending tee_time.

Example: window 09:00-10:00, midpoint 09:30.
  09:37 → distance 7 min  (ranks before 09:22, distance 8 min)

This module has no imports beyond models and stdlib — keep it that way.
"""

from __future__ import annotations

from datetime import time

from .models import BookingRequest, TeeTimeSlot, TimeWindow


def rank_slots_for_request(
    slots: list[TeeTimeSlot],
    request: BookingRequest,
) -> list[TeeTimeSlot]:
    """Filter `slots` to those matching `request` criteria and sort by distance
    from the midpoint of the matching time window (midpoint-distance sort).

    Filtering criteria:
    - available_spots >= len(request.players)
    - slot.holes matches request.holes (0 means any)
    - slot.price_per_player <= request.max_price_per_player (if set)
    - slot.tee_time falls within one of request.time_windows

    Sort key: |slot.tee_time - window_midpoint| in minutes, ascending.
    Secondary (tiebreaker): ascending tee_time.

    Returns:
        A new list, sorted by midpoint distance. Empty = no matching inventory.
    """
    candidates: list[tuple[TeeTimeSlot, TimeWindow]] = []
    for s in slots:
        if s.available_spots < len(request.players):
            continue
        if request.holes not in (s.holes, 0):
            continue
        cap = request.max_price_per_player
        if cap is not None and s.price_per_player > cap:
            continue
        window = _matching_window(s, request)
        if window is None:
            continue
        candidates.append((s, window))
    # Sort by distance from window midpoint (ascending), tee_time as tiebreaker.
    candidates.sort(key=lambda sw: (_midpoint_distance_minutes(sw[0], sw[1]), sw[0].tee_time))
    return [s for s, _ in candidates]


def _matching_window(slot: TeeTimeSlot, request: BookingRequest) -> TimeWindow | None:
    """Return the first time window that contains `slot.tee_time`, or None."""
    local = slot.tee_time
    for w in request.time_windows:
        if w.earliest <= local.time() <= w.latest:
            return w
    return None


def _midpoint_distance_minutes(slot: TeeTimeSlot, window: TimeWindow) -> float:
    """Distance in minutes between the slot's tee_time and the window midpoint.

    Arithmetic is done in total minutes-since-midnight to avoid datetime
    subtraction complexity. The slot's `.time()` component is used directly —
    callers ensure the slot timezone and window times share the same reference
    (both ET for production ForeUP slots, both UTC for test slots).
    """
    earliest_min = _time_to_minutes(window.earliest)
    latest_min = _time_to_minutes(window.latest)
    midpoint_min = (earliest_min + latest_min) / 2
    slot_min = _time_to_minutes(slot.tee_time.time())
    return abs(slot_min - midpoint_min)


def _time_to_minutes(t: time) -> float:
    """Convert a time object to total minutes since midnight (including seconds)."""
    return t.hour * 60.0 + t.minute + t.second / 60.0
