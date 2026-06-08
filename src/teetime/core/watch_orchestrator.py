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

    NOTE: ACA Job `*/10` cron firing is best-effort; real-world intervals can
    be 10-20 minutes depending on scheduler load. The "144 polls/day" figure assumes
    exact 10-minute intervals and is an upper bound. See PLAN.md §20 SF-4 note.

ACA Job scheduling (ACA Jobs are not long-running):
    The watch job is NOT a single long-running process (that would require an
    ACA Container App, not a Job, and would cost ~$5+/month running idle).
    Instead, each ACA Job invocation runs once, checks for availability, then
    exits. The cron on the ACA Job fires every 10 minutes (*/10 * * * *).
    The job runs for at most ~30 seconds per invocation (one HTTP round-trip).
    This is the correct pattern for ACA Jobs. (An earlier design ran this as a
    GH Actions workflow; that was removed — the ACA Job watch cron is the only
    scheduler now.)

    IMPORTANT: ACA Job scheduled triggers use standard cron syntax and fire in
    UTC. The watch job's cron does not need a DST gate because it is not racing
    a wall-clock window — it just polls whenever it fires.

State management:
    The watch job shares the same in-process `InMemoryStore` as the main booking
    job (single ACA Job invocation; durable SQLite/Blob state was cut — see PLAN.md
    M3). It needs to know:
    1. What date(s) to watch — the CLI (`teetime watch`) computes the next upcoming
       occurrence of EACH wanted weekday within the horizon via
       `next_occurrences_within_horizon` and calls `check_once` once per date with a
       request scoped to that date + its weekday's windows. `--date YYYY-MM-DD` overrides
       to a single date.
    2. Whether a booking already exists — `list_reservations` and `get_terminal`
       short-circuit the poll.
    3. The deadline past which watching is pointless — after the target_date has
       passed (local date in scheduler timezone > target_date).

    There is NO durable `watch_state` and no `WatchState` Protocol method. The watch
    job is stateless across runs beyond the live `list_reservations` check; the
    cross-run source of truth is the remote reservation list, not a local store.

ADVISORY LOCK OWNERSHIP:
    check_once() does NOT acquire `request_lock` for its read-only availability
    check. Once a bookable slot is found, it acquires the lock for the
    book + record_terminal sequence (matching the main Orchestrator's pattern).
    If it finds a higher-priority slot and delegates to
    `UpgradeOrchestrator.maybe_upgrade()`, that method acquires the lock itself.
    The caller must NOT hold the lock when calling maybe_upgrade().
    See upgrade_orchestrator.py module docstring for the canonical lock statement.

How check_once() determines the "current booking" for maybe_upgrade():
    check_once() calls store.get_terminal(request.request_id, target_date) to
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
from dataclasses import replace as dc_replace
from datetime import date, datetime
from decimal import Decimal
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
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    SlotId,
    TeeTimeSlot,
    WatchConfig,
)
from .slot_utils import rank_slots_for_request
from .upgrade_orchestrator import UpgradeOrchestrator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..notifications.notifier import Notifier
    from ..persistence.store import BookingStore
    from .adapter import CourseAdapter
    from .clock import Clock
    from .config import OneBookingPolicyConfig, SchedulerConfig

log = logging.getLogger(__name__)


class WatchOrchestrator:
    """Single-invocation watch check. Called once per ACA Job invocation (or a direct
    `teetime watch` CLI call in dev).

    The caller (CLI command `teetime watch`) is responsible for:
    - Starting the process on each poll interval (via the ACA Job cron).
    - Passing each per-date-scoped request + `target_date` to `check_once` (one call per
      wanted date, computed via `next_occurrences_within_horizon`), or a single date via
      `--date YYYY-MM-DD`. There is no durable `watch_state`; the watch job is stateless
      across runs beyond the live `list_reservations` check.

    This class does NOT loop internally. Each invocation does one check and exits.
    The polling loop is handled externally by the scheduler (the ACA Job watch cron).

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
        policy: OneBookingPolicyConfig | None = None,
    ) -> None:
        self._adapters = adapters
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._scheduler = scheduler
        self._watch_config = watch_config
        self._creds = creds or {}
        self._policy = policy

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

        # The watcher polls on EVERY run (MULTIDAY PR4) — the old time-of-day polling-hours
        # gate was removed because it blinded us during the 06:00 drop and early-morning
        # cancellations. The deadline gate below is retained (don't poll a past target date).
        # Anti-bot rate limiting is the 10-min cron cadence + the poll_interval_s>=300 floor.
        # Gate: deadline — if target_date is in the past, stop watching.
        if self._is_past_watch_deadline(now, target_date):
            log.info("watch: target_date %s has passed, stopping", target_date)
            return None

        # Gate 3: idempotency — already BOOKED in the store (main job or prior watch run).
        prior = await self._store.get_terminal(request.request_id, target_date)
        if prior is not None and prior.outcome == BookingOutcome.BOOKED:
            if self._policy is not None and self._policy.enabled:
                # Policy is active — check whether a higher-priority slot opened up.
                # NOTE: caller must NOT hold request_lock here; _try_upgrade acquires it.
                upgraded = await self._try_upgrade(request, target_date, prior)
                if upgraded is not None:
                    return upgraded
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
                # Log and continue to the next course — don't abandon the whole
                # preference list because one course has a network blip. If all
                # courses fail, check_once returns None and the cron retries.
                log.warning(
                    "watch: transient error on course %s: %s", course_id, exc, exc_info=True
                )
                continue

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
        matching = [
            r
            for r in existing
            if r.tee_time.date() == target_date and r.party_size == len(request.players)
        ]
        if matching:
            if self._policy is not None and self._policy.enabled:
                # No store record at this point (Gate 3 would have caught a BOOKED
                # terminal before we reach _check_course). Synthesize a managed
                # BookingResult so UpgradeOrchestrator can cancel+rebook if a better
                # slot is found.
                current = self._synthesize_managed_booking(matching[0], course_id, request)
                upgraded = await self._try_upgrade(request, target_date, current)
                if upgraded is not None:
                    return upgraded
            log.debug("watch: existing reservation for %s on %s", course_id, target_date)
            return None

        # Search for newly available slots. MULTIDAY PR4 (must-fix 1): scope the request to
        # THIS target_date before searching so a multi-date watch never searches another
        # date, and filter ranked candidates to target_date as a STRUCTURAL guarantee
        # (rank_slots_for_request filters by window/spots/price, NOT by date). Together these
        # ensure the check for a Saturday-dated TARGET books only a Saturday slot (per target
        # date, not per execution day — a run still checks every wanted date). User contract.
        scoped = dc_replace(request, target_dates=(target_date,))
        try:
            slots = await adapter.search(scoped)
        except (NoInventoryError, InventoryNotPublishedError):
            return None  # Nothing on this course; try next.

        candidates = [
            c for c in rank_slots_for_request(slots, scoped) if c.tee_time.date() == target_date
        ]
        if not candidates:
            return None

        return await self._book_candidates(adapter, scoped, target_date, candidates)

    async def _book_candidates(
        self,
        adapter: CourseAdapter,
        request: BookingRequest,
        target_date: date,
        candidates: list[TeeTimeSlot],
    ) -> BookingResult | None:
        """Acquire the advisory lock and attempt to book the first available candidate.

        In dry_run mode: returns a DRY_RUN result immediately without acquiring
        the lock or POSTing to the adapter. Mirrors Orchestrator._run_course behaviour.

        Returns a BOOKED BookingResult on success, or None if the lock was
        contended or all candidates were gone.
        """
        if request.dry_run:
            # Dry run: surface the best candidate without making any real booking.
            return BookingResult(
                request_id=request.request_id,
                outcome=BookingOutcome.DRY_RUN,
                course_id=candidates[0].course_id,
                slot=candidates[0],
                confirmation_code=None,
                booked_at=None,
                attempts=0,
            )

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

    # --- upgrade helpers --------------------------------------------------

    async def _try_upgrade(
        self,
        request: BookingRequest,
        target_date: date,
        current_booking: BookingResult,
    ) -> BookingResult | None:
        """Delegate to UpgradeOrchestrator.maybe_upgrade().

        Caller must ensure self._policy is not None and self._policy.enabled
        before calling this method. The upgrade orchestrator acquires and releases
        request_lock itself — the caller must NOT hold the lock.

        Returns the upgraded BookingResult on success, or None if no upgrade
        was possible (no higher-priority slot available, or unmanaged booking).
        """
        assert self._policy is not None  # Caller guarantee; narrows type for mypy.
        orchestrator = UpgradeOrchestrator(
            adapters=self._adapters,
            store=self._store,
            notifier=self._notifier,
            clock=self._clock,
            scheduler=self._scheduler,
            policy=self._policy,
            creds=self._creds,
        )
        return await orchestrator.maybe_upgrade(request, target_date, current_booking)

    def _synthesize_managed_booking(
        self,
        reservation: ExistingReservation,
        course_id: CourseId,
        request: BookingRequest,
    ) -> BookingResult:
        """Synthesize a managed BookingResult from a live ExistingReservation.

        Used in _check_course() when list_reservations() finds an existing booking
        but the store has no BOOKED record (manual booking, cache eviction, or
        booking made before this bot existed). Stamping TTB: on the
        confirmation_code allows UpgradeOrchestrator to treat this reservation as
        upgradeable and cancel it if a better slot is found.

        ForeUpAdapter.cancel_reservation() strips the TTB: prefix before calling
        the live API, so the cancel works correctly with the raw server id.

        The synthesized slot's tee_time is used by _current_booking_priority() to
        determine which priority window the existing booking falls into.
        """
        return BookingResult(
            request_id=request.request_id,
            outcome=BookingOutcome.BOOKED,
            course_id=course_id,
            slot=TeeTimeSlot(
                course_id=course_id,
                slot_id=SlotId(reservation.confirmation_code),
                tee_time=reservation.tee_time,
                holes=18,
                available_spots=reservation.party_size,
                price_per_player=Decimal("0"),
                cart_included=False,
            ),
            confirmation_code=f"{MANAGED_BOOKING_TAG}{reservation.confirmation_code}",
            booked_at=None,
            attempts=0,
        )

    # --- time gates -------------------------------------------------------

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
