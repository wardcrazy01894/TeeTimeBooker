"""MU-16b (MULTIUSER_PLAN §11 steps 6-7): adopt the operator's TOML-era reservations as OWNED.

At the prod cutover the TOML bot already holds next weekend's tee times. Without adoption the
tenant watcher would adopt them UNOWNED (safe, but it never upgrades an unowned booking). The seed
records them ``adopted_owned`` from a TRUSTED persisted snapshot, after the operator confirms.
Real ``InMemoryTenantStore``; nothing mocked.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta

from teetime.core.clock import FakeClock
from teetime.tenant.models import BookingSource, BookingState, RankedWindow, RowStatus
from teetime.tenant.seed import AdoptionKind, apply_adoptions, plan_adoptions

from .runner_builders import TARGET, slot
from .watch_runner_builders import (
    WATCH_NOW,
    book_row,
    ledger_entry,
    new_store,
    seed,
    stored_row,
    trusted_snapshot,
)

IN_WINDOW = slot(8, 30)
OUT_OF_WINDOW = slot(11, 0)


async def test_a_pending_row_with_a_matching_reservation_is_planned_as_booked() -> None:
    store = new_store()
    s = await seed(store, n=1)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", IN_WINDOW),))
    (plan,) = plan_adoptions([s.row], snap, owned=[])
    assert (plan.row.id, plan.raw_id, plan.kind) == (s.row.id, "R1", AdoptionKind.BOOK)


async def test_nothing_is_planned_from_an_untrusted_snapshot() -> None:
    store = new_store()
    s = await seed(store, n=1)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", IN_WINDOW),))
    untrusted = replace(snap, trusted=False)
    assert plan_adoptions([s.row], untrusted, owned=[]) == []


async def test_a_reservation_outside_every_window_or_party_is_not_adopted() -> None:
    store = new_store()
    s = await seed(store, n=1)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", OUT_OF_WINDOW),))
    assert plan_adoptions([s.row], snap, owned=[]) == []


async def test_the_best_ranked_option_wins_when_two_reservations_match() -> None:
    store = new_store()
    s = await seed(
        store,
        n=1,
        options=(
            RankedWindow(1, time(9, 0), time(9, 30)),
            RankedWindow(2, time(7, 0), time(8, 59)),
        ),
    )
    snap = trusted_snapshot(
        s, at=WATCH_NOW, entries=(("EARLY", slot(7, 30)), ("RANK1", slot(9, 0)))
    )
    (plan,) = plan_adoptions([s.row], snap, owned=[])
    assert plan.raw_id == "RANK1"


async def test_a_booked_unowned_row_gets_a_ledger_entry_only() -> None:
    store = new_store()
    s = await seed(store, n=1)
    row = await book_row(store, s, raw_id="R1", tee=IN_WINDOW, owned=False)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", IN_WINDOW),))
    (plan,) = plan_adoptions([row], snap, owned=[])
    assert (plan.raw_id, plan.kind) == ("R1", AdoptionKind.LEDGER)


async def test_a_booked_owned_row_needs_nothing() -> None:
    store = new_store()
    s = await seed(store, n=1)
    row = await book_row(store, s, raw_id="R1", tee=IN_WINDOW, owned=True)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", IN_WINDOW),))
    owned = [ledger_entry(row, "R1", IN_WINDOW)]
    assert plan_adoptions([row], snap, owned=owned) == []


async def test_a_booked_row_whose_reservation_was_replaced_is_repointed() -> None:
    """SF7: the TOML watcher upgraded between the first seed and the flip. The old id is gone,
    a new in-window reservation exists for the same date and party: re-point to it."""
    store = new_store()
    s = await seed(store, n=1)
    row = await book_row(store, s, raw_id="OLD", tee=slot(7, 30), owned=True)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("NEW", IN_WINDOW),))
    owned = [ledger_entry(row, "OLD", slot(7, 30))]
    (plan,) = plan_adoptions([row], snap, owned=owned)
    assert (plan.raw_id, plan.kind, plan.replaces) == ("NEW", AdoptionKind.REPOINT, "OLD")


async def test_apply_books_the_row_owned_and_ledgers_it_adopted_owned() -> None:
    store = new_store()
    s = await seed(store, n=1)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", IN_WINDOW),))
    plans = plan_adoptions([s.row], snap, owned=[])
    report = await apply_adoptions(store, plans, clock=FakeClock(start=WATCH_NOW))
    assert report.adopted == (s.row.id,) and report.failed == ()
    row = await stored_row(store, s)
    assert (row.status, row.booked_raw_id) == (RowStatus.BOOKED, "R1")
    (entry,) = await store.list_owned_bookings(s.account.id, target_date=TARGET)
    assert (entry.raw_reservation_id, entry.source, entry.state) == (
        "R1",
        BookingSource.ADOPTED_OWNED,
        BookingState.HELD,
    )


async def test_apply_ledger_only_makes_an_unowned_booking_owned() -> None:
    store = new_store()
    s = await seed(store, n=1)
    row = await book_row(store, s, raw_id="R1", tee=IN_WINDOW, owned=False)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", IN_WINDOW),))
    report = await apply_adoptions(
        store, plan_adoptions([row], snap, owned=[]), clock=FakeClock(start=WATCH_NOW)
    )
    assert report.adopted == (row.id,)
    (entry,) = await store.list_owned_bookings(s.account.id, target_date=TARGET)
    assert entry.raw_reservation_id == "R1" and entry.source is BookingSource.ADOPTED_OWNED
    assert (await stored_row(store, s)).status is RowStatus.BOOKED


async def test_apply_repoint_moves_the_row_and_retires_the_old_ledger_entry() -> None:
    store = new_store()
    s = await seed(store, n=1)
    row = await book_row(store, s, raw_id="OLD", tee=slot(7, 30), owned=True)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("NEW", IN_WINDOW),))
    owned = await store.list_owned_bookings(s.account.id, target_date=TARGET)
    report = await apply_adoptions(
        store, plan_adoptions([row], snap, owned=owned), clock=FakeClock(start=WATCH_NOW)
    )
    assert report.adopted == (row.id,)
    assert (await stored_row(store, s)).booked_raw_id == "NEW"
    states = {
        e.raw_reservation_id: e.state
        for e in await store.list_owned_bookings(s.account.id, target_date=TARGET)
    }
    assert states["NEW"] is BookingState.HELD
    assert states["OLD"] is not BookingState.HELD


async def test_a_row_that_moved_since_the_plan_is_skipped_not_overwritten() -> None:
    """The fingerprint taken at plan time guards the write (M5): a row the watcher booked in
    between is left alone and reported."""
    store = new_store()
    s = await seed(store, n=1)
    snap = trusted_snapshot(s, at=WATCH_NOW, entries=(("R1", IN_WINDOW),))
    plans = plan_adoptions([s.row], snap, owned=[])
    await book_row(store, s, raw_id="OTHER", tee=slot(8, 0), owned=True)
    report = await apply_adoptions(
        store, plans, clock=FakeClock(start=WATCH_NOW + timedelta(minutes=1))
    )
    assert report.adopted == () and report.failed == (s.row.id,)
    assert (await stored_row(store, s)).booked_raw_id == "OTHER"
