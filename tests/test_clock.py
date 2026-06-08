"""M1.T1 tests: Clock Protocol, RealClock, FakeClock, busy_wait_until.

TDD: these tests define the contract documented in PLAN.md §6 and
core/clock.py. Implementation lives in src/teetime/core/clock.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from teetime.core.clock import (
    Clock,
    FakeClock,
    RealClock,
    busy_wait_until,
    measure_ntp_offset,
)

# --- Structural Protocol contract ----------------------------------------


def test_real_clock_satisfies_protocol() -> None:
    assert isinstance(RealClock(), Clock)


def test_fake_clock_satisfies_protocol() -> None:
    assert isinstance(FakeClock(start=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)), Clock)


# --- RealClock -----------------------------------------------------------


def test_real_clock_now_is_tz_aware_utc() -> None:
    now = RealClock().now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_real_clock_now_is_close_to_system_time() -> None:
    before = datetime.now(tz=UTC)
    rc_now = RealClock().now_utc()
    after = datetime.now(tz=UTC)
    assert before - timedelta(seconds=1) <= rc_now <= after + timedelta(seconds=1)


# --- NTP offset correction (resilience) ----------------------------------


def test_real_clock_applies_offset() -> None:
    """RealClock(offset=...) shifts now_utc() by the measured NTP offset."""
    offset = timedelta(seconds=5)
    base = datetime.now(tz=UTC)
    got = RealClock(offset=offset).now_utc()
    assert base + offset - timedelta(seconds=1) <= got <= base + offset + timedelta(seconds=1)


def test_real_clock_default_offset_is_zero() -> None:
    """Default RealClock (no offset) tracks system time, unchanged behaviour."""
    before = datetime.now(tz=UTC)
    got = RealClock().now_utc()
    after = datetime.now(tz=UTC)
    assert before - timedelta(seconds=1) <= got <= after + timedelta(seconds=1)


def test_measure_ntp_offset_returns_timedelta_from_response() -> None:
    """measure_ntp_offset returns the NTP server's reported offset as a timedelta."""

    class _Resp:
        offset = 3.0

    class _FakeClient:
        def request(self, server: str, version: int = 3, timeout: float = 2.0) -> _Resp:
            return _Resp()

    assert measure_ntp_offset(_client_factory=_FakeClient) == timedelta(seconds=3.0)


def test_measure_ntp_offset_returns_zero_on_failure() -> None:
    """Any NTP failure (blocked UDP, timeout) degrades to a zero offset, never raises."""

    class _BoomClient:
        def request(self, *args: object, **kwargs: object) -> object:
            raise OSError("ntp unreachable")

    assert measure_ntp_offset(_client_factory=_BoomClient) == timedelta(0)


async def test_real_clock_sleep_actually_sleeps() -> None:
    clock = RealClock()
    start = clock.now_utc()
    await clock.sleep(0.05)
    elapsed = (clock.now_utc() - start).total_seconds()
    assert elapsed >= 0.04, f"expected >=40ms; got {elapsed * 1000:.1f}ms"


# --- FakeClock -----------------------------------------------------------


def test_fake_clock_returns_start_time() -> None:
    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    fc = FakeClock(start=t0)
    assert fc.now_utc() == t0


async def test_fake_clock_sleep_advances_time_without_real_io() -> None:
    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    fc = FakeClock(start=t0)
    real_before = datetime.now(tz=UTC)
    await fc.sleep(60.0)  # 60 simulated seconds
    real_after = datetime.now(tz=UTC)
    assert fc.now_utc() == t0 + timedelta(seconds=60)
    # Real wall-clock should NOT have advanced 60s; bound it to <1s.
    assert (real_after - real_before).total_seconds() < 1.0


async def test_fake_clock_sleep_yields_event_loop() -> None:
    """A hot Python loop must not starve other coroutines under FakeClock —
    `sleep(0)` is the canonical yield point and FakeClock.sleep MUST call it."""
    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    fc = FakeClock(start=t0)
    other_ran = False

    async def other_task() -> None:
        nonlocal other_ran
        other_ran = True

    task = asyncio.create_task(other_task())
    await fc.sleep(0.001)
    await task
    assert other_ran


# --- busy_wait_until -----------------------------------------------------


async def test_busy_wait_returns_immediately_if_target_in_past() -> None:
    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    fc = FakeClock(start=t0)
    target = t0 - timedelta(seconds=5)
    await busy_wait_until(target, fc)
    # FakeClock did not advance because no sleep happened.
    assert fc.now_utc() == t0


async def test_busy_wait_lands_within_accuracy_of_target() -> None:
    """The race-window contract: from 1.5 s before T0, lands within 50 ms."""
    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    fc = FakeClock(start=t0 - timedelta(seconds=1.5))
    await busy_wait_until(t0, fc)
    delta = abs((fc.now_utc() - t0).total_seconds())
    assert delta <= 0.05, f"landed {delta * 1000:.1f}ms off target (>50ms)"


async def test_busy_wait_uses_coarse_step_when_far_from_target() -> None:
    """Far from target the loop sleeps in chunks of ~coarse_step_s, not 1 ms each.
    Verified by counting `sleep` calls — should be O(seconds/coarse_step), not
    O(seconds/fine_step) which would be 1000x more."""
    t0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
    fc = FakeClock(start=t0 - timedelta(seconds=10))
    await busy_wait_until(t0, fc, coarse_threshold_s=2.0, coarse_step_s=0.5)
    # 10s away → ~16 coarse sleeps to get within 2s, then ~2000 fine sleeps.
    # Without coarse step it'd be ~10_000 fine sleeps. Cap is generous on
    # purpose — we're just verifying the coarse path runs at all.
    assert fc.sleep_count <= 5000


# --- DST edge: PLAN §19 risk #7 ------------------------------------------


def test_dst_spring_forward_t0_resolves_unambiguously() -> None:
    """March 8 2026 is the spring-forward Sunday. 06:00 ET still exists
    (the skipped hour is 02:00 to 03:00). zoneinfo must resolve it cleanly."""
    et = ZoneInfo("America/New_York")
    t0_local = datetime(2026, 3, 8, 6, 0, 0, tzinfo=et)
    t0_utc = t0_local.astimezone(UTC)
    # 06:00 EDT == 10:00 UTC (post-spring-forward we are on EDT, UTC-4).
    assert t0_utc == datetime(2026, 3, 8, 10, 0, 0, tzinfo=UTC)


def test_dst_fall_back_t0_resolves_to_second_six_am() -> None:
    """Nov 1 2026 is the fall-back Sunday. 06:00 ET is unambiguous (only the
    01:00 hour repeats). Default fold=0 semantics are fine."""
    et = ZoneInfo("America/New_York")
    t0_local = datetime(2026, 11, 1, 6, 0, 0, tzinfo=et)
    t0_utc = t0_local.astimezone(UTC)
    # 06:00 EST == 11:00 UTC (post-fall-back we are on EST, UTC-5).
    assert t0_utc == datetime(2026, 11, 1, 11, 0, 0, tzinfo=UTC)
