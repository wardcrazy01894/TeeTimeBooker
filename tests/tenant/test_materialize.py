"""MU-6: the standing-rule materializer (MULTIUSER_PLAN §7.7, §3.4) against the reference
``InMemoryTenantStore``. Every §12 MU-6 named test lives here, plus one test per ``DateAction``
value (pure ``classify_date_history``), the deactivation CALL ORDER (a recording wrapper around
the Protocol) and the tick's sweep / no-op / per-rule isolation.

Fixed clock: ``NOW`` = Fri 2026-09-25 12:00Z (08:00 EDT). With ``POLICY`` (advance 7, so a
21-day horizon) course-local today is 9/25 and the horizon end is 10/16, so a Saturday rule
covers exactly ``SATURDAYS`` = 9/26, 10/3 (``TARGET``), 10/10.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from teetime.core.booking_cutoff import cutoff_instant
from teetime.core.models import CourseId
from teetime.core.release_policy import ReleasePolicy
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.materialize import (
    MIN_HORIZON_DAYS,
    DateAction,
    MaterializeReport,
    RuleConflictError,
    apply_rule_edit,
    classify_date_history,
    dates_for_rule,
    horizon_days,
    materialize_rule,
    materialize_tick,
)
from teetime.tenant.models import (
    USER_WITHDRAW_REASON,
    Actor,
    RequestRow,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    StandingRule,
    row_request_id,
    rule_row_id,
)
from teetime.tenant.store import TenantStore, VersionConflictError

from .conformance import (
    BOOKER,
    BOOKER_UNTIL,
    COURSE_TIMEZONES,
    CUTOFF,
    MAX_ACCOUNTS_PER_COURSE,
    MB,
    NOW,
    TARGET,
    TZ,
    WATCHER,
    Tenant,
    _book,
    _explicit,
    _get,
    _lease,
    _outcome,
    _rule,
    _tenant,
)

POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
POLICIES = {str(MB): POLICY}
TODAY = date(2026, 9, 25)  # course-local today at NOW
THROUGH = date(2026, 10, 16)  # TODAY + 21
SATURDAYS = (date(2026, 9, 26), TARGET, date(2026, 10, 10))
SUNDAYS = (date(2026, 9, 27), date(2026, 10, 4), date(2026, 10, 11))
SUN = 6


def _store() -> InMemoryTenantStore:
    return InMemoryTenantStore(
        course_timezones=COURSE_TIMEZONES,
        cutoff=CUTOFF,
        max_accounts_per_course=MAX_ACCOUNTS_PER_COURSE,
    )


async def _materialize(
    store: TenantStore, rule: StandingRule, *, now: datetime = NOW, policy: ReleasePolicy = POLICY
) -> MaterializeReport:
    return await materialize_rule(rule, store=store, policy=policy, cutoff=CUTOFF, now=now)


async def _edit(
    store: TenantStore,
    old: StandingRule,
    new: StandingRule,
    t: Tenant,
    *,
    now: datetime = NOW,
    policy: ReleasePolicy = POLICY,
) -> MaterializeReport:
    return await apply_rule_edit(
        old, new, store=store, policy=policy, cutoff=CUTOFF, now=now, user_id=t.user.id
    )


async def _tick(
    store: TenantStore, *, now: datetime = NOW, policies: dict[str, ReleasePolicy] = POLICIES
) -> list[MaterializeReport]:
    return await materialize_tick(store=store, policies=policies, cutoff=CUTOFF, now=now)


async def _own_row(store: TenantStore, t: Tenant, rule: StandingRule, day: date) -> RequestRow:
    rows = await store.rows_for_account_date(t.account.id, day)
    matches = [r for r in rows if r.id == rule_row_id(rule.id, day)]
    assert len(matches) == 1, f"rule {rule.id} has {len(matches)} rows on {day}"
    return matches[0]


async def _own_row_or_none(
    store: TenantStore, t: Tenant, rule: StandingRule, day: date
) -> RequestRow | None:
    rows = await store.rows_for_account_date(t.account.id, day)
    matches = [r for r in rows if r.id == rule_row_id(rule.id, day)]
    return matches[0] if matches else None


async def _stored_rule(store: TenantStore, rule: StandingRule) -> StandingRule:
    stored = await store.get_rule_unscoped(rule.id)
    assert stored is not None
    return stored


async def _skip(store: TenantStore, t: Tenant, row: RequestRow) -> RequestRow:
    return await store.transition_row(
        row.id, user_id=t.user.id, to=RowStatus.SKIPPED, actor=Actor.WEB, reason=None, now=NOW
    )


async def _withdraw_explicit(store: TenantStore, t: Tenant, row: RequestRow) -> RequestRow:
    return await store.transition_row(
        row.id,
        user_id=t.user.id,
        to=RowStatus.WITHDRAWN,
        actor=Actor.WEB,
        reason=USER_WITHDRAW_REASON,
        now=NOW,
    )


async def _cancel_external(store: TenantStore, booked: RequestRow) -> RequestRow:
    await _lease(store, booked, owner=WATCHER)
    await store.record_outcomes(
        [
            _outcome(
                booked,
                actor=Actor.WATCHER,
                to_status=RowStatus.CANCELLED,
                status_reason="external",
                last_outcome="cancelled_external",
                release_lease_owner=WATCHER,
            )
        ]
    )
    return await _get(store, booked)


class _Recording:
    """Records the NAME of every store method called, in order, delegating to the real store.
    Never mocks the SUT: the materializer is exercised against the real in-memory store."""

    def __init__(self, inner: TenantStore) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self.failing: dict[str, Callable[..., bool]] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            should_fail = self.failing.get(name)
            if should_fail is not None and should_fail(*args, **kwargs):
                raise RuntimeError(f"injected failure in {name}")
            return attr(*args, **kwargs)

        return wrapped

    def only(self, *names: str) -> list[str]:
        return [c for c in self.calls if c in names]


def _recording(inner: TenantStore) -> tuple[_Recording, TenantStore]:
    rec = _Recording(inner)
    return rec, cast(TenantStore, rec)


# --- pure helpers ----------------------------------------------------------------------------


def _pure_row(
    *,
    status: RowStatus,
    source: RowSource = RowSource.RULE,
    rule_id: RuleId | None = None,
    reason: str | None = None,
    target: date = TARGET,
    superseded_from: RowStatus | None = None,
) -> RequestRow:
    row_id = rule_row_id(rule_id, target) if rule_id is not None else RowId(uuid4())
    return RequestRow(
        id=row_id,
        course_account_id=cast(Any, uuid4()),
        course_id=MB,
        target_date=target,
        timezone=TZ,
        window_earliest=time(8, 0),
        window_latest=time(10, 0),
        party_size=2,
        status=status,
        source=source,
        cutoff_at=cutoff_instant(target, timezone=TZ, cutoff=CUTOFF).astimezone(UTC),
        request_id=row_request_id(row_id),
        version=1,
        rule_id=rule_id,
        status_reason=reason,
        superseded_from=superseded_from,
    )


def _pure_rule(rule_id: RuleId | None = None) -> StandingRule:
    return StandingRule(
        id=rule_id or RuleId(uuid4()),
        course_account_id=cast(Any, uuid4()),
        weekday=TARGET.weekday(),
        window_earliest=time(8, 0),
        window_latest=time(10, 0),
        party_size=2,
        active=True,
        materialized_through=None,
        version=1,
    )


def test_horizon_covers_advance_plus_7() -> None:
    assert MIN_HORIZON_DAYS == 21
    assert horizon_days(POLICY) == 21  # max(21, 7 + 7)
    assert horizon_days(replace(POLICY, advance_days=14)) == 21  # 14 + 7 = 21 = the floor
    assert horizon_days(replace(POLICY, advance_days=21)) == 28  # advance + 7 wins past 14


def test_dates_for_rule_walks_the_inclusive_horizon() -> None:
    rule = _pure_rule()
    assert dates_for_rule(rule, today=TODAY, horizon=21) == list(SATURDAYS)
    # Both ends inclusive: a Saturday today AND the Saturday exactly ``horizon`` days out.
    sat_today = date(2026, 9, 26)
    got = dates_for_rule(rule, today=sat_today, horizon=21)
    assert got[0] == sat_today
    assert got[-1] == sat_today + timedelta(days=21)
    assert len(got) == 4
    assert dates_for_rule(replace(rule, weekday=SUN), today=TODAY, horizon=21) == list(SUNDAYS)


# --- classify_date_history: one test per DateAction --------------------------------------------


def test_classify_skip_frozen_wins_over_everything() -> None:
    rule = _pure_rule()
    own_withdrawn = _pure_row(
        status=RowStatus.WITHDRAWN, rule_id=rule.id, reason="rule_deactivated"
    )
    assert classify_date_history(rule, [], frozen=True) is DateAction.SKIP_FROZEN
    assert classify_date_history(rule, [own_withdrawn], frozen=True) is DateAction.SKIP_FROZEN


def test_classify_skip_user_terminal_blocks_every_rule() -> None:
    rule = _pure_rule()
    for reason in ("user", "external", "already_gone"):
        cancelled = _pure_row(status=RowStatus.CANCELLED, source=RowSource.EXPLICIT, reason=reason)
        assert classify_date_history(rule, [cancelled], frozen=False) is (
            DateAction.SKIP_USER_TERMINAL
        )
    # ...even when the rule's own system-withdrawn row could otherwise be reactivated, and even
    # when the terminal row belongs to ANOTHER (older) rule.
    other_rule = _pure_rule()
    cancelled_rule_row = _pure_row(
        status=RowStatus.CANCELLED, rule_id=other_rule.id, reason="external"
    )
    own_withdrawn = _pure_row(
        status=RowStatus.WITHDRAWN, rule_id=rule.id, reason="rule_weekday_changed"
    )
    assert classify_date_history(rule, [own_withdrawn, cancelled_rule_row], frozen=False) is (
        DateAction.SKIP_USER_TERMINAL
    )
    # A withdrawn one-off is NOT terminal (round-3 M1): the date is free to materialize.
    undone = _pure_row(
        status=RowStatus.WITHDRAWN, source=RowSource.EXPLICIT, reason=USER_WITHDRAW_REASON
    )
    assert classify_date_history(rule, [undone], frozen=False) is DateAction.CREATE


def test_classify_nothing_for_own_active_superseded_or_blocked_row() -> None:
    rule = _pure_rule()
    for status in (RowStatus.PENDING, RowStatus.BOOKED, RowStatus.SKIPPED):
        own = _pure_row(status=status, rule_id=rule.id)
        assert classify_date_history(rule, [own], frozen=False) is DateAction.NOTHING
    # Own row SUPERSEDED (the one-off holds the slot): the materializer never un-supersedes.
    explicit = _pure_row(status=RowStatus.PENDING, source=RowSource.EXPLICIT)
    own_superseded = _pure_row(
        status=RowStatus.SUPERSEDED, rule_id=rule.id, superseded_from=RowStatus.PENDING
    )
    assert classify_date_history(rule, [own_superseded, explicit], frozen=False) is (
        DateAction.NOTHING
    )
    # Own row system-withdrawn but the slot is HELD: leave it (the one-off's withdraw restores).
    own_withdrawn = _pure_row(
        status=RowStatus.WITHDRAWN, rule_id=rule.id, reason="rule_deactivated"
    )
    assert classify_date_history(rule, [own_withdrawn, explicit], frozen=False) is (
        DateAction.NOTHING
    )
    # Own row LOST (the date passed unbooked): nothing to do.
    own_lost = _pure_row(status=RowStatus.LOST, rule_id=rule.id, reason="cutoff")
    assert classify_date_history(rule, [own_lost], frozen=False) is DateAction.NOTHING


def test_classify_reactivate_own_system_withdrawn_row_when_slot_free() -> None:
    rule = _pure_rule()
    for reason in ("rule_weekday_changed", "rule_deactivated", "rule_deleted"):
        own = _pure_row(status=RowStatus.WITHDRAWN, rule_id=rule.id, reason=reason)
        assert classify_date_history(rule, [own], frozen=False) is DateAction.REACTIVATE
    # A withdrawn one-off alongside does not hold the slot: still reactivate.
    undone = _pure_row(
        status=RowStatus.WITHDRAWN, source=RowSource.EXPLICIT, reason=USER_WITHDRAW_REASON
    )
    own = _pure_row(status=RowStatus.WITHDRAWN, rule_id=rule.id, reason="rule_deactivated")
    assert classify_date_history(rule, [undone, own], frozen=False) is DateAction.REACTIVATE


def test_classify_create_when_no_own_row_and_slot_free() -> None:
    rule = _pure_rule()
    assert classify_date_history(rule, [], frozen=False) is DateAction.CREATE
    # ANOTHER rule's withdrawn / lost rows do not hold the slot and are never reactivated by
    # this rule (reactivation is for the rule's OWN row only).
    other = _pure_rule()
    others = [
        _pure_row(status=RowStatus.WITHDRAWN, rule_id=other.id, reason="rule_deactivated"),
        _pure_row(status=RowStatus.LOST, rule_id=other.id, reason="cutoff"),
    ]
    assert classify_date_history(rule, others, frozen=False) is DateAction.CREATE


def test_classify_create_superseded_when_another_active_row_holds_slot() -> None:
    rule = _pure_rule()
    for status in (RowStatus.PENDING, RowStatus.BOOKED, RowStatus.SKIPPED):
        explicit = _pure_row(status=status, source=RowSource.EXPLICIT)
        assert classify_date_history(rule, [explicit], frozen=False) is (
            DateAction.CREATE_SUPERSEDED
        )
    other = _pure_rule()
    other_pending = _pure_row(status=RowStatus.PENDING, rule_id=other.id)
    assert classify_date_history(rule, [other_pending], frozen=False) is (
        DateAction.CREATE_SUPERSEDED
    )


# --- materialize_rule ------------------------------------------------------------------------


async def test_materialize_idempotent_on_rule_date() -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    first = await _materialize(s, rule)
    assert {r for r in first.inserted} == {rule_row_id(rule.id, d) for d in SATURDAYS}
    assert first.materialized_through == THROUGH
    assert first.superseded_on_insert == first.reactivated == ()
    assert first.skipped_frozen == first.skipped_user_terminal == ()
    rows_before = [await _own_row(s, t, rule, d) for d in SATURDAYS]
    assert all(r.status is RowStatus.PENDING for r in rows_before)
    assert await s.rules_needing_materialization(through=THROUGH) == []

    second = await _materialize(s, await _stored_rule(s, rule))
    assert second.inserted == second.reactivated == ()
    assert second.materialized_through == THROUGH
    for day, before in zip(SATURDAYS, rows_before, strict=True):
        rows = await s.rows_for_account_date(t.account.id, day)
        assert rows == [before]  # exactly one row per (rule, date), untouched


async def test_materialize_skips_frozen_dates() -> None:
    """Sat 9/26 08:00 EDT: today's own date is past its 16:00-yesterday cutoff -> no row; the
    horizon end moves to 10/17, which is in."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    report = await _materialize(s, rule, now=now)
    assert report.skipped_frozen == (date(2026, 9, 26),)
    assert await s.rows_for_account_date(t.account.id, date(2026, 9, 26)) == []
    expected = (TARGET, date(2026, 10, 10), date(2026, 10, 17))
    assert set(report.inserted) == {rule_row_id(rule.id, d) for d in expected}
    assert report.materialized_through == date(2026, 10, 17)
    # Fri 10/2 16:00 EDT (= 20:00Z): a PAST date (Sun 9/27) is never walked at all, and Sun 10/4
    # is NOT frozen yet (its cutoff is 10/3 16:00 EDT), so the walk is 10/4, 10/11, 10/18.
    later = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
    fresh = await s.upsert_rule(_rule(t, weekday=SUN), user_id=t.user.id)
    report = await _materialize(s, fresh, now=later)
    assert report.skipped_frozen == ()
    assert await _own_row_or_none(s, t, fresh, date(2026, 9, 27)) is None
    sundays = (date(2026, 10, 4), date(2026, 10, 11), date(2026, 10, 18))
    assert set(report.inserted) == {rule_row_id(fresh.id, d) for d in sundays}
    assert report.materialized_through == date(2026, 10, 23)
    # ...and at exactly 10/4's cutoff instant (inclusive, Edge E8) 10/4 IS frozen.
    at_cutoff = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
    report = await _materialize(s, await _stored_rule(s, fresh), now=at_cutoff)
    assert report.skipped_frozen == (date(2026, 10, 4),)
    assert (await _own_row(s, t, fresh, date(2026, 10, 4))).status is RowStatus.PENDING


async def test_materialize_creates_superseded_when_one_off_holds_the_date() -> None:
    s = _store()
    t = await _tenant(s)
    explicit = await _explicit(s, t)  # TARGET is held by a one-off
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    report = await _materialize(s, rule)
    own = await _own_row(s, t, rule, TARGET)
    assert own.status is RowStatus.SUPERSEDED
    assert report.superseded_on_insert == (own.id,)
    assert set(report.inserted) == {rule_row_id(rule.id, d) for d in SATURDAYS if d != TARGET}
    assert s.slot_pointer(t.account.id, TARGET) == explicit.id


async def test_materializer_does_not_resurrect_cancelled_rule_row() -> None:
    """Q7: a rule row booked then cancelled externally is user-terminal; the slot is free but
    no later materialization of THIS rule re-creates or reactivates anything for that date."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    booked = await _book(s, await _own_row(s, t, rule, TARGET))
    cancelled = await _cancel_external(s, booked)
    assert (cancelled.status, cancelled.status_reason) == (RowStatus.CANCELLED, "external")
    assert s.slot_pointer(t.account.id, TARGET) is None
    report = await _materialize(s, await _stored_rule(s, rule))
    assert report.skipped_user_terminal == (TARGET,)
    assert report.inserted == report.reactivated == ()
    assert await s.rows_for_account_date(t.account.id, TARGET) == [cancelled]


async def test_new_rule_does_not_resurrect_external_cancel() -> None:
    """Round-2 M1: a brand-new rule_id after an external cancel is blocked by the (account,
    date) HISTORY for that date, while every other date materializes normally."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    cancelled = await _cancel_external(s, await _book(s, await _own_row(s, t, rule, TARGET)))
    await _edit(s, await _stored_rule(s, rule), replace(rule, active=False), t)
    fresh = await s.upsert_rule(_rule(t, rule_id=RuleId(uuid4())), user_id=t.user.id)
    report = await _materialize(s, fresh)
    assert report.skipped_user_terminal == (TARGET,)
    assert set(report.inserted) == {rule_row_id(fresh.id, d) for d in SATURDAYS if d != TARGET}
    assert await _own_row_or_none(s, t, fresh, TARGET) is None
    assert cancelled in await s.rows_for_account_date(t.account.id, TARGET)


async def test_withdrawn_explicit_restores_superseded_rule_row() -> None:
    """The store's D2 restore brings the rule row back PENDING; a following materialization
    sees an own active row and does nothing (no duplicate, no reactivate)."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    explicit = await _explicit(s, t)
    assert (await _own_row(s, t, rule, TARGET)).status is RowStatus.SUPERSEDED
    await _withdraw_explicit(s, t, explicit)
    restored = await _own_row(s, t, rule, TARGET)
    assert restored.status is RowStatus.PENDING
    report = await _materialize(s, await _stored_rule(s, rule))
    assert report.inserted == report.reactivated == ()
    assert await _own_row(s, t, rule, TARGET) == restored


async def test_withdrawn_explicit_does_not_block_rule_rematerialization() -> None:
    """Round-3 M1 scenario A: rule row superseded by a one-off, one-off withdrawn, rule
    deactivated + reactivated -> the date gets a pending row again."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    explicit = await _explicit(s, t)
    await _withdraw_explicit(s, t, explicit)
    inactive = replace(await _stored_rule(s, rule), active=False)
    await _edit(s, await _stored_rule(s, rule), inactive, t)
    assert (await _own_row(s, t, rule, TARGET)).status is RowStatus.WITHDRAWN
    stored = await _stored_rule(s, rule)
    report = await _edit(s, stored, replace(stored, active=True), t)
    own = await _own_row(s, t, rule, TARGET)
    assert (own.status, own.status_reason) == (RowStatus.PENDING, None)
    assert own.id in report.reactivated


async def test_new_rule_materializes_date_of_withdrawn_explicit() -> None:
    """Round-3 M1 scenario B: a one-off created then withdrawn (``user_withdrawn`` is NOT
    user-terminal), then a rule for that weekday -> the date gets a row."""
    s = _store()
    t = await _tenant(s)
    explicit = await _explicit(s, t)
    await _withdraw_explicit(s, t, explicit)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    report = await _materialize(s, rule)
    assert report.skipped_user_terminal == ()
    own = await _own_row(s, t, rule, TARGET)
    assert own.status is RowStatus.PENDING
    assert own.id in report.inserted


async def test_second_active_rule_same_weekday_refused() -> None:
    """Round-3 SF1 surfaces through the materializer: activating a second rule onto an occupied
    weekday raises ``RuleConflictError`` (never swallowed) and touches no row."""
    s = _store()
    t = await _tenant(s)
    first = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, first)
    dormant = await s.upsert_rule(_rule(t, active=False), user_id=t.user.id)
    with pytest.raises(RuleConflictError):
        await _edit(s, dormant, replace(dormant, active=True), t)
    sunday = await s.upsert_rule(_rule(t, weekday=SUN), user_id=t.user.id)
    with pytest.raises(RuleConflictError):
        await _edit(s, sunday, replace(sunday, weekday=first.weekday), t)
    for day in SATURDAYS:
        rows = await s.rows_for_account_date(t.account.id, day)
        assert [r.rule_id for r in rows] == [first.id]
        assert rows[0].status is RowStatus.PENDING


# --- apply_rule_edit --------------------------------------------------------------------------


async def test_rule_window_edit_updates_pending_only() -> None:
    """A window/party edit rewrites PENDING, unleased, not-frozen rows in place and nothing
    else: BOOKED, SKIPPED, SUPERSEDED and a leased row keep their old window/party."""
    s = _store()
    t = await _tenant(s)
    wide = replace(POLICY, advance_days=28)  # horizon 35 -> five Saturdays through 10/30
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule, policy=wide)
    days = (*SATURDAYS, date(2026, 10, 17), date(2026, 10, 24))
    booked = await _book(s, await _own_row(s, t, rule, days[0]))
    skipped = await _skip(s, t, await _own_row(s, t, rule, days[1]))
    leased = await _own_row(s, t, rule, days[2])
    assert await s.claim_rows([leased.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
    plain = await _own_row(s, t, rule, days[3])
    await _explicit(s, t, target=days[4])
    superseded = await _own_row(s, t, rule, days[4])
    assert superseded.status is RowStatus.SUPERSEDED

    stored = await _stored_rule(s, rule)
    edited = replace(stored, window_latest=time(11, 0), party_size=3)
    report = await _edit(s, stored, edited, t, policy=wide)
    assert report.rewritten == (plain.id,)
    assert report.skipped_leased == (leased.id,)
    assert report.inserted == ()  # the horizon was already full
    after = await _own_row(s, t, rule, days[3])
    assert (after.window_latest, after.party_size, after.version) == (
        time(11, 0),
        3,
        plain.version + 1,
    )
    assert after.status is RowStatus.PENDING
    for before in (booked, skipped, superseded):
        assert await _get(s, before) == before
    still_leased = await _get(s, leased)
    assert (still_leased.window_latest, still_leased.party_size, still_leased.version) == (
        time(10, 0),
        2,
        leased.version,
    )
    assert still_leased.lease_owner == BOOKER
    assert (await _stored_rule(s, rule)).version == stored.version + 1


async def test_rule_window_edit_skips_frozen_rows() -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    first = await _own_row(s, t, rule, SATURDAYS[0])
    now = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)  # 9/26's cutoff (16:00 EDT on 9/25)
    stored = await _stored_rule(s, rule)
    report = await _edit(s, stored, replace(stored, party_size=4), t, now=now)
    assert first.id not in report.rewritten
    assert await _get(s, first) == first
    assert set(report.rewritten) == {rule_row_id(rule.id, d) for d in SATURDAYS[1:]}


async def test_apply_rule_edit_surfaces_version_conflict() -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    stored = await _stored_rule(s, rule)
    await s.upsert_rule(replace(stored, party_size=3), user_id=t.user.id)  # someone else edited
    with pytest.raises(VersionConflictError):
        await _edit(s, stored, replace(stored, party_size=4), t)


async def test_rule_deactivate_withdraws_pending_keeps_booked() -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    booked = await _book(s, await _own_row(s, t, rule, SATURDAYS[0]))
    skipped = await _skip(s, t, await _own_row(s, t, rule, SATURDAYS[1]))
    pending = await _own_row(s, t, rule, SATURDAYS[2])

    stored = await _stored_rule(s, rule)
    report = await _edit(s, stored, replace(stored, active=False), t)
    assert report.withdrawn == (pending.id,)
    assert report.materialized_through is None
    assert report.inserted == report.reactivated == ()
    withdrawn = await _get(s, pending)
    assert (withdrawn.status, withdrawn.status_reason) == (
        RowStatus.WITHDRAWN,
        "rule_deactivated",
    )
    assert await _get(s, booked) == booked
    assert await _get(s, skipped) == skipped
    after = await _stored_rule(s, rule)
    assert (after.active, after.materialized_through) == (False, None)
    assert await s.rules_needing_materialization(through=THROUGH) == []
    assert s.ruleday_pointer(t.account.id, rule.weekday) is None


async def test_rule_deactivate_withdraws_superseded_rows() -> None:
    """Round-4 D1: superseded rows are not immune; ``superseded_from`` is kept (round-5)."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    explicit = await _explicit(s, t)
    stored = await _stored_rule(s, rule)
    report = await _edit(s, stored, replace(stored, active=False), t)
    own = await _own_row(s, t, rule, TARGET)
    assert (own.status, own.status_reason, own.superseded_from) == (
        RowStatus.WITHDRAWN,
        "rule_deactivated",
        RowStatus.PENDING,
    )
    assert set(report.withdrawn) == {rule_row_id(rule.id, d) for d in SATURDAYS}
    assert await _get(s, explicit) == explicit
    assert s.slot_pointer(t.account.id, TARGET) == explicit.id


async def test_rule_deactivate_skips_leased_rows() -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    leased = await _own_row(s, t, rule, TARGET)
    assert await s.claim_rows([leased.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
    stored = await _stored_rule(s, rule)
    report = await _edit(s, stored, replace(stored, active=False), t)
    assert report.skipped_leased == (leased.id,)
    assert set(report.withdrawn) == {rule_row_id(rule.id, d) for d in SATURDAYS if d != TARGET}
    still = await _get(s, leased)
    assert (still.status, still.lease_owner) == (RowStatus.PENDING, BOOKER)
    # The straggler is never offered to the booker even before the sweep gets it.
    assert await s.load_event_rows(targets={MB: TARGET}, now=NOW) == []


async def test_deactivation_order_reset_withdraw_reset_inactive() -> None:
    """§7.7: reset the watermark FIRST, withdraw the rows, reset AGAIN, THEN write the rule
    inactive — so a crash part-way leaves an ACTIVE rule with a cleared watermark for the tick.
    Pinned as the store CALL ORDER through a recording wrapper around the Protocol."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    rec, store = _recording(s)
    stored = await _stored_rule(s, rule)
    await _edit(store, stored, replace(stored, active=False), t)
    key = rec.only("reset_materialized_through", "transition_row", "upsert_rule")
    assert key == [
        "reset_materialized_through",
        *(["transition_row"] * len(SATURDAYS)),
        "reset_materialized_through",
        "upsert_rule",
    ]
    assert rec.calls[0] == "reset_materialized_through"
    assert rec.calls[-1] == "upsert_rule"
    assert "set_materialized_through" not in rec.calls
    assert "insert_rule_row_if_absent" not in rec.calls


async def test_reactivate_restores_withdrawn() -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    before = [await _own_row(s, t, rule, d) for d in SATURDAYS]
    stored = await _stored_rule(s, rule)
    await _edit(s, stored, replace(stored, active=False), t)
    inactive = await _stored_rule(s, rule)
    report = await _edit(s, inactive, replace(inactive, active=True), t)
    assert set(report.reactivated) == {r.id for r in before}
    assert report.inserted == ()
    assert report.materialized_through == THROUGH
    for row in before:
        after = await _get(s, row)
        assert (after.status, after.status_reason) == (RowStatus.PENDING, None)
        assert after.version == row.version + 2  # withdraw + reactivate, same row
    after_rule = await _stored_rule(s, rule)
    assert (after_rule.active, after_rule.materialized_through) == (True, THROUGH)


async def test_deactivate_then_reactivate_rematerializes() -> None:
    """Reactivation with a changed window/party: the reactivated rows are refreshed from the
    current rule (``reactivate_rule_row``), and the horizon is walked in full."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    stored = await _stored_rule(s, rule)
    await _edit(s, stored, replace(stored, active=False), t)
    inactive = await _stored_rule(s, rule)
    revived = replace(inactive, active=True, window_earliest=time(7, 0), party_size=3)
    report = await _edit(s, inactive, revived, t)
    assert len(report.reactivated) == len(SATURDAYS)
    for day in SATURDAYS:
        own = await _own_row(s, t, rule, day)
        assert (own.status, own.window_earliest, own.party_size) == (
            RowStatus.PENDING,
            time(7, 0),
            3,
        )
    assert await s.rules_needing_materialization(through=THROUGH) == []


async def test_weekday_flip_back_rematerializes() -> None:
    """Round-2 M1: Sat -> Sun withdraws the Saturday rows (``rule_weekday_changed``, superseded
    ones too, D1) and creates Sundays; Sun -> Sat withdraws the Sundays and REACTIVATES the
    same Saturday rows — except one whose slot a one-off still holds, which stays withdrawn."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    sat_rows = {d: await _own_row(s, t, rule, d) for d in SATURDAYS}
    held = date(2026, 10, 10)
    explicit = await _explicit(s, t, target=held)

    stored = await _stored_rule(s, rule)
    moved = await _edit(s, stored, replace(stored, weekday=SUN), t)
    assert set(moved.withdrawn) == {r.id for r in sat_rows.values()}
    assert set(moved.inserted) == {rule_row_id(rule.id, d) for d in SUNDAYS}
    for day, before in sat_rows.items():
        row = await _get(s, before)
        assert (row.status, row.status_reason) == (RowStatus.WITHDRAWN, "rule_weekday_changed")
        assert row.superseded_from == (RowStatus.PENDING if day == held else None)
    assert (await _stored_rule(s, rule)).materialized_through == THROUGH

    stored = await _stored_rule(s, rule)
    back = await _edit(s, stored, replace(stored, weekday=TARGET.weekday()), t)
    assert set(back.withdrawn) == {rule_row_id(rule.id, d) for d in SUNDAYS}
    assert set(back.reactivated) == {sat_rows[d].id for d in SATURDAYS if d != held}
    assert back.inserted == ()
    for day in SATURDAYS:
        row = await _get(s, sat_rows[day])
        if day == held:
            assert row.status is RowStatus.WITHDRAWN  # the one-off's withdraw restores it
            assert s.slot_pointer(t.account.id, day) == explicit.id
        else:
            assert (row.status, row.status_reason) == (RowStatus.PENDING, None)
    for day in SUNDAYS:
        row = await _own_row(s, t, rule, day)
        assert (row.status, row.status_reason) == (RowStatus.WITHDRAWN, "rule_weekday_changed")


async def test_deactivate_withdraw_reactivate_rematerializes_via_system_withdrawn() -> None:
    """Round-4 D1: deactivate (the superseded row is withdrawn) -> one-off withdrawn (rule
    inactive, so nothing restored) -> reactivate: the own row is system-withdrawn with a free
    slot, so the normal REACTIVATE path brings it back PENDING."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    explicit = await _explicit(s, t)
    stored = await _stored_rule(s, rule)
    await _edit(s, stored, replace(stored, active=False), t)
    await _withdraw_explicit(s, t, explicit)
    own = await _own_row(s, t, rule, TARGET)
    assert own.status is RowStatus.WITHDRAWN
    assert s.slot_pointer(t.account.id, TARGET) is None
    inactive = await _stored_rule(s, rule)
    report = await _edit(s, inactive, replace(inactive, active=True), t)
    assert own.id in report.reactivated
    after = await _own_row(s, t, rule, TARGET)
    assert (after.status, after.superseded_from) == (RowStatus.PENDING, None)
    assert s.slot_pointer(t.account.id, TARGET) == after.id


async def test_skip_survives_supersede_deactivate_withdraw_reactivate() -> None:
    """Round-5: a hidden skip comes back SKIPPED, never PENDING."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    await _skip(s, t, await _own_row(s, t, rule, TARGET))
    explicit = await _explicit(s, t)
    stored = await _stored_rule(s, rule)
    await _edit(s, stored, replace(stored, active=False), t)
    own = await _own_row(s, t, rule, TARGET)
    assert (own.status, own.superseded_from) == (RowStatus.WITHDRAWN, RowStatus.SKIPPED)
    await _withdraw_explicit(s, t, explicit)
    inactive = await _stored_rule(s, rule)
    report = await _edit(s, inactive, replace(inactive, active=True), t)
    assert own.id in report.reactivated
    after = await _own_row(s, t, rule, TARGET)
    assert (after.status, after.superseded_from) == (RowStatus.SKIPPED, None)
    assert await s.load_event_rows(targets={MB: TARGET}, now=NOW) == []


async def test_edit_of_dormant_rule_only_upserts() -> None:
    s = _store()
    t = await _tenant(s)
    dormant = await s.upsert_rule(_rule(t, active=False), user_id=t.user.id)
    rec, store = _recording(s)
    report = await _edit(store, dormant, replace(dormant, party_size=4), t)
    assert rec.calls == ["upsert_rule"]
    assert report.materialized_through is None
    assert (await _stored_rule(s, dormant)).party_size == 4


# --- materialize_tick -------------------------------------------------------------------------


async def test_tick_reactivates_withdrawn_rows_before_watermark() -> None:
    """Round-6: the §7.7 residual (a crash after the re-advance) leaves an active rule with a
    current watermark and withdrawn rows. The next day's tick walks the FULL horizon, so the
    row BEFORE the watermark is reactivated — never only the dates past the watermark."""
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    stranded = await s.transition_row(
        rule_row_id(rule.id, TARGET),
        user_id=None,
        to=RowStatus.WITHDRAWN,
        actor=Actor.MATERIALIZER,
        reason="rule_deactivated",
        now=NOW,
    )
    assert (await _stored_rule(s, rule)).materialized_through == THROUGH  # still current
    assert await _tick(s) == []  # nothing due today: the watermark covers the horizon
    assert (await _get(s, stranded)).status is RowStatus.WITHDRAWN

    tomorrow = NOW + timedelta(days=1)
    (report,) = await _tick(s, now=tomorrow)
    assert report.rule_id == str(rule.id)
    assert report.reactivated == (stranded.id,)
    assert report.inserted == (rule_row_id(rule.id, date(2026, 10, 17)),)
    assert report.materialized_through == THROUGH + timedelta(days=1)
    revived = await _get(s, stranded)
    assert (revived.status, revived.status_reason) == (RowStatus.PENDING, None)


async def test_tick_sweeps_rows_no_longer_covered() -> None:
    """Stragglers a deactivation / weekday move / deletion had to skip while leased are
    withdrawn by the tick with the reason matching the cause, once the lease has expired."""
    s = _store()
    t = await _tenant(s)
    sat = await s.upsert_rule(_rule(t), user_id=t.user.id)
    sun = await s.upsert_rule(_rule(t, weekday=SUN), user_id=t.user.id)
    tue = await s.upsert_rule(_rule(t, weekday=1), user_id=t.user.id)
    for rule in (sat, sun, tue):
        await _materialize(s, rule)
    deactivated = await _own_row(s, t, sat, TARGET)
    moved = await _own_row(s, t, sun, date(2026, 10, 4))
    deleted = await _own_row(s, t, tue, date(2026, 10, 6))
    for row in (deactivated, moved, deleted):
        assert await s.claim_rows([row.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
    stored = await _stored_rule(s, sat)
    await _edit(s, stored, replace(stored, active=False), t)
    stored = await _stored_rule(s, sun)
    await _edit(s, stored, replace(stored, weekday=0), t)
    del s._rules[tue.id]  # in-memory only: the Protocol has no rule delete yet (MU-5 nit)
    for row in (deactivated, moved, deleted):
        assert (await _get(s, row)).status is RowStatus.PENDING  # leased: skipped by the edit

    await _tick(s)  # leases still held: not swept
    for row in (deactivated, moved, deleted):
        assert (await _get(s, row)).status is RowStatus.PENDING

    after_lease = BOOKER_UNTIL + timedelta(seconds=1)
    await _tick(s, now=after_lease)
    expected = {
        deactivated.id: "rule_deactivated",
        moved.id: "rule_weekday_changed",
        deleted.id: "rule_deleted",
    }
    for row in (deactivated, moved, deleted):
        swept = await _get(s, row)
        assert (swept.status, swept.status_reason) == (RowStatus.WITHDRAWN, expected[row.id])
        assert swept.lease_owner is None
    assert await s.rows_no_longer_covered(now=after_lease) == []


async def test_tick_is_noop_when_nothing_due() -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, rule)
    rec, store = _recording(s)
    assert await _tick(store) == []
    assert rec.calls == ["rules_needing_materialization", "rows_no_longer_covered"]


async def test_tick_materializes_only_rules_short_of_their_horizon() -> None:
    s = _store()
    t = await _tenant(s)
    done = await s.upsert_rule(_rule(t), user_id=t.user.id)
    await _materialize(s, done)
    fresh = await s.upsert_rule(_rule(t, weekday=SUN), user_id=t.user.id)
    (report,) = await _tick(s)
    assert report.rule_id == str(fresh.id)
    assert set(report.inserted) == {rule_row_id(fresh.id, d) for d in SUNDAYS}
    assert (await _stored_rule(s, fresh)).materialized_through == THROUGH
    assert await _tick(s) == []


async def test_tick_isolates_per_rule_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A failure on one rule is logged (with its id) and the other rules still materialize."""
    s = _store()
    t = await _tenant(s)
    sat = await s.upsert_rule(_rule(t), user_id=t.user.id)
    sun = await s.upsert_rule(_rule(t, weekday=SUN), user_id=t.user.id)
    rec, store = _recording(s)
    rec.failing["set_materialized_through"] = lambda rule_id, *_a, **_k: rule_id == sat.id
    with caplog.at_level(logging.ERROR, logger="teetime.tenant.materialize"):
        reports = await _tick(store)
    assert [r.rule_id for r in reports] == [str(sun.id)]
    assert (await _stored_rule(s, sun)).materialized_through == THROUGH
    assert (await _stored_rule(s, sat)).materialized_through is None
    assert any(str(sat.id) in r.getMessage() and r.levelno >= logging.ERROR for r in caplog.records)


async def test_tick_isolates_policy_lookup_failure_and_still_sweeps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review round 1 must-fix: a transient store error in the per-rule POLICY LOOKUP
    (``get_account_unscoped``) must be isolated like a failure inside ``materialize_rule`` —
    the other rules still materialize AND the ``rows_no_longer_covered`` sweep still runs."""
    s = _store()
    broken = await _tenant(s)
    healthy = await _tenant(s, n=1)
    sat = await s.upsert_rule(_rule(broken), user_id=broken.user.id)  # its lookup will fail
    sun = await s.upsert_rule(_rule(healthy, weekday=SUN), user_id=healthy.user.id)
    # A straggler for the sweep: a rule deactivated while its row was leased.
    tue = await s.upsert_rule(_rule(healthy, weekday=1), user_id=healthy.user.id)
    await _materialize(s, tue)
    straggler = await _own_row(s, healthy, tue, date(2026, 10, 6))
    assert await s.claim_rows([straggler.id], owner=BOOKER, until=BOOKER_UNTIL, now=NOW)
    stored = await _stored_rule(s, tue)
    await _edit(s, stored, replace(stored, active=False), healthy)
    assert (await _get(s, straggler)).status is RowStatus.PENDING

    rec, store = _recording(s)
    rec.failing["get_account_unscoped"] = lambda account_id, *_a, **_k: (
        account_id == broken.account.id
    )
    after_lease = BOOKER_UNTIL + timedelta(seconds=1)
    with caplog.at_level(logging.ERROR, logger="teetime.tenant.materialize"):
        reports = await _tick(store, now=after_lease)
    assert [r.rule_id for r in reports] == [str(sun.id)]
    assert (await _stored_rule(s, sun)).materialized_through is not None
    assert (await _stored_rule(s, sat)).materialized_through is None
    swept = await _get(s, straggler)
    assert (swept.status, swept.status_reason) == (RowStatus.WITHDRAWN, "rule_deactivated")
    assert "rows_no_longer_covered" in rec.calls
    assert any(str(sat.id) in r.getMessage() and r.levelno >= logging.ERROR for r in caplog.records)


async def test_tick_skips_rule_whose_course_has_no_policy(caplog: pytest.LogCaptureFixture) -> None:
    s = _store()
    t = await _tenant(s)
    rule = await s.upsert_rule(_rule(t), user_id=t.user.id)
    other = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
    with caplog.at_level(logging.WARNING, logger="teetime.tenant.materialize"):
        assert await _tick(s, policies={str(CourseId("foreup:1:1")): other}) == []
    assert (await _stored_rule(s, rule)).materialized_through is None
    assert any(str(rule.id) in r.getMessage() for r in caplog.records)


async def test_tick_uses_each_courses_own_horizon() -> None:
    """Two courses with different policies: the query is one superset read, but each rule is
    materialized to ITS course's horizon (and a rule already at its horizon is left alone)."""
    other_course = CourseId("foreup:1:1")
    s = _store()
    mb = await _tenant(s)
    oc = await _tenant(s, course=other_course, n=1)
    mb_rule = await s.upsert_rule(_rule(mb), user_id=mb.user.id)
    oc_rule = await s.upsert_rule(_rule(oc), user_id=oc.user.id)
    long_policy = replace(POLICY, advance_days=21)  # horizon 28
    policies = {str(MB): POLICY, str(other_course): long_policy}
    reports = {r.rule_id: r for r in await _tick(s, policies=policies)}
    assert reports[str(mb_rule.id)].materialized_through == THROUGH
    assert reports[str(oc_rule.id)].materialized_through == TODAY + timedelta(days=28)
    assert len(reports[str(oc_rule.id)].inserted) == 4
    # MB's rule is at ITS horizon even though the superset query (through 10/23) returns it.
    assert await _tick(s, policies=policies) == []
