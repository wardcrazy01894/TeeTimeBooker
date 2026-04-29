"""Notifier: deliver one terminal BookingResult to the user.

Pluggable: backend chosen via NotifierConfig.backend. Errors in the notifier
MUST NOT mask a successful booking — orchestrator catches and logs.
"""

from __future__ import annotations

import sys
from typing import IO, Protocol, runtime_checkable

from ..core.models import BookingResult


@runtime_checkable
class Notifier(Protocol):
    """One method. Idempotent on best-effort basis (don't double-send if possible)."""

    async def notify(self, result: BookingResult) -> None:
        """Deliver `result` to whatever channel this notifier represents."""
        ...


class NoopNotifier:
    """Used in tests and dry-runs. Silent success — never raises, never writes."""

    async def notify(self, result: BookingResult) -> None:
        return None


class ConsoleNotifier:
    """Prints a single-line summary to a stream (default: stdout)."""

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    async def notify(self, result: BookingResult) -> None:
        line = (
            f"[teetime] outcome={result.outcome.value} "
            f"course={result.course_id} "
            f"confirmation={result.confirmation_code or '-'} "
            f"attempts={result.attempts}"
        )
        if result.error_message:
            line += f" error={result.error_message!r}"
        print(line, file=self._stream)
