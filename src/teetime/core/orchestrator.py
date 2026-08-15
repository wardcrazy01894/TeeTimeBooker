"""Top-level booking flow. Owns the race, the fallback, the idempotency check.

v0 (M2.T1) implements:
- request_lock for concurrent-run defense
- idempotency short-circuit on existing terminal
- pre-T0 busy_wait
- per-course: authenticate -> list_reservations (PLAN §9 layer 2) -> search
  with poll loop -> filter/pick best -> book (or DRY_RUN gate)
- terminal persistence + notifier delivery

An UNCERTAIN book (timeout/5xx — the POST may or may not have landed) is NOT
reconciled synchronously in-run. This file deliberately keeps `book()`
non-retryable (it raises out), so an ambiguous POST is never re-fired within a
run — which means the booker can never double-book itself. Reconciliation is
the WATCHER's job, asynchronously: every ~10-min poll re-authenticates (rebuilding
ForeUP's login-response reservation snapshot), checks `list_reservations`, and
recovers either way — a silently-landed booking is detected, a genuinely-failed
one is re-booked, and a duplicate is collapsed by the watch reconcile crash-net.
The ~10-min unknown window is harmless for an unattended single-user bot (nobody
acts on the gap), so the dedicated synchronous post-mortem path (formerly planned
as M2.T3) was cut. Consequence: the watcher's uptime is load-bearing — it is now
the system of record for reconciliation. See PLAN.md §9.1.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from .adapter import (
    AuthStateReportable,
    BlindPostCapable,
    InventoryNotPublishedError,
    NoInventoryError,
    RateLimitError,
    ReservationCacheRefreshable,
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


def _blind_outcome_label(result: BookingResult | BaseException) -> str:
    """Compact outcome tag for the per-POST blind-burst diagnostic line.

    Deliberately coarse: the line exists to correlate T0 OFFSET with success/failure
    (STAGGER_PLAN §3.3), and the detailed per-failure logging already happens below.
    PII-free — slot ids and exception types only.
    """
    if isinstance(result, BookingResult):
        return result.outcome.value.upper()
    if isinstance(result, SlotGoneError):
        return "gone"
    return type(result).__name__


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
                # Idempotency replay: a prior terminal exists. Log it too (same line as the
                # main-loop terminal below) so a re-run's resolved decision is in the app log,
                # not silently returned. (The pre-T0 already-booked short-circuit has its own
                # `race: short-circuited pre-T0` line, so it is intentionally not double-logged.)
                log.info(
                    "booking: run terminal (idempotent replay) outcome=%s course=%s "
                    "confirmation=%s date=%s",
                    prior.outcome.value,
                    prior.course_id,
                    prior.confirmation_code,
                    resolved_date,
                )
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
                except RateLimitError as exc:
                    # A 429 anywhere in this course's flow (search/blind fallback/book) means the
                    # platform is throttling us. A 429 is REJECTED before processing, so no
                    # reservation was created (unlike the §9 UNCERTAIN timeout/5xx) — it is
                    # safe to treat like an empty course: log + try the next preference, and
                    # if none book, record a clean NO_INVENTORY terminal (+ notify) instead of
                    # crashing the job with an uncaught error and no record. CaptchaError /
                    # AuthError are deliberately NOT caught here — they are operator-action
                    # errors and propagate for a non-zero exit (a broken CAPTCHA/credential
                    # pipeline must not hide behind a clean NO_INVENTORY).
                    log.warning(
                        "course %s: rate-limited (429, retry_after=%ss) — skipping this course",
                        course_id,
                        exc.retry_after_s,
                    )
                    continue
                if result is not None:
                    break

            if result is None:
                result = self._terminal_no_inventory(request)

            # Log the run's resolved terminal to the structured app log (stderr → Log
            # Analytics). The ConsoleNotifier writes to a SEPARATE stdout stream and the
            # `run` CLI only logs on FAILURE, so without this the orchestrator's decision
            # (which course won; BOOKED vs ALREADY_BOOKED vs NO_INVENTORY) was absent from
            # the app log — mirrors the watcher's booked-line. confirmation_code is a
            # TTB:-prefixed id, never PII.
            log.info(
                "booking: run terminal outcome=%s course=%s confirmation=%s date=%s",
                result.outcome.value,
                result.course_id,
                result.confirmation_code,
                resolved_date,
            )
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

        # Blind-POST fast path (BLIND_POST_PLAN.md §6/§11). Two-part capability gate
        # (isinstance AND the boolean) + race path + PRIMARY course + not-dry-run + a
        # positive fan-out cap. Everything else (non-capable, fallback course, dry-run,
        # watcher/local-demo) takes the unchanged search path below.
        if self._should_blind_post(adapter, course_id, request):
            return await self._blind_post_course(adapter, course_id, request)

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

        return await self._book_from_candidates(adapter, course_id, candidates, request)

    async def _book_from_candidates(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        candidates: list[TeeTimeSlot],
        request: BookingRequest,
    ) -> BookingResult:
        """Sequential book loop over ranked candidates. Returns the first BOOKED; on
        SlotGoneError tries the next. If every candidate is gone, raises
        `_CourseSkippedError` so `run()` advances to the next course (NO_INVENTORY if
        none book) — never an uncaught crash. Shared by the search path and the blind
        path's search fallback."""
        # candidates is non-empty here (callers raise _CourseSkippedError on empty), so
        # the loop runs at least once and last_exc is set if we reach the exhaustion case.
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

    # --- blind-POST fast path (BLIND_POST_PLAN.md §6/§7/§11) -----------

    @staticmethod
    def _is_blind_capable(adapter: CourseAdapter) -> bool:
        """Blind-POST gate: the explicit `capabilities.blind_post` flag (NOT isinstance).
        A True flag promises captcha_pool_size() + synthesize_blind_slots() exist, so callers
        cast to `BlindPostCapable` to invoke them. See AdapterCapabilities / BLIND_POST_PLAN.md
        §3 — this replaces the old `isinstance(adapter, BlindPostCapable) and supports_blind_post`
        double-gate (isinstance was always True for any ForeUP adapter; the boolean was the real,
        hidden guard)."""
        return adapter.capabilities.blind_post

    def _should_blind_post(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
    ) -> bool:
        """Gate: blind-capable AND race path AND PRIMARY course AND not dry-run AND a
        positive fan-out cap. Any miss → the unchanged search path."""
        if request.dry_run or not self._prefetch_book:
            return False
        if self._scheduler.blind_post_max_count <= 0:
            return False
        if not self._is_blind_capable(adapter):
            return False
        primary = self._primary_adapter(request)
        return primary is not None and primary[0] == course_id

    async def _blind_post_course(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
    ) -> BookingResult:
        """Fire the top-N ranked in-window blind book POSTs CONCURRENTLY at T0, keep the best
        that books and cancel the rest IN-RUN. If zero book, re-guard against a landed-but-
        uncertain POST, then fire a FRESH search (AFTER the re-guard) and fall back to the
        sequential search-book loop. See BLIND_POST_PLAN.md §6 + RESEARCH_FALLBACK_PLAN.

        NO concurrent hedge search (RESEARCH_FALLBACK_PLAN §2 Q1): the happy path issues zero
        search GETs, and the 0-booked path issues exactly one FRESH search post-re-guard — the
        freshest possible snapshot, taken after the re-auth, with no shared-client cookie race.

        Runs inside `run()`'s advisory lock (no new lock acquisition; the reconcile +
        record_terminal happen under that lock, §6 lock discipline)."""
        # The gate (`_should_blind_post`) guarantees BlindPostCapable; narrow for mypy.
        capable = cast(BlindPostCapable, adapter)
        target_date = request.target_dates[0]
        blind_slots = capable.synthesize_blind_slots(
            request, target_date, max_count=self._scheduler.blind_post_max_count
        )
        # N = min(in-window grid count, pooled tokens in hand). Each book() pops one pooled
        # token synchronously; with N <= pool none inline-solves at T0 (§5 token budget). The
        # blind_post_fallback_token_reserve tokens beyond N stay pooled for the fresh-search
        # fallback below (RESEARCH_FALLBACK_PLAN §2 Q3).
        n = min(len(blind_slots), capable.captcha_pool_size())
        # Re-rank before pairing offsets. `synthesize_blind_slots` is CONTRACTED to return
        # ranked slots (Mangrove Bay ranks with this same `rank_slots_for_request`), so for
        # the live adapter this is a no-op. It is applied anyway because the stagger's
        # safety property depends on it: offsets ascend with position, so the BEST slot must
        # fire FIRST or ForeUP's 1-reservation-per-day rule could reject the better sibling
        # in favour of a worse one that happened to POST earlier (STAGGER_PLAN §2.2). That
        # invariant is too important to leave resting on an adapter convention.
        fire = rank_slots_for_request(blind_slots[:n], request)

        # Launch ONLY the blind burst at T0 — no concurrent hedge search. The tasks are
        # created simultaneously; each one then sleeps to its OWN offset relative to T0
        # before POSTing (STAGGER_PLAN §3.2), so the burst spans the release boundary
        # instead of point-sampling it.
        t0 = self._compute_t0()
        offsets = self._stagger_offsets_for(len(fire))
        # Filled in by each task at its SEND instant; the diagnostic below reports these
        # MEASURED offsets, never the planned ones (see _fire_blind_post's docstring — on a
        # late-landing run every POST goes out at once and the planned ladder is a lie).
        sent_offsets_ms: list[int | None] = [None] * len(fire)
        blind_tasks = [
            asyncio.create_task(
                self._fire_blind_post(
                    adapter,
                    s,
                    request,
                    offset_ms=off,
                    t0=t0,
                    sent_offsets_ms=sent_offsets_ms,
                    index=i,
                )
            )
            for i, (s, off) in enumerate(zip(fire, offsets, strict=True))
        ]
        log.info(
            "course %s: blind-POST firing %d book POST(s), staggered at T0 offsets %s ms",
            course_id,
            len(fire),
            offsets,
        )

        blind_results = await asyncio.gather(*blind_tasks, return_exceptions=True)
        booked: list[BookingResult] = []
        gone = 0  # SlotGoneError count — logged in aggregate so a total wipeout is diagnosable
        # A BaseException surfacing in gather's RESULTS — a CHILD book() task raising
        # asyncio.CancelledError, or any other BaseException subclass — is CAPTURED here, not
        # re-raised mid-loop: a booked SIBLING later in `fire` must be secured first, or it would
        # be left live on the server with no in-run keep/record/cancel (full-repo-scan #e1). It is
        # propagated after the booked branch below, but only if nothing booked. SCOPE (verified):
        # gather(return_exceptions=True) does NOT route KeyboardInterrupt/SystemExit here — it
        # re-raises those out of the `await` before this loop runs — and a default SIGTERM kills
        # the process with no Python exception; the parent task's OWN cancellation likewise
        # propagates out of the await, not into the results. So this guards the exotic
        # child-raised-CancelledError / BaseException-subclass case — defensive depth, not a hot
        # path (nothing in the current code cancels individual blind_tasks; the hedge was removed).
        pending_base_exc: BaseException | None = None
        # blind_results is in the same order as `fire` (gather preserves task order),
        # so each result pairs with the slot whose POST produced it.
        for slot, off, sent, r in zip(fire, offsets, sent_offsets_ms, blind_results, strict=True):
            # THE diagnostic line (STAGGER_PLAN §3.3). Pairing each POST's T0 offset with
            # its outcome is what distinguishes a pre-open rejection (a CLEAN CUTOFF —
            # everything at or before some offset gone, everything after booked) from a
            # genuine slot race (outcomes unordered w.r.t. offset). ForeUP returns the same
            # 400 body for both, and the server `Date` header's 1-second resolution cannot
            # separate them, so this ordering is the only available signal.
            #
            # Reports the MEASURED send offset. `sent` is None only if the task never reached
            # its send (an exception raised in the sleep itself) — then say so rather than
            # substitute the planned value, which would assert a send that never happened.
            # `planned` is carried alongside so sleep jitter and late starts are both visible.
            log.info(
                "course %s: blind-POST sent %s (planned %+dms) slot %s → %s",
                course_id,
                f"{sent:+d}ms" if sent is not None else "NEVER",
                off,
                slot.slot_id,
                _blind_outcome_label(r),
            )
            if isinstance(r, BookingResult) and r.outcome == BookingOutcome.BOOKED:
                booked.append(r)
            elif isinstance(r, SlotGoneError):
                gone += 1  # slot claimed between synthesize and book — drop
                continue
            elif isinstance(r, BookingResult):  # pragma: no cover - defensive (#e2)
                # Non-BOOKED BookingResult: unreachable today (a live-mode book() returns BOOKED
                # or raises), but if that ever changes, log rather than drop it silently.
                log.warning(
                    "course %s: blind POST for slot %s returned non-BOOKED outcome %s — dropping",
                    course_id,
                    slot.slot_id,
                    r.outcome,
                )
            elif isinstance(r, Exception):
                # UNCERTAIN (timeout/5xx): the POST MAY have landed. Drop this candidate;
                # the guards below (a sibling booked, or the re-guard list_reservations)
                # prevent a double-book. Anything that slips past them is reconciled by the
                # watcher on its next ~10-min poll (re-auth + list_reservations + the duplicate
                # crash-net) — there is no synchronous post-mortem path. Name the slot so an
                # operator can tell WHICH tee time may have landed silently.
                log.warning(
                    "course %s: blind POST for slot %s raised %r — dropping candidate",
                    course_id,
                    slot.slot_id,
                    r,
                )
            elif isinstance(r, BaseException):
                # A captured BaseException (a child CancelledError or other BaseException
                # subclass — see the scope note above). Capture it; do NOT raise here (would
                # abandon a booked sibling). Re-raised after the booked branch iff nothing booked.
                # Log the capture NOW: when a sibling books, this branch is otherwise the only
                # gather outcome with no breadcrumb — the deferred exception would vanish from
                # the logs entirely (full-repo-scan 2026-07-09 observability M2).
                log.warning(
                    "course %s: blind POST for slot %s surfaced BaseException %r — captured "
                    "(deferred; re-raised only if nothing booked)",
                    course_id,
                    slot.slot_id,
                    r,
                )
                pending_base_exc = r

        if gone:
            # Aggregate, not per-slot: at a competitive drop many/all blind POSTs can lose the
            # race. One line tells an operator how many of the N fired came back slot-gone. A
            # TOTAL wipeout (every fired POST gone AND nothing booked) is the prime "why no 6am
            # booking" signal, so escalate it to WARNING — distinct from a partial loss (INFO).
            total_wipeout = gone == len(fire) and not booked
            log.log(
                logging.WARNING if total_wipeout else logging.INFO,
                "course %s: blind-POST %d of %d slot(s) came back slot-gone (claimed pre-book)%s",
                course_id,
                gone,
                len(fire),
                " — TOTAL wipeout, falling back to a fresh search" if total_wipeout else "",
            )

        if booked:
            # FAST PATH WON. Keep the rank-0 booked slot, cancel the rest by their own id.
            best, extras = self._keep_best(booked, request)
            await self._cancel_extras(adapter, extras)
            log.info(
                "course %s: blind-POST booked %d, kept %s, cancelled %d extra(s)",
                course_id,
                len(booked),
                best.confirmation_code,
                len(extras),
            )
            return best

        # 0 BLIND BOOKED, so there is no reservation to secure. If a BaseException was captured
        # during the burst, propagate it NOW — we deferred it only to protect a booked sibling,
        # and there is none (#e1).
        if pending_base_exc is not None:
            raise pending_base_exc

        # 0 BLIND BOOKED. A POST may have LANDED-but-UNCERTAIN. Re-guard with a FORCED-FRESH
        # read (refresh_reservations rebuilds ForeUP's login cache — a plain idempotent re-auth
        # would not, must-fix 1/3) BEFORE the search fallback so we never book a second slot on
        # top of a landed one (must-fix 4).
        match = await self._reguard_before_fallback(adapter, course_id, request)
        if match is not None:
            log.info(
                "course %s: blind-POST re-guard found landed reservation %s — ALREADY_BOOKED",
                course_id,
                match.confirmation_code,
            )
            return BookingResult(
                request_id=request.request_id,
                outcome=BookingOutcome.ALREADY_BOOKED,
                course_id=course_id,
                slot=None,
                confirmation_code=match.confirmation_code,
                booked_at=self._clock.now_utc(),
                attempts=0,
            )

        # CORRECTNESS FALLBACK: fire a FRESH search NOW — strictly after the re-guard re-auth,
        # so it sees the freshest post-burst availability (not a stale ~T0 hedge snapshot) and
        # never races the shared-client cookie rotation (RESEARCH_FALLBACK_PLAN §2 Q1/Q2). The
        # real search is authoritative; blind booked zero, so the blind and search book-sets are
        # mutually exclusive — no same-slot dedup needed. Log the transition so the path taken is
        # explicit in the logs rather than inferred from the absence of a reguard-match line.
        log.info(
            "course %s: blind-POST booked 0, re-guard clean — firing fresh fallback search",
            course_id,
        )
        slots = await self._poll_for_slots(adapter, request)
        if not slots:
            raise _CourseSkippedError()
        candidates = self._rank_slots(slots, request)
        if not candidates:
            raise _CourseSkippedError()
        return await self._book_from_candidates(adapter, course_id, candidates, request)

    def _keep_best(
        self,
        booked: list[BookingResult],
        request: BookingRequest,
    ) -> tuple[BookingResult, list[BookingResult]]:
        """Pick the rank-0 booked result (by the SAME rank_slots_for_request the search
        path uses) and return (best, extras-to-cancel). Falls back to the first booked if
        ranking yields nothing (defensive — synthesized slots are all in-window)."""
        slots = [r.slot for r in booked if r.slot is not None]
        ranked = rank_slots_for_request(slots, request)
        if not ranked:
            return booked[0], booked[1:]
        best_slot_id = ranked[0].slot_id
        best = next(
            (r for r in booked if r.slot is not None and r.slot.slot_id == best_slot_id),
            booked[0],
        )
        extras = [r for r in booked if r is not best]
        return best, extras

    async def _cancel_extras(
        self,
        adapter: CourseAdapter,
        extras: list[BookingResult],
    ) -> None:
        """Cancel each surplus reservation by its OWN confirmation_code (PR0 made the
        teetime_id extraction load-bearing). ANY cancel failure is logged CRITICAL (the user
        then holds >1 reservation) but does NOT abort — we still keep the best, and the PR4
        watch net is the backstop. See BLIND_POST_PLAN.md §7.

        The catch is deliberately ``Exception``, not just ``CancelError``: the real adapter
        can also surface ``RateLimitError`` (a throttled DELETE), ``CaptchaError``, or a raw
        ``httpx.TransportError`` here, and letting ANY of them propagate would discard the
        successfully-booked ``best`` — a 429 turned the run into NO_INVENTORY (+ non-zero
        exit) while the user actually held a live reservation, and a captcha/transport blip
        crashed the job with no terminal. With a booking in hand, nothing a SURPLUS cancel
        does may lose it. full-repo-scan 2026-07-09 correctness H2."""
        for r in extras:
            if r.confirmation_code is None:
                log.critical(
                    "blind-POST: a surplus reservation has no confirmation_code — cannot "
                    "cancel it; the user may hold an extra reservation (course %s)",
                    r.course_id,
                )
                continue
            try:
                await adapter.cancel_reservation(r.confirmation_code)
            except Exception as exc:
                log.critical(
                    "blind-POST: FAILED to cancel surplus reservation %s (%r) — the user "
                    "holds >1 reservation; PR4 watch net will reconcile",
                    r.confirmation_code,
                    exc,
                )

    async def _reguard_before_fallback(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
    ) -> ExistingReservation | None:
        """Force a FRESH reservation snapshot, THEN list_reservations, returning a
        matching reservation if a blind POST landed-but-uncertain.

        Snapshot-before-list order is load-bearing (must-fix 3). Crucially the refresh
        must be a *forced* re-fetch: ForeUP's ``list_reservations()`` reads a cache built
        at login time and ``authenticate()`` is IDEMPOTENT (a 2nd call short-circuits on
        ``_logged_in`` and does NOT rebuild the cache), so a plain ``authenticate()`` here
        would return the STALE pre-burst snapshot and let the fallback book a SECOND
        reservation. Adapters that expose ``ReservationCacheRefreshable`` get the forced
        re-login; others fall back to ``authenticate()`` (sufficient for live-GET stores
        or any adapter that never reaches this path). If the forced re-auth RAISES, this does
        NOT proceed on the existing session — it raises ``_CourseSkippedError`` so the course
        is skipped cleanly (the session is unauthenticated; a fallback book would crash and
        could double-book a landed POST). Never crashes the run; the watcher reconciles."""
        creds = self._creds.get(course_id)
        if creds is not None:
            try:
                if isinstance(adapter, ReservationCacheRefreshable):
                    await adapter.refresh_reservations(creds)
                else:
                    await adapter.authenticate(creds)
            except Exception as exc:
                # The forced re-auth FAILED — the session is now unauthenticated (ForeUP's
                # refresh_reservations resets _logged_in BEFORE re-login, so a failure leaves
                # it False). We can neither trust list_reservations to reveal a landed-but-
                # uncertain blind POST NOR safely book a fallback: book() would raise AuthError
                # on the dead session (crashing the run with no terminal) AND a stale snapshot
                # could let us double-book on top of a landed reservation. SKIP the course
                # cleanly instead; the watcher reconciles any landed reservation on its next
                # ≤10-min poll. full-repo-scan correctness #1.
                log.warning(
                    "course %s: blind-POST re-guard re-auth failed (%s) — skipping the "
                    "fallback this run; watcher will reconcile",
                    course_id,
                    exc,
                )
                raise _CourseSkippedError() from exc
        existing = await adapter.list_reservations()
        return self._first_matching_reservation(existing, request)

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
            # SF#1 (RACE_PREWARM_PLAN §3.1): record the post-T0 re-auth SKIP only if the login
            # ACTUALLY established a session. ForeUP soft-fails on a 400/401/rejected body —
            # authenticate() RETURNS without raising and leaves _logged_in False. Recording the
            # skip then would suppress the T0 re-auth and book() would raise AuthError on a
            # never-logged-in session, turning a transient 401 into a lost booking. Adapters with
            # no soft-fail path (not AuthStateReportable) treat a clean return as success.
            if not self._login_established(adapter):
                log.warning(
                    "race: login pre-warm for %s returned but established no session (soft "
                    "login failure) — will authenticate inline at T0",
                    course_id,
                )
                return None
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

    @staticmethod
    def _login_established(adapter: CourseAdapter) -> bool:
        """True if the adapter's last authenticate() established a real session.

        An adapter that reports auth state (``AuthStateReportable``) must say so via
        ``is_authenticated`` — this distinguishes ForeUP's soft login failure (returns
        without raising, no session) from a real login. An adapter that does NOT report
        state has no soft-fail path, so a clean ``authenticate()`` return IS success."""
        if isinstance(adapter, AuthStateReportable):
            return adapter.is_authenticated
        return True

    async def _prefetch_captcha_for(
        self,
        adapter: CourseAdapter,
        course_id: CourseId,
        request: BookingRequest,
    ) -> None:
        """Pre-solve N CAPTCHA tokens for the primary adapter so the first N ranked
        candidates at T0 each consume a pooled token instead of blocking ~75s on a fresh
        solve. N = scheduler.captcha_prefetch_count for the single-POST race path, but SCALES
        to min(blind_post_max_count, in-window grid count) when the primary is blind-capable
        (BLIND_POST_PLAN.md §5/OQ3) so every concurrent blind POST has a token in hand. Best-
        effort: any failure is logged and swallowed (book() solves inline). No-op for adapters
        without a CAPTCHA (TeeItUp, Fake).
        """
        count = self._captcha_prefetch_count_for(adapter, request)
        try:
            await adapter.prepare_book(None, request, count=count)
        except Exception as exc:
            log.warning(
                "race: CAPTCHA pre-fetch failed for %s (%s) — will solve inline in book()",
                course_id,
                exc,
            )

    def _captcha_prefetch_count_for(
        self,
        adapter: CourseAdapter,
        request: BookingRequest,
    ) -> int:
        """Tokens to pre-solve for the primary adapter. For a blind-capable primary (race,
        not dry-run, positive cap), scale to min(blind_post_max_count, in-window grid count)
        for the blind burst PLUS blind_post_fallback_token_reserve spare tokens that REMAIN
        pooled for the 0-booked fresh-search fallback (RESEARCH_FALLBACK_PLAN §2 Q3) — so the
        burst pops its N and the fallback book still finds a pooled token instead of a ~75 s
        inline solve. Otherwise the single-POST race depth (captcha_prefetch_count). A 0-grid
        blind case falls back to the single-POST depth (NO reserve added) so the search
        fallback still has tokens (§5)."""
        default = self._scheduler.captcha_prefetch_count
        if request.dry_run or self._scheduler.blind_post_max_count <= 0:
            return default
        if not self._is_blind_capable(adapter):
            return default
        # NOTE: this prefetch-time call (T0-lead) and `_blind_post_course`'s burst-time call
        # (T0) both invoke synthesize_blind_slots with the same params. For MB's STATIC morning
        # grid they are deterministic — len here == the burst's len — so the reserve tokens are
        # guaranteed to survive the burst (burst N = min(len, pool) = len; reserve = pool - N).
        # A FUTURE dynamic-grid adapter that could return MORE slots at T0 than at prefetch time
        # would let the larger burst consume the reserve — revisit this invariant before
        # onboarding one (RESEARCH_FALLBACK_PLAN §2 Q3).
        blind_slots = cast(BlindPostCapable, adapter).synthesize_blind_slots(
            request, request.target_dates[0], max_count=self._scheduler.blind_post_max_count
        )
        count = min(self._scheduler.blind_post_max_count, len(blind_slots))
        if count <= 0:
            return default
        return count + self._scheduler.blind_post_fallback_token_reserve

    # --- timing ---------------------------------------------------------

    def _compute_t0(self) -> datetime:
        """T0 = today (in scheduler.timezone) at scheduler.fire_time, as UTC.

        Computed against the injected clock so FakeClock-driven tests are deterministic.
        """
        tz = ZoneInfo(self._scheduler.timezone)
        local_now = self._clock.now_utc().astimezone(tz)
        target_local = datetime.combine(
            local_now.date(),
            self._scheduler.fire_time,
            tzinfo=tz,
        )
        return target_local.astimezone(UTC)

    def _compute_t0_minus_early(self) -> datetime:
        """The busy-wait target: T0 minus early_arrival_ms."""
        return self._compute_t0() - timedelta(milliseconds=self._scheduler.early_arrival_ms)

    def _stagger_offsets_for(self, count: int) -> list[int]:
        """The per-POST fire offsets (ms relative to T0) for a burst of `count` slots,
        paired positionally with the RANKED slots (STAGGER_PLAN §3.1).

        Surplus slots beyond the configured list reuse the LAST offset, so widening
        `blind_post_max_count` degrades the tail to simultaneous firing rather than
        silently dropping POSTs. An empty config list means "fire everything on busy-wait
        completion" — expressed as the wakeup offset itself so the caller has one uniform
        code path and `_fire_blind_post` computes a non-positive (no-sleep) delay.
        """
        wakeup = -self._scheduler.early_arrival_ms
        stagger = self._scheduler.blind_post_stagger_ms
        if not stagger:
            return [wakeup] * count
        raw = [stagger[min(i, len(stagger) - 1)] for i in range(count)]
        # Clamp to the busy-wait wakeup: an offset before it cannot be honoured (we are not
        # awake), and `_fire_blind_post` would fire it immediately anyway. Clamping HERE —
        # rather than letting the delay go non-positive silently — is what keeps the
        # offset→outcome diagnostic honest, because these are the values that get logged.
        effective = [max(off, wakeup) for off in raw]
        if effective != raw:
            log.warning(
                "blind-POST stagger offsets %s ms precede the busy-wait wakeup (%d ms); "
                "clamped to %s ms — raise scheduler.early_arrival_ms to honour them",
                raw,
                wakeup,
                effective,
            )
        return effective

    async def _fire_blind_post(
        self,
        adapter: CourseAdapter,
        slot: TeeTimeSlot,
        request: BookingRequest,
        *,
        offset_ms: int,
        t0: datetime,
        sent_offsets_ms: list[int | None] | None = None,
        index: int = 0,
    ) -> BookingResult:
        """Sleep until `t0 + offset_ms`, then issue this slot's blind book POST.

        A non-positive delay fires IMMEDIATELY — a cron that lands past an offset must
        never wait. The sleep goes through the injected Clock, so `FakeClock` drives it.

        Records the ACTUAL send offset (ms from T0, measured immediately before the POST)
        into ``sent_offsets_ms[index]``. That measurement — not the planned offset — is what
        the per-POST diagnostic reports, for two reasons:

        1. **A late-starting run makes the planned offset a LIE.** If the cron lands past T0
           (documented reality: a mid-deploy fire, or a run starting past `T0 - lead`), every
           delay is non-positive and all N POSTs go out SIMULTANEOUSLY. Logging the planned
           ladder would show outcomes spread across three offsets that never happened, and an
           operator reading a 0/N would conclude "unordered ⇒ not the boundary" — the exact
           wrong call, with the next week's fix aimed at the wrong thing.
        2. It absorbs `Clock.sleep` scheduling jitter, so any blurring of the ladder is
           self-documenting rather than assumed absent.
        """
        delay = ((t0 + timedelta(milliseconds=offset_ms)) - self._clock.now_utc()).total_seconds()
        if delay > 0:
            await self._clock.sleep(delay)
        if sent_offsets_ms is not None:
            # Measured at the send instant, AFTER any sleep. Distinct indices, single-threaded
            # event loop → no interleaving hazard between the concurrent burst tasks.
            sent = (self._clock.now_utc() - t0).total_seconds() * 1000.0
            sent_offsets_ms[index] = round(sent)
        return await adapter.book(slot, request)

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
                    log.info(
                        "search: no slots found for %s — inventory still unpublished at "
                        "max_poll_seconds deadline (%ss); giving up on this course",
                        request.target_dates[0],
                        self._scheduler.max_poll_seconds,
                    )
                    return []
                await self._clock.sleep(poll)
                continue
            except NoInventoryError:
                log.info(
                    "search: no slots found for %s — course reported no inventory; "
                    "giving up on this course",
                    request.target_dates[0],
                )
                return []
            if slots:
                return slots
            if self._clock.now_utc() >= deadline:
                log.info(
                    "search: no slots found for %s — search returned empty through "
                    "max_poll_seconds deadline (%ss); giving up on this course",
                    request.target_dates[0],
                    self._scheduler.max_poll_seconds,
                )
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
