"""Top-level booking flow. Owns the race, the fallback, the idempotency check.

Sequence (see PLAN.md "6:00 AM race strategy" for full detail):

    1. resume_or_create(request)        # idempotency lookup in BookingStore
    2. wait_until(T0 - early_arrival)   # busy_wait_until via Clock
    3. for course in course_preferences:
         adapter = build(course)
         adapter.authenticate()
         loop:
             slots = adapter.search()          # may raise InventoryNotPublishedError
             if slots: break (filter by criteria, pick best)
             sleep(poll_interval)
             if elapsed > max_poll_seconds: break course
         result = adapter.book(slot)
         if result.outcome == BOOKED: break
    4. notifier.notify(result)
    5. store.record_terminal(result)

The orchestrator is the ONLY component that talks to BookingStore for writes
during a run. Adapters never touch the store directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import BookingRequest, BookingResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..notifications.notifier import Notifier
    from ..persistence.store import BookingStore
    from .adapter import CourseAdapter
    from .clock import Clock
    from .config import SchedulerConfig
    from .models import CourseId


class Orchestrator:
    """Runs one BookingRequest end-to-end. Single-use; build a new one per request."""

    def __init__(
        self,
        adapters: Mapping[CourseId, CourseAdapter],
        store: BookingStore,
        notifier: Notifier,
        clock: Clock,
        scheduler: SchedulerConfig,
    ) -> None:
        self._adapters = adapters
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._scheduler = scheduler

    async def run(self, request: BookingRequest) -> BookingResult:
        """Execute the full flow. Stub — see M2.T1."""
        raise NotImplementedError
