"""Cancellation-monitor orchestrator (M-feature-1).

This module implements the "watch" job that polls for newly available tee times
on a target date that was not successfully booked at the 6 AM opening, OR that
the user wants to improve upon (see upgrade_orchestrator.py for the improvement
path).

Design decisions (see PLAN.md M-feature-1 for the full analysis):

Polling interval:
    Default 10 minutes (600 s). Absolute floor is 5 minutes (300 s); WatchConfig
    will raise ValueError below that floor. Rationale: the 6 AM race window uses
    250 ms poll cadence because it is a competitive real-time window. The watch
    job is NOT racing anyone — it is monitoring for cancellations on a day that
    has already opened. At 10 minutes, 144 polls/day — well within the "one user
    making normal bookings" tier of any reasonable anti-bot policy. PLAN.md §12
    forbids hammering. 10 minutes respects that.

    NOTE: GH Actions `*/10` cron firing is best-effort; real-world intervals can
    be 10-20 minutes depending on runner load. The "144 polls/day" figure assumes
    exact 10-minute intervals and is an upper bound. See PLAN.md §20 SF-4 note.

ACA Job scheduling (ACA Jobs are not long-running):
    The watch job is NOT a single long-running process (that would require an
    ACA Container App, not a Job, and would cost ~$5+/month running idle).
    Instead, each ACA Job invocation runs once, checks for availability, then
    exits. The cron on the ACA Job fires every 10 minutes (*/10 * * * *).
    The job runs for at most ~30 seconds per invocation (one HTTP round-trip).
    This is the correct pattern for ACA Jobs. The v0 GH Actions equivalent is
    a separate workflow with a schedule of every 10 minutes during watch hours.

    IMPORTANT: ACA Job scheduled triggers use standard cron syntax and fire in
    UTC. The watch job's cron does not need a DST gate because it is not racing
    a wall-clock window — it just polls whenever it fires.

State management:
    The watch job reads from the same SQLite store (or Blob Storage in v1) as
    the main booking job. It needs to know:
    1. What date to watch — derived from `clock.today() + target_offsets[0] days`
       (same formula as the main booking job). No separate `watch_state` store
       table is required; the target date is computed from Clock at invocation
       time, matching the same date the main job was targeting. The caller
       (`teetime watch` CLI) may also pass `--date YYYY-MM-DD` to override.
    2. Whether a booking already exists — `list_reservations` and `get_terminal`
       short-circuit the poll.
    3. The deadline past which watching is pointless — after the target_date has
       passed (local date in scheduler timezone > target_date).

    There is NO separate `watch_state` table and no `WatchState` Protocol method.
    The watch job is stateless beyond the existing `booking_history` table.

ADVISORY LOCK OWNERSHIP:
    check_once() does NOT acquire `request_lock` for its read-only availability
    check. Once a bookable slot is found, it acquires the lock for the
    book + record_terminal sequence (matching the main Orchestrator's pattern).
    If it finds a higher-priority slot and delegates to
    `UpgradeOrchestrator.maybe_upgrade()`, that method acquires the lock itself.
    The caller must NOT hold the lock when calling maybe_upgrade().
    See upgrade_orchestrator.py module docstring for the canonical lock statement.

How check_once() determines the "current booking" for maybe_upgrade():
    check_once() calls store.get_booked(request.request_id, target_date) to
    retrieve the BookingResult stored by the main booking run. That record carries
    the TTB:-prefixed confirmation_code which is the source of truth for the
    is_managed check. check_once() also calls list_reservations() to confirm the
    booking still exists on the server (layer-2 pre-flight check). It then passes
    the STORE RECORD (BookingResult) — not the ExistingReservation — to
    maybe_upgrade(). This is the ONLY correct way: ExistingReservation from
    list_reservations() always has a raw server id (no TTB: prefix), so
    checking is_managed on it would always return False and make the upgrade guard
    a permanent no-op.

Race condition with the 6 AM booking run:
    The watch job's read-only search phase is lock-free. If it proceeds to book,
    it acquires the lock. If the 6 AM booking job holds the lock, ConcurrentRunError
    is caught and check_once returns None. Safe.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..persistence.store import ConcurrentRunError
from .adapter import (
    AuthError,
    CaptchaError,
    InventoryNotPublishedError,
    NoInventoryError,
    SlotGoneError,
)
from .models import (
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    TeeTimeSlot,
    WatchConfig,
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


class WatchOrchestrator:
    """Single-invocation watch check. Called once per ACA Job / GH Actions step.

    The caller (CLI command `teetime watch`) is responsible for:
    - Starting the process on each poll interval (via ACA Job cron or GH workflow).
    - Passing the `target_date` to watch. The date is computed by the CLI from
      `clock.today() + target_offsets[0]` (same as the main booking job), or
      overridden via `--date YYYY-MM-DD`. There is no `watch_state` store table;
      the watch job is stateless beyond the existing `booking_history` table.

    This class does NOT loop internally. Each invocation does one check and exits.
    The polling loop is handled externally by the scheduler (ACA cron / GH Actions).

    LOCK OWNERSHIP: check_once() acquires `request_lock` only for the booking
    phase (book + record_terminal), matching the Orchestrator pattern. The
    read-only search phase is lock-free. If check_once delegates to
    UpgradeOrchestrator.maybe_upgrade(), that method acquires the lock itself;
    check_once must NOT hold the lock when calling maybe_upgrade().

    CURRENT BOOKING RESOLUTION: check_once() retrieves the current managed
    booking via store.get_terminal(request.request_id, target_date) and passes
    the resulting BookingResult (which carries the TTB:-prefixed confirmation_code)
    to UpgradeOrchestrator.maybe_upgrade(). It does NOT pass the ExistingReservation
    from list_reservations(), which would always have is_managed=False (raw server
    id, no TTB: prefix).

    See PLAN.md M-feature-1.T2 for the implementation contract.
    """

    def __init__(
        self,
        adapters: Mapping[CourseId, CourseAdapter],
        store: BookingStore,
        notifier: Notifier,
        clock: Clock,
        scheduler: SchedulerConfig,
        watch_config: WatchConfig,
        creds: Mapping[CourseId, CourseCredentials] | None = None,
    ) -> None:
        self._adapters = adapters
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._scheduler = scheduler
        self._watch_config = watch_config
        self._creds = creds or {}

    async def check_once(
        self,
        request: BookingRequest,
        target_date: date,
    ) -> BookingResult | None:
        """Perform one availability check for `target_date`.

        Returns:
            BookingResult with outcome=BOOKED if a slot was found and booked.
            None if no slot was available on this check (caller should schedule
            the next invocation via the external cron).

        Raises:
            CaptchaError: re-raised after notifying (operator must disable the job).
            AuthError: re-raised after notifying (operator must fix credentials).
            All other exceptions are caught, logged, and result in None return.
        """
        now = self._clock.now_utc()

        # Gate 1: polling hours (course-local wall-clock time).
        if self._is_outside_polling_hours(now):
            log.debug(
                "watch: outside polling hours (%d-%d), skipping",
                self._watch_config.polling_start_hour,
                self._watch_config.polling_end_hour,
            )
            return None

        # Gate 2: deadline — if target_date is in the past, stop watching.
        if self._is_past_watch_deadline(now, target_date):
            log.info("watch: target_date %s has passed, stopping", target_date)
            return None

        # Gate 3: idempotency — already BOOKED in the store (main job or prior watch run).
        prior = await self._store.get_terminal(request.request_id, target_date)
        if prior is not None and prior.outcome == BookingOutcome.BOOKED:
            log.debug(
                "watch: store already has BOOKED terminal for (%s, %s), skipping",
                request.request_id,
                target_date,
            )
            return prior

        # Search phase: lock-free — check each course for available slots.
        for course_id in request.course_preferences:
            adapter = self._adapters.get(course_id)
            if adapter is None:
                continue

            try:
                result = await self._check_course(adapter, course_id, request, target_date)
            except (CaptchaError, AuthError) as exc:
                # Fatal errors: notify operator and re-raise so the calling
                # CLI/workflow can disable the watch job.
                outcome = (
                    BookingOutcome.CAPTCHA_BLOCKED
                    if isinstance(exc, CaptchaError)
                    else BookingOutcome.AUTH_FAILED
                )
                error_result = BookingResult(
                    request_id=request.request_id,
                    outcome=outcome,
                    course_id=course_id,
                    slot=None,
                    confirmation_code=None,
                    booked_at=None,
                    attempts=0,
                    error_message=str(exc),
                )
                try:
                    await self._notifier.notify(error_result)
                except Exception:
                    log.warning("watch: notifier raised during fatal error handling", exc_info=True)
                raise

            except Exception as exc:
                # Transient errors (network blips, unexpected HTTP responses).
                # Return None so the cron can retry on the next interval.
                log.warning(
                    "watch: transient error on course %s: %s", course_id, exc, exc_info=True
                )
                return None

            if result is not None:
                return result

        return None

    # --- per-course search + book ----------------------------------------

    async def _check_course(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
        target_date: date,
    ) -> BookingResult | None:
        """Check one course for available slots. Returns a BOOKED result or None."""
        creds = self._creds.get(course_id)
        if creds is not None:
            await adapter.authenticate(creds)

        # Layer 2: pre-book remote reservation check (§9).
        existing = await adapter.list_reservations()
        already = any(
            r.tee_time.date() == target_date and r.party_size == len(request.players)
            for r in existing
        )
        if already:
            log.debug("watch: existing reservation for %s on %s", course_id, target_date)
            return None

        # Search for newly available slots.
        try:
            slots = await adapter.search(request)
        except (NoInventoryError, InventoryNotPublishedError):
            return None  # Nothing on this course; try next.

        candidates = rank_slots_for_request(slots, request)
        if not candidates:
            return None

        return await self._book_candidates(adapter, request, target_date, candidates)

    async def _book_candidates(
        self,
        adapter: CourseAdapter,
        request: BookingRequest,
        target_date: date,
        candidates: list[TeeTimeSlot],
    ) -> BookingResult | None:
        """Acquire the advisory lock and attempt to book the first available candidate.

        Returns a BOOKED BookingResult on success, or None if the lock was
        contended or all candidates were gone.
        """
        try:
            async with self._store.request_lock(request.request_id):
                # Re-check inside lock — the 6 AM job or another watch invocation
                # may have booked between our search and lock acquisition.
                re_check = await self._store.get_terminal(request.request_id, target_date)
                if re_check is not None and re_check.outcome == BookingOutcome.BOOKED:
                    return re_check

                for candidate in candidates:
                    try:
                        result = await adapter.book(candidate, request)
                    except SlotGoneError:
                        continue  # Race lost on this slot — try next candidate.

                    if result.outcome == BookingOutcome.BOOKED:
                        # Clear any prior non-BOOKED terminal (e.g., NO_INVENTORY
                        # from the 6 AM run) before recording the new BOOKED result.
                        await self._store.delete_terminal(request.request_id, target_date)
                        await self._store.record_terminal(result, target_date)
                        await self._notifier.notify(result)
                        log.info(
                            "watch: booked %s on %s (confirmation=%s)",
                            result.course_id,
                            target_date,
                            result.confirmation_code,
                        )
                        return result

        except ConcurrentRunError:
            log.debug(
                "watch: request_lock held by another run for %s — skipping",
                request.request_id,
            )

        return None

    # --- time gates -------------------------------------------------------

    def _is_outside_polling_hours(self, now: datetime) -> bool:
        """Return True if current wall-clock time is outside polling_start/end hours.

        Polling is suppressed outside the configured hours to reduce load
        during nighttime when cancellations are vanishingly rare.
        Hours are checked in the scheduler's timezone (course-local wall clock).
        """
        tz = ZoneInfo(self._scheduler.timezone)
        local_hour = now.astimezone(tz).hour
        return (
            local_hour < self._watch_config.polling_start_hour
            or local_hour >= self._watch_config.polling_end_hour
        )

    def _is_past_watch_deadline(self, now: datetime, target_date: date) -> bool:
        """Return True if target_date is in the past (local date > target_date).

        Watching stops the day after target_date: the round has happened, no
        cancellations are relevant.  Within target_date itself (e.g. watching on
        the morning of the round), polling continues so last-minute cancellations
        can be caught.
        """
        tz = ZoneInfo(self._scheduler.timezone)
        local_date = now.astimezone(tz).date()
        return local_date > target_date
