"""MU-10a: ownership / adoption, the vanish verdict, the upgrade gate and the dry-run gate
(MULTIUSER_PLAN §7.5, §7.6, §7.8). All pure: plain data in, a decision out.
"""

from __future__ import annotations

from datetime import UTC, time, timedelta

import pytest

from teetime.tenant.models import BookingState, RowStatus
from teetime.tenant.watcher import (
    VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS,
    VANISH_MIN_SNAPSHOT_GAP,
    MissingBookingVerdict,
    Ownership,
    WatchAction,
    classify_missing_booking,
    dry_run_gate,
    is_owned,
    ownership_of,
    upgrade_allowed,
)

from .watcher_builders import (
    NEXT_DAY,
    NOW,
    account,
    entry,
    local,
    owned,
    reservation,
    row,
    snapshot,
)

T1 = NOW - timedelta(minutes=30)  # older
T2 = NOW - timedelta(minutes=10)  # newer, >= VANISH_MIN_SNAPSHOT_GAP after T1
HELD = time(9, 30)


def _booked(**kw: object):
    base: dict[str, object] = {
        "status": RowStatus.BOOKED,
        "booked_tee": HELD,
        "booked_raw_id": "R1",
    }
    base.update(kw)
    return row(account(0), **base)  # type: ignore[arg-type]


# --- §7.6 ownership + adoption ---------------------------------------------------------


def test_ledgered_held_or_held_extra_is_owned() -> None:
    r = _booked()
    res = reservation("R1", HELD)
    for state in (BookingState.HELD, BookingState.HELD_EXTRA):
        ledger = [owned(r, "R1", state=state)]
        assert ownership_of(res, row=r, owned=ledger) is Ownership.OWNED
        assert is_owned(res, row=r, owned=ledger) is True


def test_owned_matches_a_ttb_prefixed_code_too() -> None:
    """Defensive: a ``TTB:``-stamped code (the engine's synthesized booking) names the same raw id."""
    r = _booked()
    ledger = [owned(r, "R1")]
    assert ownership_of(reservation("TTB:R1", HELD), row=r, owned=ledger) is Ownership.OWNED


def test_watch_adopt_manual_is_unowned_no_upgrade() -> None:
    """A live reservation with no ledger entry is MANUAL: adopted unowned, never upgraded, never a
    reconcile-cancel candidate. An id we ledgered as CANCELLED is likewise not ours any more."""
    r = _booked(booked_raw_id="MANUAL")
    res = reservation("MANUAL", HELD)

    assert ownership_of(res, row=r, owned=[]) is Ownership.UNOWNED
    assert is_owned(res, row=r, owned=[]) is False
    assert upgrade_allowed(r, res, owned=[]) is False
    for state in (
        BookingState.CANCELLED_EXTRA,
        BookingState.CANCELLED_UPGRADE,
        BookingState.CANCELLED_USER,
        BookingState.VANISHED,
    ):
        ledger = [owned(r, "MANUAL", state=state)]
        assert ownership_of(res, row=r, owned=ledger) is Ownership.UNOWNED
        assert upgrade_allowed(r, res, owned=ledger) is False
    # Another id's ledger entry says nothing about this reservation.
    assert ownership_of(res, row=r, owned=[owned(r, "R-other")]) is Ownership.UNOWNED


def test_needs_reconcile_adopts_on_exact_recorded_slot_only() -> None:
    """§7.6 residual (tightened): adoption claims ownership only on an EXACT tee-time match with
    a recorded UNCERTAIN slot — merely "in window" is NOT enough."""
    r = row(account(0), needs_reconcile=True)  # pending + needs_reconcile
    uncertain = (local(r.target_date, time(9, 20)),)
    exact = reservation("NEW", time(9, 20))
    in_window_only = reservation("NEW", time(9, 30))

    assert (
        ownership_of(exact, row=r, owned=[], uncertain_tee_times=uncertain)
        is Ownership.ADOPTED_RECONCILE
    )
    assert is_owned(exact, row=r, owned=[], uncertain_tee_times=uncertain) is True
    assert (
        ownership_of(in_window_only, row=r, owned=[], uncertain_tee_times=uncertain)
        is Ownership.UNOWNED
    )
    # Same wall-clock, different party: not the slot we POSTed.
    other_party = reservation("NEW", time(9, 20), party_size=2)
    assert (
        ownership_of(other_party, row=r, owned=[], uncertain_tee_times=uncertain)
        is Ownership.UNOWNED
    )
    # Without the flag an exact match is still manual (the flag is the crash evidence).
    calm = row(account(0), needs_reconcile=False)
    assert (
        ownership_of(exact, row=calm, owned=[], uncertain_tee_times=uncertain) is Ownership.UNOWNED
    )
    # No recorded slots passed -> fail-safe: unowned.
    assert ownership_of(exact, row=r, owned=[]) is Ownership.UNOWNED


def test_exact_match_compares_instants_not_wall_clock_strings() -> None:
    """A UTC-stored uncertain instant matches a course-local reservation at the same instant."""
    r = row(account(0), needs_reconcile=True)
    instant_local = local(r.target_date, time(9, 20))
    uncertain_utc = (instant_local.astimezone(UTC),)
    assert (
        ownership_of(
            reservation("NEW", time(9, 20)), row=r, owned=[], uncertain_tee_times=uncertain_utc
        )
        is Ownership.ADOPTED_RECONCILE
    )


# --- the upgrade gate MU-10 MUST apply (E5 does not) --------------------------------------


def test_watch_upgrade_gated_on_ownership() -> None:
    r = _booked()
    res = reservation("R1", HELD)
    assert upgrade_allowed(r, res, owned=[owned(r, "R1")]) is True
    assert upgrade_allowed(r, res, owned=[owned(r, "R1", state=BookingState.HELD_EXTRA)]) is True
    assert upgrade_allowed(r, res, owned=[]) is False
    # needs_reconcile + exact recorded slot: owned by adoption, hence upgradable.
    rec = _booked(needs_reconcile=True)
    assert upgrade_allowed(rec, res, owned=[], uncertain_tee_times=(res.tee_time,)) is True
    assert (
        upgrade_allowed(
            rec, res, owned=[], uncertain_tee_times=(local(rec.target_date, time(9, 40)),)
        )
        is False
    )
    # Only a BOOKED row has anything to upgrade.
    pending = row(account(0))
    assert upgrade_allowed(pending, res, owned=[owned(pending, "R1")]) is False


# --- §7.5 vanish inference ----------------------------------------------------------------


def _miss(at, *, trusted: bool = True, entries=()):
    return snapshot(account(0), at=at, trusted=trusted, entries=entries)


def test_vanish_constants() -> None:
    assert VANISH_CONSECUTIVE_TRUSTED_SNAPSHOTS == 2
    assert timedelta(minutes=10) == VANISH_MIN_SNAPSHOT_GAP


def test_watch_vanish_needs_two_trusted_snapshots() -> None:
    r = _booked()
    present = (entry("R1", HELD),)

    # One trusted miss is not evidence yet.
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2)], owned=[])
        is MissingBookingVerdict.NOT_YET
    )
    # Newest misses but the previous trusted one saw it: not consecutive.
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2), _miss(T1, entries=present)], owned=[])
        is MissingBookingVerdict.NOT_YET
    )
    # Newest still sees it.
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2, entries=present), _miss(T1)], owned=[])
        is MissingBookingVerdict.NOT_YET
    )
    # Two consecutive trusted misses >= 10 min apart: the expected common case.
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2), _miss(T1)], owned=[])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )
    # Order of the input sequence does not matter (sorted by observed_at internally).
    assert (
        classify_missing_booking(r, snapshots=[_miss(T1), _miss(T2)], owned=[])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )
    # Two trusted misses too close together are ONE observation, not two.
    close = [_miss(T2), _miss(T2 - VANISH_MIN_SNAPSHOT_GAP + timedelta(seconds=1))]
    assert classify_missing_booking(r, snapshots=close, owned=[]) is MissingBookingVerdict.NOT_YET
    exact_gap = [_miss(T2), _miss(T2 - VANISH_MIN_SNAPSHOT_GAP)]
    assert (
        classify_missing_booking(r, snapshots=exact_gap, owned=[])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )


def test_watch_soft_auth_not_persisted() -> None:
    """An UNTRUSTED snapshot (soft login / non-JSON / no list — the E6 flag) never counts: it is
    neither a miss nor a sighting. Only the trusted subsequence feeds the vanish rule."""
    r = _booked()
    present = (entry("R1", HELD),)

    # Untrusted newest + one trusted miss: still only one trusted miss.
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2, trusted=False), _miss(T1)], owned=[])
        is MissingBookingVerdict.NOT_YET
    )
    # Two untrusted misses: nothing.
    untrusted = [_miss(T2, trusted=False), _miss(T1, trusted=False)]
    assert (
        classify_missing_booking(r, snapshots=untrusted, owned=[]) is MissingBookingVerdict.NOT_YET
    )
    # An untrusted snapshot that "sees" the reservation is not a sighting either: the two
    # trusted misses around it are consecutive AMONG TRUSTED snapshots.
    sandwiched = [
        _miss(T2),
        _miss(T2 - timedelta(minutes=5), trusted=False, entries=present),
        _miss(T1),
    ]
    assert (
        classify_missing_booking(r, snapshots=sandwiched, owned=[])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )


def test_watch_vanish_excluded_when_upgrade_started() -> None:
    """M2: the intent marker survives a crash mid-upgrade; a missing reservation is then
    bot-caused (-> pending + needs_reconcile), never external."""
    r = _booked(upgrade_started_at=NOW - timedelta(minutes=15))
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2), _miss(T1)], owned=[])
        is MissingBookingVerdict.BOT_CAUSED
    )


def test_watch_vanish_excluded_when_upgrade_marker_set() -> None:
    """The marker wins even when a replacement exists: the upgrade's rebook may be that
    replacement, and the reconcile (not the vanish path) decides."""
    r = _booked(upgrade_started_at=NOW - timedelta(minutes=15))
    replacement = (entry("R2", time(9, 20)),)
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2, entries=replacement), _miss(T1)], owned=[])
        is MissingBookingVerdict.BOT_CAUSED
    )


def test_watch_vanish_excluded_when_ledgered_cancelled_by_us() -> None:
    r = _booked()
    misses = [_miss(T2), _miss(T1)]
    for state in (BookingState.CANCELLED_UPGRADE, BookingState.CANCELLED_EXTRA):
        ledger = [owned(r, "R1", state=state)]
        assert (
            classify_missing_booking(r, snapshots=misses, owned=ledger)
            is MissingBookingVerdict.BOT_CAUSED
        ), state


def test_watch_vanish_excluded_for_ledgered_cancel() -> None:
    """Only a cancel WE made excludes: a held ledger entry (the normal case) or a cancel of some
    OTHER id leaves the verdict external."""
    r = _booked()
    misses = [_miss(T2), _miss(T1)]
    assert (
        classify_missing_booking(r, snapshots=misses, owned=[owned(r, "R1")])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )
    other = [owned(r, "R-other", state=BookingState.CANCELLED_EXTRA)]
    assert (
        classify_missing_booking(r, snapshots=misses, owned=other)
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )


def test_watch_vanish_not_excluded_by_cancelled_user_ledger() -> None:
    """Pins the deliberate reading of §7.5 (MU-10a note): a same-raw-id ``cancelled_user`` ledger
    entry does NOT exclude vanish. With no upgrade marker and no cancelled_upgrade/extra entry the
    verdict stays EXTERNAL_CANCEL (never re-book), not BOT_CAUSED (which would re-book what the
    user just cancelled). A BOOKED row cannot carry such an entry in a consistent store; if it
    ever does, this is the safe direction."""
    r = _booked()
    misses = [_miss(T2), _miss(T1)]
    ledger = [owned(r, "R1", state=BookingState.CANCELLED_USER)]
    assert (
        classify_missing_booking(r, snapshots=misses, owned=ledger)
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )
    # Alongside a held entry for the same id (the ordinary ledger shape), still external.
    both = [owned(r, "R1"), owned(r, "R1", state=BookingState.CANCELLED_USER)]
    assert (
        classify_missing_booking(r, snapshots=misses, owned=both)
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )


def test_watch_replacement_is_adopted_not_external_cancel() -> None:
    """A reservation for the SAME (date, party) under another id replaced ours: adopt it (owned
    iff ledgered — the caller's ownership_of decides), never read as an external cancel."""
    r = _booked()
    replacement = (entry("R2", time(9, 20)),)
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2, entries=replacement), _miss(T1)], owned=[])
        is MissingBookingVerdict.ADOPT_REPLACEMENT
    )


def test_watch_replacement_reservation_adopted_not_external() -> None:
    """Only a same-(date, party) reservation is a replacement; the NEWEST trusted snapshot is the
    one consulted."""
    r = _booked()
    other_day = (entry("R2", time(9, 20), day=NEXT_DAY),)
    other_party = (entry("R2", time(9, 20), party_size=2),)
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2, entries=other_day), _miss(T1)], owned=[])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2, entries=other_party), _miss(T1)], owned=[])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )
    # A replacement seen only in the OLDER trusted snapshot is gone too.
    stale_replacement = [_miss(T2), _miss(T1, entries=(entry("R2", time(9, 20)),))]
    assert (
        classify_missing_booking(r, snapshots=stale_replacement, owned=[])
        is MissingBookingVerdict.EXTERNAL_CANCEL
    )


def test_replacement_date_is_read_in_the_course_timezone() -> None:
    """A late-evening course-local reservation is on TARGET even though its UTC date is the next
    day: the (date, party) match must use the row's timezone."""
    r = _booked()
    late = (entry("R2", time(22, 0)),)  # 22:00 EDT = 02:00 UTC next day
    assert (
        classify_missing_booking(r, snapshots=[_miss(T2, entries=late), _miss(T1)], owned=[])
        is MissingBookingVerdict.ADOPT_REPLACEMENT
    )


def test_classify_preconditions() -> None:
    pending = row(account(0))
    with pytest.raises(ValueError, match="BOOKED"):
        classify_missing_booking(pending, snapshots=[_miss(T2), _miss(T1)], owned=[])
    # §4.6 defensive case: booked with no raw id is resolved by adoption, not vanish inference.
    no_id = _booked(booked_raw_id=None)
    assert (
        classify_missing_booking(no_id, snapshots=[_miss(T2), _miss(T1)], owned=[])
        is MissingBookingVerdict.NOT_YET
    )
    other = snapshot(account(1), at=T2)
    with pytest.raises(ValueError, match="account"):
        classify_missing_booking(_booked(), snapshots=[other, _miss(T1)], owned=[])


# --- §7.8 dry-run gate ----------------------------------------------------------------------


def test_watch_dry_run_never_cancels_or_upgrades() -> None:
    forbidden = {
        WatchAction.RECONCILE_CANCEL,
        WatchAction.UPGRADE,
        WatchAction.MARK_CANCELLED_EXTERNAL,
    }
    for action in WatchAction:
        assert dry_run_gate(dry_run=True, action=action) is (action not in forbidden), action
        assert dry_run_gate(dry_run=False, action=action) is True, action
    # Read-only login, snapshot persistence and every DB-only action stay enabled in dry-run.
    for action in (WatchAction.LOGIN, WatchAction.PERSIST_SNAPSHOT, WatchAction.ADOPT):
        assert dry_run_gate(dry_run=True, action=action) is True
