"""MU-15a: `infra/bicep/release_events.json` is the single source of truth for release-event
job derivation (MULTIUSER_PLAN §6.2). Both `compute.bicep` and `killswitch.bicep` read it with
`loadJsonContent`. This test pins the JSON to the pure `core/release_policy.py` helpers so the
committed crons can never drift from what `cron_pair` derives for the courses it lists.
"""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

import pytest

from teetime.core.release_policy import ReleasePolicy, cron_pair, release_key
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.courses.teeitup.sydney_marovitz import SydneyMarovitzAdapter

RELEASE_EVENTS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "release_events.json"

# Map course id (as it appears in release_events.json) -> the adapter ClassVar carrying its
# ReleasePolicy (MU-1, E4). Only courses actually referenced by an event need an entry here;
# a course id in the JSON with no entry below is a test bug (fails loudly at lookup).
_COURSE_POLICIES: dict[str, ReleasePolicy] = {
    "foreup:mangrove_bay": MangroveBayAdapter.release_policy,
    "teeitup:sydney_marovitz": SydneyMarovitzAdapter.release_policy,
}


@pytest.fixture(scope="module")
def events() -> list[dict[str, object]]:
    return json.loads(RELEASE_EVENTS.read_text())


def test_release_events_json_is_a_nonempty_list(events: list[dict[str, object]]) -> None:
    assert isinstance(events, list)
    assert len(events) >= 1


def test_release_events_crons_match_cron_pair(events: list[dict[str, object]]) -> None:
    for event in events:
        for course_id in event["courses"]:  # type: ignore[union-attr]
            policy = _COURSE_POLICIES[course_id]  # type: ignore[index]
            pair = cron_pair(policy)
            assert (pair.daylight, pair.standard) == (event["cronDst"], event["cronStd"]), (
                f"{event['key']}/{course_id}: release_events.json crons != cron_pair(policy)"
            )


def test_release_events_courses_share_release_key(events: list[dict[str, object]]) -> None:
    for event in events:
        keys = {release_key(_COURSE_POLICIES[c]) for c in event["courses"]}  # type: ignore[union-attr,index]
        assert len(keys) == 1, f"{event['key']}: its courses do not share (timezone, release_time)"
        (key,) = keys
        assert key.timezone == event["timezone"]
        assert key.release_time == time.fromisoformat(event["releaseTime"])  # type: ignore[arg-type]


def test_each_hosted_course_appears_in_exactly_one_event(
    events: list[dict[str, object]],
) -> None:
    hosted = {cid for cid, p in _COURSE_POLICIES.items() if p.hosted_booking}
    seen: dict[str, str] = {}
    for event in events:
        for course_id in event["courses"]:  # type: ignore[union-attr]
            policy = _COURSE_POLICIES[course_id]  # type: ignore[index]
            if not policy.hosted_booking:
                continue
            assert course_id not in seen, (
                f"{course_id} appears in both event {seen[course_id]!r} and {event['key']!r}"
            )
            seen[course_id] = event["key"]  # type: ignore[assignment]
    assert set(seen) == hosted, "every hosted_booking=True course must appear in exactly one event"


def test_non_hosted_course_is_absent_from_every_event(events: list[dict[str, object]]) -> None:
    # Sydney Marovitz (hosted_booking=False, §6.4) gets NO ACA job in v1 — it must not be
    # listed in any event, or the (unimplemented) generic job-derivation path would try to
    # deploy a TeeItUp booking job with no PAN wiring.
    listed = {c for event in events for c in event["courses"]}  # type: ignore[union-attr]
    assert "teeitup:sydney_marovitz" not in listed


def test_mangrove_bay_keeps_legacy_job_name_prefix(events: list[dict[str, object]]) -> None:
    # MB's job must keep the pre-MU-15a names (teetime-job-<env>-edt/-est) so the eventual
    # cutover (§11) flips ARGUMENTS on the existing resources instead of creating parallel ones.
    (mb_event,) = [e for e in events if "foreup:mangrove_bay" in e["courses"]]  # type: ignore[operator]
    assert mb_event["jobNamePrefix"] == "teetime-job"
    assert mb_event["key"] == "mb0600et"
