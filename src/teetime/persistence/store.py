"""BookingStore: in-run state for a single orchestrator invocation.

Stores three things behind one Protocol:

    1. booking_history     — terminal BookingResult per RequestId (idempotency)
    2. attempt_log         — append-only event log per (RequestId, attempt)
    3. session_cache       — adapter-supplied opaque blobs (auth tokens, cookies)

Anything else (e.g. course metadata) belongs in code, not the store.

v0 has a single implementation, `InMemoryStore`: state is per-process and does
NOT persist across runs. A durable SQLite/blob backend was considered and
deliberately dropped (PLAN.md §16, M3 removed) — the live `list_reservations()`
pre-book check is the cross-run source of truth, so durable local state earns
its keep nowhere in this design.

Concurrency: the store exposes an advisory lock on RequestId so two coroutines in
the same process can't double-book. Cross-process serialization is handled by the
ACA Job / GitHub Actions concurrency groups, not the store.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ..core.models import BookingResult, CourseId, RequestId


@runtime_checkable
class BookingStore(Protocol):
    """Persistence contract. Implementation: InMemoryStore (v0, non-durable)."""

    async def initialize(self) -> None:
        """Create schema if missing. Idempotent. Called once at process start."""
        ...

    async def get_terminal(
        self,
        request_id: RequestId,
        resolved_date: date,
    ) -> BookingResult | None:
        """Return the recorded terminal result for `(request_id, resolved_date)`.
        Used for idempotency: if non-None, orchestrator returns it without re-running.
        Composite key rationale: see PLAN.md §13.1.
        """
        ...

    async def record_terminal(self, result: BookingResult, resolved_date: date) -> None:
        """Persist a final result keyed by `(request_id, resolved_date)`. MUST fail
        loudly if a terminal already exists for this pair with a different outcome
        (defends against double-book within a single day).
        """
        ...

    async def delete_terminal(
        self,
        request_id: RequestId,
        resolved_date: date,
    ) -> None:
        """Delete the terminal record for `(request_id, resolved_date)`.

        This is the ONLY mechanism by which the idempotency block is cleared to
        allow a rebook after a cancel (M-feature-2). The orchestrator calls this
        after a successful cancel_reservation() and before attempting the upgrade
        booking. Calling delete_terminal when no record exists is a no-op (not
        an error).

        Contract: the caller MUST hold the advisory lock (request_lock) for the
        duration of delete_terminal + subsequent record_terminal, so no concurrent
        run can observe the gap between deletion and re-insertion.

        See PLAN.md M-feature-2 §"Idempotency key collision on rebook".
        """
        ...

    async def append_attempt(
        self,
        request_id: RequestId,
        attempt: int,
        event: str,
        payload: dict[str, object],
        at: datetime,
    ) -> None:
        """Append-only audit trail. Best-effort durability."""
        ...

    async def cache_session(
        self,
        course_id: CourseId,
        blob: bytes,
        expires_at: datetime,
    ) -> None:
        """Persist an opaque session token (cookies, JWT) for adapter reuse."""
        ...

    async def load_session(self, course_id: CourseId) -> bytes | None:
        """Return cached session bytes if not yet expired, else None."""
        ...

    def request_lock(self, request_id: RequestId) -> AbstractAsyncContextManager[None]:
        """Advisory lock for `request_id`. Two concurrent orchestrator runs against
        the same request id MUST serialize through this. Raises ConcurrentRunError on
        contention (no waiting — fail fast so the second run can exit early).
        """
        ...


class ConcurrentRunError(RuntimeError):
    """Raised by request_lock() when another process holds the lock for this request."""
