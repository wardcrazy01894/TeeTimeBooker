"""In-memory BookingStore. Used by orchestrator unit tests (M2.T1) and any
contributor wanting to spike a flow without touching SQLite.

NOT used in production. Real persistence is `SqliteStore` in v0 and a cloud-KV
sibling at v1.

Implementation parity: this MUST satisfy `BookingStore` structurally and be
behaviorally indistinguishable from `SqliteStore` for single-process tests.
The cross-table atomicity SQLite gives us is trivially satisfied here because
there is only one process.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date, datetime
from typing import AsyncIterator

from ..core.models import BookingResult, CourseId, RequestId
from .store import ConcurrentRunError


class InMemoryStore:
    """Dict-backed BookingStore. Stub — real impl alongside M2.T1."""

    def __init__(self) -> None:
        self._history: dict[tuple[RequestId, date], BookingResult] = {}
        self._sessions: dict[CourseId, tuple[bytes, datetime]] = {}
        self._attempts: list[tuple[RequestId, int, str, dict[str, object], datetime]] = []
        self._locks: dict[RequestId, asyncio.Lock] = {}
        self._held: set[RequestId] = set()

    async def initialize(self) -> None:
        # No schema to create; in-memory is ready on construction.
        return None

    async def get_terminal(
        self,
        request_id: RequestId,
        resolved_date: date,
    ) -> BookingResult | None:
        raise NotImplementedError  # M2.T1

    async def record_terminal(self, result: BookingResult, resolved_date: date) -> None:
        raise NotImplementedError  # M2.T1

    async def append_attempt(
        self,
        request_id: RequestId,
        attempt: int,
        event: str,
        payload: dict[str, object],
        at: datetime,
    ) -> None:
        raise NotImplementedError  # M2.T1

    async def cache_session(
        self,
        course_id: CourseId,
        blob: bytes,
        expires_at: datetime,
    ) -> None:
        raise NotImplementedError  # M2.T1

    async def load_session(self, course_id: CourseId) -> bytes | None:
        raise NotImplementedError  # M2.T1

    def request_lock(self, request_id: RequestId) -> AbstractAsyncContextManager[None]:
        return self._lock_impl(request_id)

    @asynccontextmanager
    async def _lock_impl(self, request_id: RequestId) -> AsyncIterator[None]:
        # Real impl in M2.T1: fail fast (no waiting) on contention to match
        # SqliteStore's BEGIN IMMEDIATE behavior. ConcurrentRunError is the
        # raised exception — see PLAN.md §9 layer 5.
        if request_id in self._held:
            raise ConcurrentRunError(f"already holding lock for {request_id}")
        raise NotImplementedError
        yield  # pragma: no cover
