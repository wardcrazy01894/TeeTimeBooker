"""One-booking policy enforcer (M-feature-2).

This module implements the "one booking" invariant: at any time the user holds
at most one managed TeeTimeBooker reservation. If a HIGHER-priority slot becomes
available (as defined by OneBookingPolicyConfig.priority_slots), the orchestrator
will:

    1. Verify the current managed booking still exists (list_reservations).
    2. Verify the higher-priority slot is actually bookable (search).
    3. Attempt to book the new slot (without cancelling the old one yet).
    4. Only if book() succeeds: cancel the old booking.
    5. Update the persistent state to reflect the new booking.

The critical design decision is step 3-before-4 (book-before-cancel). This
avoids the failure mode where we cancel a good booking and then fail to rebook,
leaving the user with nothing.

WARNING: This approach risks a brief moment where the user holds TWO bookings
simultaneously (between step 3 succeeding and step 4 completing). We accept
this narrow window because:
  a) The window is milliseconds to seconds (a single HTTP round-trip).
  b) The alternative (cancel-before-rebook) risks leaving the user with ZERO
     bookings, which is far worse.
  c) Mangrove Bay's cancellation window is at least 24 hours before the round,
     so the brief dual-booking does not trigger any violation of the booking rules.
  d) The ForeUP backend may itself enforce a max-bookings-per-user limit. If so,
     step 3 (book new slot) will fail before step 4 (cancel old), and the user
     retains their original booking. This is the correct safe-failure mode.

See PLAN.md M-feature-2 for the full design and state machine.

ADVISORY LOCK OWNERSHIP (MF-2 canonical statement):
    UpgradeOrchestrator.maybe_upgrade() acquires and releases `request_lock`
    ITSELF. The caller (WatchOrchestrator.check_once) MUST NOT hold the lock
    when calling maybe_upgrade(). Acquiring the lock inside check_once and then
    delegating to maybe_upgrade would cause a deadlock on the same RequestId.

    The full sequence inside maybe_upgrade():
        async with store.request_lock(request_id):
            [verify current booking still exists]
            [search for higher-priority slot]
            [book new slot]
            [cancel old slot]
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
    store.get_booked(request_id, target_date), which returns a BookingResult with
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
    If two slots of equal priority index are available, we pick the one with
    the earlier tee_time (Feature 3 — ascending time sort). This is consistent
    with the main orchestrator's _rank_slots behavior after Feature 3 lands.
    Ties are not possible across different course_ids at the same priority —
    the priority list is ordered by the operator and ambiguity is not possible
    within a single priority index on the same course (the time sort handles it).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from .models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    PrioritySlot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..notifications.notifier import Notifier
    from ..persistence.store import BookingStore
    from .adapter import CourseAdapter
    from .clock import Clock
    from .config import OneBookingPolicyConfig, SchedulerConfig


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
            1. Build the priority list for `target_date` from policy.priority_slots.
            2. Determine the priority of `current_booking` (by matching course_id
               and time_window). If current_booking.confirmation_code does not start
               with MANAGED_BOOKING_TAG, return None immediately — never touch manual
               or unrecognised bookings.
            3. Acquire request_lock.
            4. For each priority slot with a lower index (higher priority) than the
               current booking's priority:
               a. Search the adapter for available slots in that window.
               b. If any slots found:
                  - book the best one (earliest time in window, per Feature 3).
                  - If book() succeeds: cancel the old booking (strip TTB: prefix
                    from current_booking.confirmation_code to get the raw server id),
                    then update state.
                  - If book() fails: log warning, continue to next candidate.
               c. If cancel_reservation() fails after successful book():
                  record the new booking as BOOKED anyway (we have a real
                  confirmation code). Log a warning that a duplicate booking
                  may exist and the user must manually cancel the old one.
                  Notify the user with dual-booking warning.
            5. Return None if no upgrade was possible (all higher-priority slots
               still unavailable or all book() attempts failed). Notifier is
               called with an error-level result when all candidates fail to book
               so the user receives a signal about the missed upgrade opportunity.

        Raises:
            No exceptions propagated — all failures logged. CaptchaError and
            AuthError are re-raised after notification.

        See PLAN.md M-feature-2.T3 for the full implementation contract.
        """
        raise NotImplementedError(
            "UpgradeOrchestrator.maybe_upgrade — implement in M-feature-2.T3. "
            "See PLAN.md M-feature-2 for the cancel+rebook protocol."
        )

    def _current_booking_priority(
        self,
        current: BookingResult,
        priority_slots: Sequence[PrioritySlot],
    ) -> int:
        """Return the priority index of `current`, or `sys.maxsize` if not found.

        `current` is a BookingResult (store record) whose slot carries the course_id
        and tee_time used to locate it in the priority list.

        'Not found' means the booking was made outside the configured priority system
        (e.g. a migration from before this feature was enabled, or a booking made
        before [one_booking_policy] was configured). Returning sys.maxsize ensures
        that any configured priority slot is treated as an upgrade over an untracked
        booking — the correct safe default. The caller never needs to handle None.
        """
        raise NotImplementedError(
            "UpgradeOrchestrator._current_booking_priority — implement in M-feature-2.T3."
        )

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
        raise NotImplementedError(
            "UpgradeOrchestrator._build_priority_list — implement in M-feature-2.T2."
        )
