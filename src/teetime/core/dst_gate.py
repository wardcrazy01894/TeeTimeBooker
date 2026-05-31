"""DST-half gate. Re-homes the deleted ``book.yml`` ``dst`` step into the entrypoint.

Background: ``compute.bicep`` registers two same-day crons (one tuned for EDT, one for
EST) because we do not re-edit cron schedules twice a year. Only ONE is correct in any
given DST season; the other fires at the wrong UTC offset. With a real busy-wait
(``teetime run --wait``) the wrong-season cron misfires in TWO ways:

- EST-tuned cron firing in EDT season -> 06:50 ET -> T0 (06:00 ET) is ~50 min in the
  PAST -> ``busy_wait_until`` returns instantly -> the bot books ~50 min late.
- EDT-tuned cron firing in EST season -> 04:50 ET -> T0 is ~70 min in the FUTURE -> the
  bot busy-waits ~70 min inside the replica -> exceeds the booking ``replicaTimeout`` ->
  replica killed mid-race, no booking.

This gate makes the wrong-season cron exit cleanly in BOTH directions. See PLAN.md §6.3.
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

from .clock import Clock


def should_proceed(clock: Clock, *, timezone: str, fire_time: time) -> bool:
    """Return True iff the current course-local wall-clock hour == ``fire_time.hour - 1``.

    Called ONLY on the real-timing (``--wait``) cron path, BEFORE ``busy_wait_until``.
    The ``--no-wait`` path (manual/local/on-demand) bypasses this entirely, matching the
    old ``workflow_dispatch`` always-proceed semantics.

    The cron lands the runner at :50 of the hour preceding T0=06:00 ET, so at gate time
    the correct season reads ET hour 5 and the wrong season reads 4 or 6. The predicate
    is ``fire_time.hour - 1`` (not a hardcoded 5) so the gate stays correct if
    ``fire_time`` ever changes. Reads the HOUR only — sub-hour precision is the
    busy-wait's job.

    A ``False`` return means "wrong-season cron — the caller should exit 0; this is not
    an error." It deliberately does NOT proceed once the ET hour reaches ``fire_time.hour``
    (a late-landing runner past T0): the slots have already dropped, so racing a public
    window late is useless and risks double-attempt churn — the watch job is the
    missed-drop recovery path. Pure function of (clock, timezone, fire_time), so it is
    ``FakeClock``-deterministic.
    """
    et_hour = clock.now_utc().astimezone(ZoneInfo(timezone)).hour
    return et_hour == fire_time.hour - 1
