"""The worked example behind "How the bot picks your tee time" (operator request 2026-09-30).

Users kept asking why the bot took 9:07 rather than 8:52. The rule (``core.slot_utils.
rank_slots_for_request``): options in rank order; inside one option, the tee time closest to the
MIDDLE of its window, a tie going to the earlier time. This builds a concrete example for one
window from Mangrove Bay's real tee-time grid, so the page shows actual times in the actual
order. ``tests/web/test_ranking_explainer.py`` pins it to the engine's own ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from ..courses.foreup.mangrove_bay import BLIND_POST_MORNING_GRID

EXAMPLE_WINDOW = (time(8, 0), time(10, 0))
RANKED_SHOWN = 5  # how many ranked times the panel lists (then "and so on")


@dataclass(frozen=True, slots=True)
class ExampleTee:
    hhmm: str  # "09:07"
    label: str  # "9:07 AM"
    x_pct: float  # position on the timeline, 0 = window start, 100 = window end
    rank: int | None  # 1 = first choice; None = not among the RANKED_SHOWN listed
    offset_text: str  # "the middle" | "7 min after" | "8 min before"


@dataclass(frozen=True, slots=True)
class RankingExample:
    earliest_label: str
    latest_label: str
    middle_label: str
    tees: tuple[ExampleTee, ...]  # every tee time in the window, in time order
    ranked: tuple[ExampleTee, ...]  # the first RANKED_SHOWN, best first


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _label(minutes: float) -> str:
    h, m = divmod(round(minutes), 60)
    return f"{time(h, m):%I:%M %p}".lstrip("0")


def _offset_text(delta: float) -> str:
    if delta == 0:
        return "the middle"
    return f"{abs(delta):g} min {'after' if delta > 0 else 'before'}"


def ranking_example(
    earliest: time = EXAMPLE_WINDOW[0],
    latest: time = EXAMPLE_WINDOW[1],
    grid: tuple[str, ...] | list[str] | None = None,
) -> RankingExample:
    lo, hi = _minutes(earliest), _minutes(latest)
    middle = (lo + hi) / 2
    times = sorted(
        m
        for m in (_minutes(time.fromisoformat(g)) for g in (grid or BLIND_POST_MORNING_GRID or ()))
        if lo <= m <= hi
    )
    # The engine's key inside one window: distance from the middle, then the earlier time.
    order = sorted(times, key=lambda m: (abs(m - middle), m))
    rank_of = {m: i + 1 for i, m in enumerate(order[:RANKED_SHOWN])}
    span = hi - lo or 1

    def tee(m: int) -> ExampleTee:
        return ExampleTee(
            hhmm=f"{m // 60:02d}:{m % 60:02d}",
            label=_label(m),
            x_pct=round((m - lo) / span * 100, 2),
            rank=rank_of.get(m),
            offset_text=_offset_text(m - middle),
        )

    tees = tuple(tee(m) for m in times)
    by_min = {t.hhmm: t for t in tees}
    ranked = tuple(by_min[f"{m // 60:02d}:{m % 60:02d}"] for m in order[:RANKED_SHOWN])
    return RankingExample(
        earliest_label=_label(lo),
        latest_label=_label(hi),
        middle_label=_label(middle),
        tees=tees,
        ranked=ranked,
    )


def cutoff_text(days_before: int, time_of_day: time) -> str:
    """The booking cutoff in words, from the configured ``BookingCutoffConfig`` ("4 PM the day
    before"), so the panel never states a cutoff the watcher does not use."""
    clock = f"{time_of_day:%I:%M %p}".lstrip("0").replace(":00 ", " ")
    if days_before == 0:
        return f"{clock} that day"
    if days_before == 1:
        return f"{clock} the day before"
    return f"{clock}, {days_before} days before"
