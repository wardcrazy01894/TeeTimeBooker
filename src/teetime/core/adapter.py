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

from datetime import date
from typing import Protocol, runtime_checkable

from .models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
)


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


class RateLimitError(AdapterError):
    """We were throttled. Includes optional retry-after seconds."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class SlotGoneError(AdapterError):
    """Slot was visible at search() but disappeared by book() — race lost."""


class CancelError(AdapterError):
    """cancel_reservation() failed. Booking was NOT cancelled.

    Implementations MUST raise this rather than a bare exception so the
    orchestrator can distinguish "cancel failed, still have a booking" from
    other error categories. Crucially, this error means the pre-cancel booking
    is still live and the user's position is safe (nothing was lost).

    See PLAN.md M-feature-2 for the cancel+rebook safety protocol.
    """


@runtime_checkable
class BlindPostCapable(Protocol):
    """Opt-in capability: a course adapter that can fire BLIND book POSTs at T0.

    See BLIND_POST_PLAN.md §3. Blind-POST builds a book payload from a frozen
    static template plus a *computed* slot id (no live search dependency) and fires
    the top-N ranked in-window candidates CONCURRENTLY at the 06:00 drop, keeping
    the best reservation that books and cancelling the rest.

    The capability gate is intentionally an ADAPTER ATTRIBUTE, never a config flag:
    a non-capable course cannot blind-POST even with a fat-fingered config, because
    the orchestrator branches on `isinstance(adapter, BlindPostCapable) and
    adapter.supports_blind_post` against the concrete adapter object. A bare ForeUP
    course (or TeeItUp, or the default FakeAdapter) is NOT capable until it ships and
    validates its own template + tee-time grid. Mangrove Bay is the only capable
    course in this feature's v0.

    Note (nit 1): the Protocol declares `supports_blind_post: bool` as a plain
    attribute while concrete impls (MangroveBayAdapter, ForeUpAdapter base) declare it
    `ClassVar[bool]`. This is intentional and sound — `runtime_checkable` only checks
    member PRESENCE, and a ClassVar is readable as an instance attribute, so
    `isinstance(adapter, BlindPostCapable) and adapter.supports_blind_post` works for
    both class-level and instance-level declarations. Consequence: every ForeUP adapter
    satisfies isinstance once the base ships the members; the BOOLEAN is the real guard.
    """

    supports_blind_post: bool

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
        reservation cache is rebuilt). Best-effort: the caller wraps this in a
        try/except — a re-auth blip must not crash the run."""
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
        responses), the orchestrator transitions to the UNCERTAIN state in the §9
        state machine and MUST NOT call book() again until reconcile via
        list_reservations() has run — see PLAN.md §9.
        """
        ...

    async def list_reservations(self) -> list[ExistingReservation]:
        """Return the authenticated user's existing reservations on this course.

        Load-bearing for §9 layers 2 (pre-book remote check) and 4 (post-mortem
        reconciliation). The orchestrator calls this BEFORE the book() POST to
        guard against an already-existing booking, and AGAIN after any uncertain
        book() failure to determine whether the POST landed.

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
