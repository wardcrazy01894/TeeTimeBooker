"""Shared slot filtering and ranking utilities.

Extracted so Orchestrator and WatchOrchestrator can reuse the same logic
without coupling to each other. Both use identical filtering criteria
(spots, holes, price, time-window membership) and the midpoint-distance sort.

Sort key: (window index, midpoint distance, tee_time). WINDOW LIST ORDER IS PRIORITY —
a slot in an earlier-listed window always outranks a slot in a later one (PERDAY_WINDOWS_PLAN
Q1). Within a single window the slot closest to that window's midpoint wins (|slot.tee_time -
midpoint| in minutes, ascending); equidistant slots break by ascending tee_time.

Example: window 09:00-10:00, midpoint 09:30.
  09:37 → distance 7 min  (ranks before 09:22, distance 8 min)
Example (two windows [09:00-10:00, 17:00-19:00]): a 09:50 slot (window 0) beats an 18:00 slot
  (window 1, dead-center) because window 0 is preferred.

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

    Sort key: (window index, |slot.tee_time - window_midpoint| minutes, tee_time). Window
    LIST ORDER is the primary key (earlier-listed window preferred), so a slot in window[0]
    always outranks a slot in window[1] regardless of midpoint distance.

    Returns:
        A new list, sorted by (window index, midpoint distance). Empty = no matching inventory.
    """
    candidates: list[tuple[TeeTimeSlot, int, TimeWindow]] = []
    for s in slots:
        if s.available_spots < len(request.players):
            continue
        if request.holes not in (s.holes, 0):
            continue
        cap = request.max_price_per_player
        if cap is not None and s.price_per_player > cap:
            continue
        match = _matching_window(s, request)
        if match is None:
            continue
        idx, window = match
        candidates.append((s, idx, window))
    # Sort by window index (priority), then distance from that window's midpoint, then tee_time.
    candidates.sort(
        key=lambda swi: (swi[1], midpoint_distance_minutes(swi[0], swi[2]), swi[0].tee_time)
    )
    return [s for s, _, _ in candidates]


def _matching_window(slot: TeeTimeSlot, request: BookingRequest) -> tuple[int, TimeWindow] | None:
    """Return (index, window) of the FIRST time window that contains `slot.tee_time`, or None.
    The index is the window's position in request.time_windows = its priority (lower = preferred).
    """
    local = slot.tee_time
    for idx, w in enumerate(request.time_windows):
        if w.earliest <= local.time() <= w.latest:
            return (idx, w)
    return None


def midpoint_distance_minutes(slot: TeeTimeSlot, window: TimeWindow) -> float:
    """Distance in minutes between the slot's tee_time and the window midpoint.

    Public: UpgradeOrchestrator uses this to compare a held booking against a
    same-tier candidate (within-window upgrade — strictly closer wins).

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
