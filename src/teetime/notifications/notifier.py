"""Notifier: deliver one terminal BookingResult to the user.

Pluggable: backend chosen via NotifierConfig.backend. Errors in the notifier
MUST NOT mask a successful booking — orchestrator catches and logs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..core.models import BookingResult


@runtime_checkable
class Notifier(Protocol):
    """One method. Idempotent on best-effort basis (don't double-send if possible)."""

    async def notify(self, result: BookingResult) -> None:
        """Deliver `result` to whatever channel this notifier represents."""
        ...


class NoopNotifier:
    """Used in tests and dry-runs. Stub."""

    async def notify(self, result: BookingResult) -> None:
        raise NotImplementedError


class ConsoleNotifier:
    """Prints to stdout. Used for local manual runs. Stub."""

    async def notify(self, result: BookingResult) -> None:
        raise NotImplementedError
