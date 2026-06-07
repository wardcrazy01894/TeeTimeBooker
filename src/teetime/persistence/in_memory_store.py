"""In-memory BookingStore — the production store in v0.

State lives in process memory for the duration of a single run; there is no
durable cross-run persistence. The source of truth for existing bookings is the
live `list_reservations()` pre-book check, not this store (see PLAN.md §9). The
`get_terminal`/`request_lock` methods provide within-run idempotency and an
advisory lock; they do not survive process exit. A durable backend (SqliteStore)
was considered and deliberately dropped — see PLAN.md §16 (M3 removed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime

from ..core.models import BookingResult, CourseId, RequestId
from ..core.redaction import redact_payload
from .store import ConcurrentRunError


class InMemoryStore:
    """Dict-backed BookingStore. The v0 production store (single-process,
    non-durable)."""

    def __init__(self) -> None:
        self._history: dict[tuple[RequestId, date], BookingResult] = {}
        self._sessions: dict[CourseId, tuple[bytes, datetime]] = {}
        self._attempts: list[tuple[RequestId, int, str, dict[str, object], datetime]] = []
        self._held: set[RequestId] = set()

    async def initialize(self) -> None:
        return None

    async def get_terminal(
        self,
        request_id: RequestId,
        resolved_date: date,
    ) -> BookingResult | None:
        return self._history.get((request_id, resolved_date))

    async def record_terminal(self, result: BookingResult, resolved_date: date) -> None:
        key = (result.request_id, resolved_date)
        existing = self._history.get(key)
        if existing is not None and existing.outcome != result.outcome:
            raise ValueError(
                f"conflicting terminal for {key}: existing={existing.outcome}, new={result.outcome}"
            )
        self._history[key] = result

    async def delete_terminal(
        self,
        request_id: RequestId,
        resolved_date: date,
    ) -> None:
        """Delete the terminal record for (request_id, resolved_date). No-op if absent."""
        self._history.pop((request_id, resolved_date), None)

    async def append_attempt(
        self,
        request_id: RequestId,
        attempt: int,
        event: str,
        payload: dict[str, object],
        at: datetime,
    ) -> None:
        # PCI/PII guard at the store boundary (PLAN.md §10.1): redact card fields + player
        # PII here so no caller can leak PAN/CVV by forgetting to scrub the payload first.
        self._attempts.append((request_id, attempt, event, redact_payload(payload), at))

    async def cache_session(
        self,
        course_id: CourseId,
        blob: bytes,
        expires_at: datetime,
    ) -> None:
        self._sessions[course_id] = (blob, expires_at)

    async def load_session(self, course_id: CourseId) -> bytes | None:
        entry = self._sessions.get(course_id)
        if entry is None:
            return None
        blob, expires_at = entry
        if expires_at <= datetime.now(tz=UTC):
            return None
        return blob

    def request_lock(self, request_id: RequestId) -> AbstractAsyncContextManager[None]:
        return self._lock_impl(request_id)

    @asynccontextmanager
    async def _lock_impl(self, request_id: RequestId) -> AsyncIterator[None]:
        if request_id in self._held:
            raise ConcurrentRunError(f"already holding lock for {request_id}")
        self._held.add(request_id)
        try:
            yield
        finally:
            self._held.discard(request_id)
