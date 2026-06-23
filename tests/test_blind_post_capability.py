"""BLIND_POST_PLAN.md capability surface — now expressed via the explicit
`AdapterCapabilities` record (PR: capability flags over runtime_checkable protocols).

The orchestrator gates the blind path on `adapter.capabilities.blind_post`, NOT on
`isinstance(adapter, BlindPostCapable)`. The old two-part `isinstance(_, BlindPostCapable)
and supports_blind_post` gate was a footgun: `runtime_checkable` only checks member
PRESENCE, so EVERY ForeUP adapter satisfied the isinstance once the base shipped the
methods — the boolean was the real (but hidden) guard. The flag makes that explicit and
unfoolable. `BlindPostCapable` survives only as the typing cast-target the orchestrator
uses to CALL the methods once the flag says they exist.
"""

from __future__ import annotations

from datetime import date, time
from uuid import uuid4

import pytest

from teetime.core.models import (
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    TimeWindow,
)
from teetime.courses.foreup.base import ForeUpAdapter
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.courses.teeitup.sydney_marovitz import SydneyMarovitzAdapter
from teetime.dev.fake_adapter import FakeAdapter

CID = CourseId("fake:course")


def _request() -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 5, 13),),
        time_windows=(TimeWindow(earliest=time(8, 45), latest=time(10, 0)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        dry_run=False,
    )


def _gate_capable(adapter: object) -> bool:
    """The exact predicate the orchestrator gate uses."""
    return adapter.capabilities.blind_post  # type: ignore[attr-defined]


def _bare_foreup() -> ForeUpAdapter:
    """A ForeUP course with NO committed blind-POST template/grid (base behavior).
    Mangrove Bay overrides synthesize + flips the flag; the bare base does not."""
    return ForeUpAdapter(
        course_id=CourseId("foreup:bare"),
        course_pk=1,
        booking_class_id=1,
        schedule_id=1,
        timezone="America/New_York",
    )


# --- capability flag ------------------------------------------------------


def test_foreup_base_is_not_blind_capable() -> None:
    """A bare ForeUP course inherits the blind-POST methods (for parity) but its
    capabilities.blind_post defaults False, so the gate excludes it — the flag is the
    guard, not method presence."""
    adapter = _bare_foreup()
    assert adapter.capabilities.blind_post is False
    assert _gate_capable(adapter) is False


def test_mangrove_bay_is_blind_capable() -> None:
    """Mangrove Bay is the one ForeUP course that flips capabilities.blind_post True
    (and overrides synthesize), so the orchestrator gate admits it to the blind path."""
    adapter = MangroveBayAdapter()
    assert adapter.capabilities.blind_post is True
    assert _gate_capable(adapter) is True


def test_teeitup_is_not_blind_capable() -> None:
    """TeeItUp ships capabilities.blind_post=False (no template/grid, no CAPTCHA pool) —
    it can never reach the blind path even by accident."""
    adapter = SydneyMarovitzAdapter()
    assert adapter.capabilities.blind_post is False
    assert _gate_capable(adapter) is False


# --- FakeAdapter capability knob -----------------------------------------


def test_fake_adapter_capable_when_knob_true() -> None:
    adapter = FakeAdapter(course_id=CID, supports_blind_post=True)
    assert adapter.capabilities.blind_post is True
    assert _gate_capable(adapter) is True


def test_fake_adapter_default_is_not_capable() -> None:
    """Default FakeAdapter mirrors a bare ForeUP course: it has the methods (for parity)
    but capabilities.blind_post defaults False → the gate excludes it."""
    adapter = FakeAdapter(course_id=CID)
    assert adapter.capabilities.blind_post is False
    assert _gate_capable(adapter) is False


# --- captcha_pool_size ----------------------------------------------------


def test_fake_captcha_pool_size_scriptable() -> None:
    adapter = FakeAdapter(course_id=CID, supports_blind_post=True)
    # Default is high so the burst is bounded by slot count, not tokens.
    assert adapter.captcha_pool_size() >= 1
    adapter.set_captcha_pool_size(1)
    assert adapter.captcha_pool_size() == 1


# --- synthesize_blind_slots ----------------------------------------------


def test_foreup_base_synthesize_raises_not_implemented() -> None:
    """The base ForeUP synthesize is a stub: a bare ForeUP course has no committed
    template+grid, so calling it raises. (Mangrove Bay overrides it.)"""
    adapter = _bare_foreup()
    with pytest.raises(NotImplementedError):
        adapter.synthesize_blind_slots(_request(), date(2026, 5, 16), max_count=3)


def test_fake_synthesize_returns_scripted_slots_truncated() -> None:
    adapter = FakeAdapter(course_id=CID, supports_blind_post=True)
    req = _request()
    # Unscripted → falls back to a single default slot.
    assert len(adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=3)) == 1
    # Scripted → returns the list, truncated to max_count. We recycle the unscripted
    # default slot purely as a convenient TeeTimeSlot value to fill the scripted list.
    default_slots = adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=3)
    scripted = default_slots * 5  # 5 copies to exceed max_count
    adapter.set_blind_slots(scripted)
    out = adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=2)
    assert len(out) == 2


def test_fake_synthesize_call_count_tracked() -> None:
    adapter = FakeAdapter(course_id=CID, supports_blind_post=True)
    req = _request()
    assert adapter.synthesize_blind_slots_call_count == 0
    adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=1)
    assert adapter.synthesize_blind_slots_call_count == 1
