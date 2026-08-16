"""CourseAdapter contract. Adding a new course is a new file implementing this Protocol.

Two operations: search() finds candidate slots, book() commits one. Adapters own
auth, HTTP transport, retry-of-transient (network blips), and translating
course-specific errors into our typed exceptions. They do NOT own:
- scheduling (the orchestrator wakes them at T0)
- cross-course fallback (orchestrator)
- idempotency (BookingStore)
- notifications
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol, runtime_checkable

from .models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
)

# Why a booking POST was definitively rejected. See SlotGoneError for what each means
# and why the distinction is load-bearing for reading a staggered blind burst.
SlotGoneReason = Literal["unavailable", "daily_limit", "conflict", "unknown"]


class AdapterError(Exception):
    """Base for adapter-raised errors. Orchestrator catches and routes to outcome."""


class InventoryNotPublishedError(AdapterError):
    """The 7-day window hasn't opened yet (HTTP 200 + empty list, or 4xx specific to
    'too far in advance'). Distinct from NoInventoryError because it warrants a poll-retry.
    """


class NoInventoryError(AdapterError):
    """Inventory IS published but nothing matches the request (window/players/price)."""


class AuthError(AdapterError):
    """Credentials rejected, session expired, or account locked."""


class CaptchaError(AdapterError):
    """A captcha challenge was returned. v0 stops here and notifies the user."""


class OtpChallengeError(CaptchaError):
    """The platform demanded an emailed one-time booking code to complete the booking
    (Mangrove Bay email-OTP, announced 2026-07-15).

    The challenge is UI-only today — the 2026-07-15 live recon confirmed the direct
    API book POST is unchallenged — so this error firing means ForeUP EXTENDED
    enforcement to the API path. It subclasses CaptchaError deliberately, so the
    CaptchaError operator-loud paths fire for free: the booking run() does not
    catch it (clean non-zero exit) and the watcher's check_once notify+re-raises.
    Two scoped exceptions inherit CaptchaError's PRE-EXISTING softer handling:
    the UpgradeOrchestrator's rebook-after-cancel wraps book() in a log-and-continue
    (the accepted upgrade-loss risk — a challenge there is a WARNING, not a crash),
    and a challenge on a BLIND-burst POST is dropped like any non-SlotGone error
    (the sequential fallback re-raises it loudly — see the CLAUDE.md documented
    residual for the zero-fallback-slots edge). It can never be misread as a benign
    SlotGone → NO_INVENTORY on the sequential book path. The fetch-code-and-verify
    wiring (core/otp.py OtpSource) is deliberately NOT attached until the
    challenge's API shape has been observed live — this error IS the observation
    signal.
    """


class RateLimitError(AdapterError):
    """We were throttled. Includes optional retry-after seconds."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class SlotGoneError(AdapterError):
    """Slot was visible at search() but disappeared by book() — race lost.

    ``reason`` classifies WHY the platform rejected the booking. It is DIAGNOSTIC ONLY —
    every reason means the same thing to control flow (no reservation was created; try
    the next-ranked slot) — but the values carry opposite evidential weight when reading
    a staggered blind burst (STAGGER_PLAN §3.3):

    * ``"unavailable"`` — the slot was not bookable ("Time not available."). This is the
      one that CARRIES race/boundary evidence: either someone claimed it first, or our
      POST arrived before the platform's release flip. Byte-identical bodies, which is
      exactly why the burst is staggered.
    * ``"daily_limit"`` — rejected by ForeUP's "1 online reservation per day" rule. If ANY
      sibling booked, this is the EXPECTED consequence of our own burst winning and carries
      NO information about the race: observed live 2026-08-16, where the 250 ms stagger let
      the rank-0 booking commit before the surplus POSTs were processed (1 booked / 2
      daily_limit — the first non-uniform outcome in the LOG RETENTION WINDOW, though the
      pre-stagger 2026-07-11 drop produced the same 1/2 shape, so the stagger is not
      established as its cause). If NOTHING booked, it means the
      opposite and is highly informative: a reservation for that date already existed which
      this burst did not make (see ``_rejection_summary`` in the orchestrator).
    * ``"conflict"`` — HTTP 409, tagged by the ForeUP adapter only. Other adapters raise
      1-arg and land on ``"unknown"``; nothing reads this value, so that is harmless.
    * ``"unknown"`` — an unrecognised body; the default, so non-ForeUP adapters and any
      future rejection wording stay correct rather than being misfiled under an observed
      reason.

    Collapsing these under one "claimed pre-book" label made a burst that we ourselves
    invalidated read as lost races, which is the opposite conclusion.
    """

    def __init__(self, message: str, reason: SlotGoneReason = "unknown") -> None:
        super().__init__(message)
        self.reason: SlotGoneReason = reason


class CancelError(AdapterError):
    """cancel_reservation() failed. Booking was NOT cancelled.

    Implementations MUST raise this rather than a bare exception so the
    orchestrator can distinguish "cancel failed, still have a booking" from
    other error categories. Crucially, this error means the pre-cancel booking
    is still live and the user's position is safe (nothing was lost).

    See PLAN.md M-feature-2 for the cancel+rebook safety protocol.
    """


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """Explicit capability record exposed by every CourseAdapter as ``.capabilities``.

    Replaces the misleading ``isinstance(adapter, BlindPostCapable) and
    adapter.supports_blind_post`` double-gate. Because ``runtime_checkable`` only checks
    member PRESENCE, EVERY ForeUP adapter satisfied ``isinstance(_, BlindPostCapable)``
    once the base shipped the methods — so the boolean was the *real* (but hidden) guard.
    A plain flag on the adapter says exactly what it means and cannot be half-satisfied.

    ``blind_post=True`` is a PROMISE that ``captcha_pool_size()`` + ``synthesize_blind_slots()``
    are implemented (the orchestrator casts to ``BlindPostCapable`` to call them). A bare
    ForeUP course, TeeItUp, and the default FakeAdapter are NOT capable; Mangrove Bay is the
    only capable course in v0.

    Scope note: the other opt-in capabilities — ``ReservationCacheRefreshable`` and
    ``AuthStateReportable`` — remain honest ``isinstance`` presence-checks, because for those
    "has the method" *is* the capability (they cannot desync the way a flag could) and they are
    not footguns. This record can grow to fold them in if that calculus ever changes.
    """

    blind_post: bool = False


class BlindPostCapable(Protocol):
    """Structural shape the orchestrator casts to in order to CALL the blind-POST methods.

    Typing-only cast target — the runtime gate is ``adapter.capabilities.blind_post`` (see
    ``AdapterCapabilities``), NOT ``isinstance``. See BLIND_POST_PLAN.md §3: blind-POST builds
    a book payload from a frozen static template plus a *computed* slot id (no live search
    dependency) and fires the top-N ranked in-window candidates CONCURRENTLY at the 06:00 drop,
    keeping the best reservation that books and cancelling the rest.
    """

    def captcha_pool_size(self) -> int:
        """Number of pre-solved CAPTCHA tokens currently in the FIFO pool.

        The orchestrator sizes the blind burst at ``min(len(blind_slots),
        captcha_pool_size())`` so each concurrent ``book()`` pops a pooled token and
        none inline-solves at T0 (the latency failure the feature removes). See
        BLIND_POST_PLAN.md §5/§6. ``ForeUpAdapter`` returns ``len(self._captcha_tokens)``;
        adapters with no CAPTCHA may return a large/scriptable value.
        """
        ...

    def synthesize_blind_slots(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        max_count: int,
    ) -> list[TeeTimeSlot]:
        """Build up to `max_count` blind-POST candidate slots WITHOUT searching.

        Pure / synchronous: enumerate the course's valid morning tee-time grid that
        falls inside `request.time_windows` for `target_date`, compute each slot's
        deterministic id (ForeUP `start_front` = ``f"{YYYY}{month-1:02d}{DD}{HH}{MM}"``,
        month 0-indexed — see BLIND_POST_PLAN.md §2 fact 1), and return them RANKED by
        the SAME `slot_utils.rank_slots_for_request` ordering the search path uses (so
        "keep best" across blind + search agree), truncated to `max_count`.

        Each returned TeeTimeSlot's `raw` is the frozen static template with `time`
        (1-indexed calendar month) and `start_front` (0-indexed) overwritten, so the
        adapter's existing `book()` can POST it directly with no search.

        Returns [] if no grid time falls inside the window (caller falls back to the
        real search). MUST NOT perform any I/O.
        """
        ...


@runtime_checkable
class ReservationCacheRefreshable(Protocol):
    """Opt-in capability: an adapter whose ``list_reservations()`` reads a SNAPSHOT
    populated at ``authenticate()`` time, and which can force that snapshot to be
    re-fetched mid-run.

    ForeUP is the motivating case: ``list_reservations()`` returns a cache built from
    the ``POST /login`` response body (the live ``GET /reservations`` endpoint is a
    ~6 MB user profile with no usable list — see the root CLAUDE.md note), and
    ``authenticate()`` is IDEMPOTENT — once ``_logged_in`` is True a second call is a
    no-op, so it will NOT rebuild the cache. The blind-POST re-guard
    (``Orchestrator._reguard_before_fallback``) needs a snapshot taken AFTER the T0
    burst to see a landed-but-uncertain reservation; a plain re-``authenticate()``
    would silently return the STALE pre-burst snapshot and let the fallback book a
    SECOND reservation (double-book). ``refresh_reservations`` forces a fresh login so
    the post-burst snapshot is real.

    Adapters that already read reservations live (or that never reach the blind-POST
    re-guard — only the blind-capable primary does) need not implement this; the
    re-guard falls back to ``authenticate()`` for them.
    """

    async def refresh_reservations(self, creds: CourseCredentials) -> None:
        """Force a fresh fetch of the reservation snapshot (e.g. ForeUP: reset the
        logged-in flag and re-run the warm-up GET + login POST so the login-response
        reservation cache is rebuilt). If this raises, the re-guard does NOT proceed on the
        existing session — it SKIPS the course this run (the session is now unauthenticated,
        so a fallback book would crash and could double-book a landed POST); the watcher
        reconciles. Never crashes the run."""
        ...


@runtime_checkable
class AuthStateReportable(Protocol):
    """Opt-in capability: an adapter whose ``authenticate()`` can return WITHOUT raising
    yet WITHOUT establishing a logged-in session — a "soft" login failure.

    ForeUP is the motivating case: a 400/401 or a server-rejected login body is logged and
    SWALLOWED — ``authenticate()`` returns normally with ``_logged_in`` still False (the
    PHPSESSID warm-up alone lets ``search()`` work; only ``book()`` needs the login). The
    race pre-warm (``Orchestrator._prewarm_login``) must NOT record such a course in
    ``_prewarmed_course_ids``: if it did, ``run()`` would skip the T0 re-auth and ``book()``
    would raise ``AuthError`` on a session that never logged in — turning a TRANSIENT 401
    pre-T0 into a silently lost booking. ``is_authenticated`` lets the orchestrator tell a
    real login from a soft failure without coupling to any adapter's internals.

    Adapters with no soft-fail path (``authenticate()`` either succeeds or raises — e.g.
    TeeItUp, FakeAdapter's default) need not implement this; the pre-warm treats a clean
    ``authenticate()`` return as success for them.
    """

    @property
    def is_authenticated(self) -> bool:
        """True iff a username/password login has actually established a session."""
        ...


@runtime_checkable
class CourseAdapter(Protocol):
    """Structural contract every course implementation satisfies.

    Lifecycle: orchestrator instantiates one adapter per (CourseId, BookingRequest),
    calls authenticate() once, search() one-or-more times (poll loop), then book()
    on the chosen slot. Adapters MAY hold an open HTTP session for the lifetime of
    a single orchestration run; they MUST be safe to discard mid-flight.
    """

    course_id: CourseId
    # Explicit capability record (see AdapterCapabilities). The orchestrator branches on
    # `adapter.capabilities.blind_post` rather than `isinstance(adapter, BlindPostCapable)`,
    # so a non-capable course can never reach the blind path even with a mis-edited config.
    capabilities: AdapterCapabilities

    async def authenticate(self, creds: CourseCredentials) -> None:
        """Establish an authenticated session. Idempotent. Raises AuthError on bad creds."""
        ...

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        """Return slots matching request criteria for this adapter's course.

        Raises:
            InventoryNotPublishedError: window not yet open.
            RateLimitError, AuthError, CaptchaError: as named.

        An empty list (no exception) means inventory IS published but nothing matches.

        ``skip_initial_spacing`` (Change D / PR3): drop the leading anti-bot courtesy
        sleep before the FIRST per-date GET. Set True ONLY by the booking Orchestrator
        on the race path (``prefetch_book=True``), where this GET leads the post-T0 burst
        and there is nothing to space from. The watcher leaves it False so its
        inter-date-check spacing (its only spacing) is preserved.
        """
        ...

    async def prepare_book(
        self,
        slot: TeeTimeSlot | None,
        request: BookingRequest,
        *,
        count: int = 1,
    ) -> None:
        """Pre-fetch expensive prerequisites for book() (e.g., CAPTCHA tokens).

        Two callers:
        - UpgradeOrchestrator calls it BEFORE cancel_reservation() (with the chosen
          slot, count=1) so the cancel-to-book window is ~1-2 seconds rather than ~60s.
        - Orchestrator calls it on the race path DURING the pre-T0 busy-wait, BEFORE
          any slot exists, so `slot` is None there, with count=N to pre-solve N tokens
          CONCURRENTLY (one per ranked fallback candidate). The CAPTCHA solve does not
          depend on the slot (it is a page-level reCAPTCHA), so `slot` may be None.

        After this returns, book(slot, request) should be able to complete without any
        slow blocking steps for up to `count` calls.

        Adapters that need no pre-fetching (e.g., FakeAdapter, Chronogolf) should
        implement this as a no-op. ForeUpAdapter overrides it to solve `count` CAPTCHA
        tokens and cache them in a FIFO pool for use in book().

        Raise contract (NI10): with count == 1, a total solve failure RE-RAISES so the
        UpgradeOrchestrator aborts the upgrade and leaves the original booking untouched.
        With count > 1 (race prefetch) it is best-effort and NEVER raises — book() falls
        back to an inline solve when the pool runs dry.
        """
        ...

    async def book(
        self,
        slot: TeeTimeSlot,
        request: BookingRequest,
    ) -> BookingResult:
        """Commit the booking. Returns BookingResult with confirmation_code on success.

        MUST be safe to retry only if SlotGoneError is raised. On ANY other failure
        mode (including raw network errors, ambiguous 5xx, and surprise non-error
        responses), the booking is UNCERTAIN (the POST may have landed): the
        orchestrator MUST NOT call book() again in-run. Reconciliation is deferred
        to the watcher, which re-checks list_reservations() on its next poll — see
        PLAN.md §9 / §9.1.
        """
        ...

    async def list_reservations(self) -> list[ExistingReservation]:
        """Return the authenticated user's existing reservations on this course.

        Load-bearing for §9 layer 2 (the pre-book remote check) and for the
        watcher's asynchronous reconciliation. The orchestrator calls this BEFORE
        the book() POST to guard against an already-existing booking; the watcher
        calls it on each poll (after re-authenticating) to detect a booking that
        landed during an earlier UNCERTAIN run. The booker does NOT re-call it
        in-run after an uncertain book() — book() raises out instead (§9.1).

        MUST be a read-only operation with no side effects on the booking system.
        MAY return reservations for dates outside the current request — caller
        filters. Implementations should sort by `tee_time` ascending for stable
        matching.
        """
        ...

    async def cancel_reservation(self, confirmation_code: str) -> None:
        """Cancel an existing reservation identified by `confirmation_code`.

        This method MUST be idempotent with respect to an already-cancelled
        reservation (e.g. 404 on cancel should NOT raise CancelError — the
        reservation is gone, which is the desired post-condition).

        Raises:
            CancelError: if the cancellation was definitively refused by the
                course backend (e.g. non-cancellable reservation type, past
                the cancellation window, or a 4xx that is NOT a 404).
                A CancelError means the original booking is still live.
            AuthError: if the session is not authenticated.
            RateLimitError: if throttled.

        The orchestrator uses a "cancel-only-if-booked, rebook-only-if-cancelled"
        protocol (see PLAN.md M-feature-2). cancel_reservation is NEVER called
        unless list_reservations has confirmed the booking still exists first.
        """
        ...

    async def aclose(self) -> None:
        """Release HTTP sessions, browser contexts, etc. Always called by orchestrator."""
        ...
