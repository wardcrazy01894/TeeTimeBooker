"""MULTIUSER_PLAN MU-10b: the snapshot-driven half of ``run_tenant_watch`` — soft auth and
snapshot trust (§7.5), vanish inference (two trusted snapshots + the M2 exclusions), adoption
and the ownership gate on the upgrade (§7.6), the recorder-derived upgrade outcome (M2), the
duplicate reconcile, orphans, and dry-run safety (§7.8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from teetime.core.adapter import AdapterError
from teetime.core.clock import FakeClock
from teetime.core.models import MANAGED_BOOKING_TAG
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    Actor,
    BookingSource,
    BookingState,
    RankedWindow,
    RowFingerprint,
    RowStatus,
)
from teetime.tenant.notify import UserEventKind
from teetime.tenant.runner import WatchReport
from teetime.tenant.store import RowOutcome
from teetime.tenant.watch_runner import run_tenant_watch, seeded_terminal

from . import watcher_builders
from .watch_runner_builders import (
    BETTER,
    CUTOFF,
    HELD_EARLY,
    KEYRING,
    POLICIES,
    POLICY_ON,
    SEED_NOW,
    TARGET,
    WATCH_NOW,
    WINDOW,
    FakeFactory,
    RecordingNotifier,
    Seeded,
    UntrustedSnapshotFake,
    WatchFake,
    book_row,
    ledger_entry,
    new_store,
    now_with_cadence,
    reservation,
    seed,
    slot,
    stored_row,
    trusted_snapshot,
    watch_scheduler,
)

HELD_RAW = f"FAKE-{HELD_EARLY.slot_id}"
BETTER_RAW = f"FAKE-{BETTER.slot_id}"


async def _watch(
    store: Any,
    factory: FakeFactory,
    *,
    now: datetime = WATCH_NOW,
    dry_run: bool = False,
    notifier: RecordingNotifier | None = None,
) -> WatchReport:
    return await run_tenant_watch(
        policies=POLICIES,
        store=store,
        clock=FakeClock(start=now),
        scheduler=watch_scheduler(),
        booking_policy=POLICY_ON,
        cutoff=CUTOFF,
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=notifier or RecordingNotifier(),
        dry_run=dry_run,
    )


class SpyStore:
    """Collaborator spy: records every ``TenantStore`` method name called, then delegates."""

    def __init__(self, inner: InMemoryTenantStore) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return await target(*args, **kwargs)

        return call


async def _owned_booking(
    store: InMemoryTenantStore, *, fake: WatchFake, live: bool = True
) -> tuple[Seeded, FakeFactory]:
    """An account whose row is BOOKED on HELD_EARLY (07:30, owned, ledgered held). GRID offers
    08:15, the window midpoint: strictly better, so the row is an upgrade candidate."""
    s = await seed(store, n=1)
    await book_row(store, s, raw_id=HELD_RAW, tee=HELD_EARLY)
    if live:
        fake.set_existing_reservations([reservation(HELD_RAW, HELD_EARLY)])
    return s, FakeFactory(adapters={s.account.id: fake})


# --- §7.5 soft auth + snapshot trust ------------------------------------------------------------


async def test_watch_soft_auth_not_persisted() -> None:
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    fake.set_auth_soft_fail()
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    assert report.logins == 1
    assert await store.get_snapshot(s.account.id) is None
    account = await store.get_account_unscoped(s.account.id)
    assert account is not None and account.consecutive_soft_auth_failures == 1
    assert fake.list_reservations_call_count == 0
    assert fake.book_call_count == 0
    assert (await stored_row(store, s)).status is RowStatus.PENDING


async def test_watch_third_soft_auth_failure_notifies_auth_failed() -> None:
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    fake.set_auth_soft_fail()
    factory = FakeFactory(adapters={s.account.id: fake})
    notifier = RecordingNotifier()

    for k in range(3):
        await _watch(store, factory, now=WATCH_NOW + timedelta(minutes=10 * k), notifier=notifier)

    assert [e.kind for e in notifier.events] == [UserEventKind.AUTH_FAILED]
    assert notifier.events[0].user_id == s.user.id


async def test_watch_untrusted_snapshot_is_not_persisted_and_nothing_acts() -> None:
    store = new_store()
    s = await seed(store, n=1)
    fake = UntrustedSnapshotFake()
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    assert report.logins == 1
    assert await store.get_snapshot(s.account.id) is None
    assert fake.book_call_count == 0
    assert (await stored_row(store, s)).status is RowStatus.PENDING


async def test_watch_persists_the_trusted_snapshot() -> None:
    store = new_store()
    s, factory = await _owned_booking(store, fake=WatchFake(slots=[]))

    await _watch(store, factory, now=now_with_cadence(s.account))

    snap = await store.get_snapshot(s.account.id)
    assert snap is not None and snap.trusted and snap.source == "watcher"
    assert [e.raw_id for e in snap.entries] == [HELD_RAW]


# --- §7.5 vanish inference ---------------------------------------------------------------------


async def test_watch_vanish_needs_two_trusted_snapshots() -> None:
    store = new_store()
    fake = WatchFake()  # the reservation is gone at the course; GRID keeps it an upgrade candidate
    s, factory = await _owned_booking(store, fake=fake, live=False)
    notifier = RecordingNotifier()

    first = await _watch(store, factory, notifier=notifier)

    assert (await stored_row(store, s)).status is RowStatus.BOOKED  # ONE miss infers nothing
    assert first.cancelled_external == ()
    assert fake.book_call_count == fake.cancel_call_count == 0  # never "upgraded" while missing

    second = await _watch(store, factory, now=WATCH_NOW + timedelta(minutes=10), notifier=notifier)

    row = await stored_row(store, s)
    assert (row.status, row.status_reason) == (RowStatus.CANCELLED, "external")
    assert second.cancelled_external == (s.row.id,)
    assert [e.kind for e in notifier.events] == [UserEventKind.CANCELLED_EXTERNAL]
    assert fake.book_call_count == 0


async def test_watch_two_misses_inside_ten_minutes_are_one_observation() -> None:
    store = new_store()
    fake = WatchFake()
    s, factory = await _owned_booking(store, fake=fake, live=False)

    await _watch(store, factory)
    await _watch(store, factory, now=WATCH_NOW + timedelta(minutes=5))

    assert (await stored_row(store, s)).status is RowStatus.BOOKED


async def test_external_cancel_marked_notified_not_rebooked() -> None:
    store = new_store()
    fake = WatchFake()
    s, factory = await _owned_booking(store, fake=fake, live=False)
    await store.save_snapshot(trusted_snapshot(s, at=WATCH_NOW - timedelta(minutes=15), entries=()))
    notifier = RecordingNotifier()

    await _watch(store, factory, notifier=notifier)
    later = await _watch(store, factory, now=WATCH_NOW + timedelta(minutes=10), notifier=notifier)

    assert (await stored_row(store, s)).status is RowStatus.CANCELLED
    assert later.rows_loaded == 0  # a cancelled row leaves the watch set: never re-booked
    assert fake.book_call_count == 0
    assert [e.kind for e in notifier.events] == [UserEventKind.CANCELLED_EXTERNAL]


async def test_external_cancel_frees_slot_for_explicit_rerequest() -> None:
    store = new_store()
    s, factory = await _owned_booking(store, fake=WatchFake(), live=False)
    await store.save_snapshot(trusted_snapshot(s, at=WATCH_NOW - timedelta(minutes=15), entries=()))

    await _watch(store, factory)

    again = await store.create_explicit_row(
        user_id=s.user.id,
        account_id=s.account.id,
        target_date=TARGET,
        options=(RankedWindow(1, WINDOW[0], WINDOW[1]),),
        party_size=2,
        now=WATCH_NOW + timedelta(minutes=1),
    )
    assert again.status is RowStatus.PENDING


async def test_watch_vanish_with_upgrade_marker_is_bot_caused() -> None:
    """M2: a crashed upgrade left ``upgrade_started_at`` set; the missing reservation is OUR
    doing -> PENDING + needs_reconcile (re-book allowed), never cancelled(external)."""
    store = new_store()
    fake = WatchFake(slots=[])
    s, factory = await _owned_booking(store, fake=fake, live=False)
    row = await stored_row(store, s)
    fingerprint = (row.status, row.version, row.booked_raw_id)
    until = WATCH_NOW + timedelta(minutes=5)
    marker_at = WATCH_NOW - timedelta(minutes=30)
    assert await store.acquire_row_lease(
        row.id, owner="watcher:dead", until=until, now=marker_at, expected=None
    )
    assert await store.set_upgrade_marker(
        row.id, owner="watcher:dead", at=marker_at, expected=RowFingerprint(*fingerprint)
    )
    await store.release_row_lease(row.id, owner="watcher:dead")
    await store.save_snapshot(trusted_snapshot(s, at=WATCH_NOW - timedelta(minutes=15), entries=()))

    report = await _watch(store, factory, now=now_with_cadence(s.account))

    row = await stored_row(store, s)
    assert row.status is RowStatus.PENDING
    assert row.needs_reconcile is True
    assert row.upgrade_started_at is None
    assert report.reconcile_flagged == (s.row.id,)
    assert report.cancelled_external == ()


async def test_watch_replacement_reservation_adopted_not_external() -> None:
    store = new_store()
    fake = WatchFake(slots=[])
    s, factory = await _owned_booking(store, fake=fake, live=False)
    manual = slot(8, 0)
    fake.set_existing_reservations([reservation("MANUAL-9", manual)])
    await store.save_snapshot(
        trusted_snapshot(s, at=WATCH_NOW - timedelta(minutes=15), entries=(("MANUAL-9", manual),))
    )

    report = await _watch(store, factory, now=now_with_cadence(s.account))

    row = await stored_row(store, s)
    assert row.status is RowStatus.BOOKED
    assert row.booked_raw_id == "MANUAL-9"
    assert row.booked_confirmation == "MANUAL-9"  # unowned: no TTB:, so never upgraded
    assert report.adopted == (s.row.id,)
    assert report.cancelled_external == ()
    assert fake.cancel_call_count == fake.book_call_count == 0


# --- §7.6 adoption and the ownership gate -------------------------------------------------------


async def test_watch_adopt_manual_is_unowned_no_upgrade() -> None:
    """A pending row's account already holds a (manual) reservation for the date: it is adopted
    UNOWNED (no ledger entry, raw confirmation) and a later run never upgrades or cancels it,
    even with a strictly better slot on offer."""
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()  # GRID: 08:15 is strictly better than the manual 07:30
    fake.set_existing_reservations([reservation("MANUAL-1", HELD_EARLY)])
    factory = FakeFactory(adapters={s.account.id: fake})

    first = await _watch(store, factory)

    row = await stored_row(store, s)
    assert (row.status, row.booked_raw_id, row.booked_confirmation) == (
        RowStatus.BOOKED,
        "MANUAL-1",
        "MANUAL-1",
    )
    assert await store.list_owned_bookings(s.account.id, target_date=TARGET) == []
    assert first.adopted == (s.row.id,)

    await _watch(
        store, factory, now=now_with_cadence(s.account, base=WATCH_NOW + timedelta(hours=1))
    )

    assert fake.cancel_call_count == 0
    assert fake.book_call_count == 0
    assert (await stored_row(store, s)).booked_raw_id == "MANUAL-1"


async def test_watch_adopts_a_ledgered_reservation_as_owned() -> None:
    """A pending row whose (account, date) ledger already holds the live reservation (a WRITE #2
    that was refused, M4) is adopted OWNED: ``TTB:`` confirmation, ledger entry kept."""
    store = new_store()
    s = await seed(store, n=1)
    entry = ledger_entry(s.row, HELD_RAW, HELD_EARLY)
    with pytest.raises(ExceptionGroup):
        await store.record_outcomes(
            [
                RowOutcome(
                    row_id=s.row.id,
                    course_account_id=s.account.id,
                    target_date=TARGET,
                    actor=Actor.BOOKING_RUNNER,
                    to_status=RowStatus.BOOKED,
                    last_outcome="booked",
                    at=WATCH_NOW - timedelta(hours=1),
                    booking=entry,
                    release_lease_owner=None,  # no lease -> refused, ledger still written
                )
            ]
        )
    fake = WatchFake(slots=[])
    fake.set_existing_reservations([reservation(HELD_RAW, HELD_EARLY)])
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    row = await stored_row(store, s)
    assert row.status is RowStatus.BOOKED
    assert row.booked_confirmation == f"{MANAGED_BOOKING_TAG}{HELD_RAW}"
    assert row.needs_reconcile is False
    assert report.adopted == (s.row.id,)


def test_seeded_terminal_carries_ttb_only_when_owned() -> None:
    """The §7.6 gate on the upgrade: the engine's pre-seeded terminal is ``TTB:``-managed ONLY
    for an owned booking, so ``maybe_upgrade``'s managed guard refuses a manual reservation."""
    base = watcher_builders
    acct = base.account(0)
    booked = replace(
        base.row(acct, status=RowStatus.BOOKED, booked_raw_id="R-1"), booked_at=SEED_NOW
    )

    assert seeded_terminal(booked, owned=True).confirmation_code == "TTB:R-1"
    assert seeded_terminal(booked, owned=False).confirmation_code == "R-1"
    held = seeded_terminal(booked, owned=False).slot
    assert held is not None and held.tee_time.utcoffset() == timedelta(hours=-4)  # course-local
    with pytest.raises(ValueError, match="BOOKED"):
        seeded_terminal(base.row(acct), owned=True)


async def test_watch_upgrade_gated_on_ownership_end_to_end() -> None:
    """A BOOKED row holding an UNOWNED (manual) reservation, a strictly better slot on offer, and
    the account logging in anyway (cadence): the unmodified engine runs, and nothing is cancelled
    or booked."""
    store = new_store()
    s = await seed(store, n=1)
    await book_row(store, s, raw_id="MANUAL-7", tee=HELD_EARLY, owned=False)
    fake = WatchFake()
    fake.set_existing_reservations([reservation("MANUAL-7", HELD_EARLY)])
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory, now=now_with_cadence(s.account))

    assert report.logins == 1
    assert fake.cancel_call_count == 0
    assert fake.book_call_count == 0
    row = await stored_row(store, s)
    assert (row.status, row.booked_raw_id, row.upgrade_started_at) == (
        RowStatus.BOOKED,
        "MANUAL-7",
        None,
    )


# --- the recorder-derived upgrade outcome (M2) ---------------------------------------------------


async def test_watch_upgrades_an_owned_booking() -> None:
    store = new_store()
    fake = WatchFake()
    s, factory = await _owned_booking(store, fake=fake)
    notifier = RecordingNotifier()

    report = await _watch(store, factory, notifier=notifier)

    row = await stored_row(store, s)
    assert (row.status, row.booked_raw_id) == (RowStatus.BOOKED, BETTER_RAW)
    assert row.upgrade_started_at is None
    assert row.lease_owner is None
    ledger = {
        e.raw_reservation_id: (e.state, e.source)
        for e in await store.list_owned_bookings(s.account.id, target_date=TARGET)
    }
    assert ledger[HELD_RAW][0] is BookingState.CANCELLED_UPGRADE
    assert ledger[BETTER_RAW] == (BookingState.HELD, BookingSource.UPGRADE)
    assert report.upgraded == (s.row.id,)
    assert [e.kind for e in notifier.events] == [UserEventKind.UPGRADED]


async def test_watch_upgrade_failed_rebook_sets_pending_reconcile() -> None:
    """The engine cancels the old reservation, the rebook raises, and ``maybe_upgrade`` returns
    None (the OLD terminal): only the recorder shows the cancel, so the row goes PENDING +
    needs_reconcile (a bot-caused loss) with the old id ledgered cancelled_upgrade."""
    store = new_store()
    fake = WatchFake()
    fake.set_book_side_effects([AdapterError("read timeout after POST")])
    s, factory = await _owned_booking(store, fake=fake)

    report = await _watch(store, factory)

    row = await stored_row(store, s)
    assert row.status is RowStatus.PENDING
    assert row.needs_reconcile is True
    assert row.upgrade_started_at is None
    assert row.booked_raw_id is None
    ledger = await store.list_owned_bookings(s.account.id, target_date=TARGET)
    assert [(e.raw_reservation_id, e.state) for e in ledger] == [
        (HELD_RAW, BookingState.CANCELLED_UPGRADE)
    ]
    assert report.reconcile_flagged == (s.row.id,)
    assert fake.cancel_call_count == 1


# --- the duplicate reconcile (E5 = ownership) ------------------------------------------------------


async def test_watch_reconcile_collapses_an_owned_extra() -> None:
    store = new_store()
    s = await seed(store, n=1)
    extra = slot(7, 30)
    kept = slot(8, 0)
    await book_row(
        store,
        s,
        raw_id="KEPT",
        tee=kept,
        extras=(ledger_entry(s.row, "EXTRA", extra, state=BookingState.HELD_EXTRA),),
    )
    fake = WatchFake(slots=[])
    fake.set_existing_reservations([reservation("KEPT", kept), reservation("EXTRA", extra)])
    factory = FakeFactory(adapters={s.account.id: fake})

    await _watch(store, factory, now=now_with_cadence(s.account))

    assert fake.cancel_call_count == 1
    assert [r.confirmation_code for r in await fake.list_reservations()] == ["KEPT"]
    row = await stored_row(store, s)
    assert (row.status, row.booked_raw_id) == (RowStatus.BOOKED, "KEPT")
    ledger = {
        e.raw_reservation_id: e.state
        for e in await store.list_owned_bookings(s.account.id, target_date=TARGET)
    }
    assert ledger == {"KEPT": BookingState.HELD, "EXTRA": BookingState.CANCELLED_EXTRA}


async def test_watch_reconcile_keeping_the_extra_moves_the_row_to_it() -> None:
    """Keep-best may keep the OWNED extra and cancel the row's own reservation. The row then
    follows the survivor (BOOKED on it, still owned) rather than reading the cancel as a failed
    upgrade."""
    store = new_store()
    s = await seed(store, n=1)
    worse, better = slot(7, 30), slot(8, 0)
    await book_row(
        store,
        s,
        raw_id="ROW",
        tee=worse,
        extras=(ledger_entry(s.row, "EXTRA", better, state=BookingState.HELD_EXTRA),),
    )
    fake = WatchFake(slots=[])
    fake.set_existing_reservations([reservation("ROW", worse), reservation("EXTRA", better)])
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory, now=now_with_cadence(s.account))

    row = await stored_row(store, s)
    assert (row.status, row.booked_raw_id, row.needs_reconcile) == (
        RowStatus.BOOKED,
        "EXTRA",
        False,
    )
    assert row.booked_confirmation == f"{MANAGED_BOOKING_TAG}EXTRA"
    ledger = {
        e.raw_reservation_id: e.state
        for e in await store.list_owned_bookings(s.account.id, target_date=TARGET)
    }
    assert ledger["EXTRA"] is BookingState.HELD
    assert ledger["ROW"] is BookingState.CANCELLED_EXTRA
    assert report.reconcile_flagged == ()


async def test_watch_reconcile_never_cancels_a_manual_second_booking() -> None:
    store = new_store()
    s = await seed(store, n=1)
    mine, manual = slot(7, 30), slot(8, 0)
    await book_row(store, s, raw_id="MINE", tee=mine)
    fake = WatchFake(slots=[])
    fake.set_existing_reservations([reservation("MINE", mine), reservation("MANUAL", manual)])
    factory = FakeFactory(adapters={s.account.id: fake})

    await _watch(store, factory, now=now_with_cadence(s.account))

    assert fake.cancel_call_count == 0


# --- orphans (§7.6, MU-5 SF4) -----------------------------------------------------------------------


async def test_watch_reports_ledger_only_orphans() -> None:
    store = new_store()
    s = await seed(store, n=1)
    with pytest.raises(ExceptionGroup):
        await store.record_outcomes(
            [
                RowOutcome(
                    row_id=s.row.id,
                    course_account_id=s.account.id,
                    target_date=TARGET,
                    actor=Actor.BOOKING_RUNNER,
                    to_status=RowStatus.BOOKED,
                    last_outcome="booked",
                    at=WATCH_NOW - timedelta(hours=1),
                    booking=ledger_entry(s.row, "LOST-1", HELD_EARLY),
                    release_lease_owner=None,
                )
            ]
        )
    factory = FakeFactory(adapters={s.account.id: WatchFake(slots=[])})

    report = await _watch(store, factory)

    assert report.orphans == ("LOST-1",)


# --- §7.8 dry-run ----------------------------------------------------------------------------------


async def test_watch_dry_run_never_cancels_or_upgrades() -> None:
    store = new_store()
    fake = WatchFake()
    s, factory = await _owned_booking(store, fake=fake)

    spy = SpyStore(store)

    report = await _watch(spy, factory, dry_run=True)

    assert "set_upgrade_marker" not in spy.calls  # no upgrade intent is ever recorded
    assert report.logins == 1  # a read-only login still happens
    assert fake.cancel_call_count == 0
    assert fake.book_call_count == 0
    row = await stored_row(store, s)
    assert (row.status, row.booked_raw_id, row.upgrade_started_at) == (
        RowStatus.BOOKED,
        HELD_RAW,
        None,
    )


async def test_dry_run_watcher_never_cancels() -> None:
    """Dry-run: no duplicate-reconcile cancel and no CANCELLED(external) write (§7.8)."""
    store = new_store()
    s = await seed(store, n=1)
    extra, kept = slot(7, 30), slot(8, 0)
    await book_row(
        store,
        s,
        raw_id="KEPT",
        tee=kept,
        extras=(ledger_entry(s.row, "EXTRA", extra, state=BookingState.HELD_EXTRA),),
    )
    fake = WatchFake(slots=[])
    fake.set_existing_reservations([reservation("KEPT", kept), reservation("EXTRA", extra)])
    factory = FakeFactory(adapters={s.account.id: fake})

    await _watch(store, factory, now=now_with_cadence(s.account), dry_run=True)

    assert fake.cancel_call_count == 0

    fake.set_existing_reservations([])  # both gone at the course
    await store.save_snapshot(trusted_snapshot(s, at=WATCH_NOW - timedelta(hours=2), entries=()))
    await _watch(
        store,
        factory,
        now=now_with_cadence(s.account, base=WATCH_NOW + timedelta(hours=1)),
        dry_run=True,
    )

    assert (await stored_row(store, s)).status is RowStatus.BOOKED  # vanish logged, not written
    assert fake.book_call_count == 0
