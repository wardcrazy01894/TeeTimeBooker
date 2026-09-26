"""MULTIUSER_PLAN MU-9b carry-overs from the #233 review: the runner's store calls are bounded
in time so a slow store can neither eat the CAPTCHA prefetch lead (READ #1 / WRITE #1) nor keep
the process alive past the self-deadline (WRITE #2). Every test runs on a ``VirtualClock``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest

from teetime.core.models import BookingOutcome
from teetime.dev.virtual_clock import VirtualClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.runner import (
    STORE_CALL_TIMEOUT_S,
    ExitStatus,
    RunReport,
    exit_code_for,
    run_release_event,
)

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


class HangingStore:
    """Delegates to ``inner`` but PARKS (on the shared clock) forever in the named methods: a
    Cosmos call that never answers. A collaborator stand-in, never the runner."""

    def __init__(self, inner: InMemoryTenantStore, clock: VirtualClock, *hang: str) -> None:
        self._inner = inner
        self._clock = clock
        self._hang = set(hang)
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        target = getattr(self._inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if name in self._hang:
                await self._clock.sleep(1_000_000)
            return await target(*args, **kwargs)

        return call


async def _run(
    store: Any, clock: VirtualClock, *, lead_s: int = 120, replica_timeout_s: float = 1200.0
) -> tuple[RunReport, ScriptedFactory]:
    factory = ScriptedFactory()
    report = await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=store,
        clock=clock,
        scheduler=scheduler(lead_s=lead_s),
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=NullUserNotifier(),
        dry_run=False,
        wait=True,
        replica_timeout_s=replica_timeout_s,
    )
    return report, factory


def test_store_call_timeout_leaves_the_prefetch_lead_intact() -> None:
    """The latency assumption, pinned: the 05:50 cron reaches READ #1 at ~05:51, so read +
    claim (each at most STORE_CALL_TIMEOUT_S) finish minutes before T0 - lead (05:58)."""
    assert 0 < STORE_CALL_TIMEOUT_S <= 30


@pytest.mark.parametrize("method", ["load_event_rows", "claim_rows"])
async def test_store_read_timeout_bounds_prefetch_lead(method: str) -> None:
    """A store that never answers READ #1 / WRITE #1 is abandoned after STORE_CALL_TIMEOUT_S:
    a systemic (non-zero) report, before any adapter is built, well before the race window."""
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=9 * 60)  # the 05:50 cron + container start
    started = clock.now_utc()

    report, factory = await _run(HangingStore(inner, clock, method), clock)

    assert report.systemic_error == f"{method}: TimeoutError"
    assert exit_code_for(report) is ExitStatus.SYSTEMIC_FAILURE
    assert factory.calls == []
    assert clock.now_utc() <= started + timedelta(seconds=2 * STORE_CALL_TIMEOUT_S)


async def test_store_read_never_runs_into_the_race_window() -> None:
    """A LATE start (T0 - 130 s with a 120 s lead): the read's budget is clamped to the start of
    the race window [T0 - lead - 1 s, ...], not the full STORE_CALL_TIMEOUT_S."""
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=130)

    report, _ = await _run(HangingStore(inner, clock, "load_event_rows"), clock)

    assert report.systemic_error == "load_event_rows: TimeoutError"
    assert clock.now_utc() <= T0 - timedelta(seconds=121)


async def test_store_call_budget_is_not_clamped_on_a_manual_run_past_the_window() -> None:
    """``--no-wait`` after the window opened (a manual re-run at 06:05): there is no race left
    to protect, so a healthy store is still read — the clamp never turns into a zero budget."""
    inner = new_store()
    await seed_account(inner, n=1)
    clock = race_clock(before_t0_s=-300)
    factory = ScriptedFactory()

    report = await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=inner,
        clock=clock,
        scheduler=scheduler(lead_s=30),
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=NullUserNotifier(),
        dry_run=False,
        wait=False,
    )

    assert report.systemic_error is None
    assert report.rows_claimed == 1


async def test_outcome_writer_bounded_by_self_deadline(capsys: pytest.CaptureFixture[str]) -> None:
    """WRITE #2 against a store that never answers: the writer is abandoned at the writer
    deadline (self-deadline + a short grace, still before the replica timeout), every
    unwritten outcome is dumped as JSON on stdout, and the run exits non-zero."""
    inner = new_store()
    a = await seed_account(inner, n=1)
    clock = race_clock()  # start = T0 - 45 s
    started = clock.now_utc()
    replica = 300.0

    report, _ = await _run(
        HangingStore(inner, clock, "record_outcomes"), clock, lead_s=30, replica_timeout_s=replica
    )

    assert clock.now_utc() < started + timedelta(seconds=replica - 30)
    (out,) = report.outcomes
    assert out.outcome is BookingOutcome.BOOKED
    assert report.outcome_write_failures == (a.row.id,)
    assert exit_code_for(report) is ExitStatus.SYSTEMIC_FAILURE
    dumped = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [d["tenant_run_unwritten_outcome"] for d in dumped] == [str(a.row.id)]
    assert dumped[0]["to_status"] == "booked"
