"""Top-level booking flow. Owns the race, the fallback, the idempotency check.

v0 (M2.T1) implements:
- request_lock for concurrent-run defense
- idempotency short-circuit on existing terminal
- pre-T0 busy_wait
- per-course: authenticate -> list_reservations (PLAN §9 layer 2) -> search
  with poll loop -> filter/pick best -> book (or DRY_RUN gate)
- terminal persistence + notifier delivery

The full §9.1 state machine (UNCERTAIN -> RECONCILING -> BOOKED/LOST) is M2.T3.
This file deliberately keeps `book()` non-retryable (raises out) so M2.T3 can
add the reconciliation branch in one place.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .adapter import (
    InventoryNotPublishedError,
    NoInventoryError,
    SlotGoneError,
)
from .clock import busy_wait_until
from .models import (
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
)
from .slot_utils import rank_slots_for_request

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..notifications.notifier import Notifier
    from ..persistence.store import BookingStore
    from .adapter import CourseAdapter
    from .clock import Clock
    from .config import SchedulerConfig

log = logging.getLogger(__name__)


class Orchestrator:
    """Runs one BookingRequest end-to-end. Single-use; build a new one per request."""

    def __init__(
        self,
        adapters: Mapping[CourseId, CourseAdapter],
        store: BookingStore,
        notifier: Notifier,
        clock: Clock,
        scheduler: SchedulerConfig,
        creds: Mapping[CourseId, CourseCredentials] | None = None,
        prefetch_book: bool = False,
    ) -> None:
        self._adapters = adapters
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._scheduler = scheduler
        self._creds = creds or {}
        # Race path only: pre-solve the CAPTCHA during the pre-T0 busy-wait so the
        # book() POST fires within seconds of the 06:00 drop. Set True by the --wait
        # ACA booking job; left False everywhere else (watcher, local demo) so a token
        # is only ever solved when we are actually about to book. See PLAN.md §9.
        self._prefetch_book = prefetch_book

    async def run(self, request: BookingRequest) -> BookingResult:
        resolved_date = request.target_dates[0]

        async with self._store.request_lock(request.request_id):
            prior = await self._store.get_terminal(request.request_id, resolved_date)
            if prior is not None:
                return prior

            t0_target = self._compute_t0_minus_early()
            if self._prefetch_book:
                # Race path: pre-solve the CAPTCHA DURING the busy-wait. Wait to
                # T0 - lead, pre-fetch the token, then wait the remainder to T0 so the
                # post-T0 book() POST fires immediately. The 2026-06-07 prod failure was
                # the ~78s solve running AFTER T0, pushing the POST ~100s past the drop.
                lead = timedelta(seconds=self._scheduler.captcha_prefetch_lead_s)
                await busy_wait_until(t0_target - lead, self._clock)
                await self._prefetch_captcha(request)
            await busy_wait_until(t0_target, self._clock)
            fired = self._clock.now_utc()
            # Verification surface (M6 PR6): proves the bot busy-waited and fired at T0.
            # Under dry-run the final POST is suppressed, so this log line is the only
            # evidence the race fired on time. See AZURE_PLAN §10 (dev verification).
            log.info(
                "race: busy-wait complete; firing at %s (target=%s, drift_ms=%.1f)",
                fired.isoformat(),
                t0_target.isoformat(),
                (fired - t0_target).total_seconds() * 1000.0,
            )

            result: BookingResult | None = None
            for course_id in request.course_preferences:
                adapter = self._adapters.get(course_id)
                if adapter is None:
                    continue
                try:
                    result = await self._run_course(adapter, course_id, request)
                except _CourseSkippedError:
                    continue
                if result is not None:
                    break

            if result is None:
                result = self._terminal_no_inventory(request)

            await self._store.record_terminal(result, resolved_date)
            await self._notifier.notify(result)
            return result

    # --- per-course flow ------------------------------------------------

    async def _run_course(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
    ) -> BookingResult | None:
        creds = self._creds.get(course_id)
        if creds is not None:
            await adapter.authenticate(creds)

        # Layer 2: pre-book remote check.
        existing = await adapter.list_reservations()
        match = self._first_matching_reservation(existing, request)
        if match is not None:
            return BookingResult(
                request_id=request.request_id,
                outcome=BookingOutcome.ALREADY_BOOKED,
                course_id=course_id,
                slot=None,
                confirmation_code=match.confirmation_code,
                booked_at=self._clock.now_utc(),
                attempts=0,
            )

        slots = await self._poll_for_slots(adapter, request)
        if not slots:
            raise _CourseSkippedError()

        candidates = self._rank_slots(slots, request)
        if not candidates:
            raise _CourseSkippedError()

        if request.dry_run:
            return BookingResult(
                request_id=request.request_id,
                outcome=BookingOutcome.DRY_RUN,
                course_id=course_id,
                slot=candidates[0],
                confirmation_code=None,
                booked_at=None,
                attempts=1,
            )

        last_exc: SlotGoneError | None = None
        for candidate in candidates:
            try:
                return await adapter.book(candidate, request)
            except SlotGoneError as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise _CourseSkippedError()

    # --- race-path pre-fetch -------------------------------------------

    async def _prefetch_captcha(self, request: BookingRequest) -> None:
        """Pre-solve the CAPTCHA for the primary (first-preference) adapter so book()
        at T0 consumes a cached token instead of blocking ~75s on the solve.

        Best-effort: any failure is logged and swallowed — the race still proceeds and
        book() solves the token inline if needed (a pre-fetch hiccup must never cost the
        booking). Only the first preference with a registered adapter is pre-solved (the
        slot we will almost certainly book); a fallback course solves its own token in
        book(). No-op for adapters without a CAPTCHA (TeeItUp, Fake).
        """
        for course_id in request.course_preferences:
            adapter = self._adapters.get(course_id)
            if adapter is None:
                continue
            try:
                await adapter.prepare_book(None, request)
            except Exception as exc:
                log.warning(
                    "race: CAPTCHA pre-fetch failed for %s (%s) — will solve inline in book()",
                    course_id,
                    exc,
                )
            return

    # --- timing ---------------------------------------------------------

    def _compute_t0_minus_early(self) -> datetime:
        """T0 = today (in scheduler.timezone) at scheduler.fire_time. Subtract
        early_arrival_ms; that's the busy-wait target. Computed against the
        injected clock so FakeClock-driven tests are deterministic."""
        tz = ZoneInfo(self._scheduler.timezone)
        local_now = self._clock.now_utc().astimezone(tz)
        target_local = datetime.combine(
            local_now.date(),
            self._scheduler.fire_time,
            tzinfo=tz,
        )
        target_utc = target_local.astimezone(UTC)
        return target_utc - timedelta(milliseconds=self._scheduler.early_arrival_ms)

    # --- search loop ---------------------------------------------------

    async def _poll_for_slots(
        self,
        adapter: CourseAdapter,
        request: BookingRequest,
    ) -> list[TeeTimeSlot]:
        """Poll search() until we get a non-empty result or hit max_poll_seconds.
        On NoInventoryError or empty post-T0 result, return [] immediately so
        the orchestrator falls back to the next course."""
        deadline = self._clock.now_utc() + timedelta(seconds=self._scheduler.max_poll_seconds)
        poll = self._scheduler.poll_interval_ms / 1000.0
        while True:
            try:
                slots = await adapter.search(request)
            except InventoryNotPublishedError:
                if self._clock.now_utc() >= deadline:
                    return []
                await self._clock.sleep(poll)
                continue
            except NoInventoryError:
                return []
            if slots:
                return slots
            if self._clock.now_utc() >= deadline:
                return []
            await self._clock.sleep(poll)

    # --- slot selection ------------------------------------------------

    def _rank_slots(
        self,
        slots: list[TeeTimeSlot],
        request: BookingRequest,
    ) -> list[TeeTimeSlot]:
        """Filter to matching slots and return them sorted by distance from the
        window midpoint (closest wins). Empty list = no inventory.

        Delegates to the shared `rank_slots_for_request` helper in slot_utils
        so WatchOrchestrator can reuse the same logic without duplication.

        Feature 3 (M-feature-3): midpoint-distance sort — for window 09:00-10:00
        (midpoint 09:30), prefer 09:37 over 09:22. See slot_utils module docstring.
        """
        return rank_slots_for_request(slots, request)

    # --- pre-book reservation match -----------------------------------

    def _first_matching_reservation(
        self,
        reservations: list[ExistingReservation],
        request: BookingRequest,
    ) -> ExistingReservation | None:
        for r in reservations:
            if r.tee_time.date() in request.target_dates and r.party_size == len(request.players):
                return r
        return None

    # --- terminals ----------------------------------------------------

    def _terminal_no_inventory(self, request: BookingRequest) -> BookingResult:
        return BookingResult(
            request_id=request.request_id,
            outcome=BookingOutcome.NO_INVENTORY,
            course_id=None,
            slot=None,
            confirmation_code=None,
            booked_at=None,
            attempts=0,
        )


class _CourseSkippedError(Exception):
    """Internal signal: this course exhausted its options, try the next one."""
