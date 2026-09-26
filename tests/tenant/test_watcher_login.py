"""MU-10a: the tenant watcher's PURE grouping + login decisions (MULTIUSER_PLAN §7.1-§7.3).

No I/O, no store, no adapter: ``group_rows_for_search`` and ``needs_login`` take plain data.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import datetime, time, timedelta
from uuid import uuid4

import pytest

from teetime.core.models import TeeTimeSlot
from teetime.tenant.models import BookingState, RankedWindow, RowStatus
from teetime.tenant.watcher import (
    MAX_BOOKED_SNAPSHOT_AGE_S,
    RECONCILE_EVERY_N_RUNS,
    LoginReason,
    SearchGroupKey,
    group_rows_for_search,
    needs_login,
)

from .watcher_builders import (
    MB,
    NEXT_DAY,
    NOW,
    OTHER_COURSE,
    TARGET,
    ZONE,
    CourseAccountId,
    account,
    event,
    owned,
    slot,
    snapshot,
)

# A run index at which NO builder account is on its cadence turn (asserted by a fixture below),
# so the other reasons can be tested in isolation.
FRESH = snapshot(account(0), at=NOW - timedelta(minutes=5))


def _off_cadence_run_index(*accounts_n: int) -> int:
    """The smallest run_index at which none of the given accounts is on its cadence turn."""
    for run_index in range(10_000):
        if all((account(n).id.int + run_index) % RECONCILE_EVERY_N_RUNS != 0 for n in accounts_n):
            return run_index
    raise AssertionError("no off-cadence run index found")


# --- §7.2 grouping -----------------------------------------------------------------------


def test_watch_groups_by_course_date_party() -> None:
    a, b, c = account(0), account(1), account(2)
    rows = [
        event(a, target=TARGET, party_size=4),
        event(b, target=TARGET, party_size=4),
        event(c, target=NEXT_DAY, party_size=4),
        event(account(3, course=OTHER_COURSE), target=TARGET, party_size=4),
        event(a, target=TARGET, party_size=4, status=RowStatus.BOOKED),
    ]

    groups = group_rows_for_search(rows)

    assert set(groups) == {
        SearchGroupKey(MB, TARGET, 4),
        SearchGroupKey(MB, NEXT_DAY, 4),
        SearchGroupKey(OTHER_COURSE, TARGET, 4),
    }
    same = groups[SearchGroupKey(MB, TARGET, 4)]
    # Input order is preserved inside a group (the runner's per-row loop is deterministic), and
    # BOOKED rows share the group with PENDING ones (one search serves both).
    assert same == [rows[0], rows[1], rows[4]]
    assert groups[SearchGroupKey(MB, NEXT_DAY, 4)] == [rows[2]]
    assert groups[SearchGroupKey(OTHER_COURSE, TARGET, 4)] == [rows[3]]


def test_party_size_grouping_respects_mb_subset_quirk() -> None:
    """MB ``/times?players=4`` returns a SUBSET of ``players=2`` (courses/CLAUDE.md), so a party-2
    and a party-4 row on the same date are DIFFERENT groups — one shared search per party."""
    two = event(account(0), party_size=2)
    four = event(account(1), party_size=4)

    groups = group_rows_for_search([two, four])

    assert groups == {
        SearchGroupKey(MB, TARGET, 2): [two],
        SearchGroupKey(MB, TARGET, 4): [four],
    }


def test_group_rows_empty_input() -> None:
    assert group_rows_for_search([]) == {}


# --- §7.1 step 3: needs_login ------------------------------------------------------------


def test_watch_no_login_without_opportunity() -> None:
    run_index = _off_cadence_run_index(0)
    pending = event(account(0))

    # No slots at all.
    assert (
        needs_login(pending, group_slots=[], snapshot=FRESH, run_index=run_index, now=NOW) is None
    )
    # Inventory, but nothing inside THIS row's window (the group search used the union).
    out_of_window = [slot(time(7, 0)), slot(time(11, 30))]
    assert (
        needs_login(
            pending, group_slots=out_of_window, snapshot=FRESH, run_index=run_index, now=NOW
        )
        is None
    )
    # In-window but not bookable for this party (fewer open spots than players).
    too_small = [slot(time(9, 20), spots=2)]
    assert (
        needs_login(pending, group_slots=too_small, snapshot=FRESH, run_index=run_index, now=NOW)
        is None
    )


def test_pending_row_with_bookable_in_window_slot_logs_in() -> None:
    run_index = _off_cadence_run_index(0)
    pending = event(account(0))

    reason = needs_login(
        pending, group_slots=[slot(time(9, 20))], snapshot=FRESH, run_index=run_index, now=NOW
    )

    assert reason is LoginReason.BOOKABLE_SLOT


def test_bookable_slot_outranks_cadence_as_the_reason() -> None:
    """Reasons are reported in §7.1 priority order: an opportunity beats a cadence turn."""
    acct = account(0)
    on_cadence = next(
        i for i in range(RECONCILE_EVERY_N_RUNS) if (acct.id.int + i) % RECONCILE_EVERY_N_RUNS == 0
    )
    reason = needs_login(
        event(acct), group_slots=[slot(time(9, 20))], snapshot=FRESH, run_index=on_cadence, now=NOW
    )
    assert reason is LoginReason.BOOKABLE_SLOT


def test_booked_row_ignores_bookable_slots_unless_strictly_better() -> None:
    """A BOOKED row never re-books; only an UPGRADE candidate (strictly closer to the window
    midpoint, the ``UpgradeOrchestrator`` within-window rule) is an opportunity."""
    run_index = _off_cadence_run_index(0)
    acct = account(0)
    booked = event(acct, status=RowStatus.BOOKED, booked_tee=time(9, 30), booked_raw_id="R1")
    ledger = [owned(booked.row, "R1", tee=time(9, 30))]
    # Midpoint 09:22:30: held 09:30 is 7.5 min away. 09:40 is worse, 09:15 ties, 09:20 is better.
    worse = [slot(time(9, 40))]
    tie = [slot(time(9, 15))]
    better = [slot(time(9, 20))]

    assert (
        needs_login(
            booked, group_slots=worse, snapshot=FRESH, run_index=run_index, now=NOW, owned=ledger
        )
        is None
    )
    assert (
        needs_login(
            booked, group_slots=tie, snapshot=FRESH, run_index=run_index, now=NOW, owned=ledger
        )
        is None
    )
    assert (
        needs_login(
            booked, group_slots=better, snapshot=FRESH, run_index=run_index, now=NOW, owned=ledger
        )
        is LoginReason.UPGRADE_CANDIDATE
    )


def test_unowned_booked_row_is_never_an_upgrade_candidate() -> None:
    """§7.6: a manual (unowned) reservation is never upgraded, so a better slot is no reason to
    log in for it. Ownership = the row's raw id ledgered held / held_extra."""
    run_index = _off_cadence_run_index(0)
    acct = account(0)
    booked = event(acct, status=RowStatus.BOOKED, booked_tee=time(9, 30), booked_raw_id="MANUAL")
    better = [slot(time(9, 20))]

    assert (
        needs_login(booked, group_slots=better, snapshot=FRESH, run_index=run_index, now=NOW)
        is None
    )
    # A ledger entry for a DIFFERENT id, or for this id but cancelled, does not confer ownership.
    other = [owned(booked.row, "R-other")]
    cancelled = [owned(booked.row, "MANUAL", state=BookingState.CANCELLED_EXTRA)]
    for ledger in (other, cancelled):
        assert (
            needs_login(
                booked,
                group_slots=better,
                snapshot=FRESH,
                run_index=run_index,
                now=NOW,
                owned=ledger,
            )
            is None
        )
    held_extra = [owned(booked.row, "MANUAL", state=BookingState.HELD_EXTRA)]
    assert (
        needs_login(
            booked,
            group_slots=better,
            snapshot=FRESH,
            run_index=run_index,
            now=NOW,
            owned=held_extra,
        )
        is LoginReason.UPGRADE_CANDIDATE
    )


def test_needs_reconcile_row_logs_in() -> None:
    run_index = _off_cadence_run_index(0)
    pending = event(account(0), needs_reconcile=True)
    assert (
        needs_login(pending, group_slots=[], snapshot=FRESH, run_index=run_index, now=NOW)
        is LoginReason.NEEDS_RECONCILE
    )


def test_stale_booker_lease_logs_in_but_live_lease_does_not() -> None:
    run_index = _off_cadence_run_index(0)
    acct = account(0)
    stale = event(acct, lease=("booker:evt-1", NOW - timedelta(seconds=1)))
    live = event(acct, lease=("booker:evt-1", NOW + timedelta(seconds=600)))

    assert (
        needs_login(stale, group_slots=[], snapshot=FRESH, run_index=run_index, now=NOW)
        is LoginReason.STALE_BOOKER_LEASE
    )
    # An UNEXPIRED lease means another process is acting: no reason to log in for it.
    assert needs_login(live, group_slots=[], snapshot=FRESH, run_index=run_index, now=NOW) is None


def test_watch_reconcile_cadence_spreads_accounts() -> None:
    """Over N consecutive runs each account gets exactly ONE cadence turn, and at any one run
    only the accounts with ``(account_id.int + run_index) % N == 0`` log in."""
    n = RECONCILE_EVERY_N_RUNS
    accounts = [account(i) for i in range(24)]
    rows = {
        a.id: event(a, status=RowStatus.BOOKED, booked_raw_id=f"R{i}")
        for i, a in enumerate(accounts)
    }
    base = 1_234_560  # any run index; the cadence is relative

    turns = {a.id: 0 for a in accounts}
    for run_index in range(base, base + n):
        for a in accounts:
            reason = needs_login(
                rows[a.id],
                group_slots=[],
                snapshot=snapshot(a, at=NOW - timedelta(minutes=5)),
                run_index=run_index,
                now=NOW,
            )
            expected = (a.id.int + run_index) % n == 0
            assert (reason is LoginReason.CADENCE) is expected, (a.id, run_index)
            turns[a.id] += reason is LoginReason.CADENCE
    assert all(count == 1 for count in turns.values()), turns


def test_watch_cadence_uses_uuid_int_not_hash() -> None:
    """The turn is a pure function of ``account_id.int`` (stable across processes). Python's
    ``hash()`` of a str/UUID is salted per process; an implementation using it would disagree
    with the UUID-int formula for ~(N-1)/N of random ids."""
    n = RECONCILE_EVERY_N_RUNS
    run_index = 77
    for _ in range(60):
        acct_id = uuid4()
        acct = dc_replace(account(0), id=CourseAccountId(acct_id))
        pending = event(acct)
        reason = needs_login(
            pending, group_slots=[], snapshot=None, run_index=run_index, now=NOW, cadence=n
        )
        assert (reason is LoginReason.CADENCE) is ((acct_id.int + run_index) % n == 0)


def test_cadence_parameter_overrides_default() -> None:
    acct = account(0)
    pending = event(acct)
    # cadence=1: every run is a turn.
    assert (
        needs_login(pending, group_slots=[], snapshot=FRESH, run_index=5, now=NOW, cadence=1)
        is LoginReason.CADENCE
    )
    with pytest.raises(ValueError, match="cadence"):
        needs_login(pending, group_slots=[], snapshot=FRESH, run_index=5, now=NOW, cadence=0)


def test_stale_snapshot_backstop_applies_to_booked_rows_only() -> None:
    run_index = _off_cadence_run_index(0)
    acct = account(0)
    booked = event(acct, status=RowStatus.BOOKED, booked_raw_id="R1")
    pending = event(acct)
    old = snapshot(acct, at=NOW - timedelta(seconds=MAX_BOOKED_SNAPSHOT_AGE_S + 1))
    fresh_enough = snapshot(acct, at=NOW - timedelta(seconds=MAX_BOOKED_SNAPSHOT_AGE_S))

    assert (
        needs_login(booked, group_slots=[], snapshot=None, run_index=run_index, now=NOW)
        is LoginReason.STALE_SNAPSHOT
    )
    assert (
        needs_login(booked, group_slots=[], snapshot=old, run_index=run_index, now=NOW)
        is LoginReason.STALE_SNAPSHOT
    )
    assert (
        needs_login(booked, group_slots=[], snapshot=fresh_enough, run_index=run_index, now=NOW)
        is None
    )
    # A PENDING row has nothing to re-list: no backstop login.
    assert needs_login(pending, group_slots=[], snapshot=None, run_index=run_index, now=NOW) is None


def test_needs_login_rejects_a_snapshot_of_another_account() -> None:
    other = snapshot(account(1), at=NOW)
    with pytest.raises(ValueError, match="account"):
        needs_login(event(account(0)), group_slots=[], snapshot=other, run_index=0, now=NOW)


def test_needs_login_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="aware"):
        needs_login(
            event(account(0)),
            group_slots=[],
            snapshot=FRESH,
            run_index=0,
            now=datetime(2026, 9, 25, 12, 0),  # naive
        )


def test_now_utc_vs_local_agree() -> None:
    """The decision is instant-based: the same instant in UTC or course-local gives one answer."""
    run_index = _off_cadence_run_index(0)
    acct = account(0)
    booked = event(acct, status=RowStatus.BOOKED, booked_raw_id="R1")
    old = snapshot(acct, at=NOW - timedelta(minutes=100))
    now_local = NOW.astimezone(ZONE)
    assert (
        needs_login(booked, group_slots=[], snapshot=old, run_index=run_index, now=NOW)
        is needs_login(booked, group_slots=[], snapshot=old, run_index=run_index, now=now_local)
        is LoginReason.STALE_SNAPSHOT
    )


def test_upgrade_candidate_honours_option_rank() -> None:
    """§16.2 (MU-R1): with several options at one course, a slot in a BETTER-ranked option is an
    upgrade even when it is farther from its own window's midpoint (the UpgradeOrchestrator
    higher-tier leg); within the SAME option only a strictly closer slot is; a slot in a worse
    option never is."""
    run_index = _off_cadence_run_index(0)
    acct = account(0)
    opts = (RankedWindow(1, time(9, 0), time(10, 0)), RankedWindow(3, time(7, 0), time(8, 0)))
    booked = event(
        acct,
        status=RowStatus.BOOKED,
        booked_tee=time(7, 30),  # dead centre of the rank-3 option
        booked_raw_id="R1",
        options=opts,
    )
    ledger = [owned(booked.row, "R1")]

    def reason(slots: list[TeeTimeSlot]) -> LoginReason | None:
        return needs_login(
            booked, group_slots=slots, snapshot=FRESH, run_index=run_index, now=NOW, owned=ledger
        )

    assert reason([slot(time(9, 0))]) is LoginReason.UPGRADE_CANDIDATE  # better tier, edge
    assert reason([slot(time(7, 5))]) is None  # same tier, farther from its midpoint
    booked_edge = event(
        acct, status=RowStatus.BOOKED, booked_tee=time(9, 55), booked_raw_id="R1", options=opts
    )
    ledger_edge = [owned(booked_edge.row, "R1")]
    assert (
        needs_login(
            booked_edge,
            group_slots=[slot(time(7, 30))],  # worse tier, even though centred
            snapshot=FRESH,
            run_index=run_index,
            now=NOW,
            owned=ledger_edge,
        )
        is None
    )
