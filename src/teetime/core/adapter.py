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

    async def search(self, request: BookingRequest) -> list[TeeTimeSlot]:
        """Return slots matching request criteria for this adapter's course.

        Raises:
            InventoryNotPublishedError: window not yet open.
            RateLimitError, AuthError, CaptchaError: as named.

        An empty list (no exception) means inventory IS published but nothing matches.
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
