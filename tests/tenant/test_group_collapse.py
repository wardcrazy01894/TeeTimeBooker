"""MU-R2 part 2: ``tenant.groups.collapse_group``, the §16.4 collapse EXECUTOR (store + course
calls). The pure keep/cancel decision is ``plan_collapse`` (test_groups.py); this pins the
ordering the plan relies on: lease -> upgrade marker -> cancel -> ONE outcome write."""

from __future__ import annotations

from datetime import datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

from teetime.core.adapter import CancelError
from teetime.core.clock import FakeClock
from teetime.core.models import BookingOutcome, BookingResult
from teetime.tenant.groups import collapse_group
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    GROUP_DOWNGRADE_REASON,
    Actor,
    BookingSource,
    BookingState,
    OwnedBooking,
    OwnedBookingId,
    RankedWindow,
    RequestRow,
    RowStatus,
)
from teetime.tenant.store import RowOutcome

from .runner_builders import SETUP_NOW, TARGET, TZ, new_store, seed_account

BOOKER = "booker-setup"
A_OPTS = (RankedWindow(1, time(9), time(10)), RankedWindow(3, time(8), time(9)))
B_OPTS = (RankedWindow(2, time(9), time(10)),)


class CancelSpy:
    """Only ``cancel_reservation`` is exercised by the executor."""

    def __init__(self, *, fail: bool = False) -> None:
        self.cancelled: list[str] = []
        self.fail = fail

    async def cancel_reservation(self, confirmation_code: str) -> None:
        if self.fail:
            raise CancelError("course said no")
        self.cancelled.append(confirmation_code)


async def _book(
    store: InMemoryTenantStore, row: RequestRow, raw: str, tee: time, *, owned: bool = True
) -> RequestRow:
    claimed = await store.claim_rows(
        [row.id], owner=BOOKER, until=SETUP_NOW.replace(hour=23), now=SETUP_NOW
    )
    assert claimed == frozenset({row.id})
    local = datetime.combine(row.target_date, tee, tzinfo=ZoneInfo(TZ))
    booking = OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=row.course_account_id,
        course_id=row.course_id,
        target_date=row.target_date,
        raw_reservation_id=raw,
        tee_time=local,
        party_size=row.party_size,
        source=BookingSource.BLIND,
        state=BookingState.HELD,
    )
    await store.record_outcomes(
        [
            RowOutcome(
                row_id=row.id,
                course_account_id=row.course_account_id,
                target_date=row.target_date,
                actor=Actor.BOOKING_RUNNER,
                to_status=RowStatus.BOOKED,
                last_outcome="booked",
                at=SETUP_NOW,
                result=BookingResult(
                    request_id=row.request_id,
                    outcome=BookingOutcome.BOOKED,
                    course_id=row.course_id,
                    slot=None,
                    confirmation_code=f"TTB:{raw}",
                    booked_at=SETUP_NOW,
                    attempts=1,
                ),
                booking=booking if owned else None,
                release_lease_owner=BOOKER,
            )
        ]
    )
    (stored,) = [
        r for r in await store.rows_for_account_date(row.course_account_id, row.target_date)
    ]
    return stored


async def _group(
    *, a_owned: bool = True, b_owned: bool = True
) -> tuple[InMemoryTenantStore, RequestRow, RequestRow]:
    store = new_store()
    group = uuid4()
    a = await seed_account(store, n=1, group_id=group, options=A_OPTS)
    b = await seed_account(store, n=2, group_id=group, options=B_OPTS)
    a_row = await _book(store, a.row, "A3", time(8, 30), owned=a_owned)  # rank 3
    b_row = await _book(store, b.row, "B2", time(9, 15), owned=b_owned)  # rank 2
    return store, a_row, b_row


async def test_collapse_cancels_the_worse_owned_booking_and_downgrades_its_row() -> None:
    store, a_row, b_row = await _group()
    spies = {a_row.course_account_id: CancelSpy(), b_row.course_account_id: CancelSpy()}
    report = await collapse_group(
        [a_row, b_row],
        store=store,
        adapters=spies,  # type: ignore[arg-type]
        actor=Actor.BOOKING_RUNNER,
        owner="runner-1",
        clock=FakeClock(start=SETUP_NOW),
    )
    assert report.downgraded == (a_row.id,)
    assert spies[a_row.course_account_id].cancelled == ["A3"]
    assert spies[b_row.course_account_id].cancelled == []
    (after,) = await store.rows_for_account_date(a_row.course_account_id, TARGET)
    assert (after.status, after.status_reason, after.needs_reconcile) == (
        RowStatus.PENDING,
        GROUP_DOWNGRADE_REASON,
        False,
    )
    assert after.lease_owner is None
    assert after.upgrade_started_at is None
    states = {
        b.raw_reservation_id: b.state
        for b in await store.list_owned_bookings(a_row.course_account_id, target_date=TARGET)
    }
    assert states == {"A3": BookingState.CANCELLED_GROUP}


async def test_collapse_never_cancels_a_manual_booking() -> None:
    store, a_row, b_row = await _group(a_owned=False)
    spies = {a_row.course_account_id: CancelSpy(), b_row.course_account_id: CancelSpy()}
    report = await collapse_group(
        [a_row, b_row],
        store=store,
        adapters=spies,  # type: ignore[arg-type]
        actor=Actor.WATCHER,
        owner="watch-1",
        clock=FakeClock(start=SETUP_NOW),
    )
    assert report.downgraded == ()
    assert report.manual == (a_row.id,)
    assert all(s.cancelled == [] for s in spies.values())


async def test_failed_cancel_leaves_the_row_booked_with_the_marker_for_the_next_run() -> None:
    store, a_row, b_row = await _group()
    spies = {a_row.course_account_id: CancelSpy(fail=True), b_row.course_account_id: CancelSpy()}
    report = await collapse_group(
        [a_row, b_row],
        store=store,
        adapters=spies,  # type: ignore[arg-type]
        actor=Actor.WATCHER,
        owner="watch-1",
        clock=FakeClock(start=SETUP_NOW),
    )
    assert report.failed == (a_row.id,)
    (after,) = await store.rows_for_account_date(a_row.course_account_id, TARGET)
    assert after.status is RowStatus.BOOKED
    assert after.upgrade_started_at is not None  # §7.5: a later vanish is BOT_CAUSED
    assert after.lease_owner is None


async def test_collapse_skips_a_row_leased_by_someone_else() -> None:
    store, a_row, b_row = await _group()
    assert await store.acquire_row_lease(
        a_row.id, owner="web:x", until=SETUP_NOW.replace(hour=23), now=SETUP_NOW, expected=None
    )
    spies = {a_row.course_account_id: CancelSpy(), b_row.course_account_id: CancelSpy()}
    report = await collapse_group(
        [a_row, b_row],
        store=store,
        adapters=spies,  # type: ignore[arg-type]
        actor=Actor.WATCHER,
        owner="watch-1",
        clock=FakeClock(start=SETUP_NOW),
    )
    assert report.skipped == (a_row.id,)
    assert spies[a_row.course_account_id].cancelled == []


async def test_collapse_does_nothing_in_dry_run() -> None:
    store, a_row, b_row = await _group()
    spies = {a_row.course_account_id: CancelSpy(), b_row.course_account_id: CancelSpy()}
    report = await collapse_group(
        [a_row, b_row],
        store=store,
        adapters=spies,  # type: ignore[arg-type]
        actor=Actor.WATCHER,
        owner="watch-1",
        clock=FakeClock(start=SETUP_NOW),
        dry_run=True,
    )
    assert report.downgraded == ()
    assert report.skipped == (a_row.id,)
    assert spies[a_row.course_account_id].cancelled == []
