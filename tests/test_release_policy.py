"""MULTIUSER_PLAN MU-1: ``core/release_policy.py`` — WHEN a course's booking window opens.

A ``ReleasePolicy`` is pure data attached to an adapter class; the helpers here derive from it
(a) the target date for a run — in the COURSE timezone, never UTC; (b) the UTC cron pair for the
release event (one per DST half, firing ``lead_minutes`` before the release — 05:50 for MB's
06:00, exactly what ``compute.bicep`` ships); (c) the release instant across DST; and (d) a
grouping key so courses sharing ``(timezone, release_time)`` land in ONE job (§6.2).

Nothing on the production path reads these yet (E4: "nothing reads it"); the tests pin the
semantics the tenant runner (MU-9a) and the release-events parity test (MU-15a) will rely on.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from teetime.core.clock import FakeClock
from teetime.core.dst_gate import should_proceed
from teetime.core.release_policy import (
    CronPair,
    ReleaseKey,
    ReleasePolicy,
    cron_pair,
    fire_time_for,
    release_instant_for,
    release_key,
    target_date_for,
    validate_release_policy,
)
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.courses.teeitup.sydney_marovitz import SydneyMarovitzAdapter

RELEASE_EVENTS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "release_events.json"

NY = "America/New_York"
CHI = "America/Chicago"
MB_POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=NY)
CHI_0600 = ReleasePolicy(advance_days=15, release_time=time(6, 0), timezone=CHI)


def _utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _cron_instant(cron: str, on: date) -> datetime:
    """The UTC instant a daily ``M H * * *`` cron fires on ``on``."""
    minute, hour, rest = cron.split(" ", 2)
    assert rest == "* * *", cron
    return datetime.combine(on, time(int(hour), int(minute)), tzinfo=UTC)


# --- target date --------------------------------------------------------------------------


def test_target_date_uses_course_tz_not_utc() -> None:
    # 02:30 UTC on Sep 26 is still 22:30 EDT on Sep 25: the course-local day has NOT rolled.
    now = _utc(2026, 9, 26, 2, 30)
    assert now.date() == date(2026, 9, 26)  # the trap: UTC "today"
    assert target_date_for(MB_POLICY, now) == date(2026, 10, 2)  # Sep 25 + 7, NOT Oct 3


def test_target_date_adds_advance_days_in_course_tz() -> None:
    # Plain daytime case: 14:00 UTC = 10:00 EDT, same calendar day everywhere.
    assert target_date_for(MB_POLICY, _utc(2026, 9, 25, 14, 0)) == date(2026, 10, 2)
    assert target_date_for(CHI_0600, _utc(2026, 9, 25, 14, 0)) == date(2026, 10, 10)


def test_target_date_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        target_date_for(MB_POLICY, datetime(2026, 9, 25, 14, 0))


# --- cron pair ------------------------------------------------------------------------------


def test_cron_pair_mb_matches_compute_bicep() -> None:
    """The derived MB pair must EQUAL the strings prod ships. Since MU-15a, compute.bicep
    derives its booking-job crons from ``release_events.json`` (loadJsonContent) rather than
    hand-written vars, so that JSON is what's read here — tests/test_release_events_parity.py
    is the sibling that pins the WHOLE file (not just MB) to this same function."""
    events = json.loads(RELEASE_EVENTS.read_text())
    (mb_event,) = [e for e in events if "foreup:mangrove_bay" in e["courses"]]
    assert cron_pair(MangroveBayAdapter.release_policy) == (
        mb_event["cronDst"],
        mb_event["cronStd"],
    )
    # Belt-and-braces: the literal values the plan (§6.1) and PLAN.md §6.3 document.
    assert cron_pair(MB_POLICY) == ("50 9 * * *", "50 10 * * *")


def test_cron_pair_chicago() -> None:
    # §6.1's worked example: a 06:00 America/Chicago release is one UTC hour later than MB's.
    assert cron_pair(CHI_0600) == ("50 10 * * *", "50 11 * * *")


def test_cron_pair_honours_lead_minutes() -> None:
    # 60 is the largest lead that keeps an on-the-hour release's fire in the gate hour (05:00).
    assert cron_pair(MB_POLICY, lead_minutes=60) == ("0 9 * * *", "0 10 * * *")
    assert cron_pair(MB_POLICY, lead_minutes=30) == ("30 9 * * *", "30 10 * * *")


def test_cron_pair_is_a_tuple_pair_with_no_dedupe_for_a_dst_zone() -> None:
    pair = cron_pair(MB_POLICY)
    assert isinstance(pair, CronPair)
    assert (pair.daylight, pair.standard) == ("50 9 * * *", "50 10 * * *")
    assert pair.deduped is False
    assert pair.jobs == ("50 9 * * *", "50 10 * * *")  # two ACA jobs, one per DST half


def test_cron_pair_no_dst_zone_is_deduped_to_one_job() -> None:
    """Arizona never observes DST: both halves are the same UTC instant (MST = UTC-7). Two
    jobs named -edt/-est would then fire at the SAME instant and BOTH pass the gate — two
    runners racing one event with no lease in toml mode. The pair says so, and ``jobs`` is
    the single cron MU-15a's event loop must derive ONE job from."""
    az = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone="America/Phoenix")
    pair = cron_pair(az)
    assert pair == ("50 12 * * *", "50 12 * * *")  # still a 2-tuple for the JSON shape
    assert pair.deduped is True
    assert pair.jobs == ("50 12 * * *",)


def test_cron_pair_halves_ordered_by_utc_offset_not_dst_flag() -> None:
    """Jan 1 and Jul 1 of the probe year are classified by comparing ``utcoffset()`` — the
    daylight half is the one with the LARGER offset (clocks sprung forward). Morocco is the
    case a ``dst()`` heuristic mishandles: tzdata models it as permanent UTC+1 with ``dst()``
    non-zero year-round (except the Ramadan dip in between), so both probes share an offset
    and the pair must be DEDUPED, not two identical 'daylight' crons."""
    casa = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone="Africa/Casablanca")
    pair = cron_pair(casa)
    assert pair == ("50 4 * * *", "50 4 * * *")
    assert pair.deduped is True
    # The Jan/Jul probes cannot see a DST period that lies strictly BETWEEN them (Casablanca's
    # Ramadan shift): that limitation is documented on cron_pair and is unreachable by any
    # planned course (all are US zones).


def test_cron_pair_probe_year_selects_that_years_rules() -> None:
    """tzdata changes only FUTURE rules, so a fixed probe year would keep an old pair after a
    DST abolition. Brazil abolished DST in 2019: probing 2018 yields a real pair (Jan is
    summer time there), probing 2026 yields a deduped one."""
    sp = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone="America/Sao_Paulo")
    old = cron_pair(sp, probe_year=2018)
    assert old == ("50 7 * * *", "50 8 * * *")  # daylight FIRST even though it is January
    assert old.deduped is False
    new = cron_pair(sp, probe_year=2026)
    assert new == ("50 8 * * *", "50 8 * * *")
    assert new.deduped is True


def test_cron_pair_default_probe_year_is_current_utc_year() -> None:
    # The default reads the CURRENT year so an abolition shows up as soon as tzdata ships it;
    # for a zone whose rules are stable across years the explicit and default forms agree.
    this_year = datetime.now(tz=UTC).year
    assert cron_pair(MB_POLICY) == cron_pair(MB_POLICY, probe_year=this_year)


def test_fire_time_is_release_minus_lead() -> None:
    assert fire_time_for(MB_POLICY) == time(5, 50)
    assert fire_time_for(MB_POLICY, lead_minutes=25) == time(5, 35)


# --- validation -----------------------------------------------------------------------------


def test_validate_accepts_mb_policy() -> None:
    validate_release_policy(MB_POLICY)  # must not raise


def test_validate_rejects_midnight_release() -> None:
    midnight = ReleasePolicy(advance_days=15, release_time=time(0, 0), timezone=CHI)
    with pytest.raises(ValueError, match="release_time"):
        validate_release_policy(midnight)
    # cron_pair validates too — a midnight policy can never derive a job.
    with pytest.raises(ValueError, match="release_time"):
        cron_pair(midnight)


@pytest.mark.parametrize(
    "release_time",
    [
        pytest.param(time(0, 5), id="lead_crosses_midnight"),
        pytest.param(time(1, 0), id="dst_gap_hour_1"),
        pytest.param(time(3, 59), id="just_below_floor"),
        pytest.param(time(23, 0), id="above_ceiling"),
    ],
)
def test_validate_rejects_out_of_band_hours(release_time: time) -> None:
    policy = ReleasePolicy(advance_days=7, release_time=release_time, timezone=NY)
    with pytest.raises(ValueError, match="release_time"):
        validate_release_policy(policy)


def test_validate_rejects_lead_that_crosses_midnight() -> None:
    # 04:00 is inside the hour band, but a 5-hour lead would put the cron on the previous day —
    # the DST gate's `hour == fire_time.hour - 1` reading then names the wrong calendar day.
    policy = ReleasePolicy(advance_days=7, release_time=time(4, 0), timezone=NY)
    validate_release_policy(policy)  # fine with the default 10-min lead
    with pytest.raises(ValueError, match="midnight"):
        validate_release_policy(policy, lead_minutes=5 * 60)
    with pytest.raises(ValueError, match="midnight"):
        cron_pair(policy, lead_minutes=5 * 60)


def test_validate_rejects_negative_lead() -> None:
    with pytest.raises(ValueError, match="lead_minutes"):
        validate_release_policy(MB_POLICY, lead_minutes=-1)


@pytest.mark.parametrize(
    ("release_time", "lead"),
    [
        # 06:30 - 10 min = 06:20: the fire lands in hour 6, the gate wants hour 5. Summer: both
        # crons land 06:20/07:20 EDT -> NEVER books; winter: the WRONG (daylight) cron lands
        # 05:20 EST and PASSES with T0 70 min out -> busy-wait blows the 1200 s replica timeout.
        pytest.param(time(6, 30), 10, id="0630_lead10_lands_in_release_hour"),
        # Lead 0: fire == release (hour 6). December: the daylight cron lands 05:00 EST and
        # passes with T0 a full hour away.
        pytest.param(time(6, 0), 0, id="lead0_fire_equals_release"),
        # Lead 70: fire 04:50, hour 4 != 5. December never books.
        pytest.param(time(6, 0), 70, id="lead70_lands_two_hours_early"),
        pytest.param(time(6, 30), 91, id="0630_lead91_just_past_the_hour"),
    ],
)
def test_validate_rejects_lead_outside_gate_hour(release_time: time, lead: int) -> None:
    """The derived fire time MUST land in hour ``release_time.hour - 1``: that is the reading
    ``dst_gate.should_proceed`` makes (§6.3 reuses it per event), so anything else is a policy
    whose crons can never pass the gate in one season, or pass in the WRONG one."""
    policy = ReleasePolicy(advance_days=7, release_time=release_time, timezone=NY)
    with pytest.raises(ValueError, match="hour"):
        validate_release_policy(policy, lead_minutes=lead)
    with pytest.raises(ValueError, match="hour"):
        cron_pair(policy, lead_minutes=lead)


@pytest.mark.parametrize(
    ("release_time", "lead"),
    [
        pytest.param(time(6, 30), 45, id="0630_lead45"),
        pytest.param(time(6, 30), 90, id="0630_lead90_boundary"),
        pytest.param(time(6, 0), 60, id="0600_lead60_boundary"),
        pytest.param(time(22, 15), 20, id="2215_lead20"),
    ],
)
def test_validate_accepts_lead_inside_gate_hour(release_time: time, lead: int) -> None:
    policy = ReleasePolicy(advance_days=7, release_time=release_time, timezone=NY)
    validate_release_policy(policy, lead_minutes=lead)
    assert fire_time_for(policy, lead_minutes=lead).hour == release_time.hour - 1


def test_validate_rejects_bad_timezone() -> None:
    bad = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone="Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="timezone"):
        validate_release_policy(bad)


def test_validate_rejects_negative_advance_days() -> None:
    bad = ReleasePolicy(advance_days=-1, release_time=time(6, 0), timezone=NY)
    with pytest.raises(ValueError, match="advance_days"):
        validate_release_policy(bad)


def test_validate_rejects_tz_aware_release_time() -> None:
    bad = ReleasePolicy(advance_days=7, release_time=time(6, 0, tzinfo=ZoneInfo(NY)), timezone=NY)
    with pytest.raises(ValueError, match="naive"):
        validate_release_policy(bad)


# --- release instant across DST -------------------------------------------------------------


def test_release_instant_dst_spring_forward() -> None:
    # 2026-03-08 is the spring-forward Sunday: by 06:00 ET the zone is EDT (UTC-4).
    assert release_instant_for(MB_POLICY, _utc(2026, 3, 8, 9, 50)) == _utc(2026, 3, 8, 10, 0)
    # The day before is still EST (UTC-5).
    assert release_instant_for(MB_POLICY, _utc(2026, 3, 7, 10, 50)) == _utc(2026, 3, 7, 11, 0)


def test_release_instant_dst_fall_back() -> None:
    # 2026-11-01 is the fall-back Sunday: 06:00 ET is the (unambiguous) EST instant, UTC-5.
    assert release_instant_for(MB_POLICY, _utc(2026, 11, 1, 10, 50)) == _utc(2026, 11, 1, 11, 0)
    # The day before is still EDT (UTC-4).
    assert release_instant_for(MB_POLICY, _utc(2026, 10, 31, 9, 50)) == _utc(2026, 10, 31, 10, 0)


def test_release_instant_is_utc_aware() -> None:
    inst = release_instant_for(MB_POLICY, _utc(2026, 9, 25, 9, 50))
    assert inst.tzinfo is UTC
    assert inst == _utc(2026, 9, 25, 10, 0)


def test_release_instant_uses_course_local_today() -> None:
    # 02:30 UTC Sep 26 = 22:30 EDT Sep 25 → "today's" release is Sep 25 06:00 EDT (already past),
    # matching Orchestrator._compute_t0's `local_now.date()` anchoring — NOT the UTC date.
    assert release_instant_for(MB_POLICY, _utc(2026, 9, 26, 2, 30)) == _utc(2026, 9, 25, 10, 0)


def test_release_instant_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        release_instant_for(MB_POLICY, datetime(2026, 9, 25, 14, 0))


# --- grouping key ---------------------------------------------------------------------------


def test_release_key_groups_identical_policies() -> None:
    mb_like_14 = ReleasePolicy(advance_days=14, release_time=time(6, 0), timezone=NY)
    assert release_key(MB_POLICY) == release_key(mb_like_14)  # advance_days is NOT part of it
    assert release_key(MB_POLICY) != release_key(CHI_0600)
    assert release_key(MB_POLICY) == ReleaseKey(timezone=NY, release_time=time(6, 0))
    groups: dict[ReleaseKey, list[ReleasePolicy]] = {}
    for p in (MB_POLICY, CHI_0600, mb_like_14):
        groups.setdefault(release_key(p), []).append(p)
    assert len(groups) == 2
    assert groups[release_key(MB_POLICY)] == [MB_POLICY, mb_like_14]


def test_release_key_ignores_hosted_flag() -> None:
    unhosted = ReleasePolicy(
        advance_days=7, release_time=time(6, 0), timezone=NY, hosted_booking=False
    )
    assert release_key(unhosted) == release_key(MB_POLICY)


# --- DST gate reproducibility --------------------------------------------------------------


@pytest.mark.parametrize(
    ("on", "correct_half"),
    [
        pytest.param(date(2026, 5, 31), 0, id="edt_season_daylight_cron"),
        pytest.param(date(2026, 12, 6), 1, id="est_season_standard_cron"),
        pytest.param(date(2026, 3, 8), 0, id="spring_forward_morning"),
        pytest.param(date(2026, 11, 1), 1, id="fall_back_morning"),
    ],
)
def test_dst_gate_semantics_reproducible_from_policy(on: date, correct_half: int) -> None:
    """Today's `should_proceed` (hour == fire_time.hour - 1) holds at the policy-derived cron.

    The correct-season cron from `cron_pair` lands at course-local :50 of the hour before the
    release, so the gate proceeds; the wrong-season cron lands an hour off and the gate exits —
    exactly the semantics `compute.bicep` + `dst_gate.py` implement today, now derivable from
    the policy alone (§6.3: "no new DST logic is needed").
    """
    pair = cron_pair(MB_POLICY)
    assert fire_time_for(MB_POLICY).hour == MB_POLICY.release_time.hour - 1
    for half, cron in enumerate(pair):
        clock = FakeClock(start=_cron_instant(cron, on))
        proceed = should_proceed(
            clock, timezone=MB_POLICY.timezone, fire_time=MB_POLICY.release_time
        )
        assert proceed is (half == correct_half), (cron, on)


_GATE_SWEEP_DATES = (date(2026, 5, 31), date(2026, 12, 6), date(2026, 3, 8), date(2026, 11, 1))


@pytest.mark.parametrize(
    ("release_time", "lead"),
    [
        pytest.param(time(6, 0), 10, id="mb_0600_lead10"),
        pytest.param(time(6, 0), 60, id="0600_lead60_boundary"),
        pytest.param(time(6, 30), 45, id="0630_lead45"),
        pytest.param(time(6, 30), 90, id="0630_lead90_boundary"),
        pytest.param(time(4, 0), 60, id="0400_lead60_band_floor"),
        pytest.param(time(22, 15), 20, id="2215_lead20_band_ceiling_utc_day_crosses"),
    ],
)
@pytest.mark.parametrize("on", _GATE_SWEEP_DATES, ids=lambda d: d.isoformat())
def test_every_valid_policy_has_exactly_one_gate_passing_cron_per_day(
    release_time: time, lead: int, on: date
) -> None:
    """validates ⇒ on EVERY UTC day (mid-season AND both transition Sundays) exactly ONE of the
    two derived crons passes ``should_proceed`` — never zero (a season that never books), never
    two (a double fire). Pinned across the band, not just for MB."""
    policy = ReleasePolicy(advance_days=7, release_time=release_time, timezone=NY)
    validate_release_policy(policy, lead_minutes=lead)
    passing = [
        cron
        for cron in cron_pair(policy, lead_minutes=lead)
        if should_proceed(
            FakeClock(start=_cron_instant(cron, on)), timezone=NY, fire_time=release_time
        )
    ]
    assert len(passing) == 1, (release_time, lead, on, passing)


def test_correct_cron_fires_lead_minutes_before_release_instant() -> None:
    # In each half, the correct-season cron instant is exactly `lead_minutes` before the release.
    for on, half in ((date(2026, 5, 31), 0), (date(2026, 12, 6), 1)):
        fire = _cron_instant(cron_pair(MB_POLICY)[half], on)
        assert (release_instant_for(MB_POLICY, fire) - fire).total_seconds() == 10 * 60


# --- adapter ClassVars (E4) -----------------------------------------------------------------


def test_mangrove_bay_release_policy_classvar() -> None:
    assert MangroveBayAdapter.release_policy == ReleasePolicy(
        advance_days=7, release_time=time(6, 0), timezone=NY, hosted_booking=True
    )
    validate_release_policy(MangroveBayAdapter.release_policy)


def test_sydney_marovitz_release_policy_classvar() -> None:
    p = SydneyMarovitzAdapter.release_policy
    assert p.advance_days == 15
    assert p.timezone == CHI
    assert p.hosted_booking is False  # §6.4: no Marovitz job is derived in v1
    validate_release_policy(p)  # the S-M4 placeholder must at least be a derivable policy


def test_adapter_policies_are_distinct_release_events() -> None:
    assert release_key(MangroveBayAdapter.release_policy) != release_key(
        SydneyMarovitzAdapter.release_policy
    )
