"""MULTIUSER_PLAN MU-9b: ``plan_release_event`` (``teetime tenant-plan``) and the async pool
factory the CLI uses to run the site-key pre-flight AFTER the claim (§4.2 order).

``tenant-plan`` prints an event's rows and the blind-slot allocation WITHOUT any ForeUP call,
claim, decrypt or CAPTCHA solve: one read, then the same pure allocation the runner does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from teetime.core.models import BookingRequest, BookingResult, CourseId, TeeTimeSlot
from teetime.courses.foreup.token_pool import SharedCaptchaPool
from teetime.dev.blind_fake_adapter import BlindFakeAdapter
from teetime.tenant.models import RankedWindow, RowStatus
from teetime.tenant.runner import plan_release_event, run_release_event, tenant_scheduler

from .runner_builders import (
    EVENT,
    GRID,
    KEYRING,
    MB,
    POLICIES,
    T0,
    TARGET,
    TZ,
    WINDOW,
    CountingProvider,
    NullUserNotifier,
    PooledFactory,
    ScriptedFactory,
    SpyStore,
    new_store,
    race_clock,
    seed_account,
)


class NoNetworkBlindAdapter(BlindFakeAdapter):
    """Blind-capable fake whose every ForeUP-touching member FAILS the test if called."""

    def __init__(self) -> None:
        super().__init__(course_id=MB)
        self.set_blind_slots(list(GRID))
        self.network_calls: list[str] = []

    def _forbid(self, name: str) -> None:
        self.network_calls.append(name)
        raise AssertionError(f"tenant-plan made a ForeUP call: {name}")

    async def authenticate(self, creds: Any) -> None:
        self._forbid("authenticate")

    async def search(self, request: BookingRequest, **kwargs: Any) -> list[TeeTimeSlot]:
        self._forbid("search")
        return []

    async def prepare_book(self, slot: Any, request: BookingRequest, *, count: int = 1) -> None:
        self._forbid("prepare_book")

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        self._forbid("book")
        raise AssertionError

    async def list_reservations(self) -> list[Any]:
        self._forbid("list_reservations")
        return []

    async def cancel_reservation(self, confirmation_code: str) -> None:
        self._forbid("cancel_reservation")


async def test_tenant_plan_makes_no_foreup_call() -> None:
    inner = new_store()
    a = await seed_account(inner, n=1)
    b = await seed_account(inner, n=2)
    clock = race_clock(before_t0_s=9 * 60)
    spy = SpyStore(inner, clock)
    adapters = {a.account.id: NoNetworkBlindAdapter(), b.account.id: NoNetworkBlindAdapter()}
    factory = ScriptedFactory(adapters=dict(adapters))  # type: ignore[arg-type]

    plan = await plan_release_event(
        event=EVENT,
        policies=POLICIES,
        store=spy,
        clock=clock,
        scheduler=tenant_scheduler(),
        adapter_factory=factory,
    )

    assert spy.names() == ["load_event_rows"]  # no claim, no write
    assert all(ad.network_calls == [] for ad in adapters.values())
    assert all(call.pool is None and call.dry_run for call in factory.calls)
    assert plan.targets == {MB: TARGET}
    by_row = {p.row_id: p for p in plan.rows}
    assert set(by_row) == {a.row.id, b.row.id}
    first, second = (by_row[r] for r in plan.orders[MB])
    assert first.allowlist_times[0] == "08:15"  # rank-0 goes to the week's first pick
    assert len(first.allowlist_times) == len(second.allowlist_times) == 3
    assert not set(first.allowlist_times) & set(second.allowlist_times)  # disjoint (§5.4)
    assert (first.options, first.party_size, first.search_only) == (
        (RankedWindow(1, *WINDOW),),
        2,
        False,
    )
    lines = plan.render()
    assert lines[0].startswith("tenant-plan mb0600et: release 06:00 America/New_York")
    assert any(f"row {a.row.id}" in line for line in lines)
    assert all(a.account.username not in line for line in lines)  # no course login names


async def test_tenant_plan_with_no_rows() -> None:
    clock = race_clock(before_t0_s=9 * 60)
    plan = await plan_release_event(
        event=EVENT,
        policies=POLICIES,
        store=new_store(),
        clock=clock,
        scheduler=tenant_scheduler(),
        adapter_factory=ScriptedFactory(),
    )
    assert plan.rows == ()
    assert "0 pending row(s)" in "\n".join(plan.render())


async def test_runner_accepts_an_async_pool_factory_called_after_the_claim() -> None:
    """The CLI's pool factory is async: it runs the site-key pre-flight (a ForeUP GET) once
    per course, AFTER READ #1 + the claim (§4.2) and well before the race window."""
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=9 * 60)
    spy = SpyStore(inner, clock)
    provider = CountingProvider(clock)
    calls: list[tuple[CourseId, datetime, list[str]]] = []

    async def pool_factory(course: CourseId) -> SharedCaptchaPool:
        calls.append((course, clock.now_utc(), spy.names()))
        await clock.sleep(1.0)  # the pre-flight GET
        return SharedCaptchaPool(provider=provider, clock=clock, course_id=course)

    report = await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=spy,
        clock=clock,
        scheduler=tenant_scheduler(),
        keyring=KEYRING,
        adapter_factory=PooledFactory(clock),
        notifier=NullUserNotifier(),
        dry_run=False,
        wait=True,
        pool_factory=pool_factory,
    )

    assert report.systemic_error is None
    ((course, at, names_before),) = calls
    assert course == MB
    assert names_before == ["load_event_rows", "claim_rows"]
    assert at < T0 - timedelta(seconds=121)
    assert provider.calls == 5


# --- MU-R2 group floor (§16.3) -------------------------------------------------------------


def _book_in_memory(store: Any, seeded: Any, tee: datetime) -> None:
    """Test-only: mark a seeded row BOOKED directly in the reference store (the booking path
    itself is covered elsewhere); only its status and tee time matter to the floor."""
    row = store._rows[seeded.row.id]
    store._rows[row.id] = replace(row, status=RowStatus.BOOKED, booked_tee_time=tee)


async def test_plan_applies_group_floor_from_a_sibling_booking() -> None:
    """The operator's example: A 9-10 (rank 1) > B 9-10 (rank 2) > A 8-9 (rank 3). With B booked
    at rank 2, A is attempted ONLY for its rank-1 window; with a rank-1 booking, not at all."""
    inner = new_store()
    group = uuid4()
    a = await seed_account(
        inner,
        n=1,
        group_id=group,
        options=(RankedWindow(1, time(9), time(10)), RankedWindow(3, time(8), time(9))),
    )
    b = await seed_account(
        inner, n=2, group_id=group, options=(RankedWindow(2, time(9), time(10)),)
    )
    _book_in_memory(inner, b, datetime.combine(TARGET, time(9, 15), tzinfo=ZoneInfo(TZ)))
    clock = race_clock(before_t0_s=9 * 60)
    factory = ScriptedFactory(adapters={a.account.id: NoNetworkBlindAdapter()})  # type: ignore[dict-item]

    plan = await plan_release_event(
        event=EVENT,
        policies=POLICIES,
        store=inner,
        clock=clock,
        scheduler=tenant_scheduler(),
        adapter_factory=factory,
    )
    (planned,) = plan.rows
    assert planned.row_id == a.row.id
    assert planned.options == (RankedWindow(1, time(9), time(10)),)


async def test_plan_skips_a_row_no_option_of_which_beats_the_sibling() -> None:
    inner = new_store()
    group = uuid4()
    a = await seed_account(
        inner, n=1, group_id=group, options=(RankedWindow(2, time(9), time(10)),)
    )
    b = await seed_account(inner, n=2, group_id=group, options=(RankedWindow(1, time(8), time(9)),))
    _book_in_memory(inner, b, datetime.combine(TARGET, time(8, 30), tzinfo=ZoneInfo(TZ)))
    plan = await plan_release_event(
        event=EVENT,
        policies=POLICIES,
        store=inner,
        clock=race_clock(before_t0_s=9 * 60),
        scheduler=tenant_scheduler(),
        adapter_factory=ScriptedFactory(adapters={a.account.id: NoNetworkBlindAdapter()}),  # type: ignore[dict-item]
    )
    assert plan.rows == ()
