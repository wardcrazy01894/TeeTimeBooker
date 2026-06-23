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
    CancelError,
    CaptchaError,
    InventoryNotPublishedError,
    NoInventoryError,
    RateLimitError,
    SlotGoneError,
)
from .booking_cutoff import REASON_CUTOFF, REASON_SKIP, frozen_reason
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
    from .config import BookingCutoffConfig, OneBookingPolicyConfig, SchedulerConfig

log = logging.getLogger(__name__)

# Watcher-specific stop reason (the cutoff/skip reasons come from booking_cutoff as
# REASON_CUTOFF / REASON_SKIP — keyed by the constants below so the dict can't drift from
# what `_should_stop_acting_on_date` / `frozen_reason` actually return).
_REASON_DEADLINE = "deadline"

# Distinct log line per stop-acting reason (LEADTIME_SKIP_PLAN): an operator reading a run can
# tell WHY a date was frozen — deadline vs hard cutoff vs an explicit skip — rather than seeing
# one generic "stopping" message. Keyed by the reason `_should_stop_acting_on_date` returns.
_STOP_ACTING_MESSAGES: dict[str, str] = {
    _REASON_DEADLINE: "watch: target_date %s has passed, stopping",
    REASON_CUTOFF: "watch: target_date %s frozen by 4PM-day-before cutoff, stopping",
    REASON_SKIP: "watch: target_date %s skipped (TEETIME_SKIP_DATES), stopping",
}


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
        booking_cutoff: BookingCutoffConfig | None = None,
        skip_dates: frozenset[date] = frozenset(),
    ) -> None:
        self._adapters = adapters
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._scheduler = scheduler
        self._watch_config = watch_config
        self._creds = creds or {}
        self._policy = policy
        # Hard booking cutoff (LEADTIME_SKIP_PLAN F1). None = no cutoff (back-compat for
        # existing callers/tests). When set, a date past its cutoff is frozen for BOTH new
        # bookings and upgrades via _should_stop_acting_on_date at the top of check_once.
        self._cutoff = booking_cutoff
        # Skip dates (LEADTIME_SKIP_PLAN F2). A target date in this set is frozen for BOTH new
        # bookings and upgrades — the same stop-acting gate. Defense-in-depth: even if the CLI
        # forgot to filter a skipped date, the orchestrator refuses it (Edge E5).
        self._skip_dates = skip_dates

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
            RateLimitError: re-raised (logged, NOT notified) so the run aborts and
                the 10-min cron cadence is the backoff floor (PLAN §12) — never
                folded into the try-next-course transient path.
            All other exceptions are caught, logged, and result in None return.
        """
        now = self._clock.now_utc()

        # The watcher polls on EVERY run (MULTIDAY PR4) — the old time-of-day polling-hours
        # gate was removed because it blinded us during the 06:00 drop and early-morning
        # cancellations. The deadline gate below is retained (don't poll a past target date).
        # Anti-bot rate limiting is the 10-min cron cadence + the poll_interval_s>=300 floor.
        # Gate: stop-acting — freeze this date (NO new booking AND NO upgrade) if it is past the
        # watch deadline OR past the hard 4PM-day-before booking cutoff (LEADTIME_SKIP_PLAN F1) OR
        # in the operator's skip set (F2). Evaluated ABOVE the Gate-3 upgrade and the search loop,
        # so a frozen date is never booked or upgraded. Each reason logs its OWN distinct line.
        stop_reason = self._should_stop_acting_on_date(now, target_date)
        if stop_reason is not None:
            log.info(_STOP_ACTING_MESSAGES[stop_reason], target_date)
            return None

        # Gate 3: idempotency — already BOOKED in the store (main job or prior watch run).
        # An ALREADY_BOOKED terminal deliberately does NOT short-circuit here: it falls
        # through to _check_course, which does a live list_reservations and so gets BOTH the
        # duplicate reconcile AND a recovery-book if the reservation was cancelled externally.
        # Only a BOOKED terminal short-circuits, so the reconcile crash-net must be run
        # explicitly below (it otherwise lives only in _check_course).
        prior = await self._store.get_terminal(request.request_id, target_date)
        if prior is not None and prior.outcome == BookingOutcome.BOOKED:
            if self._policy is not None and self._policy.enabled:
                try:
                    # CRASH-NET (M1): the blind-POST in-run _cancel_extras can fail and strand a
                    # live duplicate while the booking job still recorded BOOKED for the kept
                    # slot. The Gate-3 short-circuit otherwise bypasses the _check_course
                    # reconcile, so the extra would persist on every watch run. Reconcile here
                    # BEFORE upgrading.
                    await self._reconcile_booked_course(request, target_date, prior)
                    # Policy is active — check whether a higher-priority slot opened up.
                    # NOTE: caller must NOT hold request_lock here; _try_upgrade acquires it.
                    upgraded = await self._try_upgrade(request, target_date, prior)
                    if upgraded is not None:
                        return upgraded
                except ConcurrentRunError:
                    # A concurrent run (e.g. the 6 AM booker) holds request_lock — _try_upgrade's
                    # maybe_upgrade acquire raises it. Defer: the held BOOKED terminal stays valid
                    # and the next 10-min run retries. (The _check_course path swallows this via
                    # its search-loop handler; the Gate-3 short-circuit needs its own — without it
                    # a lock race would crash an otherwise-healthy watch run.)
                    log.debug(
                        "watch: request_lock held by another run during Gate-3 act for %s — "
                        "deferring",
                        request.request_id,
                    )
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

            except RateLimitError as exc:
                # A 429 is NOT a generic transient blip: it is an explicit "back off"
                # from the platform. Do NOT fall through to the next course (that just
                # hammers the same throttled API) and do NOT keep polling more dates this
                # run — re-raise so the run aborts cleanly and the 10-min cron cadence
                # becomes the backoff floor (PLAN §12). Unlike Captcha/Auth it is not
                # operator-actionable, so we log (honouring retry-after) but do not notify.
                log.warning(
                    "watch: rate-limited on course %s (retry_after=%ss) — backing off, "
                    "deferring to the next cron run",
                    course_id,
                    exc.retry_after_s,
                )
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
                # CRASH-NET backstop (BLIND_POST_PLAN PR4): >1 live reservation for this
                # (target_date, party_size) means a crash (or a failed in-run cancel) left
                # duplicates — the blind-POST happy path cancels surplus reservations in-run
                # (_cancel_extras). Reconcile down to one: keep the best-ranked, cancel the
                # rest, under the advisory lock. Gated on the same policy.enabled as the
                # upgrade cancel (we never cancel a held booking unless the operator opted in).
                if len(matching) > 1:
                    matching = await self._reconcile_duplicate_reservations(
                        adapter, course_id, request, target_date, matching
                    )
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
        except (NoInventoryError, InventoryNotPublishedError) as exc:
            # Don't swallow silently: an unattended watch run that finds nothing must
            # leave a breadcrumb so "why didn't it book?" is answerable from logs alone.
            log.info(
                "watch: no published inventory on course %s for %s (%s); trying next course",
                course_id,
                target_date,
                type(exc).__name__,
            )
            return None  # Nothing on this course; try next.

        candidates = [
            c for c in rank_slots_for_request(slots, scoped) if c.tee_time.date() == target_date
        ]
        if not candidates:
            return None

        return await self._book_candidates(adapter, scoped, target_date, candidates)

    # --- multi-reservation reconcile (CRASH-NET backstop, BLIND_POST_PLAN PR4) ----

    async def _reconcile_booked_course(
        self,
        request: BookingRequest,
        target_date: date,
        prior: BookingResult,
    ) -> None:
        """Run the duplicate-reservation reconcile for a date that already has a BOOKED
        store terminal (the Gate-3 short-circuit path).

        Why this exists: a failed in-run _cancel_extras (Orchestrator blind-POST path) can
        leave a live duplicate while the booking job still records BOOKED for the kept slot.
        Gate 3 returns on that terminal without reaching _check_course — where the >1-reservation
        reconcile crash-net lives — so without this the stranded extra would never be collapsed.

        Best-effort and cheap on the only live adapter: ForeUP authenticate() is idempotent and
        list_reservations() reads the login-response cache, so the redundant fetch the subsequent
        _try_upgrade also makes costs ~nothing.

        Exception contract: Captcha/Auth/RateLimit surface to the caller (matching the adjacent
        _try_upgrade and the search-loop handler). A TRANSIENT blip (network/timeout/unexpected
        HTTP) during this pre-check must NOT crash an otherwise-healthy BOOKED run — check_once's
        docstring promises "all other exceptions are caught ... None return" — so it is logged and
        the reconcile is skipped for this cycle (the next 10-min run retries). A CancelError on an
        extra never crashes either (handled inside _reconcile_duplicate_reservations)."""
        course_id = prior.course_id
        if course_id is None:
            return
        adapter = self._adapters.get(course_id)
        if adapter is None:
            return
        try:
            creds = self._creds.get(course_id)
            if creds is not None:
                await adapter.authenticate(creds)
            existing = await adapter.list_reservations()
        except (CaptchaError, AuthError, RateLimitError):
            # Operator-action / explicit-backoff errors must surface exactly as they do from the
            # search loop and the adjacent _try_upgrade — re-raise.
            raise
        except Exception as exc:
            # Transient blip — skip the reconcile this cycle rather than crash a healthy run.
            log.warning(
                "watch: transient error during duplicate reconcile for %s on %s: %s — skipping",
                course_id,
                target_date,
                exc,
                exc_info=True,
            )
            return
        matching = [
            r
            for r in existing
            if r.tee_time.date() == target_date and r.party_size == len(request.players)
        ]
        if len(matching) > 1:
            # Return value unused: the Gate-3 path returns the store BOOKED terminal, not a
            # synthesized booking, so the reconciled list is not needed here.
            await self._reconcile_duplicate_reservations(
                adapter, course_id, request, target_date, matching
            )

    async def _reconcile_duplicate_reservations(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
        target_date: date,
        matching: list[ExistingReservation],
    ) -> list[ExistingReservation]:
        """Collapse >1 live reservation for (target_date, party_size) down to one.

        Keep the best-ranked reservation (same midpoint-distance ranking the booking
        path uses) and cancel the rest, under the advisory lock. This is the BACKSTOP —
        the blind-POST happy path cancels surplus reservations in-run (Orchestrator.
        _cancel_extras); this recovers a crash (or a prior failed cancel) that left
        duplicates.

        Returns the surviving reservation(s):
        - ``[kept]`` on success (the kept one is returned even if an extra's cancel
          failed — that extra is logged CRITICAL and retried on the next watch run);
        - ``matching`` unchanged if the request_lock was contended (another run is
          already acting — let it reconcile rather than racing it).

        Best-effort: a CancelError never crashes the run.
        """
        ranked = self._rank_reservations(matching, course_id, request, target_date)
        keep, extras = ranked[0], ranked[1:]
        try:
            async with self._store.request_lock(request.request_id):
                for res in extras:
                    try:
                        await adapter.cancel_reservation(res.confirmation_code)
                    except CancelError:
                        log.critical(
                            "watch: failed to cancel duplicate reservation %s on %s — "
                            "manual cleanup may be required",
                            res.confirmation_code,
                            target_date,
                        )
        except ConcurrentRunError:
            log.debug(
                "watch: request_lock held by another run during reconcile for %s — deferring",
                request.request_id,
            )
            return matching

        log.info(
            "watch: reconciled %d duplicate reservations on %s — kept %s, cancelled %d",
            len(matching),
            target_date,
            keep.confirmation_code,
            len(extras),
        )
        return [keep]

    def _rank_reservations(
        self,
        reservations: list[ExistingReservation],
        course_id: CourseId,
        request: BookingRequest,
        target_date: date,
    ) -> list[ExistingReservation]:
        """Order reservations best-first using the SAME midpoint-distance ranking the
        booking path uses (rank_slots_for_request). In-window reservations rank first;
        any out-of-window ones (dropped by the window filter) are appended by ascending
        tee_time so the reconcile order is TOTAL and deterministic (never silently drops
        a duplicate from the cancel set)."""
        scoped = dc_replace(request, target_dates=(target_date,))
        by_slot_id: dict[SlotId, ExistingReservation] = {}
        slots: list[TeeTimeSlot] = []
        for res in reservations:
            slot = self._synthesize_managed_booking(res, course_id, request).slot
            assert slot is not None  # _synthesize_managed_booking always sets a slot
            by_slot_id[slot.slot_id] = res
            slots.append(slot)

        ranked_slots = rank_slots_for_request(slots, scoped)
        ranked = [by_slot_id[s.slot_id] for s in ranked_slots]
        ranked_ids = {s.slot_id for s in ranked_slots}
        leftover = sorted(
            (res for sid, res in by_slot_id.items() if sid not in ranked_ids),
            key=lambda r: r.tee_time,
        )
        return ranked + leftover

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

        The synthesized slot's tee_time is used by _current_booking_tier() to
        determine which priority window the existing booking falls into (and, for
        the within-window pass, how far from that window's midpoint it sits).
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

    def _should_stop_acting_on_date(self, now: datetime, target_date: date) -> str | None:
        """The single 'stop acting on this date' predicate (LEADTIME_SKIP_PLAN).

        Returns a distinct REASON string when the date is frozen (NO new booking AND NO
        upgrade), or None when it is still actionable. Evaluated at the top of `check_once`,
        ABOVE both the Gate-3 store-BOOKED upgrade and the `_check_course` live-reservation
        upgrade, so a frozen date never reaches `book()` or cancel+rebook.

        The deadline gate is watcher-specific; the cutoff + skip decision is delegated to the
        SHARED `booking_cutoff.frozen_reason` primitive (the booker's `should_book_today` routes
        through the same one), so the two callers can't diverge. `now` is passed through (not
        re-read), so the deadline and cutoff are evaluated at the same instant.

        Reasons (each maps to its OWN `check_once` log line so an operator can tell WHY a date
        froze — do NOT collapse to one generic message):
        - ``_REASON_DEADLINE``: past the watch deadline (the round has happened / day after target).
        - ``REASON_CUTOFF``: past the hard 4PM-day-before booking cutoff (F1). Checked before skip
          inside `frozen_reason`, so a date both past-cutoff and skipped reports the cutoff.
        - ``REASON_SKIP``: the date is in the operator's skip set (F2, TEETIME_SKIP_DATES). The
          defense-in-depth backstop — the CLI also drops skipped dates before polling.
        """
        if self._is_past_watch_deadline(now, target_date):
            return _REASON_DEADLINE
        # Cutoff + skip share the booker's primitive (frozen_reason) so the two callers can't
        # diverge; the deadline above is watcher-specific. `now` is passed (not re-read) so the
        # deadline and cutoff are evaluated at the same instant.
        return frozen_reason(
            now,
            target_date,
            timezone=self._scheduler.timezone,
            cutoff=self._cutoff,
            skip_dates=self._skip_dates,
        )
