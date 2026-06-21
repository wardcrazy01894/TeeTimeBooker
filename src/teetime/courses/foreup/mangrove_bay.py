"""Mangrove Bay Golf Course (St. Petersburg, FL) — ForeUP adapter.

Booking page: https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes

    course_pk          = 19671
    booking_class_id   = 2149
    schedule_id        = 2149  (confirmed from DEFAULT_FILTER in page HTML)

This file is intentionally tiny: all behavior lives in base.py, EXCEPT the
blind-POST template + morning grid + synthesize_blind_slots, which are
inherently course-specific (BLIND_POST_PLAN.md §4).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx

from ...core.models import BookingRequest, CourseId, TeeTimeSlot
from ...core.slot_utils import rank_slots_for_request
from .base import FOREUP_BASE_URL, ForeUpAdapter, _parse_slot

_log = logging.getLogger(__name__)

MANGROVE_BAY_COURSE_ID = CourseId("foreup:mangrove_bay")
MANGROVE_BAY_COURSE_PK = 19671
MANGROVE_BAY_BOOKING_CLASS_ID = 2149  # teesheet/URL ID (appears in booking page URL)
MANGROVE_BAY_SCHEDULE_ID = 2149  # confirmed from DEFAULT_FILTER in booking page HTML
MANGROVE_BAY_PUBLIC_BOOKING_CLASS_ID = 12239  # "Public" class from SCHEDULES JSON
MANGROVE_BAY_BOOKING_PAGE_URL = (
    f"{FOREUP_BASE_URL}/index.php/booking/{MANGROVE_BAY_COURSE_PK}/{MANGROVE_BAY_BOOKING_CLASS_ID}"
)

# --- Blind-POST template + grid (BLIND_POST_PLAN.md §4, PR2) ----------------
# The STATIC fields of the book POST body / search-slot raw shape for the Mangrove Bay
# 18-hole schedule, captured from the live dev book-response (BLIND_POST_PLAN.md §2/§4;
# OQ2 CLOSED — this capture IS the template). Date-independent (fact 2): only `time` +
# `start_front` vary per slot and synthesize_blind_slots overwrites them. This is the
# SEARCH-slot raw shape; book() overlays players/green_fee/total/captchaid onto slot.raw,
# so this template carries NO PAN/CVV (ForeUP is card-on-file — root CLAUDE.md invariant).
# `time`/`start_front` here are placeholders overwritten per slot.
BLIND_POST_TEMPLATE: dict[str, object] = {
    # placeholders overwritten per slot by synthesize_blind_slots:
    "time": "",  # → "YYYY-MM-DD HH:MM" (1-indexed calendar month)
    "start_front": 0,  # → computed int (0-indexed month)
    "course_id": 19671,
    "course_name": "Mangrove Bay Golf Course",
    "schedule_id": 2149,
    "teesheet_id": 2149,
    "schedule_name": "Mangrove Bay",
    "require_credit_card": False,
    "teesheet_holes": 18,
    "teesheet_side_id": 3416,
    "teesheet_side_name": " ",
    "teesheet_side_order": 1,
    "reround_teesheet_side_id": 3417,
    "reround_teesheet_side_name": " ",
    "available_spots": 4,
    "available_spots_9": 0,
    "available_spots_18": 4,
    "maximum_players_per_booking": 4,
    "minimum_players": 1,
    "display_capacity": False,
    "allowed_group_sizes": ["2", "3", "4"],
    "holes": 18,
    "has_special": False,
    "special_id": False,
    "special_discount_percentage": 0,
    "group_id": False,
    "booking_class_id": False,
    "booking_fee_required": False,
    "booking_fee_price": False,
    "booking_fee_per_person": False,
    "foreup_trade_discount_rate": 0,
    "trade_min_players": 0,
    "trade_cart_requirement": "both",
    "trade_hole_requirement": "all",
    "trade_available_players": 0,
    "green_fee_tax_rate": False,
    "green_fee_tax": 0,
    "green_fee_tax_9": 0,
    "green_fee_tax_18": 0,
    "guest_green_fee_tax_rate": False,
    "guest_green_fee_tax": 0,
    "guest_green_fee_tax_9": 0,
    "guest_green_fee_tax_18": 0,
    "cart_fee_tax_rate": False,
    "cart_fee_tax": 0,
    "cart_fee_tax_9": 0,
    "cart_fee_tax_18": 0,
    "guest_cart_fee_tax_rate": False,
    "guest_cart_fee_tax": 0,
    "guest_cart_fee_tax_9": 0,
    "guest_cart_fee_tax_18": 0,
    "foreup_discount": False,
    "pay_online": "no",
    "green_fee": 46,
    "green_fee_9": 0,
    "green_fee_18": 46,
    "guest_green_fee": 46,
    "guest_green_fee_9": 0,
    "guest_green_fee_18": 46,
    "cart_fee": 0,
    "cart_fee_9": 0,
    "cart_fee_18": 0,
    "guest_cart_fee": 0,
    "guest_cart_fee_9": 0,
    "guest_cart_fee_18": 0,
    "rate_type": "riding",
    "is_designated_trade": False,
    "special_was_price": None,
    "cart_fee_18_hole": 12,
    "cart_fee_9_hole": 6.5,
    "teesheet_logo": None,
    "teesheet_color": "#0ed9c8",
    "teesheet_initials": "MB",
}

# Explicit enumerated morning tee-time grid (HH:MM, ET) for start_front enumeration.
# OQ1 DECISION: the grid is NOT a clean fixed interval, so we model it as an EXPLICIT
# list of valid start times (NOT an interval_min). synthesize_blind_slots intersects
# this list with the request window and computes each start_front.
#
# DERIVED grid (operator-approved 2026-06-20): the proven Mangrove Bay teesheet cadence is
# 8 tee times/hour at minutes :00,:07,:15,:22,:30,:37,:45,:52 (confirmed gap-free across the
# live-searchable afternoon union). Mornings sell out inside the 7-day window so could not be
# searched directly; this grid is EXTRAPOLATED from that proven cadence over the 08:45-10:00
# booking window. It is a best-effort starting point: the real T0 search runs concurrently as
# the grid-drift fallback, and synthesize_blind_slots logs the firing grid (and search() logs
# the real matched morning times) so a real drop can confirm or correct it retroactively.
# If a drop shows drift, update this list — `None` would fail loud (nit 3), but it is populated.
BLIND_POST_MORNING_GRID: list[str] | None = [
    "08:45",
    "08:52",
    "09:00",
    "09:07",
    "09:15",
    "09:22",
    "09:30",
    "09:37",
    "09:45",
    "09:52",
    "10:00",
]


class MangroveBayAdapter(ForeUpAdapter):
    """Mangrove Bay specialization. Sets the IDs; inherits all HTTP logic from ForeUpAdapter.

    Blind-POST capable (BLIND_POST_PLAN.md): at the 06:00 ET drop the race-path
    Orchestrator fires concurrent book POSTs for the top-N ranked in-window slots
    synthesized from BLIND_POST_TEMPLATE + a computed start_front, keeps the best, and
    cancels the rest. The real search runs concurrently as the grid-drift fallback.
    """

    booking_page_url = MANGROVE_BAY_BOOKING_PAGE_URL
    supports_blind_post: ClassVar[bool] = True

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        captcha_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        super().__init__(
            course_id=MANGROVE_BAY_COURSE_ID,
            course_pk=MANGROVE_BAY_COURSE_PK,
            booking_class_id=MANGROVE_BAY_BOOKING_CLASS_ID,
            schedule_id=MANGROVE_BAY_SCHEDULE_ID,
            public_booking_class_id=MANGROVE_BAY_PUBLIC_BOOKING_CLASS_ID,
            timezone="America/New_York",
            http_client=http_client,
            captcha_provider=captcha_provider,
        )

    def synthesize_blind_slots(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        max_count: int,
    ) -> list[TeeTimeSlot]:
        """Build up to max_count ranked blind-POST candidate slots for target_date
        WITHOUT a network search (BlindPostCapable; BLIND_POST_PLAN.md §4/PR2).

        Intersects the EXPLICIT BLIND_POST_MORNING_GRID start times with the request's
        time windows, computes each ForeUP ``start_front`` (``f"{YYYY}{month-1:02d}{DD}
        {HH}{MM}"`` — 0-indexed month, JS Date style) and the ``time`` field (1-indexed
        calendar month), builds a TeeTimeSlot whose ``raw`` is BLIND_POST_TEMPLATE merged
        with those two fields (so book() works unchanged — it overlays players/green_fee/
        total/captchaid onto slot.raw), ranks via rank_slots_for_request, and truncates to
        max_count.

        The raw is fed through the SAME ``_parse_slot`` the search path uses, so a
        synthesized slot is byte-identical to a searched one for the same raw — keeping the
        blind candidates and the concurrent grid-drift fallback consistent.
        """
        if BLIND_POST_MORNING_GRID is None:
            # Fail loud rather than silently enumerating nothing (BLIND_POST_PLAN nit 3).
            raise NotImplementedError(
                "BLIND_POST_MORNING_GRID is not populated for Mangrove Bay — refusing to "
                "synthesize an empty blind grid. See BLIND_POST_PLAN.md PR2."
            )
        tz = ZoneInfo(self._timezone)
        candidates: list[TeeTimeSlot] = []
        for hhmm in BLIND_POST_MORNING_GRID:
            hour, minute = (int(part) for part in hhmm.split(":"))
            # ForeUP start_front: 0-indexed month (JS Date style), as an int.
            start_front = int(
                f"{target_date.year:04d}{target_date.month - 1:02d}"
                f"{target_date.day:02d}{hour:02d}{minute:02d}"
            )
            # The `time` field uses the real (1-indexed) calendar month.
            time_field = (
                f"{target_date.year:04d}-{target_date.month:02d}-{target_date.day:02d} "
                f"{hour:02d}:{minute:02d}"
            )
            raw = {**BLIND_POST_TEMPLATE, "time": time_field, "start_front": start_front}
            candidates.append(_parse_slot(raw, target_date, self.course_id, tz))
        in_window_count = sum(
            any(w.earliest <= s.tee_time.time() <= w.latest for w in request.time_windows)
            for s in candidates
        )
        ranked = rank_slots_for_request(candidates, request)
        result = ranked[:max_count]
        # Retroactive grid-validation logging (operator request): emit the grid size, how
        # many fell in the request window, how many SURVIVED the spots/holes/price filter,
        # and the in-window times we will blind-POST. The separate in-window vs survived
        # counts let an empty result be diagnosed from logs — "0 in window" (grid drift /
        # wrong window) vs "in window but 0 survived" (mis-set holes/max_price/party size) —
        # instead of looking identical. Diff against the concurrent real search (search()
        # logs its matched morning tee times) to confirm or correct the derived grid. PII-free.
        _log.info(
            "MB blind-POST: %d grid time(s), %d in window, %d survived spots/holes/price; "
            "firing %d for %s (grid=%s, times=%s, max_count=%d)",
            len(BLIND_POST_MORNING_GRID),
            in_window_count,
            len(ranked),
            len(result),
            target_date,
            BLIND_POST_MORNING_GRID,
            [s.tee_time.strftime("%H:%M") for s in result],
            max_count,
        )
        return result
