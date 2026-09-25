"""Per-course release policy: WHEN a course's booking window opens (MULTIUSER_PLAN §6).

A ``ReleasePolicy`` says "tee times for date D become bookable at ``release_time`` in
``timezone``, ``advance_days`` days before D". It lives in ``core/`` (not ``tenant/``) because
adapters carry it as a ``ClassVar`` and adapters may only import ``core`` (layering, §2.2).

STUB — nothing imports this yet. Implemented in MULTIUSER_PLAN MU-1. The single-user CLI path
keeps reading ``scheduler.timezone`` / ``scheduler.fire_time`` / ``target_offsets`` from TOML
until the cutover (§11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

_MU1 = "MULTIUSER_PLAN.md MU-1"


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    """When a course releases inventory. Mangrove Bay = (7, 06:00, America/New_York).

    Invariants (validated by ``validate_release_policy``, MU-1): ``advance_days >= 0``;
    ``timezone`` is a valid IANA zone; ``4 <= release_time.hour <= 22`` in v1, because the DST
    gate (``core/dst_gate.should_proceed``) compares ``hour == fire_time.hour - 1`` and
    ``Orchestrator._compute_t0`` anchors T0 to ``local_now.date()``. A midnight release would
    fire on the wrong calendar day, and 01:00-03:00 collides with DST transition gaps
    (§6.1). ``hosted_booking`` gates job derivation: a course with ``False`` gets no ACA job
    (Sydney Marovitz, whose TeeItUp PAN path is out of hosted scope, §6.4).
    """

    advance_days: int
    release_time: time
    timezone: str
    hosted_booking: bool = True


def validate_release_policy(policy: ReleasePolicy) -> None:
    """Raise ``ValueError`` if ``policy`` violates the v1 invariants above (§6.1)."""
    raise NotImplementedError(_MU1)


def release_instant_for(policy: ReleasePolicy, now_utc: datetime) -> datetime:
    """Today's release instant (tz-aware UTC), where "today" is the calendar date of
    ``now_utc`` in the COURSE timezone. Uses ``zoneinfo`` so DST resolves correctly (§6.3)."""
    raise NotImplementedError(_MU1)


def target_date_for(policy: ReleasePolicy, now_utc: datetime) -> date:
    """The date whose inventory is released today: course-local today + ``advance_days``.

    Always computed in the COURSE timezone, never UTC or the runner's zone: the tenant booking
    runner's READ #1 selects rows for exactly this date (§4.2).
    """
    raise NotImplementedError(_MU1)


def cron_pair(policy: ReleasePolicy, *, lead_minutes: int = 10) -> tuple[str, str]:
    """``(daylight_cron, standard_cron)`` UTC cron expressions firing ``lead_minutes`` before
    ``release_time`` in each DST half. MB 06:00 ET -> ``("50 9 * * *", "50 10 * * *")``, the
    values ``compute.bicep`` ships today. ``tests/test_release_events_parity.py`` (MU-15a) pins
    ``infra/bicep/release_events.json`` to this function (§6.2)."""
    raise NotImplementedError(_MU1)
