"""SQLite-backed BookingStore. v0 default. File path is configurable.

Schema (DDL lives here; canonical migration step in M3.T1):

    CREATE TABLE booking_history (
        request_id    TEXT NOT NULL,
        resolved_date TEXT NOT NULL,         -- ISO YYYY-MM-DD; composite key per §13.1
        outcome       TEXT NOT NULL,
        course_id     TEXT,
        slot_id       TEXT,
        tee_time      TEXT,                  -- ISO8601, tz-aware
        confirmation  TEXT,
        booked_at     TEXT,
        attempts      INTEGER NOT NULL,
        result_json   TEXT NOT NULL,         -- full BookingResult, source of truth
        PRIMARY KEY (request_id, resolved_date)
    );
    CREATE TABLE attempt_log (
        request_id  TEXT NOT NULL,
        attempt     INTEGER NOT NULL,
        at          TEXT NOT NULL,
        event       TEXT NOT NULL,
        payload     TEXT NOT NULL
    );
    CREATE TABLE session_cache (
        course_id   TEXT PRIMARY KEY,
        blob        BLOB NOT NULL,
        expires_at  TEXT NOT NULL
    );

Locks via `BEGIN IMMEDIATE` + a row in a `request_locks` table holding the holder PID.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from ..core.models import BookingResult, CourseId, RequestId


class SqliteStore:
    """SQLite implementation of BookingStore. Stub — see M3.T1, M3.T2."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def initialize(self) -> None:
        raise NotImplementedError

    async def get_terminal(
        self,
        request_id: RequestId,
        resolved_date: date,
    ) -> BookingResult | None:
        raise NotImplementedError

    async def record_terminal(self, result: BookingResult, resolved_date: date) -> None:
        raise NotImplementedError

    async def delete_terminal(
        self,
        request_id: RequestId,
        resolved_date: date,
    ) -> None:
        """Delete the terminal record for (request_id, resolved_date).
        No-op if absent. See M-feature-2 idempotency-key-on-rebook design.
        Implement in M-feature-2.T4 (SqliteStore additions).
        """
        raise NotImplementedError

    async def append_attempt(
        self,
        request_id: RequestId,
        attempt: int,
        event: str,
        payload: dict[str, object],
        at: datetime,
    ) -> None:
        raise NotImplementedError

    async def cache_session(
        self,
        course_id: CourseId,
        blob: bytes,
        expires_at: datetime,
    ) -> None:
        raise NotImplementedError

    async def load_session(self, course_id: CourseId) -> bytes | None:
        raise NotImplementedError

    def request_lock(self, request_id: RequestId) -> AbstractAsyncContextManager[None]:
        return self._request_lock_impl(request_id)

    @asynccontextmanager
    async def _request_lock_impl(self, request_id: RequestId) -> AsyncIterator[None]:
        raise NotImplementedError
        yield  # type: ignore[unreachable]  # asynccontextmanager requires a yield
