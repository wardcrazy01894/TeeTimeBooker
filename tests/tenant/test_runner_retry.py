"""The booker's transient-store-error retries (retry audit, 2026-09-27).

READ #1 and WRITE #1 run minutes before T0 (the 05:50 cron reaches them at ~05:51, the race
window opens at T0 - lead - 1 s), so ONE Cosmos blip there used to end the whole drop for every
user as a systemic failure. WRITE #2's 60 s retry never fired against Cosmos at all:
``record_outcomes`` wraps every failure in an ``ExceptionGroup`` and the writer treated any group
as a refusal. Every test runs on a ``VirtualClock``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from azure.core.exceptions import ServiceResponseError
from azure.cosmos.exceptions import CosmosHttpResponseError

from teetime.core.models import BookingOutcome
from teetime.dev.virtual_clock import VirtualClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import TransitionRefusedError
from teetime.tenant.runner import ExitStatus, RunReport, exit_code_for, run_release_event

from .runner_builders import (
    EVENT,
    KEYRING,
    POLICIES,
    T0,
    NullUserNotifier,
    ScriptedFactory,
    new_store,
    race_clock,
    scheduler,
    seed_account,
)


class FlakyStore:
    """Delegates to ``inner``; the named method raises ``make_error()`` on its first ``fails``
    calls (a Cosmos blip the SDK gave up on), then answers normally. Records every call with its
    instant. A collaborator stand-in, never the runner."""

    def __init__(
        self,
        inner: InMemoryTenantStore,
        clock: VirtualClock,
        method: str,
        *,
        fails: int,
        make_error: Callable[[], BaseException],
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._method = method
        self._left = fails
        self._make_error = make_error
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        target = getattr(self._inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, self._clock.now_utc()))
            if name == self._method and self._left > 0:
                self._left -= 1
                raise self._make_error()
            return await target(*args, **kwargs)

        return call

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


def _blip() -> BaseException:
    return CosmosHttpResponseError(status_code=503, message="service unavailable")


async def _run(store: Any, clock: VirtualClock, *, lead_s: int = 120) -> RunReport:
    return await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=store,
        clock=clock,
        scheduler=scheduler(lead_s=lead_s),
        keyring=KEYRING,
        adapter_factory=ScriptedFactory(),
        notifier=NullUserNotifier(),
        dry_run=False,
        wait=True,
    )


async def test_read_1_survives_a_transient_store_error() -> None:
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=9 * 60)
    store = FlakyStore(inner, clock, "load_event_rows", fails=1, make_error=_blip)

    report = await _run(store, clock)

    assert report.systemic_error is None
    assert report.rows_claimed == 1
    assert store.count("load_event_rows") == 2
    (out,) = report.outcomes
    assert out.outcome is BookingOutcome.BOOKED


async def test_claim_survives_a_lost_response() -> None:
    """A claim whose response was lost may have landed; the replay re-claims as the SAME owner,
    which the store accepts (``claim_rows`` is idempotent per owner)."""
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=9 * 60)
    store = FlakyStore(
        inner, clock, "claim_rows", fails=1, make_error=lambda: ServiceResponseError("lost")
    )

    report = await _run(store, clock)

    assert report.systemic_error is None
    assert report.rows_claimed == 1
    assert store.count("claim_rows") == 2


async def test_read_1_still_fails_systemic_when_the_blips_persist() -> None:
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=9 * 60)
    store = FlakyStore(inner, clock, "load_event_rows", fails=99, make_error=_blip)

    report = await _run(store, clock)

    assert report.systemic_error == "load_event_rows: CosmosHttpResponseError"
    assert exit_code_for(report) is ExitStatus.SYSTEMIC_FAILURE
    assert store.count("load_event_rows") == 3  # bounded


async def test_non_transient_read_error_is_not_retried() -> None:
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=9 * 60)
    store = FlakyStore(
        inner,
        clock,
        "load_event_rows",
        fails=1,
        make_error=lambda: CosmosHttpResponseError(status_code=403, message="forbidden"),
    )

    report = await _run(store, clock)

    assert report.systemic_error == "load_event_rows: CosmosHttpResponseError"
    assert store.count("load_event_rows") == 1


async def test_no_read_retry_reaches_into_the_race_window() -> None:
    """A late start (T0 - 123 s, lead 120 s): the window opens 2 s later, so a retry that would
    sleep into it is not attempted — no store call ever lands inside the window."""
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=123)
    store = FlakyStore(inner, clock, "load_event_rows", fails=99, make_error=_blip)

    report = await _run(store, clock)

    assert report.systemic_error == "load_event_rows: CosmosHttpResponseError"
    window_start = T0 - timedelta(seconds=121)
    assert all(at < window_start for name, at in store.calls if name == "load_event_rows")


async def test_write_2_retries_a_transient_exception_group() -> None:
    """What ``CosmosTenantStore.record_outcomes`` raises on a blip: an ``ExceptionGroup`` whose
    only leaf is transient. It is retried (the batch is IfMatch'd on the row's etag, so a replay
    after an ambiguous success is refused, never double-applied), not reported as REFUSED."""
    inner = new_store()
    a = await seed_account(inner, n=1)
    clock = race_clock()
    store = FlakyStore(
        inner,
        clock,
        "record_outcomes",
        fails=1,
        make_error=lambda: ExceptionGroup("record_outcomes: 1 row(s) not applied", [_blip()]),
    )

    report = await _run(store, clock, lead_s=30)

    assert report.outcome_write_failures == ()
    assert store.count("record_outcomes") == 2
    row = await inner.get_row(a.row.id, user_id=a.user.id)
    assert row is not None and row.status.value == "booked"


async def test_write_2_refusal_group_is_still_not_retried() -> None:
    inner = new_store()
    a = await seed_account(inner, n=1)
    clock = race_clock()
    store = FlakyStore(
        inner,
        clock,
        "record_outcomes",
        fails=1,
        make_error=lambda: ExceptionGroup("g", [TransitionRefusedError("moved")]),
    )

    report = await _run(store, clock, lead_s=30)

    assert report.outcome_write_failures == (a.row.id,)
    assert store.count("record_outcomes") == 1
