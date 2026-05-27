"""In-memory BookingStore. Used by orchestrator unit tests (M2.T1) and any
contributor wanting to spike a flow without touching SQLite.

NOT used in production. Real persistence is `SqliteStore` in v0 and a cloud-KV
sibling at v1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime

from ..core.models import BookingResult, CourseId, RequestId
from .store import ConcurrentRunError


class InMemoryStore:
    """Dict-backed BookingStore. Behaviorally indistinguishable from SqliteStore
    for single-process tests."""

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
        self._attempts.append((request_id, attempt, event, payload, at))

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
