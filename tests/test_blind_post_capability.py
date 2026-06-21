"""PR1 of BLIND_POST_PLAN.md: the BlindPostCapable capability Protocol + the
FakeAdapter knob. No orchestrator wiring yet — these tests pin the structural
contract and the capability VALUE semantics the orchestrator gate will rely on.

Key subtlety (reviewer nit 1): `BlindPostCapable` is `runtime_checkable`, which
only checks member PRESENCE. So every ForeUP adapter (incl. Mangrove Bay) satisfies
`isinstance(_, BlindPostCapable)` once the base ships the methods — the
`supports_blind_post` BOOLEAN is the real guard. The orchestrator gate is
`isinstance(a, BlindPostCapable) and a.supports_blind_post`; both halves are tested
here. TeeItUp lacks the members, so it is the one adapter excluded by isinstance.
"""

from __future__ import annotations

from datetime import date, time
from uuid import uuid4

import pytest

from teetime.core.adapter import BlindPostCapable
from teetime.core.models import (
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    TimeWindow,
)
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
    """The exact predicate the orchestrator gate will use (PR3)."""
    return isinstance(adapter, BlindPostCapable) and adapter.supports_blind_post


# --- structural Protocol membership --------------------------------------


def test_foreup_base_structurally_satisfies_protocol_but_not_capable() -> None:
    """Mangrove Bay inherits the ForeUP base, which ships supports_blind_post +
    captcha_pool_size + synthesize_blind_slots — so it satisfies the runtime_checkable
    Protocol. But supports_blind_post is False until PR2 flips it, so it is NOT yet
    blind-capable by the gate predicate."""
    adapter = MangroveBayAdapter()
    assert isinstance(adapter, BlindPostCapable)
    assert adapter.supports_blind_post is False
    assert _gate_capable(adapter) is False


def test_teeitup_lacks_blind_members_so_not_protocol_member() -> None:
    """TeeItUp ships none of the blind members, so isinstance excludes it outright —
    a non-capable platform can never reach the blind path even by accident."""
    adapter = SydneyMarovitzAdapter()
    assert not isinstance(adapter, BlindPostCapable)
    assert _gate_capable(adapter) is False


# --- FakeAdapter capability knob -----------------------------------------


def test_fake_adapter_capable_when_knob_true() -> None:
    adapter = FakeAdapter(course_id=CID, supports_blind_post=True)
    assert isinstance(adapter, BlindPostCapable)
    assert adapter.supports_blind_post is True
    assert _gate_capable(adapter) is True


def test_fake_adapter_default_is_not_capable() -> None:
    """Default FakeAdapter mirrors a bare ForeUP course: structurally a member (it has
    the methods for parity) but supports_blind_post defaults False → gate excludes it."""
    adapter = FakeAdapter(course_id=CID)
    assert adapter.supports_blind_post is False
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
    """The base ForeUP synthesize is a stub until PR2 (Mangrove Bay overrides it).
    A bare/base ForeUP course has no committed template+grid, so calling it raises."""
    adapter = MangroveBayAdapter()
    with pytest.raises(NotImplementedError):
        adapter.synthesize_blind_slots(_request(), date(2026, 5, 16), max_count=3)


def test_fake_synthesize_returns_scripted_slots_truncated() -> None:
    adapter = FakeAdapter(course_id=CID, supports_blind_post=True)
    req = _request()
    # Unscripted → falls back to a single default slot.
    assert len(adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=3)) == 1
    # Scripted → returns the list, truncated to max_count.
    slots = adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=3)
    scripted = slots * 5  # 5 copies to exceed max_count
    adapter.set_blind_slots(scripted)
    out = adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=2)
    assert len(out) == 2


def test_fake_synthesize_call_count_tracked() -> None:
    adapter = FakeAdapter(course_id=CID, supports_blind_post=True)
    req = _request()
    assert adapter.synthesize_blind_slots_call_count == 0
    adapter.synthesize_blind_slots(req, date(2026, 5, 13), max_count=1)
    assert adapter.synthesize_blind_slots_call_count == 1
