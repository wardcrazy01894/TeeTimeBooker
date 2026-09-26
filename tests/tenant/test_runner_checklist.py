"""MULTIUSER_PLAN MU-9b: the first prod tenant drops (one account, the operator's) are verified by
the seven §11.2 log lines, and that run must BE today's single-user burst plus exactly one lease.

``ForeUP: using pooled CAPTCHA token (lease <row>: N left)`` (line 4) is the adapter's, pinned in
``tests/test_shared_captcha_pool.py``; the blind-POST lines (5) are the unmodified orchestrator's.
Every test runs on a ``VirtualClock``.
"""

from __future__ import annotations

import logging
import tomllib
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from teetime.core.config import SchedulerConfig
from teetime.core.models import (
    BookingRequest,
    CourseCredentials,
    Player,
    RequestId,
    TimeWindow,
)
from teetime.core.orchestrator import Orchestrator
from teetime.courses.foreup.token_pool import SharedCaptchaPool
from teetime.persistence.in_memory_store import InMemoryStore
from teetime.tenant import runner as runner_module
from teetime.tenant.notify import BufferingNotifier
from teetime.tenant.runner import RunReport, run_release_event, tenant_scheduler

from .runner_builders import (
    EVENT,
    KEYRING,
    MB,
    POLICIES,
    T0,
    TARGET,
    WINDOW,
    CountingProvider,
    NullUserNotifier,
    PooledFactory,
    TimedBlindAdapter,
    new_store,
    race_clock,
    seed_account,
)

_REPO = Path(__file__).resolve().parents[2]


def _pool_factory(clock: object, provider: CountingProvider) -> object:
    def build(course: object) -> SharedCaptchaPool:
        return SharedCaptchaPool(provider=provider, clock=clock, course_id=course)  # type: ignore[arg-type]

    return build


async def _one_account_drop(caplog: pytest.LogCaptureFixture) -> tuple[RunReport, str, str]:
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock(before_t0_s=9 * 60)
    provider = CountingProvider(clock)
    factory = PooledFactory(clock)
    caplog.set_level(logging.INFO)

    report = await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=store,
        clock=clock,
        scheduler=tenant_scheduler(),
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=NullUserNotifier(),
        dry_run=False,
        wait=True,
        pool_factory=_pool_factory(clock, provider),  # type: ignore[arg-type]
    )
    return report, str(a.row.id), caplog.text


async def test_first_drop_emits_section_11_2_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    report, row, text = await _one_account_drop(caplog)
    assert report.systemic_error is None

    # 1. the claim
    assert f"tenant-run: claimed 1/1 row(s) for mb0600et target={TARGET.isoformat()}" in text
    # 2. allocation of one account is the identity: its own top 3 by midpoint distance
    assert (
        f"tenant-run: allocation order=[{row}] allowlist[{row}]=[08:15,08:00,08:30] search_only=[]"
        in text
    )
    # 3. the one coordinated fill: 3 burst tokens for the one lease + the shared reserve of 2
    assert (
        f"pool: coordinated fill demanded=5 (burst=3, reserve=2) solved=5 granted={{{row}: 3}} "
        "reserve=2" in text
    )
    # 5. the unchanged staggered burst, measured offsets == planned (VirtualClock is exact)
    for planned in ("-500", "-250", "+0"):
        assert f"blind-POST sent {planned}ms (planned {planned}ms)" in text, planned
    # 6. the outcome, then the write after T0 + 10 s
    outcome_line = (
        f"tenant-run: outcome row={row} outcome=BOOKED held=1 cancelled_extra=2 held_extra=0"
    )
    assert outcome_line in text
    assert "tenant-run: wrote 1/1 outcome(s)" in text
    assert text.index(outcome_line) < text.index("tenant-run: wrote 1/1 outcome(s)")
    # 7. the runtime self-check mirroring §4.4 proof 1 (lead 120 s -> T0-121s)
    assert "tenant-run: no store/credential call inside race window [T0-121s, T0+10s]" in text


async def test_runner_emits_first_drop_checklist_lines(caplog: pytest.LogCaptureFixture) -> None:
    """The plan's §12 name: every one of the runner-owned §11.2 lines appears exactly once."""
    _, _, text = await _one_account_drop(caplog)
    for prefix in (
        "tenant-run: claimed ",
        "tenant-run: allocation order=",
        "pool: coordinated fill ",
        "tenant-run: outcome row=",
        "tenant-run: wrote ",
        "tenant-run: no store/credential call inside race window",
    ):
        assert text.count(prefix) == 1, prefix


def test_tenant_scheduler_matches_the_shipped_toml_scheduler() -> None:
    """The tenant job's race knobs ARE today's booking job's: ``config/container.toml``'s
    [scheduler] (burst 3, reserve 2, stagger (-500, -250, 0), early arrival 500 ms, lead 120 s)."""
    shipped = tomllib.loads((_REPO / "config" / "container.toml").read_text())["scheduler"]
    assert tenant_scheduler() == SchedulerConfig(**shipped)
    assert tenant_scheduler().blind_post_max_count == 3
    assert tenant_scheduler().blind_post_fallback_token_reserve == 2
    assert tenant_scheduler().blind_post_stagger_ms == (-500, -250, 0)


async def test_single_account_run_matches_todays_burst() -> None:
    """One account through the tenant runner fires EXACTLY the single-user TOML ``run --wait``
    burst: the same three slots, in the same order, at the same measured offsets from T0, with
    the same token budget (burst 3 + reserve 2 = 5: one lease of 3 plus the shared reserve)."""
    scheduler = tenant_scheduler()

    # Today's path: one Orchestrator(prefetch_book=True) over the shipped scheduler.
    toml_clock = race_clock(before_t0_s=9 * 60)
    toml_adapter = TimedBlindAdapter(toml_clock)
    request = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET,),
        time_windows=(TimeWindow(earliest=WINDOW[0], latest=WINDOW[1]),),
        players=(Player(first_name="Guest", last_name="Player", email=""),) * 2,
        course_preferences=(MB,),
        holes=18,
        dry_run=False,
    )
    await Orchestrator(
        adapters={MB: toml_adapter},
        store=InMemoryStore(),
        notifier=BufferingNotifier(),
        clock=toml_clock,
        scheduler=scheduler,
        creds={MB: CourseCredentials(username="u", password="p")},
        prefetch_book=True,
    ).run(request)

    # The tenant path: one account, one lease in a coordinated pool.
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock(before_t0_s=9 * 60)
    provider = CountingProvider(clock)
    factory = PooledFactory(clock)
    await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=store,
        clock=clock,
        scheduler=scheduler,
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=NullUserNotifier(),
        dry_run=False,
        wait=True,
        pool_factory=_pool_factory(clock, provider),  # type: ignore[arg-type]
    )
    tenant_adapter = factory.built[a.account.id]

    assert [s for s, _ in tenant_adapter.sends] == [s for s, _ in toml_adapter.sends]
    assert tenant_adapter.send_offsets_ms() == toml_adapter.send_offsets_ms() == [-500, -250, 0]
    assert toml_adapter.last_prepare_count == 5  # min(3, grid) + reserve 2, single-user
    fill = tenant_adapter.pool.report()
    assert fill is not None
    assert (fill.demanded, fill.granted) == (5, {tenant_adapter.key: 3})
    assert provider.calls == 5
    assert all(at <= T0 for _, at in tenant_adapter.sends)
    assert clock.now_utc() >= T0 + timedelta(seconds=10)  # the write ran after the quiet window


def test_race_window_self_check_flags_a_call_inside_the_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The line-7 self-check is not vacuous: a stamp inside [T0-121s, T0+10s) is CRITICAL."""
    caplog.set_level(logging.INFO)
    started = T0 - timedelta(minutes=9)
    inside = [started, T0 - timedelta(seconds=5)]

    runner_module._check_race_window(inside, t0=T0, lead_s=120, quiet_s=10.0, started=started)

    (record,) = [r for r in caplog.records if "race window" in r.getMessage()]
    assert record.levelno == logging.CRITICAL
    assert "1 store/credential call(s) INSIDE race window [T0-121s, T0+10s]" in record.getMessage()
