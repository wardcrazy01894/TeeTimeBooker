"""MULTIUSER_PLAN MU-9b: the booking runner's notifications (§4.5 "Notify" column, §8.7).

Each account's ``Orchestrator`` gets a ``BufferingNotifier`` (no I/O near T0). After WRITE #2
the runner maps every row to ``UserEvent``s: user-facing kinds go through the ``UserNotifier``;
the operator summary (every user event plus the operator-only lines) goes through
``deliver_operator_summary``, whose returned exit code is authoritative — a failed summary send
makes the run non-zero (SF6). Every test runs on a ``VirtualClock``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from teetime.core import orchestrator as orchestrator_module
from teetime.core.adapter import AdapterError, AuthError, CaptchaError
from teetime.core.models import BookingOutcome
from teetime.dev.virtual_clock import VirtualClock
from teetime.tenant.notify import BufferingNotifier, FakeEmailSender, UserEvent, UserEventKind
from teetime.tenant.runner import (
    ExitStatus,
    OperatorSink,
    RunReport,
    exit_code_for,
    run_release_event,
)

from .runner_builders import (
    EVENT,
    GRID,
    KEYRING,
    MB,
    POLICIES,
    T0,
    TARGET,
    FakeAdapterNonBlind,
    ScriptedFactory,
    Seeded,
    SpyStore,
    blind_adapter,
    new_store,
    race_clock,
    scheduler,
    seed_account,
)

_OPERATOR = "ops@example.test"


class TimedNotifier:
    """A ``UserNotifier`` that stamps each send with the shared clock."""

    def __init__(self, clock: VirtualClock, *, fail: bool = False) -> None:
        self._clock = clock
        self._fail = fail
        self.sent: list[tuple[UserEvent, datetime]] = []

    async def send(self, event: UserEvent) -> None:
        self.sent.append((event, self._clock.now_utc()))
        if self._fail:
            raise RuntimeError("mail backend down")

    def kinds(self) -> list[UserEventKind]:
        return [e.kind for e, _ in self.sent]


async def _run(
    store: Any,
    clock: VirtualClock,
    factory: ScriptedFactory,
    *,
    notifier: Any,
    sender: FakeEmailSender | None = None,
) -> RunReport:
    return await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=store,
        clock=clock,
        scheduler=scheduler(),
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=notifier,
        dry_run=False,
        wait=True,
        operator=OperatorSink(sender=sender, to=_OPERATOR) if sender is not None else None,
    )


def _one(notifier: TimedNotifier) -> UserEvent:
    (event,) = [e for e, _ in notifier.sent]
    return event


async def test_runner_emails_the_booked_user_after_the_outcome_write() -> None:
    inner = new_store()
    a = await seed_account(inner, n=1)
    clock = race_clock()
    spy = SpyStore(inner, clock)
    notifier = TimedNotifier(clock)

    report = await _run(spy, clock, ScriptedFactory(), notifier=notifier)

    assert exit_code_for(report) is ExitStatus.OK
    event = _one(notifier)
    assert event.kind is UserEventKind.BOOKED
    assert (event.user_id, event.row_id, event.course_id) == (a.user.id, a.row.id, MB)
    assert event.target_date == TARGET
    assert event.confirmation == "TTB:FAKE-s-0815"
    assert event.tee_time == GRID[3].tee_time  # 08:15, the rank-0 blind slot
    # No I/O near T0: the email goes out only after the row's WRITE #2 landed.
    (write_at,) = [at for name, at in spy.calls if name == "record_outcomes"]
    (_, sent_at) = notifier.sent[0]
    assert sent_at >= write_at >= T0 + timedelta(seconds=10)


async def test_runner_orchestrators_get_a_buffering_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine notifier is a ``BufferingNotifier`` per account (collect only); the BOOKED
    email above is derived from what it buffered."""
    seen: list[object] = []
    real_init = orchestrator_module.Orchestrator.__init__

    def spy_init(self: Any, *args: Any, **kwargs: Any) -> None:
        seen.append(kwargs["notifier"])
        real_init(self, *args, **kwargs)

    store = new_store()
    await seed_account(store, n=1)
    await seed_account(store, n=2)
    clock = race_clock()
    monkeypatch.setattr(orchestrator_module.Orchestrator, "__init__", spy_init)
    await _run(store, clock, ScriptedFactory(), notifier=TimedNotifier(clock))

    assert len(seen) == 2
    assert all(isinstance(n, BufferingNotifier) for n in seen)
    assert seen[0] is not seen[1]


async def test_runner_missed_drop_emails_user_and_exits_zero() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    empty = blind_adapter([])
    empty.set_search_response([])
    notifier = TimedNotifier(clock)
    sender = FakeEmailSender()

    report = await _run(
        store,
        clock,
        ScriptedFactory(adapters={a.account.id: empty}),
        notifier=notifier,
        sender=sender,
    )

    assert exit_code_for(report) is ExitStatus.OK
    event = _one(notifier)
    assert (event.kind, event.user_id) == (UserEventKind.MISSED_DROP, a.user.id)
    assert "no_inventory" in event.detail
    (summary,) = sender.sent
    assert summary.to == _OPERATOR
    assert "exit 0" in summary.subject
    assert "missed_drop" in summary.body


async def test_runner_auth_error_notifies_user_and_operator_and_flags_account() -> None:
    store = new_store()
    bad = await seed_account(store, n=1)
    good = await seed_account(store, n=2)
    clock = race_clock()
    failing = blind_adapter()
    failing.set_authenticate_side_effects([AuthError("bad password"), AuthError("bad password")])
    notifier = TimedNotifier(clock)
    sender = FakeEmailSender()

    report = await _run(
        store,
        clock,
        ScriptedFactory(adapters={bad.account.id: failing}),
        notifier=notifier,
        sender=sender,
    )

    assert exit_code_for(report) is ExitStatus.OK
    by_user = {e.user_id: e.kind for e, _ in notifier.sent}
    assert by_user == {bad.user.id: UserEventKind.AUTH_FAILED, good.user.id: UserEventKind.BOOKED}
    # No TenantStore write flips an account to auth_failed yet (MU-8b): the report carries it.
    assert report.auth_failed_accounts == (bad.account.id,)
    (summary,) = sender.sent
    assert "auth_failed" in summary.body
    assert "bad password" not in summary.body  # class names only, never the message


async def test_runner_captcha_error_notifies_user_and_operator_and_exits_nonzero() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    adapter = FakeAdapterNonBlind()
    adapter.set_search_response(list(GRID))
    adapter.set_book_to_raise(CaptchaError("solver down"))
    notifier = TimedNotifier(clock)
    sender = FakeEmailSender()

    report = await _run(
        store,
        clock,
        ScriptedFactory(adapters={a.account.id: adapter}),
        notifier=notifier,
        sender=sender,
    )

    assert exit_code_for(report) is ExitStatus.SYSTEMIC_FAILURE
    assert _one(notifier).kind is UserEventKind.MISSED_DROP
    (summary,) = sender.sent
    assert "exit 1" in summary.subject
    assert "CaptchaError" in summary.body


async def test_runner_uncertain_is_operator_only() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    flaky = blind_adapter()
    flaky.set_book_side_effects([AdapterError("timeout")] * 4)
    notifier = TimedNotifier(clock)
    sender = FakeEmailSender()

    report = await _run(
        store,
        clock,
        ScriptedFactory(adapters={a.account.id: flaky}),
        notifier=notifier,
        sender=sender,
    )

    assert exit_code_for(report) is ExitStatus.SYSTEMIC_FAILURE
    assert notifier.sent == []  # §4.5: UNCERTAIN notifies the operator only
    (summary,) = sender.sent
    assert "needs_reconcile" in summary.body


async def test_summary_email_failure_forces_nonzero() -> None:
    """SF6: a clean run (a booking) whose operator summary cannot be delivered exits non-zero —
    otherwise a broken ACS setup would make every miss invisible."""
    store = new_store()
    await seed_account(store, n=1)
    clock = race_clock()
    sender = FakeEmailSender(fail=True)

    report = await _run(
        store, clock, ScriptedFactory(), notifier=TimedNotifier(clock), sender=sender
    )

    assert len(sender.sent) == 1
    assert report.summary_email_failed is True
    assert exit_code_for(report) is ExitStatus.SYSTEMIC_FAILURE


async def test_summary_email_failure_exits_nonzero() -> None:
    """The plan's §12 name for the same SF6 guarantee, on a MISSED drop (the case that would
    otherwise be invisible: misses exit 0)."""
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    empty = blind_adapter([])
    empty.set_search_response([])
    sender = FakeEmailSender(fail=True)

    report = await _run(
        store,
        clock,
        ScriptedFactory(adapters={a.account.id: empty}),
        notifier=TimedNotifier(clock),
        sender=sender,
    )

    assert report.summary_email_failed is True
    assert exit_code_for(report) is ExitStatus.SYSTEMIC_FAILURE


async def test_summary_delivered_keeps_a_clean_exit() -> None:
    store = new_store()
    await seed_account(store, n=1)
    clock = race_clock()
    sender = FakeEmailSender()

    report = await _run(
        store, clock, ScriptedFactory(), notifier=TimedNotifier(clock), sender=sender
    )

    (summary,) = sender.sent
    assert "run OK (exit 0)" in summary.subject
    assert report.summary_email_failed is False
    assert exit_code_for(report) is ExitStatus.OK


async def test_no_rows_sends_no_summary() -> None:
    clock = race_clock()
    sender = FakeEmailSender()

    report = await _run(
        new_store(), clock, ScriptedFactory(), notifier=TimedNotifier(clock), sender=sender
    )

    assert sender.sent == []
    assert exit_code_for(report) is ExitStatus.OK


async def test_systemic_failure_still_sends_the_operator_summary() -> None:
    """§8.7: on ANY non-zero exit one email goes to the operator — including a store failure
    before T0 (no user event exists yet)."""
    store = new_store()
    await seed_account(store, n=1)
    clock = race_clock()
    sender = FakeEmailSender()

    class BrokenStore(SpyStore):
        async def load_event_rows(self, **kwargs: Any) -> Any:
            raise ConnectionError("cosmos down")

    report = await _run(
        BrokenStore(store, clock),
        clock,
        ScriptedFactory(),
        notifier=TimedNotifier(clock),
        sender=sender,
    )

    assert report.systemic_error == "load_event_rows: ConnectionError"
    (summary,) = sender.sent
    assert "exit 1" in summary.subject
    assert "load_event_rows: ConnectionError" in summary.body
    assert "cosmos down" not in summary.body


async def test_a_failing_user_notifier_never_masks_the_outcome() -> None:
    store = new_store()
    a: Seeded = await seed_account(store, n=1)
    clock = race_clock()

    report = await _run(store, clock, ScriptedFactory(), notifier=TimedNotifier(clock, fail=True))

    assert [o.outcome for o in report.outcomes] == [BookingOutcome.BOOKED]
    assert exit_code_for(report) is ExitStatus.OK
    assert a.row.id == report.outcomes[0].row_id
