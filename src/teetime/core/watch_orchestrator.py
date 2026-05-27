"""Cancellation-monitor orchestrator (M-feature-1).

This module implements the "watch" job that polls for newly available tee times
on a target date that was not successfully booked at the 6 AM opening, OR that
the user wants to improve upon (see upgrade_orchestrator.py for the improvement
path).

Design decisions (see PLAN.md M-feature-1 for the full analysis):

Polling interval:
    Default 10 minutes (600 s). Absolute floor is 5 minutes (300 s); WatchConfig
    will raise ValueError below that floor. Rationale: the 6 AM race window uses
    250 ms poll cadence because it is a competitive real-time window. The watch
    job is NOT racing anyone — it is monitoring for cancellations on a day that
    has already opened. At 10 minutes, 144 polls/day — well within the "one user
    making normal bookings" tier of any reasonable anti-bot policy. PLAN.md §12
    forbids hammering. 10 minutes respects that.

    NOTE: GH Actions `*/10` cron firing is best-effort; real-world intervals can
    be 10-20 minutes depending on runner load. The "144 polls/day" figure assumes
    exact 10-minute intervals and is an upper bound. See PLAN.md §20 SF-4 note.

ACA Job scheduling (ACA Jobs are not long-running):
    The watch job is NOT a single long-running process (that would require an
    ACA Container App, not a Job, and would cost ~$5+/month running idle).
    Instead, each ACA Job invocation runs once, checks for availability, then
    exits. The cron on the ACA Job fires every 10 minutes (*/10 * * * *).
    The job runs for at most ~30 seconds per invocation (one HTTP round-trip).
    This is the correct pattern for ACA Jobs. The v0 GH Actions equivalent is
    a separate workflow with a schedule of every 10 minutes during watch hours.

    IMPORTANT: ACA Job scheduled triggers use standard cron syntax and fire in
    UTC. The watch job's cron does not need a DST gate because it is not racing
    a wall-clock window — it just polls whenever it fires.

State management:
    The watch job reads from the same SQLite store (or Blob Storage in v1) as
    the main booking job. It needs to know:
    1. What date to watch — derived from `clock.today() + target_offsets[0] days`
       (same formula as the main booking job). No separate `watch_state` store
       table is required; the target date is computed from Clock at invocation
       time, matching the same date the main job was targeting. The caller
       (`teetime watch` CLI) may also pass `--date YYYY-MM-DD` to override.
    2. Whether a booking already exists — `list_reservations` and `get_terminal`
       short-circuit the poll.
    3. The deadline past which watching is pointless — midnight on target_date
       (course-local time). Computed from `clock.now_utc()` and `target_date`.

    There is NO separate `watch_state` table and no `WatchState` Protocol method.
    The watch job is stateless beyond the existing `booking_history` table.

ADVISORY LOCK OWNERSHIP:
    check_once() does NOT acquire `request_lock` for its read-only availability
    check. If it finds a higher-priority slot and delegates to
    `UpgradeOrchestrator.maybe_upgrade()`, that method acquires the lock itself.
    The caller must NOT hold the lock when calling maybe_upgrade().
    See upgrade_orchestrator.py module docstring for the canonical lock statement.

How check_once() determines the "current booking" for maybe_upgrade():
    check_once() calls store.get_booked(request.request_id, target_date) to
    retrieve the BookingResult stored by the main booking run. That record carries
    the TTB:-prefixed confirmation_code which is the source of truth for the
    is_managed check. check_once() also calls list_reservations() to confirm the
    booking still exists on the server (layer-2 pre-flight check). It then passes
    the STORE RECORD (BookingResult) — not the ExistingReservation — to
    maybe_upgrade(). This is the ONLY correct way: ExistingReservation from
    list_reservations() always has a raw server id (no TTB: prefix), so
    checking is_managed on it would always return False and make the upgrade guard
    a permanent no-op.

Race condition with the 6 AM booking run:
    The watch job's read-only search phase is lock-free. If it proceeds to an
    upgrade attempt, UpgradeOrchestrator.maybe_upgrade() tries to acquire the
    lock. If the 6 AM booking job holds the lock, ConcurrentRunError is caught
    and maybe_upgrade returns None. Safe.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from .models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    WatchConfig,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..notifications.notifier import Notifier
    from ..persistence.store import BookingStore
    from .adapter import CourseAdapter
    from .clock import Clock
    from .config import SchedulerConfig


class WatchOrchestrator:
    """Single-invocation watch check. Called once per ACA Job / GH Actions step.

    The caller (CLI command `teetime watch`) is responsible for:
    - Starting the process on each poll interval (via ACA Job cron or GH workflow).
    - Passing the `target_date` to watch. The date is computed by the CLI from
      `clock.today() + target_offsets[0]` (same as the main booking job), or
      overridden via `--date YYYY-MM-DD`. There is no `watch_state` store table;
      the watch job is stateless beyond the existing `booking_history` table.

    This class does NOT loop internally. Each invocation does one check and exits.
    The polling loop is handled externally by the scheduler (ACA cron / GH Actions).

    LOCK OWNERSHIP: check_once() is READ-only at the search phase and does NOT
    hold `request_lock`. If it delegates to UpgradeOrchestrator.maybe_upgrade(),
    that method acquires the lock itself. check_once() must NOT hold the lock
    when calling maybe_upgrade().

    CURRENT BOOKING RESOLUTION: check_once() retrieves the current managed
    booking via store.get_booked(request.request_id, target_date) and passes
    the resulting BookingResult (which carries the TTB:-prefixed confirmation_code)
    to UpgradeOrchestrator.maybe_upgrade(). It does NOT pass the ExistingReservation
    from list_reservations(), which would always have is_managed=False (raw server
    id, no TTB: prefix).

    See PLAN.md M-feature-1.T2 for the implementation contract.
    """

    def __init__(
        self,
        adapters: Mapping[CourseId, CourseAdapter],
        store: BookingStore,
        notifier: Notifier,
        clock: Clock,
        scheduler: SchedulerConfig,
        watch_config: WatchConfig,
        creds: Mapping[CourseId, CourseCredentials] | None = None,
    ) -> None:
        self._adapters = adapters
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._scheduler = scheduler
        self._watch_config = watch_config
        self._creds = creds or {}

    async def check_once(
        self,
        request: BookingRequest,
        target_date: date,
    ) -> BookingResult | None:
        """Perform one availability check for `target_date`.

        Returns:
            BookingResult with outcome=BOOKED if a slot was found and booked.
            None if no slot was available on this check (caller should schedule
            the next invocation via the external cron).

        Raises:
            No exceptions are propagated — all failures are logged and result
            in a None return so the watch job exits cleanly (the ACA/GH cron
            will retry on the next interval). The exception is CaptchaError
            and AuthError which are re-raised after notifying, so the operator
            can disable the watch job.

        The implementation follows the same §9.1 state machine as Orchestrator
        for any booking POST it makes, including UNCERTAIN -> RECONCILING.

        See PLAN.md M-feature-1.T2 for the full state machine extension.
        """
        raise NotImplementedError(
            "WatchOrchestrator.check_once — implement in M-feature-1.T2. "
            "See PLAN.md M-feature-1 for the full algorithm."
        )

    def _is_outside_polling_hours(self, now: datetime) -> bool:
        """Return True if current wall-clock time is outside polling_start/end hours.

        Polling is suppressed outside the configured hours to reduce load
        during nighttime when cancellations are vanishingly rare.

        See PLAN.md M-feature-1 §"Polling hours gate".
        """
        raise NotImplementedError(
            "WatchOrchestrator._is_outside_polling_hours — implement in M-feature-1.T2."
        )

    def _is_past_watch_deadline(self, now: datetime, target_date: date) -> bool:
        """Return True if we have passed the point where watching is useful.

        Watching stops at midnight of the target_date (course-local time):
        the booking is for that day and no slots will open after the round time.
        """
        raise NotImplementedError(
            "WatchOrchestrator._is_past_watch_deadline — implement in M-feature-1.T2."
        )
