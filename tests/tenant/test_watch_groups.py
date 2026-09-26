"""MU-R2 part 2 (MULTIUSER_PLAN §16.3/§16.4): the tenant watcher honours the group floor, books
a better-ranked sibling (the cross-course upgrade, book first) and then collapses the group,
cancelling the worse OWNED booking. Dry-run cancels nothing."""

from __future__ import annotations

from datetime import time
from typing import Any
from uuid import uuid4

from teetime.core.clock import FakeClock
from teetime.tenant.models import GROUP_DOWNGRADE_REASON, BookingState, RankedWindow, RowStatus
from teetime.tenant.watch_runner import WatchReport, run_tenant_watch

from .runner_builders import KEYRING, POLICIES, TARGET, WINDOW
from .watch_runner_builders import (
    BETTER,
    CUTOFF,
    POLICY_ON,
    WATCH_NOW,
    FakeFactory,
    RecordingNotifier,
    WatchFake,
    book_row,
    new_store,
    seed,
    stored_row,
    watch_scheduler,
)


def _opt(rank: int) -> tuple[RankedWindow, ...]:
    return (RankedWindow(rank, *WINDOW),)


async def _watch(store: Any, factory: FakeFactory, *, dry_run: bool = False) -> WatchReport:
    return await run_tenant_watch(
        policies=POLICIES,
        store=store,
        clock=FakeClock(start=WATCH_NOW),
        scheduler=watch_scheduler(),
        booking_policy=POLICY_ON,
        cutoff=CUTOFF,
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=RecordingNotifier(),
        dry_run=dry_run,
    )


async def test_watch_never_books_a_row_its_group_already_beats() -> None:
    """A holds rank 1; B's only option is rank 2: B is not attempted even with a slot open."""
    store = new_store()
    group = uuid4()
    a = await seed(store, n=1, group_id=group, options=_opt(1))
    b = await seed(store, n=2, group_id=group, options=_opt(2))
    await book_row(store, a, raw_id="A-RAW", tee=BETTER)
    b_fake = WatchFake()
    factory = FakeFactory(adapters={a.account.id: WatchFake(), b.account.id: b_fake})

    await _watch(store, factory)

    assert (await stored_row(store, b)).status is RowStatus.PENDING
    assert b_fake.book_call_count == 0


async def test_watch_books_the_better_sibling_then_collapses_the_worse_booking() -> None:
    """A holds rank 3; B (rank 1) is pending and a slot is open. The watcher books B first, then
    the collapse cancels A's reservation and downgrades A's row (ledger cancelled_group)."""
    store = new_store()
    group = uuid4()
    a = await seed(store, n=1, group_id=group, options=_opt(3))
    b = await seed(store, n=2, group_id=group, options=_opt(1))
    await book_row(store, a, raw_id="A-RAW", tee=BETTER)
    a_fake, b_fake = WatchFake(), WatchFake()
    factory = FakeFactory(adapters={a.account.id: a_fake, b.account.id: b_fake})

    await _watch(store, factory)

    assert (await stored_row(store, b)).status is RowStatus.BOOKED
    a_row = await stored_row(store, a)
    assert (a_row.status, a_row.status_reason) == (RowStatus.PENDING, GROUP_DOWNGRADE_REASON)
    assert a_fake.cancel_call_count == 1
    ledger = {
        e.raw_reservation_id: e.state
        for e in await store.list_owned_bookings(a.account.id, target_date=TARGET)
    }
    assert ledger == {"A-RAW": BookingState.CANCELLED_GROUP}


async def test_watch_dry_run_collapses_nothing() -> None:
    store = new_store()
    group = uuid4()
    a = await seed(store, n=1, group_id=group, options=_opt(3))
    b = await seed(store, n=2, group_id=group, options=_opt(1))
    await book_row(store, a, raw_id="A-RAW", tee=BETTER)
    await book_row(store, b, raw_id="B-RAW", tee=BETTER)
    a_fake = WatchFake()
    factory = FakeFactory(adapters={a.account.id: a_fake, b.account.id: WatchFake()})

    await _watch(store, factory, dry_run=True)

    assert (await stored_row(store, a)).status is RowStatus.BOOKED
    assert a_fake.cancel_call_count == 0


_ = time
