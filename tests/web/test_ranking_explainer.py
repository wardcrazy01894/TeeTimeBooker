"""The "How the bot picks your tee time" explainer (operator request 2026-09-30): users should
see that a time slot aims for its MIDDLE (8:00-10:00 tries 9:00, then 9:07, then 8:52 ...).

The example is computed, not typed: it must match what the engine really does for that window
at Mangrove Bay, or the page would teach the wrong rule."""

from __future__ import annotations

from datetime import date, time
from uuid import uuid4

from teetime.core.models import BookingRequest, CourseId, Player, RequestId, TimeWindow
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.web.ranking_explainer import ranking_example


def _engine_order(earliest: time, latest: time) -> list[str]:
    request = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 10, 3),),
        time_windows=(TimeWindow(earliest=earliest, latest=latest),),
        players=tuple(
            Player(first_name=f"P{i}", last_name="L", email=f"p{i}@x.test") for i in range(4)
        ),
        course_preferences=(CourseId("foreup:mangrove_bay"),),
        max_price_per_player=None,
        dry_run=False,
    )
    slots = MangroveBayAdapter().synthesize_blind_slots(request, date(2026, 10, 3), max_count=99)
    return [s.tee_time.strftime("%H:%M") for s in slots]


def test_the_example_is_the_engines_real_order() -> None:
    ex = ranking_example()
    assert (ex.earliest_label, ex.latest_label, ex.middle_label) == (
        "8:00 AM",
        "10:00 AM",
        "9:00 AM",
    )
    ranked = [t.hhmm for t in ex.ranked]
    assert ranked[:3] == ["09:00", "09:07", "08:52"]  # the operator's own example
    assert ranked == _engine_order(time(8, 0), time(10, 0))[: len(ranked)]


def test_each_ranked_time_says_how_far_it_is_from_the_middle() -> None:
    ex = ranking_example()
    text = {t.hhmm: t.offset_text for t in ex.ranked}
    assert text["09:00"] == "the middle"
    assert text["09:07"] == "7 min after"
    assert text["08:52"] == "8 min before"
    # A tie (15 min either side) goes to the earlier time, as the engine sorts.
    order = [t.hhmm for t in ex.ranked]
    assert order.index("08:45") < order.index("09:15")


def test_the_timeline_places_every_tee_time_inside_the_window() -> None:
    ex = ranking_example()
    assert ex.tees[0].x_pct == 0.0 and ex.tees[-1].x_pct == 100.0  # 8:00 and 10:00 are tee times
    assert all(0.0 <= t.x_pct <= 100.0 for t in ex.tees)
    assert {t.rank for t in ex.tees if t.rank} == set(range(1, len(ex.ranked) + 1))
