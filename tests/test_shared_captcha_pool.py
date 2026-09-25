"""SharedCaptchaPool + ForeUpAdapter injection (MULTIUSER_PLAN §5, engine change E1, MU-2).

The single-user non-regression gate is ``tests/test_captcha_pool.py`` passing UNMODIFIED (an
adapter built without a pool gets a private uncoordinated pool). This file pins the NEW
behaviour: coordinated fills, per-account leases granted round-robin, the shared reserve,
release, the per-course inline-solve bound and the T0-10 s fill deadline.

No real network: a fake provider stands in for 2captcha and respx mocks the booking POST.
"""

from __future__ import annotations

import asyncio
import json as stdlib_json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from teetime.core.adapter import CaptchaError
from teetime.core.clock import FakeClock
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.courses.foreup.base import FOREUP_BASE_URL, RESERVATION_PATH, ForeUpAdapter
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.courses.foreup.token_pool import LeaseKey, SharedCaptchaPool

ET = ZoneInfo("America/New_York")
CID = CourseId("foreup:mangrove_bay")
T0 = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)  # 06:00 ET
A = LeaseKey("row-a")
B = LeaseKey("row-b")
Z = LeaseKey("row-z")
_CLIENT_KWARGS = {"base_url": FOREUP_BASE_URL}
_CAPTCHA_400 = {"success": False, "msg": "Captcha verification failed", "openNewWindow": True}


class _SeqProvider:
    """Returns t0, t1, ... in CALL order; calls listed in ``fail_on`` raise TimeoutError."""

    def __init__(self, *, fail_on: frozenset[int] = frozenset()) -> None:
        self.calls = 0
        self._fail_on = fail_on

    async def __call__(self) -> str:
        n = self.calls
        self.calls += 1
        await asyncio.sleep(0)
        if n in self._fail_on:
            raise TimeoutError(f"solve {n} timed out")
        return f"t{n}"


class _GatedProvider:
    """Each call parks until ``gates[n]`` is set (calls beyond the list never finish)."""

    def __init__(self, n_gates: int) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.gates = [asyncio.Event() for _ in range(n_gates)]
        self._forever = asyncio.Event()

    async def __call__(self) -> str:
        n = self.calls
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await (self.gates[n] if n < len(self.gates) else self._forever).wait()
        finally:
            self.active -= 1
        return f"t{n}"


def _clock() -> FakeClock:
    return FakeClock(start=T0 - timedelta(seconds=120))


def _pool(provider: object, **kw: object) -> SharedCaptchaPool:
    """An ARMED pool (coordinated mode refuses to fill unarmed; see the arm tests)."""
    pool = SharedCaptchaPool(provider=provider, clock=_clock(), **kw)  # type: ignore[arg-type]
    pool.arm(t0=T0)
    return pool


async def _spin(n: int = 50) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _drain(pool: SharedCaptchaPool, key: LeaseKey) -> list[str]:
    out: list[str] = []
    while (tok := pool.pop(key)) is not None:
        out.append(tok)
    return out


# --- coordinated fill: leases, round-robin, reserve -------------------------------------


async def test_pool_round_robin_grants_rank0_first() -> None:
    """Arrivals are granted one-per-account per round in draft order, so a SHORT fill still
    gives every account its rank-0 token before anyone gets a surplus one."""
    provider = _SeqProvider(fail_on=frozenset({2, 3, 4, 5}))  # only 2 of 6 solves succeed
    pool = _pool(provider)
    pool.register(A, 3)
    pool.register(B, 3)
    await pool.prefetch(A, 3)
    assert pool.lease_size(A) == 1
    assert pool.lease_size(B) == 1


async def test_pool_round_robin_full_fill_interleaves_in_draft_order() -> None:
    provider = _SeqProvider()
    pool = _pool(provider)
    pool.register(B, 3)  # registration order IS draft order
    pool.register(A, 3)
    await pool.prefetch(A, 3)
    assert _drain(pool, B) == ["t0", "t2", "t4"]
    assert _drain(pool, A) == ["t1", "t3", "t5"]


async def test_pool_lease_isolated_from_other_key() -> None:
    """B cannot consume A's lease: once B's lease (and the reserve) is empty, pop -> None."""
    pool = _pool(_SeqProvider())
    pool.register(A, 1)
    pool.register(B, 1)
    await pool.prefetch(B, 1)
    assert pool.pop(B) == "t1"
    assert pool.pop(B) is None
    assert pool.pop(A) == "t0"


async def test_pool_reserve_gets_latest_arrivals() -> None:
    """After every lease is full, later arrivals go to the shared reserve; a key pops its own
    lease first, then the reserve (FIFO). The reserve is not counted in lease_size."""
    pool = _pool(_SeqProvider())
    pool.register(A, 1)
    pool.register(B, 1)
    pool.set_reserve(2)
    await pool.prefetch(A, 3)
    assert pool.lease_size(A) == 1
    assert pool.lease_size(B) == 1
    assert pool.pop(A) == "t0"
    assert pool.pop(A) == "t2"  # reserve, oldest first
    assert pool.pop(B) == "t1"
    assert pool.pop(B) == "t3"
    assert pool.pop(B) is None


async def test_pool_release_moves_lease_to_reserve() -> None:
    pool = _pool(_SeqProvider())
    pool.register(A, 2)
    pool.register(Z, 0)
    await pool.prefetch(A, 3)
    assert pool.lease_size(A) == 2
    pool.release(A)
    assert pool.lease_size(A) == 0
    assert _drain(pool, Z) == ["t0", "t1"]


async def test_pool_arrival_for_released_key_goes_to_reserve() -> None:
    provider = _GatedProvider(2)
    pool = _pool(provider)
    pool.register(A, 1)
    pool.register(B, 1)
    task = asyncio.create_task(pool.prefetch(A, 1))
    await _spin()
    pool.release(A)  # A finished (e.g. short-circuited ALREADY_BOOKED) before its token landed
    provider.gates[0].set()
    provider.gates[1].set()
    await task
    assert pool.lease_size(A) == 0
    assert pool.lease_size(B) == 1
    assert _drain(pool, Z) == [
        "t1"
    ]  # t0 skipped released A; an unregistered key reaches the reserve


async def test_pool_single_fill_shared_by_every_prefetch() -> None:
    """Coordinated: the first prefetch starts ONE fill of sum(k)+R solves; every other
    prefetch awaits it. The count argument is ignored (demand was registered)."""
    provider = _SeqProvider()
    pool = _pool(provider)
    pool.register(A, 2)
    pool.register(B, 1)
    pool.set_reserve(2)
    await asyncio.gather(pool.prefetch(A, 5), pool.prefetch(B, 5))
    assert provider.calls == 5


async def test_pool_k0_key_joins_fill_and_receives_nothing() -> None:
    """An over-cap account registered with k=0 must not solve tokens outside the fill."""
    provider = _SeqProvider()
    pool = _pool(provider)
    pool.register(A, 1)
    pool.register(Z, 0)
    await asyncio.gather(pool.prefetch(A, 3), pool.prefetch(Z, 3))
    assert provider.calls == 1
    assert pool.lease_size(Z) == 0


async def test_pool_fill_concurrency_bounded_by_max_concurrent_solves() -> None:
    provider = _GatedProvider(5)
    pool = _pool(provider, max_concurrent_solves=2)
    pool.register(A, 3)
    pool.set_reserve(2)
    task = asyncio.create_task(pool.prefetch(A, 3))
    await _spin()
    assert provider.active == 2
    for g in provider.gates:
        g.set()
        await _spin(10)
    await task
    assert provider.max_active == 2
    assert provider.calls == 5


async def test_pool_register_after_fill_started_raises() -> None:
    pool = _pool(_SeqProvider())
    pool.register(A, 1)
    await pool.prefetch(A, 1)
    with pytest.raises(RuntimeError):
        pool.register(B, 1)
    with pytest.raises(RuntimeError):
        pool.set_reserve(1)


async def test_pool_register_duplicate_key_raises() -> None:
    pool = _pool(_SeqProvider())
    pool.register(A, 1)
    with pytest.raises(ValueError, match="row-a"):
        pool.register(A, 1)


async def test_pool_coordinated_total_failure_does_not_raise() -> None:
    """NI10 applies to the UNcoordinated path only; a coordinated fill is best-effort."""
    pool = _pool(_SeqProvider(fail_on=frozenset({0})))
    pool.register(A, 1)
    await pool.prefetch(A, 1)  # must not raise
    assert pool.lease_size(A) == 0


# --- deadline ---------------------------------------------------------------------------


async def test_pool_fill_deadline_t0_minus_10() -> None:
    """prefetch returns at T0-10 s even though wave 1 never lands."""
    provider = _GatedProvider(0)  # every solve parks forever
    clock = _clock()
    pool = SharedCaptchaPool(provider=provider, clock=clock)
    pool.register(A, 1)
    pool.arm(t0=T0)
    await pool.prefetch(A, 1)
    deadline = T0 - timedelta(seconds=10)
    assert deadline <= clock.now_utc() < deadline + timedelta(seconds=1)
    assert pool.lease_size(A) == 0
    await pool.aclose()


async def test_pool_prefetch_returns_on_wave1_without_waiting_for_reserve() -> None:
    """Wave 2 (the reserve) keeps landing after prefetch returns; prefetch does not block on
    it. The report snapshots the reserve at return time."""
    provider = _GatedProvider(2)
    provider.gates[0].set()  # wave-1 token lands at once; the reserve token is still solving
    clock = _clock()
    pool = SharedCaptchaPool(provider=provider, clock=clock)
    pool.register(A, 1)
    pool.set_reserve(1)
    pool.arm(t0=T0)
    await pool.prefetch(A, 1)
    assert clock.now_utc() < T0 - timedelta(seconds=100)
    assert pool.lease_size(A) == 1
    report = pool.report()
    assert report is not None
    assert report.reserve == 0
    provider.gates[1].set()
    await _spin()
    assert _drain(pool, Z) == ["t1"]
    await pool.aclose()


async def test_pool_report() -> None:
    pool = _pool(_SeqProvider(fail_on=frozenset({1})))
    assert pool.report() is None
    pool.register(A, 1)
    pool.register(B, 1)
    pool.set_reserve(1)
    await pool.prefetch(A, 1)
    report = pool.report()
    assert report is not None
    assert report.demanded == 3
    assert report.solved == 2
    assert report.failures == 1
    assert report.granted == {A: 1, B: 1}
    assert report.reserve == 0
    assert report.started_at <= report.finished_at


# --- uncoordinated mode (today's prepare_book) ------------------------------------------


async def test_pool_uncoordinated_count1_reraises() -> None:
    """NI10: an unregistered key's count==1 total failure re-raises (TimeoutError ->
    CaptchaError); count>1 never raises."""
    pool = _pool(_SeqProvider(fail_on=frozenset({0, 1, 2, 3})))
    with pytest.raises(CaptchaError):
        await pool.prefetch(A, 1)
    await pool.prefetch(A, 3)  # must not raise
    assert pool.lease_size(A) == 0


async def test_pool_uncoordinated_solves_count_into_lease() -> None:
    provider = _SeqProvider()
    pool = _pool(provider)
    await pool.prefetch(A, 3)
    assert provider.calls == 3
    assert _drain(pool, A) == ["t0", "t1", "t2"]


async def test_pool_solve_inline_maps_timeout_to_captcha_error() -> None:
    pool = _pool(_SeqProvider(fail_on=frozenset({0})))
    with pytest.raises(CaptchaError):
        await pool.solve_inline()


# --- ForeUpAdapter injection (E1) -------------------------------------------------------


def _slot() -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 10, 10, 9, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw={"teesheet_id": 99001, "time": "09:00:00", "holes": 18},
    )


def _request() -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(date(2026, 10, 10),),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
    )


def _adapter(
    client: httpx.AsyncClient,
    provider: object,
    pool: SharedCaptchaPool | None,
    key: LeaseKey | None,
    **kw: object,
) -> ForeUpAdapter:
    a = ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
        http_client=client,
        captcha_provider=provider,  # type: ignore[arg-type]
        captcha_pool=pool,
        captcha_lease_key=key,
        **kw,  # type: ignore[arg-type]
    )
    a._logged_in = True
    return a


def _sent_token(route: respx.Route, i: int) -> str:
    return str(stdlib_json.loads(route.calls[i].request.content)["captchaid"])


async def test_adapter_without_pool_gets_private_pool() -> None:
    """Default: each adapter owns a private pool, so leases never bleed between adapters."""
    provider = _SeqProvider()
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        a1 = _adapter(client, provider, None, None)
        a2 = _adapter(client, provider, None, None)
        await a1.prepare_book(None, _request(), count=2)
        assert a1.captcha_pool_size() == 2
        assert a2.captcha_pool_size() == 0


async def test_adapter_pool_without_lease_key_raises() -> None:
    pool = _pool(_SeqProvider())
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        with pytest.raises(ValueError, match="captcha_lease_key"):
            _adapter(client, _SeqProvider(), pool, None)


@respx.mock
async def test_adapter_shared_pool_pops_own_lease_then_reserve() -> None:
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "C"})
    )
    provider = _SeqProvider()
    pool = _pool(provider)
    pool.register(A, 1)
    pool.register(B, 1)
    pool.set_reserve(1)
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter_a = _adapter(client, provider, pool, A)
        adapter_b = _adapter(client, provider, pool, B)
        await asyncio.gather(
            adapter_a.prepare_book(None, _request(), count=3),
            adapter_b.prepare_book(None, _request(), count=3),
        )
        assert provider.calls == 3
        assert adapter_a.captcha_pool_size() == 1  # lease only, reserve excluded
        await adapter_b.book(_slot(), _request())
        await adapter_b.book(_slot(), _request())
        await adapter_a.book(_slot(), _request())
    assert _sent_token(route, 0) == "t1"  # B's lease
    assert _sent_token(route, 1) == "t2"  # shared reserve
    assert _sent_token(route, 2) == "t0"  # A's lease, untouched by B
    assert provider.calls == 3, "no inline solve while lease/reserve tokens existed"


@respx.mock
async def test_adapter_shared_pool_reserve_token_gets_mf1_resolve() -> None:
    """MF1: a stale RESERVE token counts as pooled -> exactly one inline re-solve + re-POST."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        side_effect=[
            httpx.Response(400, json=_CAPTCHA_400, headers={"content-type": "application/json"}),
            httpx.Response(200, json={"id": "C-OK"}),
        ]
    )
    provider = _SeqProvider()
    pool = _pool(provider)
    pool.register(A, 0)
    pool.set_reserve(1)
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client, provider, pool, A)
        await adapter.prepare_book(None, _request(), count=3)
        await _spin()  # prefetch does not block on wave 2; let the reserve token land
        assert adapter.captcha_pool_size() == 0  # reserve excluded from the lease size
        result = await adapter.book(_slot(), _request())
    assert result.outcome is BookingOutcome.BOOKED
    assert _sent_token(route, 0) == "t0"
    assert _sent_token(route, 1) == "t1"
    assert provider.calls == 2


@respx.mock
async def test_pool_inline_solve_bound_is_per_course() -> None:
    """With a shared pool the inline-solve bound is the POOL's (per course, across every
    account), not each adapter's own max_concurrent_captcha_solves."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "C"})
    )
    provider = _GatedProvider(0)
    pool = _pool(provider, max_concurrent_inline_solves=2)
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapters = [
            _adapter(client, provider, pool, key, max_concurrent_captcha_solves=6) for key in (A, B)
        ]
        tasks = [
            asyncio.create_task(ad.book(_slot(), _request())) for ad in adapters for _ in range(3)
        ]
        await _spin(200)
        assert provider.max_active == 2, f"{provider.max_active} inline solves ran at once"
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_mangrove_bay_accepts_injected_pool() -> None:
    provider = _SeqProvider()
    pool = _pool(provider)
    pool.register(A, 2)
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        mb = MangroveBayAdapter(
            http_client=client,
            captcha_provider=provider,
            captcha_pool=pool,
            captcha_lease_key=A,
        )
        await mb.prepare_book(None, _request(), count=3)
        assert mb.captcha_pool_size() == 2


# --- review round 1 (PR #221) -----------------------------------------------------------


class _Boom(BaseException):
    """A BaseException that is NOT an Exception (the fill's per-solve catch misses it)."""


def _deadline() -> datetime:
    return T0 - timedelta(seconds=10)


async def test_pool_aclose_while_prefetch_waits_releases_it() -> None:
    """S1a: aclose() cancelling the fill must release a prefetch already waiting on it,
    well before the deadline (the orchestrator's prefetch has no timeout of its own)."""
    provider = _GatedProvider(0)  # every solve parks forever
    clock = _clock()
    pool = SharedCaptchaPool(provider=provider, clock=clock)  # type: ignore[arg-type]
    pool.register(A, 1)
    pool.arm(t0=T0)
    waiter = asyncio.create_task(pool.prefetch(A, 1))
    await _spin(5)
    await pool.aclose()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert clock.now_utc() < _deadline()


async def test_pool_prefetch_after_aclose_returns() -> None:
    """S1b: a prefetch arriving after aclose() returns at once (no fill will ever finish)."""
    provider = _GatedProvider(0)
    clock = _clock()
    pool = SharedCaptchaPool(provider=provider, clock=clock)  # type: ignore[arg-type]
    pool.register(A, 1)
    pool.register(B, 1)
    pool.arm(t0=T0)
    first = asyncio.create_task(pool.prefetch(A, 1))
    await _spin(5)
    await pool.aclose()
    await asyncio.wait_for(first, timeout=1.0)
    await asyncio.wait_for(pool.prefetch(B, 1), timeout=1.0)
    assert clock.now_utc() < _deadline()


async def test_pool_non_exception_baseexception_from_provider_does_not_hang(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S1c: a provider raising a non-Exception BaseException kills the fill task; prefetch
    must still return promptly and the task's exception must be retrieved + logged."""

    async def boom() -> str:
        await asyncio.sleep(0)
        raise _Boom("provider exploded")

    clock = _clock()
    pool = SharedCaptchaPool(provider=boom, clock=clock)
    pool.register(A, 1)
    pool.arm(t0=T0)
    await asyncio.wait_for(pool.prefetch(A, 1), timeout=1.0)
    await _spin(5)
    assert clock.now_utc() < _deadline()
    assert "fill task died" in caplog.text


async def test_pool_coordinated_prefetch_requires_arm() -> None:
    """S2: an unarmed coordinated fill has no T0-10 s deadline, so a short wave 1 could hold
    prefetch until wave 2 lands (after T0) -- refuse loudly instead of starting it."""
    provider = _SeqProvider()
    pool = SharedCaptchaPool(provider=provider, clock=_clock())
    pool.register(A, 1)
    with pytest.raises(RuntimeError, match="arm"):
        await pool.prefetch(A, 1)
    assert provider.calls == 0


async def test_pool_unregistered_key_on_coordinated_pool_raises() -> None:
    """S3: once demand is registered, an UNregistered key must not silently solve ``count``
    tokens outside the C bound (the over-cap footgun of §5.3)."""
    provider = _SeqProvider()
    pool = _pool(provider)
    pool.register(A, 1)
    with pytest.raises(RuntimeError, match="row-z"):
        await pool.prefetch(Z, 3)
    assert provider.calls == 0


async def test_adapter_rejects_pool_of_another_course() -> None:
    """Nit: the pool's provider is bound to one course's page URL + site key; an adapter of
    another course must not share it."""
    pool = SharedCaptchaPool(
        provider=_SeqProvider(), clock=_clock(), course_id=CourseId("foreup:other")
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        with pytest.raises(ValueError, match="foreup:other"):
            _adapter(client, _SeqProvider(), pool, A)
