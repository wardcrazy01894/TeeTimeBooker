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

import asyncio
import logging
from collections.abc import Mapping
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
        # Courses whose login was successfully pre-warmed before T0 (MF3). run() passes
        # this into _run_course so the post-T0 authenticate() is SKIPPED for them — the
        # skip is orchestrator-owned and does NOT rely on any adapter implementing an
        # idempotency guard (FakeAdapter/TeeItUp don't). See RACE_PREWARM_PLAN §3.1.
        self._prewarmed_course_ids: set[CourseId] = set()

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
                prefetch_at = t0_target - lead
                now = self._clock.now_utc()
                if now >= prefetch_at:
                    # We started past the prefetch point (e.g. the DST gate admits all of
                    # hour 5 but the ACA cron landed late). The ~75s solve will run into /
                    # past T0, so the book() POST may fire after the drop — the 2026-06-07
                    # failure mode. Surface it loudly; do NOT silently look on-time. We
                    # still prefetch immediately below: overlapping the solve with whatever
                    # time remains beats solving inline in book().
                    log.warning(
                        "race: started %.1fs past the CAPTCHA-prefetch point (T0-%ds) — "
                        "prefetch lead not fully honored; book() POST may fire after T0",
                        (now - prefetch_at).total_seconds(),
                        self._scheduler.captcha_prefetch_lead_s,
                    )
                else:
                    await busy_wait_until(prefetch_at, self._clock)
                # Pre-warm the primary adapter during the pre-T0 window: login + the
                # layer-2 reservation guard AND the CAPTCHA solve, concurrently (§3). If the
                # guard finds we are ALREADY booked, short-circuit before T0 — there is
                # nothing to race for. Emit the SF6 verification line so an operator can tell
                # a correct short-circuit from a dead replica (the normal "busy-wait complete"
                # line is skipped on this path).
                match = await self._prewarm_primary(request)
                if match is not None:
                    log.info(
                        "race: short-circuited pre-T0 — matching reservation already booked "
                        "(conf=%s); skipping busy-wait and search",
                        match.confirmation_code,
                    )
                    terminal = self._terminal_already_booked(request, match)
                    await self._store.record_terminal(terminal, resolved_date)
                    await self._notifier.notify(terminal)
                    return terminal
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
                    result = await self._run_course(
                        adapter,
                        course_id,
                        request,
                        prewarmed_course_ids=frozenset(self._prewarmed_course_ids),
                    )
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
        *,
        prewarmed_course_ids: frozenset[CourseId] = frozenset(),
    ) -> BookingResult | None:
        creds = self._creds.get(course_id)
        # MF3: skip the post-T0 authenticate ONLY for a course whose login was successfully
        # pre-warmed before T0 (orchestrator-owned skip — not adapter-guard-dependent). A
        # course that was never pre-warmed, or whose pre-warm login failed, authenticates
        # inline here exactly as before.
        if creds is not None and course_id not in prewarmed_course_ids:
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

        # candidates is non-empty here (an empty list raised _CourseSkippedError above), so
        # the loop runs at least once and last_exc is set if we reach this point.
        last_exc: SlotGoneError | None = None
        for candidate in candidates:
            try:
                return await adapter.book(candidate, request)
            except SlotGoneError as exc:
                last_exc = exc
        # Every ranked candidate was gone (a 4xx book rejection means NO reservation was
        # created — the prod 2026-06-07 failure mode at a competitive drop). Treat exhaustion
        # like an empty course: fall through to the next course preference, and if none book,
        # the run records a NO_INVENTORY terminal (+ notifies) instead of crashing the job.
        log.info(
            "course %s: all %d ranked candidates gone (last: %s) — trying next course",
            course_id,
            len(candidates),
            last_exc,
        )
        raise _CourseSkippedError()

    # --- race-path pre-warm (login + reservation guard + CAPTCHA) ------

    def _primary_adapter(self, request: BookingRequest) -> tuple[CourseId, CourseAdapter] | None:
        """The first course preference with a registered adapter — the slot we will almost
        certainly book, and the only course pre-warmed before T0. Fallback courses
        authenticate + solve their own token inline at T0."""
        for course_id in request.course_preferences:
            adapter = self._adapters.get(course_id)
            if adapter is not None:
                return course_id, adapter
        return None

    async def _prewarm_primary(self, request: BookingRequest) -> ExistingReservation | None:
        """Pre-warm the primary adapter DURING the pre-T0 busy-wait: concurrently
        (1) authenticate + run the layer-2 reservation guard (`_prewarm_login`) and
        (2) pre-solve the CAPTCHA (`_prefetch_captcha_for`).

        Returns the matching ExistingReservation if the pre-T0 guard finds we are already
        booked (caller short-circuits ALREADY_BOOKED), else None.

        MF2 — outer-gather error isolation, BOTH conditions required (not either/or):
          (1) `_prewarm_login` and `_prefetch_captcha_for` each catch their own exceptions
              and never raise; AND
          (2) the gather uses `return_exceptions=True` as defense-in-depth, so even a future
              refactor that lets one leg raise cannot cancel the other in-flight leg (a
              default gather cancels siblings on the first exception — which would silently
              destroy the token solve or the login session the race depends on).
        Awaits BOTH legs to completion before the caller busy-waits to T0.
        """
        primary = self._primary_adapter(request)
        if primary is None:
            return None
        course_id, adapter = primary
        results = await asyncio.gather(
            self._prewarm_login(adapter, course_id, request),
            self._prefetch_captcha_for(adapter, course_id, request),
            return_exceptions=True,
        )
        login_result = results[0]
        # Defense-in-depth (MF2 condition 2): _prewarm_login is contracted not to raise, but
        # if it ever does, return_exceptions=True surfaces it here as an Exception instead of
        # cancelling the prefetch leg. Swallow it (best-effort) and proceed to T0.
        if isinstance(login_result, ExistingReservation):
            return login_result
        return None

    async def _prewarm_login(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
    ) -> ExistingReservation | None:
        """Best-effort pre-T0 login + layer-2 reservation guard for the primary adapter.

        MUST NOT raise (MF2 condition 1): any exception is logged at WARNING and swallowed,
        returning None — so the post-T0 `_run_course` authenticates inline (today's degraded
        path). On authenticate SUCCESS it records `course_id` in `self._prewarmed_course_ids`
        so `run()` skips the post-T0 re-auth (MF3). A pre-warm login FAILURE leaves the set
        unchanged for that course, so the inline retry at T0 still happens.

        Returns the matching existing reservation (caller short-circuits) or None.
        """
        creds = self._creds.get(course_id)
        if creds is None:
            return None
        try:
            await adapter.authenticate(creds)
            # Auth succeeded: the session is established, so skip the post-T0 re-auth even if
            # the reservation read below fails (it has its own try and _run_course re-reads).
            self._prewarmed_course_ids.add(course_id)
            existing = await adapter.list_reservations()
            return self._first_matching_reservation(existing, request)
        except Exception as exc:
            log.warning(
                "race: login pre-warm failed for %s (%s) — will authenticate inline at T0",
                course_id,
                exc,
            )
            return None

    async def _prefetch_captcha_for(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
    ) -> None:
        """Pre-solve N CAPTCHA tokens for the primary adapter so the first N ranked
        candidates at T0 each consume a pooled token instead of blocking ~75s on a fresh
        solve. N = scheduler.captcha_prefetch_count. Best-effort: any failure is logged and
        swallowed (book() solves inline). No-op for adapters without a CAPTCHA (TeeItUp, Fake).
        """
        try:
            await adapter.prepare_book(None, request, count=self._scheduler.captcha_prefetch_count)
        except Exception as exc:
            log.warning(
                "race: CAPTCHA pre-fetch failed for %s (%s) — will solve inline in book()",
                course_id,
                exc,
            )

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
                # Change D / PR3: on the race path drop the leading courtesy sleep before
                # the first post-T0 search GET (nothing to space from). Off the race path
                # (watcher/local-demo) the flag is False, preserving anti-bot spacing.
                slots = await adapter.search(request, skip_initial_spacing=self._prefetch_book)
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

    def _terminal_already_booked(
        self, request: BookingRequest, match: ExistingReservation
    ) -> BookingResult:
        """ALREADY_BOOKED terminal for the pre-T0 short-circuit (§3.2). Mirrors the post-T0
        guard's terminal in `_run_course` so both already-booked paths are identical."""
        return BookingResult(
            request_id=request.request_id,
            outcome=BookingOutcome.ALREADY_BOOKED,
            course_id=match.course_id,
            slot=None,
            confirmation_code=match.confirmation_code,
            booked_at=self._clock.now_utc(),
            attempts=0,
        )

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
