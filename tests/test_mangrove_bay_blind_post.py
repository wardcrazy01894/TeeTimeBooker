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
    dawn = TimeWindow(earliest=time(6, 0), latest=time(7, 0))  # no grid time here
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
