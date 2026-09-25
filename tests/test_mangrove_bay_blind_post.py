"""PR2 of BLIND_POST_PLAN.md: ``MangroveBayAdapter.synthesize_blind_slots``.

Pins the derived morning grid + the ForeUP ``start_front`` computation (0-indexed
month) + the ``time`` field (1-indexed calendar month) + the BLIND_POST_TEMPLATE
overlay + midpoint ranking + truncation + the empty/in-window filtering, plus the
retroactive grid-validation logging the user requested (so that after a real 06:00
drop the derived grid can be diffed against the concurrent real search to detect
drift). Pure date arithmetic — no network.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core.models import (
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    TimeWindow,
)
from teetime.core.slot_utils import rank_slots_for_request
from teetime.courses.foreup.mangrove_bay import (
    BLIND_POST_MORNING_GRID,
    BLIND_POST_TEMPLATE,
    MangroveBayAdapter,
)

# A Saturday with a (notionally) open morning. Only date arithmetic is exercised.
SAT = date(2026, 5, 16)
WINDOW = TimeWindow(earliest=time(8, 45), latest=time(10, 0))  # midpoint 09:22:30


def _request(
    window: TimeWindow = WINDOW,
    *,
    players: int = 4,
    max_price: Decimal | None = None,
) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(SAT,),
        time_windows=(window,),
        players=tuple(
            Player(first_name=f"P{i}", last_name="L", email=f"p{i}@x.test") for i in range(players)
        ),
        course_preferences=(CourseId("foreup:mangrove_bay"),),
        max_price_per_player=max_price,
        dry_run=False,
    )


def test_grid_is_populated_not_sentinel() -> None:
    """PR2 commits the derived morning grid; the None fail-loud sentinel must be gone."""
    assert BLIND_POST_MORNING_GRID is not None
    assert len(BLIND_POST_MORNING_GRID) >= 1


def test_synthesize_filters_to_window() -> None:
    adapter = MangroveBayAdapter()
    narrow = TimeWindow(earliest=time(9, 0), latest=time(9, 30))
    slots = adapter.synthesize_blind_slots(_request(narrow), SAT, max_count=99)
    assert {s.tee_time.strftime("%H:%M") for s in slots} == {
        "09:00",
        "09:07",
        "09:15",
        "09:22",
        "09:30",
    }


def test_synthesize_ranked_closest_to_midpoint_first() -> None:
    adapter = MangroveBayAdapter()
    slots = adapter.synthesize_blind_slots(_request(), SAT, max_count=99)
    assert slots, "expected in-window candidates"
    # Window 08:45-10:00 midpoint = 09:22:30; closest grid time = 09:22.
    assert slots[0].tee_time.strftime("%H:%M") == "09:22"
    # Output order must equal the canonical ranker's order (no bespoke sort).
    expected = rank_slots_for_request(slots, _request())
    assert [s.slot_id for s in slots] == [s.slot_id for s in expected]


def test_synthesize_start_front_and_time_fields() -> None:
    adapter = MangroveBayAdapter()
    slots = adapter.synthesize_blind_slots(_request(), SAT, max_count=99)
    by_time = {s.tee_time.strftime("%H:%M"): s for s in slots}
    s = by_time["09:07"]
    # start_front: 0-indexed month (May -> 04), zero-padded; slot_id is its str.
    assert s.raw["start_front"] == 202604160907
    assert s.slot_id == "202604160907"
    # `time` field: real 1-indexed calendar month (May -> 05).
    assert s.raw["time"] == "2026-05-16 09:07"


@pytest.mark.parametrize(
    ("target", "expected_start_front", "expected_time"),
    [
        # January is the 0-indexed-month danger case: month-1=0 -> "00".
        (date(2026, 1, 17), 202600170907, "2026-01-17 09:07"),
        (date(2026, 5, 16), 202604160907, "2026-05-16 09:07"),
        # December -> month-1=11; the high end of the range.
        (date(2026, 12, 19), 202611190907, "2026-12-19 09:07"),
    ],
)
def test_synthesize_start_front_month_index_edge_cases(
    target: date, expected_start_front: int, expected_time: str
) -> None:
    """The start_front formula is 0-indexed month (JS Date style). A wrong month index
    means EVERY blind POST 400s, so lock in Jan (month-1=0 -> '00') and Dec (-> '11'),
    not just the May date the other tests use. The `time` field stays 1-indexed."""
    adapter = MangroveBayAdapter()
    # synthesize uses the target_date ARG for date math; request.target_dates is irrelevant.
    s = {
        x.tee_time.strftime("%H:%M"): x
        for x in adapter.synthesize_blind_slots(_request(), target, max_count=99)
    }["09:07"]
    assert s.raw["start_front"] == expected_start_front
    assert s.slot_id == str(expected_start_front)
    assert s.raw["time"] == expected_time


def test_synthesize_raw_is_template_overlaid() -> None:
    adapter = MangroveBayAdapter()
    s = adapter.synthesize_blind_slots(_request(), SAT, max_count=99)[0]
    # Static template fields carried through unchanged (book() relies on slot.raw).
    assert s.raw["course_id"] == 19671
    assert s.raw["schedule_id"] == 2149
    assert s.raw["teesheet_side_id"] == 3416
    # No card data ever in the template (ForeUP is card-on-file).
    assert not any(k in s.raw for k in ("card_number", "cvv", "cc_number", "ccv", "expiration"))
    # ONLY time + start_front diverge from the frozen template.
    diverged = {k for k in s.raw if BLIND_POST_TEMPLATE.get(k) != s.raw[k]}
    assert diverged == {"time", "start_front"}


def test_synthesize_truncates_to_max_count() -> None:
    adapter = MangroveBayAdapter()
    full = adapter.synthesize_blind_slots(_request(), SAT, max_count=99)
    top3 = adapter.synthesize_blind_slots(_request(), SAT, max_count=3)
    assert len(top3) == 3
    assert [s.slot_id for s in top3] == [s.slot_id for s in full[:3]]


def test_synthesize_empty_when_no_grid_time_in_window() -> None:
    adapter = MangroveBayAdapter()
    # Pre-dawn: no grid time here (the widened grid starts at 07:00, MU-3).
    dawn = TimeWindow(earliest=time(5, 0), latest=time(6, 30))
    assert adapter.synthesize_blind_slots(_request(dawn), SAT, max_count=99) == []


def test_synthesize_logs_firing_grid_for_retroactive_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """User request: log enough to retroactively confirm the derived grid was right.
    synthesize emits the configured grid + the in-window times it will blind-POST,
    so they can be diffed against the concurrent real search (which logs its matched
    morning tee times — see test_foreup_adapter)."""
    adapter = MangroveBayAdapter()
    with caplog.at_level("INFO"):
        adapter.synthesize_blind_slots(_request(), SAT, max_count=3)
    assert "blind-POST" in caplog.text
    assert "09:22" in caplog.text  # a firing time appears in the log


def test_synthesize_log_distinguishes_filtered_from_empty_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """should-fix 2: a grid time IN the window but filtered out (here by a max_price below
    the $46 green fee) must read differently in logs than 'no grid time in window', so a
    mis-set max_price/holes/party-size config is diagnosable rather than looking like drift."""
    adapter = MangroveBayAdapter()
    cheap = _request(max_price=Decimal("10.00"))  # below the 46 green fee → all filtered
    with caplog.at_level("INFO"):
        out = adapter.synthesize_blind_slots(cheap, SAT, max_count=99)
    assert out == []  # nothing survives the price filter
    # All 11 grid times are in the window, but 0 survive spots/holes/price.
    assert "11 in window" in caplog.text
    assert "0 survived" in caplog.text


# --- MULTIUSER_PLAN MU-3 / E3: the grid widened once to the full morning ------------------

# The EXACT grid that shipped from BLIND_POST_PLAN PR2 (2026-06-20) through infra/v2.16.0,
# captured literally so the pin below does not depend on the current constant.
PRE_CHANGE_GRID = [
    "08:45",
    "08:52",
    "09:00",
    "09:07",
    "09:15",
    "09:22",
    "09:30",
    "09:37",
    "09:45",
    "09:52",
    "10:00",
]
# The operator's 08:45-10:00 burst, in the rank order the pre-change grid produced for SAT
# (2026-05-16 -> start_front prefix 20260416, 0-indexed month). Midpoint 09:22:30; ties on
# midpoint distance break by ascending tee_time (09:15 before 09:30, 09:00 before 09:45,
# 08:45 before 10:00). Hand-derived, NOT recomputed through the ranker.
PRE_CHANGE_RANKED_0845_1000 = [
    ("09:22", "202604160922"),
    ("09:15", "202604160915"),
    ("09:30", "202604160930"),
    ("09:37", "202604160937"),
    ("09:07", "202604160907"),
    ("09:00", "202604160900"),
    ("09:45", "202604160945"),
    ("09:52", "202604160952"),
    ("08:52", "202604160852"),
    ("08:45", "202604160845"),
    ("10:00", "202604161000"),
]


def test_widened_grid_emits_identical_slots_for_0845_1000_window() -> None:
    """MULTIUSER_PLAN §6.5 / E3 non-regression pin: widening the GRID (not the window) must
    leave the operator's 08:45-10:00 burst byte-identical — same slots, same rank order, same
    raw `time`/`start_front` — so the STAGGER diagnostic (offset <-> rank pairing) is not
    confounded. Compared against the pre-change intersection captured literally above."""
    adapter = MangroveBayAdapter()
    full = adapter.synthesize_blind_slots(_request(), SAT, max_count=99)
    assert [(s.tee_time.strftime("%H:%M"), s.slot_id) for s in full] == PRE_CHANGE_RANKED_0845_1000
    # No time outside the pre-change grid leaks into the operator's window.
    assert {s.tee_time.strftime("%H:%M") for s in full} <= set(PRE_CHANGE_GRID)
    for s in full:
        hhmm = s.tee_time.strftime("%H:%M")
        assert s.raw["time"] == f"2026-05-16 {hhmm}"
        assert s.raw["start_front"] == int(s.slot_id)
    # The shipped prod burst is blind_post_max_count=3 (untouched by MU-3): identical top-3.
    top3 = adapter.synthesize_blind_slots(_request(), SAT, max_count=3)
    assert [s.slot_id for s in top3] == ["202604160922", "202604160915", "202604160930"]


def test_grid_widened_to_full_morning() -> None:
    """E3: the grid now spans the full morning on the proven 8/hr cadence
    (:00,:07,:15,:22,:30,:37,:45,:52) from 07:00 through 12:00, so an EARLIER window (a
    friend who golfs before 08:45) gets blind slots instead of falling to the search race.
    Every pre-change point is still present (superset); the list is strictly ascending and
    duplicate-free (rank_slots_for_request does its own sort; a sorted grid keeps the log
    readable)."""
    assert BLIND_POST_MORNING_GRID is not None
    assert set(PRE_CHANGE_GRID) <= set(BLIND_POST_MORNING_GRID)
    assert BLIND_POST_MORNING_GRID[0] == "07:00"
    assert BLIND_POST_MORNING_GRID[-1] == "12:00"
    assert sorted(BLIND_POST_MORNING_GRID) == BLIND_POST_MORNING_GRID
    assert len(set(BLIND_POST_MORNING_GRID)) == len(BLIND_POST_MORNING_GRID)
    cadence = ("00", "07", "15", "22", "30", "37", "45", "52")
    expected = [f"{h:02d}:{m}" for h in range(7, 12) for m in cadence] + ["12:00"]
    assert expected == BLIND_POST_MORNING_GRID


def test_earlier_window_now_gets_blind_slots() -> None:
    """The motivating case for E3: a 07:00-08:30 window (earlier than the operator's) used to
    synthesize [] and fall to the slower search race path; now it gets ranked grid slots."""
    adapter = MangroveBayAdapter()
    early = TimeWindow(earliest=time(7, 0), latest=time(8, 30))  # midpoint 07:45
    slots = adapter.synthesize_blind_slots(_request(early), SAT, max_count=3)
    # 07:45 (0 min), 07:52 (7 min), 07:37 (8 min) from the midpoint.
    assert [s.tee_time.strftime("%H:%M") for s in slots] == ["07:45", "07:52", "07:37"]


# --- MULTIUSER_PLAN MU-3 / E2: the blind allowlist hook (set_blind_allowlist) --------------


def test_allowlist_filters_before_truncation() -> None:
    """E2: with an allowlist set, synthesize returns (ranked candidates ∩ allowlist) truncated
    to max_count — the filter runs BEFORE truncation. If it ran after, an allowlist naming
    slots ranked 6th/8th/10th would leave a 3-burst empty; instead they ARE the burst, in
    rank order, so every allocated account still gets a full burst (§5.4)."""
    adapter = MangroveBayAdapter()
    full = adapter.synthesize_blind_slots(_request(), SAT, max_count=99)
    assert len(full) == 11
    chosen = [full[5], full[7], full[9]]  # 09:00, 09:52, 08:45 — none in the natural top-3
    adapter.set_blind_allowlist(frozenset(s.slot_id for s in chosen))
    burst = adapter.synthesize_blind_slots(_request(), SAT, max_count=3)
    assert [s.slot_id for s in burst] == [s.slot_id for s in chosen]
    assert burst == chosen  # byte-identical TeeTimeSlots, not just ids
    # Truncation still applies AFTER the filter: an allowlist wider than max_count is cut.
    adapter.set_blind_allowlist(frozenset(s.slot_id for s in full))
    assert adapter.synthesize_blind_slots(_request(), SAT, max_count=3) == full[:3]


def test_allowlist_none_is_default_and_resets() -> None:
    """Default = no allowlist = today's behaviour; set_blind_allowlist(None) restores it
    (the runner may re-plan; a stale allowlist must not leak into a later drop)."""
    adapter = MangroveBayAdapter()
    baseline = adapter.synthesize_blind_slots(_request(), SAT, max_count=3)
    assert adapter.blind_allowlist is None
    adapter.set_blind_allowlist(frozenset({baseline[2].slot_id}))
    assert adapter.synthesize_blind_slots(_request(), SAT, max_count=3) == [baseline[2]]
    adapter.set_blind_allowlist(None)
    assert adapter.blind_allowlist is None
    assert adapter.synthesize_blind_slots(_request(), SAT, max_count=3) == baseline


def test_allowlist_empty_yields_no_blind_slots() -> None:
    """An EMPTY allowlist means 'search-only this drop' (§5.3 over-cap / §5.4 exhausted):
    synthesize returns [] even though the grid has in-window candidates. Distinct from None."""
    adapter = MangroveBayAdapter()
    adapter.set_blind_allowlist(frozenset())
    assert adapter.blind_allowlist == frozenset()
    assert adapter.synthesize_blind_slots(_request(), SAT, max_count=3) == []


def test_allowlist_is_logged_for_fairness_audit(caplog: pytest.LogCaptureFixture) -> None:
    """The firing-grid log line must say an allowlist was applied and how many survived it,
    so a per-account burst in a multi-account run is auditable from logs alone."""
    adapter = MangroveBayAdapter()
    full = adapter.synthesize_blind_slots(_request(), SAT, max_count=99)
    adapter.set_blind_allowlist(frozenset({full[1].slot_id, full[4].slot_id}))
    with caplog.at_level("INFO"):
        adapter.synthesize_blind_slots(_request(), SAT, max_count=3)
    assert "allowlist" in caplog.text
    assert "2 allowed" in caplog.text
