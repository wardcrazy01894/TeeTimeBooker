"""One-booking policy enforcer (M-feature-2).

This module implements the "one booking" invariant: at any time the user holds
at most one managed TeeTimeBooker reservation. If a BETTER slot becomes
available — one in a higher-priority tier (as defined by
OneBookingPolicyConfig.priority_slots), or one in the CURRENT tier strictly
closer to the time window's midpoint (within-window upgrade) — the orchestrator
will:

    1. Verify the current managed booking still exists (list_reservations).
    2. Verify the higher-priority slot is actually bookable (search).
    3. Cancel the old booking.
    4. Only if cancel() succeeds: book the new slot.
    5. Update the persistent state to reflect the new booking.

The critical design decision is cancel-before-book (step 3-before-4). ForeUP
enforces a one-active-booking-per-user-per-day limit: a book POST while an
existing reservation is active returns HTTP 400. Therefore we MUST cancel first.

WARNING: This approach risks a narrow window (~1-2 HTTP round-trips, typically
1-2 seconds) between step 3 completing and step 4 completing, during which the
user has NO booking. We accept this window because:
  a) The window is ~1-2 seconds (two sequential HTTP round-trips).
  b) For low-demand tee-times (weekday afternoons, municipal courses), the
     probability of another user claiming the slot in that window is negligible.
  c) If book() fails after cancel, the watch job logs a warning. A subsequent
     watch invocation can book any newly available slot, including the priority-0
     one we just failed to grab.
  d) The original design (book-before-cancel) is unworkable: ForeUP's server
     rejects the second book POST with HTTP 400 before we can cancel.

See PLAN.md M-feature-2 for the full design and state machine.

ADVISORY LOCK OWNERSHIP (MF-2 canonical statement):
    UpgradeOrchestrator.maybe_upgrade() acquires and releases `request_lock`
    ITSELF. The caller (WatchOrchestrator.check_once) MUST NOT hold the lock
    when calling maybe_upgrade(). Acquiring the lock inside check_once and then
    delegating to maybe_upgrade would cause a deadlock on the same RequestId.

    The full sequence inside maybe_upgrade():
        async with store.request_lock(request_id):
            [search for higher-priority slot]
            [prepare_book — pre-fetch CAPTCHA, off the no-booking window]
            [cancel old slot]   # cancel BEFORE book: ForeUP rejects a 2nd live book POST
            [book new slot]
            [delete_terminal + record_terminal]

    All mutation is contained inside the lock block. WatchOrchestrator.check_once
    is a READ-only check before calling maybe_upgrade(); it does not acquire the
    lock for the read phase.

Ownership detection ("our" vs "manual" bookings):
    BookingResult.confirmation_code stores "TTB:<raw_foreup_id>" after a managed
    booking (Option A — see PLAN.md §20). Manual bookings made through the ForeUP
    website will NOT have this prefix in any local BookingResult.
    The upgrade orchestrator will NEVER cancel a booking whose confirmation_code
    does NOT start with MANAGED_BOOKING_TAG.

    IMPORTANT: ExistingReservation.confirmation_code (from list_reservations) is
    the RAW ForeUP id (no TTB: prefix), because the server does not echo back any
    prefix we supply in the booking POST body. Therefore the is_managed check is
    performed on the STORE RECORD (BookingResult from get_terminal / get_booked),
    NOT on the ExistingReservation from list_reservations.

    WatchOrchestrator.check_once retrieves the current managed booking via
    store.get_terminal(request_id, target_date), which returns a BookingResult with
    the TTB:-prefixed confirmation_code, and passes that BookingResult to
    maybe_upgrade(). The matching ExistingReservation (from list_reservations) is
    used only to confirm the booking still exists on the server and to supply the
    raw id for cancel_reservation() (after TTB: prefix stripping).

    See PLAN.md §20 for the full Option A vs Option B analysis.

Idempotency key handling:
    After a successful cancel+rebook, the new booking has the same
    (RequestId, resolved_date) as the old one. The store record_terminal call
    would normally reject a different outcome for an existing key (double-book
    defense). The upgrade orchestrator resolves this by:
    1. Acquiring request_lock (prevents any concurrent run).
    2. Calling store.delete_terminal(request_id, resolved_date) to clear the
       old BOOKED record.
    3. Calling store.record_terminal with the new BOOKED result.
    The delete+re-insert is atomic under the advisory lock. If the process dies
    between delete and re-insert, the next run's pre-flight list_reservations
    (§9 layer 2) sees the new booking and records ALREADY_BOOKED — no phantom.

Priority tie-breaking:
    If two slots of equal priority index are available, we pick the one closest
    to the midpoint of the time window (Feature 3 — midpoint-distance sort).
    Equidistant slots are broken by ascending tee_time. This is consistent with
    the main orchestrator's _rank_slots behavior (slot_utils.rank_slots_for_request).
    Ties are not possible across different course_ids at the same priority —
    the priority list is ordered by the operator and ambiguity is not possible
    within a single priority index on the same course (the midpoint sort handles it).

Within-window upgrade (same tier):
    After exhausting strictly-higher tiers, maybe_upgrade searches the tier the
    current booking sits in and cancel+rebooks only if a candidate is STRICTLY
    closer to that window's midpoint than the held slot (midpoint_distance_minutes,
    same metric as the ranking sort). Equidistant candidates never trigger an
    upgrade — a tie is not worth the cancel-before-book no-booking window. Strict
    improvement also guarantees convergence: each upgrade strictly decreases the
    held slot's midpoint distance, so the 10-minute watch cadence cannot thrash
    (cancel/rebook between two equally-good slots forever).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace as dc_replace
from datetime import date
from typing import TYPE_CHECKING

from .adapter import RateLimitError
from .models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    PrioritySlot,
    TeeTimeSlot,
    TimeWindow,
)
from .slot_utils import midpoint_distance_minutes, rank_slots_for_request

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..notifications.notifier import Notifier
    from ..persistence.store import BookingStore
    from .adapter import CourseAdapter
    from .clock import Clock
    from .config import OneBookingPolicyConfig, SchedulerConfig

log = logging.getLogger(__name__)


class UpgradeOrchestrator:
    """Check whether a higher-priority slot exists; if so, cancel+rebook atomically.

    Called by the watch job after a successful slot-found check.
    Single-use; build a new one per invocation.

    See PLAN.md M-feature-2 for the full algorithm and state machine.
    """

    def __init__(
        self,
        adapters: Mapping[CourseId, CourseAdapter],
        store: BookingStore,
        notifier: Notifier,
        clock: Clock,
        scheduler: SchedulerConfig,
        policy: OneBookingPolicyConfig,
        creds: Mapping[CourseId, CourseCredentials] | None = None,
    ) -> None:
        self._adapters = adapters
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._scheduler = scheduler
        self._policy = policy
        self._creds = creds or {}

    async def maybe_upgrade(
        self,
        request: BookingRequest,
        target_date: date,
        current_booking: BookingResult,
    ) -> BookingResult | None:
        """Attempt to upgrade `current_booking` to a higher-priority slot.

        `current_booking` is the STORE RECORD (a BookingResult retrieved via
        store.get_terminal), NOT an ExistingReservation from
        list_reservations. Its confirmation_code carries the TTB: prefix so that
        the managed-booking check is reliable. The caller (WatchOrchestrator.check_once)
        is responsible for cross-referencing the store record against the live
        list_reservations() response to confirm the booking still exists on the server.

        LOCK OWNERSHIP: this method acquires and releases `store.request_lock`
        itself. The CALLER MUST NOT hold the lock when calling this method.
        See the module docstring for the canonical lock ownership statement.

        Algorithm:
            1. Managed-booking guard: if current_booking.confirmation_code lacks
               the TTB: prefix, return None immediately — never touch manual bookings.
            2. Build priority list; locate the tier (PrioritySlot) containing the
               current booking.
            3. Find priority slots with a strictly lower index (higher priority).
               These are tried first; afterwards the CURRENT tier is searched for a
               within-window upgrade (a candidate strictly closer to the window
               midpoint than the held slot — ties never upgrade).
            4. Acquire request_lock.
            5. For each candidate tier (higher tiers ascending, then current tier):
               a. Search adapter for available slots in that window.
               b. If slots found: call adapter.prepare_book() to pre-fetch any
                  expensive prerequisites (e.g. CAPTCHA token). If prepare_book()
                  raises, abort this candidate (original booking preserved).
               c. Cancel old booking (cancel-before-book protocol).
               d. If cancel() fails: log warning, continue to next candidate
                  (original booking preserved).
               e. If cancel() succeeds: book the new slot (earliest tee_time).
               f. If book() fails after cancel: log critical warning (user may
                  have no booking), continue to next candidate.
               g. If book() succeeds: update store, notify, return result.
            6. Return the new BookingResult, or None if no upgrade was possible.

        Note on prepare_book() ordering: the CAPTCHA solve (~15-60 s) happens in
        prepare_book(), BEFORE cancel_reservation(). This shrinks the no-booking
        window from ~60 s to ~1-2 s (two HTTP round-trips). See PLAN.md M-feature-2.
        """
        # Step 1: Managed-booking guard (pre-lock, no mutation).
        if not (
            current_booking.confirmation_code
            and current_booking.confirmation_code.startswith(MANAGED_BOOKING_TAG)
        ):
            log.debug(
                "upgrade: booking %s has no TTB: prefix — unmanaged, skipping",
                current_booking.confirmation_code,
            )
            return None

        # Step 2: Build priority list + locate the current booking's tier.
        priority_slots = self._build_priority_list(request, target_date)
        if not priority_slots:
            return None

        current_tier = self._current_booking_tier(current_booking, priority_slots)
        current_priority = current_tier.priority if current_tier is not None else sys.maxsize

        # Step 3: Collect all slots with strictly lower index (= higher priority).
        higher = [ps for ps in priority_slots if ps.priority < current_priority]

        # Steps 4-6: Lock, search, book, cancel, update. Higher tiers first (any
        # bookable candidate there beats the current booking), then a within-window
        # pass on the CURRENT tier: upgrade only to a slot STRICTLY closer to the
        # window midpoint than the held one (a tie is not worth the no-booking
        # window of the cancel-before-book protocol).
        async with self._store.request_lock(request.request_id):
            for priority_slot in sorted(higher, key=lambda ps: ps.priority):
                result = await self._try_upgrade_slot(
                    request, target_date, current_booking, priority_slot
                )
                if result is not None:
                    return result

            if current_tier is not None and current_booking.slot is not None:
                return await self._try_upgrade_slot(
                    request,
                    target_date,
                    current_booking,
                    current_tier,
                    must_beat=current_booking.slot,
                )

        return None

    async def _try_upgrade_slot(
        self,
        request: BookingRequest,
        target_date: date,
        current_booking: BookingResult,
        priority_slot: PrioritySlot,
        must_beat: TeeTimeSlot | None = None,
    ) -> BookingResult | None:
        """Attempt one upgrade to `priority_slot`. Called while holding request_lock.

        `must_beat` (within-window upgrade): when set, only candidates STRICTLY
        closer to `priority_slot.time_window`'s midpoint than `must_beat` are
        considered. Used for the current-tier pass — a same-tier slot must be a
        real improvement to justify the cancel-before-book no-booking window;
        equidistant slots never trigger an upgrade. Higher-tier passes leave it
        None (any bookable candidate in a higher tier is an upgrade).

        Uses cancel-before-book protocol: ForeUP enforces a one-active-booking-per-
        user-per-day limit and rejects a second book POST (HTTP 400). Therefore we
        cancel the old booking first, then book the new one.

        Risk: a narrow window (~1-2 HTTP round-trips) where the user has NO booking.
        If book() fails after cancel, the user temporarily has no live reservation;
        a subsequent watch invocation may recover by booking any available slot.

        Returns the persisted BookingResult on success, or None on any failure
        (caller should try the next candidate).
        """
        adapter = self._adapters.get(priority_slot.course_id)
        if adapter is None:
            log.warning(
                "upgrade: no adapter registered for course %s, skipping",
                priority_slot.course_id,
            )
            return None

        creds = self._creds.get(priority_slot.course_id)
        if creds is not None:
            await adapter.authenticate(creds)

        search_request = dc_replace(
            request,
            target_dates=(target_date,),
            time_windows=(priority_slot.time_window,),
            course_preferences=(priority_slot.course_id,),
        )
        try:
            slots = await adapter.search(search_request)
        except RateLimitError:
            # A 429 is an explicit "back off" — propagate so the watch run aborts
            # (WatchOrchestrator's 429 contract; the 10-min cron is the backoff
            # floor). Treating it as a generic search failure would keep polling
            # the throttled platform. This path now runs on EVERY watch cycle
            # (within-window pass), so the distinction matters.
            raise
        except Exception as exc:
            log.warning("upgrade: search failed for %s: %s", priority_slot.course_id, exc)
            return None

        candidates = rank_slots_for_request(slots, search_request)
        if must_beat is not None:
            held_distance = midpoint_distance_minutes(must_beat, priority_slot.time_window)
            candidates = [
                c
                for c in candidates
                if midpoint_distance_minutes(c, priority_slot.time_window) < held_distance
            ]
        if not candidates:
            return None

        if request.dry_run:
            # Dry run: the looking/ranking/logging above all happened, but suppress
            # the mutating cancel+book POSTs. Mirrors WatchOrchestrator._book_candidates
            # and Orchestrator._run_course. CRITICAL: the ForeUP adapter's book()/
            # cancel_reservation() POST unconditionally (no per-adapter dry-run check),
            # so this gate MUST live here or a dev (--dry-run true) watch run would
            # cancel+rebook a real reservation. See full-repo-scan finding C1.
            log.info(
                "upgrade: DRY RUN — would upgrade %s to %s slot %s (no POST issued)",
                current_booking.confirmation_code,
                priority_slot.course_id,
                candidates[0].slot_id,
            )
            return BookingResult(
                request_id=request.request_id,
                outcome=BookingOutcome.DRY_RUN,
                course_id=candidates[0].course_id,
                slot=candidates[0],
                confirmation_code=None,
                booked_at=None,
                attempts=0,
            )

        return await self._cancel_and_book_slot(
            adapter,
            candidates[0],
            request,
            target_date=target_date,
            current_booking=current_booking,
            priority_slot=priority_slot,
        )

    async def _cancel_and_book_slot(
        self,
        adapter: CourseAdapter,
        best: TeeTimeSlot,
        request: BookingRequest,
        *,
        target_date: date,
        current_booking: BookingResult,
        priority_slot: PrioritySlot,
    ) -> BookingResult | None:
        """Cancel the old booking, then book the new slot (cancel-before-book protocol).

        Called while holding request_lock.

        Returns the persisted BookingResult on success, or None on any failure.
        If cancel fails: original booking preserved, book never attempted.
        If book fails after cancel: logs critical warning, returns None (user
        temporarily has no live booking; next watch invocation may recover).
        """
        # Pre-fetch expensive prerequisites (e.g. CAPTCHA token) BEFORE cancel.
        # This shrinks the no-booking window from ~60 s (CAPTCHA solve) to ~1-2 s
        # (two HTTP round-trips). If prepare_book() raises we abort early — the
        # original booking is never touched.
        try:
            await adapter.prepare_book(best, request)
        except Exception as exc:
            log.warning(
                "upgrade: prepare_book() failed for %s — aborting upgrade, original "
                "booking %s preserved. Error: %s",
                priority_slot.course_id,
                current_booking.confirmation_code,
                exc,
            )
            return None

        # Cancel-before-book: cancel the old booking first. If cancel fails, abort
        # rather than attempting a book that ForeUP would reject with HTTP 400.
        cancel_failed = await self._cancel_old_booking(current_booking)
        if cancel_failed:
            log.warning(
                "upgrade: cancel failed — aborting upgrade to preserve original booking %s",
                current_booking.confirmation_code,
            )
            return None

        # Old booking is cancelled. Now book the new slot.
        try:
            new_result = await adapter.book(best, request)
        except Exception as exc:
            log.warning(
                "upgrade: book() failed after cancel of %s — user has no active booking "
                "for %s. Next watch invocation may recover. Error: %s",
                current_booking.confirmation_code,
                target_date,
                exc,
            )
            return None

        if new_result.outcome != BookingOutcome.BOOKED:
            log.warning(
                "upgrade: book() returned %s after cancel of %s — user may have no "
                "active booking for %s.",
                new_result.outcome,
                current_booking.confirmation_code,
                target_date,
            )
            return None

        # Cancel succeeded and book succeeded → persist and notify.
        return await self._persist_upgrade(
            request,
            target_date,
            current_booking=current_booking,
            new_result=new_result,
            priority_slot=priority_slot,
            cancel_failed=False,
        )

    async def _cancel_old_booking(self, current_booking: BookingResult) -> bool:
        """Cancel the old booking (BEFORE the rebook). Returns True if cancel failed —
        in which case the upgrade is aborted and the original booking is preserved
        (no rebook is attempted), so there is no dual-booking."""
        if current_booking.course_id is None:
            log.warning("upgrade: current_booking has no course_id — cannot cancel")
            return True
        old_adapter = self._adapters.get(current_booking.course_id)
        if old_adapter is None:
            log.warning(
                "upgrade: no adapter for old course %s — cannot cancel old booking",
                current_booking.course_id,
            )
            return True
        try:
            await old_adapter.cancel_reservation(current_booking.confirmation_code or "")
        except Exception as cancel_exc:
            log.warning(
                "upgrade: cancel of old booking %s failed — aborting upgrade; the original "
                "booking is preserved (no rebook attempted). Error: %s",
                current_booking.confirmation_code,
                cancel_exc,
            )
            return True
        return False

    async def _persist_upgrade(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        # KEYWORD-ONLY deliberately: current_booking and new_result are BOTH BookingResult,
        # so a positional transposition here would type-check clean and silently persist the
        # OLD booking as the upgrade result. mypy cannot catch it; the `*` can.
        current_booking: BookingResult,
        new_result: BookingResult,
        priority_slot: PrioritySlot,
        cancel_failed: bool,
    ) -> BookingResult:
        """Delete old terminal, record new one, and notify. Returns the stored result."""
        result_to_store = new_result
        if cancel_failed:
            result_to_store = dc_replace(
                new_result,
                diagnostics={
                    **new_result.diagnostics,
                    "dual_booking_warning": True,
                    "old_confirmation_code": current_booking.confirmation_code or "",
                },
            )

        await self._store.delete_terminal(request.request_id, target_date)
        await self._store.record_terminal(result_to_store, target_date)
        await self._notifier.notify(result_to_store)

        log.info(
            "upgrade: %s from %s to %s (priority %d)%s",
            "upgraded (dual-booking warning)" if cancel_failed else "upgraded",
            current_booking.course_id,
            new_result.course_id,
            priority_slot.priority,
            " — old booking NOT cancelled, manual action required" if cancel_failed else "",
        )
        return result_to_store

    def _current_booking_tier(
        self,
        current: BookingResult,
        priority_slots: Sequence[PrioritySlot],
    ) -> PrioritySlot | None:
        """Return the PrioritySlot whose window contains `current`, or None.

        `current` is a BookingResult (store record) whose slot carries the course_id
        and tee_time used to locate it in the priority list.

        None means the booking was made outside the configured priority system
        (e.g. a migration from before this feature was enabled, or a booking made
        before [one_booking_policy] was configured). The caller maps None to
        sys.maxsize so that any configured priority slot is treated as an upgrade
        over an untracked booking — the correct safe default. The returned tier is
        also the search window for the within-window (same-tier) upgrade pass.
        """
        if current.slot is None:
            return None

        slot_time = current.slot.tee_time.time()
        for ps in priority_slots:
            if ps.course_id != current.course_id:
                continue
            if ps.time_window.earliest <= slot_time <= ps.time_window.latest:
                return ps

        return None

    def _build_priority_list(
        self,
        request: BookingRequest,
        target_date: date,
    ) -> list[PrioritySlot]:
        """Materialize the ordered PrioritySlot list for `target_date` from policy config.

        If policy.priority_slots is empty, derive a default list from
        request.course_preferences order with request.time_windows[0].

        Args:
            request: the active BookingRequest (used for fallback list when
                policy.priority_slots is empty).
            target_date: the date being watched (for any date-specific filtering
                that M-feature-2.T2 may add, e.g. day-of-week windows).
        """
        if self._policy.priority_slots:
            return [
                PrioritySlot(
                    priority=i,
                    course_id=CourseId(ps.course_id),
                    time_window=TimeWindow(
                        earliest=ps.time_window_earliest,
                        latest=ps.time_window_latest,
                    ),
                    target_date=target_date,
                )
                for i, ps in enumerate(self._policy.priority_slots)
            ]

        # Fallback: use course_preferences order with the first time window.
        if not request.time_windows:
            return []
        window = request.time_windows[0]
        return [
            PrioritySlot(
                priority=i,
                course_id=course_id,
                time_window=window,
                target_date=target_date,
            )
            for i, course_id in enumerate(request.course_preferences)
        ]
