"""Per-user notifications (MULTIUSER_PLAN §8.7).

The engine ``notifications.notifier.Notifier`` Protocol is UNCHANGED: the recipient is bound at
construction. In the booking runner each account's ``Orchestrator`` gets a ``BufferingNotifier``
(collect only, NO I/O, so nothing is sent during the race). After WRITE #2, the runner maps
results to ``UserEvent``s and sends them through a ``UserNotifier``. ``UserEvent`` (not
``BookingResult``) is the tenant contract because ``lost`` and ``cancelled(external)`` come from
the watcher, not from an engine terminal.

Backend: Azure Communication Services Email over REST (HMAC-signed via httpx, no SDK), with an
Azure-managed sender domain (MU-11). Content never includes credentials.

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..core.models import BookingResult, CourseId
from .models import RowId, UserId


class UserEventKind(StrEnum):
    BOOKED = "booked"
    UPGRADED = "upgraded"
    MISSED_DROP = "missed_drop"  # stays PENDING; the watcher keeps trying until cutoff
    LOST = "lost"  # frozen without a booking
    CANCELLED = "cancelled"  # by the user via the site
    CANCELLED_EXTERNAL = "cancelled_external"  # vanished from 2 trusted snapshots (§7.5)
    AUTH_FAILED = "auth_failed"
    OPERATOR_SUMMARY = "operator_summary"


@dataclass(frozen=True, slots=True)
class UserEvent:
    kind: UserEventKind
    user_id: UserId | None  # None for OPERATOR_SUMMARY (sent to OPERATOR-NOTIFY-EMAIL)
    row_id: RowId | None
    course_id: CourseId | None
    target_date: date | None
    tee_time: datetime | None
    confirmation: str | None
    detail: str
    at: datetime


@runtime_checkable
class UserNotifier(Protocol):
    """Deliver one ``UserEvent``. Failures are logged by the caller and never mask an outcome
    (the same rule as the engine notifier)."""

    async def send(self, event: UserEvent) -> None: ...


class BufferingNotifier:
    """Engine-``Notifier``-shaped collector: ``notify`` appends to ``results`` and does no I/O.

    One per account's ``Orchestrator`` in the booking runner, so nothing is sent near T0.
    After WRITE #2 the runner ``flush()``es it and maps each result to a ``UserEvent``."""

    def __init__(self) -> None:
        self._results: list[BookingResult] = []

    async def notify(self, result: BookingResult) -> None:
        self._results.append(result)

    @property
    def results(self) -> tuple[BookingResult, ...]:
        return tuple(self._results)

    def flush(self) -> tuple[BookingResult, ...]:
        """Hand over every buffered result (in arrival order) and empty the buffer."""
        drained = tuple(self._results)
        self._results.clear()
        return drained
