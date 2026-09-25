"""Per-course release policy: WHEN a course's booking window opens (MULTIUSER_PLAN §6).

A ``ReleasePolicy`` says "tee times for date D become bookable at ``release_time`` in
``timezone``, ``advance_days`` days before D". It lives in ``core/`` (not ``tenant/``) because
adapters carry it as a ``ClassVar`` and adapters may only import ``core`` (layering, §2.2).

Implemented in MULTIUSER_PLAN MU-1 (E4). Adapters carry the data (``MangroveBayAdapter.
release_policy``, ``SydneyMarovitzAdapter.release_policy``) but NOTHING on the production path
reads it yet: the single-user CLI path keeps reading ``scheduler.timezone`` /
``scheduler.fire_time`` / ``target_offsets`` from TOML until the cutover (§11), and the ACA crons
stay hand-written in ``compute.bicep`` until MU-15a derives them from ``cron_pair``. Until then
``tests/test_release_policy.py::test_cron_pair_mb_matches_compute_bicep`` pins that the derivation
EQUALS what prod ships.

Every helper is a pure function of its arguments — no wall-clock reads. Callers pass ``now_utc``
from an injected ``Clock`` (``clock.now_utc()``), so the tenant runner stays ``FakeClock``-
deterministic like the rest of ``core/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# v1 release-hour band (§6.1). The DST gate compares ``hour == fire_time.hour - 1`` and
# ``Orchestrator._compute_t0`` anchors T0 to ``local_now.date()``: a midnight release would fire
# the cron at 23:50 on D-1 and compute T0 as 00:00 on D-1 (in the past), booking ~24 h late on
# the wrong target date; 01:00-03:00 collides with DST transition gaps. Midnight support needs a
# "next occurrence" T0 and is a follow-up (§14).
_MIN_RELEASE_HOUR = 4
_MAX_RELEASE_HOUR = 22

# Default cron lead: the cron lands the runner 10 min before the release so the pre-T0 CAPTCHA
# prefetch + login pre-warm (lead 120 s) and the busy-wait have runway. 05:50 for MB's 06:00 —
# the value ``compute.bicep`` has shipped since M6.
DEFAULT_LEAD_MINUTES = 10

# NOTE on probing DST halves (``cron_pair``): tzdata changes only FUTURE rules — after a DST
# abolition the OLD years keep their old transitions — so a fixed reference year would keep
# returning the pre-abolition pair forever. ``cron_pair`` therefore probes the year the
# caller passes (``probe_year``), defaulting to the CURRENT UTC year so a rule change surfaces
# as soon as tzdata ships it (and the MU-15a parity test then fails loudly against the
# committed JSON). That default is the module's ONLY wall-clock read; it selects which year's
# RULES to read, never a T0 instant, so FakeClock determinism is not at stake.


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    """When a course releases inventory. Mangrove Bay = (7, 06:00, America/New_York).

    Invariants (validated by ``validate_release_policy``, MU-1): ``advance_days >= 0``;
    ``timezone`` is a valid IANA zone; ``release_time`` is naive (the zone is ``timezone``);
    ``4 <= release_time.hour <= 22`` in v1, because the DST gate
    (``core/dst_gate.should_proceed``) compares ``hour == fire_time.hour - 1`` and
    ``Orchestrator._compute_t0`` anchors T0 to ``local_now.date()``. A midnight release would
    fire on the wrong calendar day, and 01:00-03:00 collides with DST transition gaps
    (§6.1). ``hosted_booking`` gates job derivation: a course with ``False`` gets no ACA job
    (Sydney Marovitz, whose TeeItUp PAN path is out of hosted scope, §6.4).
    """

    advance_days: int
    release_time: time
    timezone: str
    hosted_booking: bool = True


class ReleaseKey(NamedTuple):
    """The identity of a RELEASE EVENT (§6.2): courses with the same ``(timezone,
    release_time)`` share one EDT/EST job pair, each keeping its own ``advance_days``.
    ``hosted_booking`` is deliberately NOT part of it — it decides whether a job is DERIVED
    for the course, not which event the course belongs to."""

    timezone: str
    release_time: time


def release_key(policy: ReleasePolicy) -> ReleaseKey:
    """Group ``policy`` into its release event: identical ``(timezone, release_time)`` ->
    identical key, so a ``dict[ReleaseKey, list[...]]`` yields one job pair per event."""
    return ReleaseKey(timezone=policy.timezone, release_time=policy.release_time)


class CronPair(NamedTuple):
    """``(daylight, standard)`` UTC crons for one release event — a plain 2-tuple for the
    ``release_events.json`` ``cronDst``/``cronStd`` shape (§6.2), plus the DEDUPE signal.

    In a zone with no DST (America/Phoenix) both halves are the SAME instant. Deploying the
    usual ``-edt``/``-est`` job pair would then fire two runners at once, and BOTH pass the
    DST gate, so two processes race one event with no lease in toml mode. ``deduped`` says
    so and ``jobs`` is what the MU-15a event loop must iterate to derive jobs from.
    """

    daylight: str
    standard: str

    @property
    def deduped(self) -> bool:
        """True when both halves are the same cron: derive ONE job, not two."""
        return self.daylight == self.standard

    @property
    def jobs(self) -> tuple[str, ...]:
        """The distinct crons to deploy: ``(daylight,)`` when deduped, else both."""
        return (self.daylight,) if self.deduped else (self.daylight, self.standard)


def _zone(policy: ReleasePolicy) -> ZoneInfo:
    try:
        return ZoneInfo(policy.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid timezone {policy.timezone!r}: {exc}") from None


def validate_release_policy(
    policy: ReleasePolicy, *, lead_minutes: int = DEFAULT_LEAD_MINUTES
) -> None:
    """Raise ``ValueError`` if ``policy`` violates the v1 invariants above (§6.1), or if a cron
    lead of ``lead_minutes`` would put the fire instant on the PREVIOUS calendar day, or if the
    fire instant would land outside the DST-gate hour ``release_time.hour - 1`` (i.e. unless
    ``release_time.minute < lead_minutes <= release_time.minute + 60``)."""
    if policy.advance_days < 0:
        raise ValueError(f"advance_days must be >= 0, got {policy.advance_days}")
    _zone(policy)  # raises on an unknown IANA name
    if policy.release_time.tzinfo is not None:
        raise ValueError(
            f"release_time must be naive (it is interpreted in {policy.timezone!r}), "
            f"got tzinfo={policy.release_time.tzinfo!r}"
        )
    if not _MIN_RELEASE_HOUR <= policy.release_time.hour <= _MAX_RELEASE_HOUR:
        raise ValueError(
            f"release_time hour must be in [{_MIN_RELEASE_HOUR:02d}, {_MAX_RELEASE_HOUR:02d}] "
            f"in v1 (DST gate + same-day T0 anchoring, MULTIUSER_PLAN §6.1), "
            f"got {policy.release_time.isoformat()}"
        )
    if lead_minutes < 0:
        raise ValueError(f"lead_minutes must be >= 0, got {lead_minutes}")
    fire_total = _minutes_of_day(policy.release_time) - lead_minutes
    if fire_total < 0:
        raise ValueError(
            f"a {lead_minutes}-minute lead before release_time "
            f"{policy.release_time.isoformat()} crosses midnight onto the previous day"
        )
    # The DST gate (§6.3 reuses `should_proceed(fire_time=release_time)` per event) admits a
    # run iff the course-local HOUR at cron time == release_time.hour - 1. So the fire time
    # MUST land in that hour — equivalently `minute < lead <= minute + 60`. Otherwise the pair
    # is silently unusable: e.g. 06:30 with the default 10-min lead fires 06:20, hour 6 — in
    # summer NEITHER cron passes (06:20 / 07:20 EDT, never books), and in winter the WRONG
    # cron passes (05:20 EST, hour 5) with T0 70 min out, so the busy-wait blows the 1200 s
    # replica timeout — exactly the failure `dst_gate.py` exists to prevent. Lead 0 fails the
    # same way (December: daylight cron lands 05:00 EST, T0 an hour away).
    if fire_total // 60 != policy.release_time.hour - 1:
        raise ValueError(
            f"a {lead_minutes}-minute lead before release_time {policy.release_time.isoformat()} "
            f"fires at {fire_total // 60:02d}:{fire_total % 60:02d}, outside the DST-gate hour "
            f"{policy.release_time.hour - 1:02d} (need release minute < lead <= minute + 60)"
        )


def _minutes_of_day(t: time) -> int:
    return t.hour * 60 + t.minute


def fire_time_for(policy: ReleasePolicy, *, lead_minutes: int = DEFAULT_LEAD_MINUTES) -> time:
    """Course-local wall-clock at which the cron lands the runner: ``release_time - lead``.
    MB -> 05:50. Its ``.hour`` is GUARANTEED to be ``release_time.hour - 1`` — the reading
    ``dst_gate.should_proceed`` makes — because ``validate_release_policy`` (called here)
    rejects any ``(release_time, lead)`` for which that does not hold."""
    validate_release_policy(policy, lead_minutes=lead_minutes)
    total = _minutes_of_day(policy.release_time) - lead_minutes
    return time(total // 60, total % 60)


def _require_aware(now_utc: datetime) -> None:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (use clock.now_utc())")


def _local_today(policy: ReleasePolicy, now_utc: datetime) -> date:
    _require_aware(now_utc)
    return now_utc.astimezone(_zone(policy)).date()


def release_instant_for(policy: ReleasePolicy, now_utc: datetime) -> datetime:
    """Today's release instant (tz-aware UTC), where "today" is the calendar date of
    ``now_utc`` in the COURSE timezone. Uses ``zoneinfo`` so DST resolves correctly (§6.3):
    on the spring-forward morning 06:00 ET is already EDT (10:00Z); on the fall-back morning
    it is EST (11:00Z), the unambiguous second 06:00 under ``fold=0`` (PLAN.md §6.3).

    Does NOT validate ``policy`` (only the zone must resolve): a midnight release computes
    silently, and the same-day anchoring is exactly why v1 rejects it elsewhere — call
    ``validate_release_policy`` at the boundary that ACCEPTS a policy (adapter ClassVar tests,
    the MU-15a event derivation), not on every read."""
    local_release = datetime.combine(
        _local_today(policy, now_utc), policy.release_time, tzinfo=_zone(policy)
    )
    return local_release.astimezone(UTC)


def target_date_for(policy: ReleasePolicy, now_utc: datetime) -> date:
    """The date whose inventory is released today: course-local today + ``advance_days``.

    Always computed in the COURSE timezone, never UTC or the runner's zone: the tenant booking
    runner's READ #1 selects rows for exactly this date (§4.2). (22:30 EDT on the 25th is
    02:30Z on the 26th — the UTC date is already tomorrow, the course's is not.)

    Does NOT validate ``policy`` (only the zone must resolve) — see ``release_instant_for``.
    """
    return _local_today(policy, now_utc) + timedelta(days=policy.advance_days)


def _utc_cron_for(fire_local_on_probe: datetime) -> str:
    """Daily ``M H * * *`` UTC cron for a zone-aware fire instant on a probe date. A daily cron
    is date-free, so the UTC conversion crossing a day boundary (a 22:xx ET release fires the
    previous UTC day) is harmless."""
    fire_utc = fire_local_on_probe.astimezone(UTC)
    return f"{fire_utc.minute} {fire_utc.hour} * * *"


def cron_pair(
    policy: ReleasePolicy,
    *,
    lead_minutes: int = DEFAULT_LEAD_MINUTES,
    probe_year: int | None = None,
) -> CronPair:
    """``CronPair(daylight, standard)`` UTC cron expressions firing ``lead_minutes`` before
    ``release_time`` in each DST half. MB 06:00 ET -> ``("50 9 * * *", "50 10 * * *")``, the
    values ``compute.bicep`` ships today. ``tests/test_release_events_parity.py`` (MU-15a) pins
    ``infra/bicep/release_events.json`` to this function (§6.2). Validates the policy first.

    **How the halves are found.** Jan 1 and Jul 1 of ``probe_year`` (default: the current UTC
    year — see the module note on why a FIXED year would be wrong after a DST abolition) are
    converted with the zone's rules and classified by ``utcoffset()``: the half with the
    LARGER offset is daylight (clocks sprung forward), so a southern-hemisphere zone still
    comes out ``(daylight, standard)``. ``dst()`` is deliberately NOT used — it is a heuristic
    tzdata does not model uniformly (Africa/Casablanca reports ``dst() != 0`` for its permanent
    UTC+1). **Equal offsets ⇒ the same cron twice**, and the pair reports ``deduped=True``:
    that is NOT tolerable as two jobs (both would fire at one instant and both pass the gate —
    two runners racing one event), so MU-15a derives from ``CronPair.jobs``, which is then a
    1-tuple.

    Known limits, all outside the ratified v1 band or unreachable by any planned course (US
    zones only), documented rather than handled: (a) a DST period lying strictly BETWEEN the
    two probes (Casablanca's Ramadan dip) is invisible — the pair is deduped and the runner
    would be an hour off for those weeks; (b) a 30-minute DST offset (Australia/Lord_Howe)
    puts the wrong-season cron in the SAME gate hour, so both jobs pass — a double fire;
    (c) a zone whose transition instant falls between the two crons (EET's 04:00-local
    fall-back with a 04:xx release) also double-fires on that one morning. The validator's
    04-22 band and 1-hour US transitions at 02:00 local make (b)/(c) impossible for MB and
    the Chicago placeholder — pinned by the (release, lead) x transition-day sweep in
    ``tests/test_release_policy.py``.
    """
    fire_local = fire_time_for(policy, lead_minutes=lead_minutes)
    zone = _zone(policy)
    year = datetime.now(tz=UTC).year if probe_year is None else probe_year
    jan = datetime.combine(date(year, 1, 1), fire_local, tzinfo=zone)
    jul = datetime.combine(date(year, 7, 1), fire_local, tzinfo=zone)
    jan_off, jul_off = jan.utcoffset(), jul.utcoffset()
    assert jan_off is not None and jul_off is not None  # ZoneInfo always supplies one
    daylight, standard = (jan, jul) if jan_off > jul_off else (jul, jan)
    return CronPair(daylight=_utc_cron_for(daylight), standard=_utc_cron_for(standard))
