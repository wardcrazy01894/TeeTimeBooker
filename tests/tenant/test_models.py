"""MU-5: pure tenant-model helpers (MULTIUSER_PLAN §3.1/§3.3/§3.4).

``check_transition`` / ``check_create`` are the §3.4 table as a pure function. The store calls them
inside the atomic write; the conformance suite (``tests/tenant/conformance.py``) pins the same
transitions end-to-end through a store. These tests pin the table itself, per actor.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest

from teetime.core.models import CourseId, derive_request_id
from teetime.tenant.models import (
    CANCEL_REASONS,
    SYSTEM_WITHDRAW_REASONS,
    USER_TERMINAL,
    USER_WITHDRAW_REASON,
    Actor,
    CourseAccountId,
    RequestRow,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    TransitionRefusedError,
    UserId,
    check_create,
    check_transition,
    derive_account_id,
    is_user_terminal,
    lease_held,
    row_is_frozen,
    row_request_id,
    rule_row_id,
)

MB = CourseId("foreup:19671:2149")
TZ = "America/New_York"
TARGET = date(2026, 10, 3)  # a Saturday
CUTOFF_AT = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)  # 16:00 EDT the day before
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
FROZEN_NOW = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)

P, B, S = RowStatus.PENDING, RowStatus.BOOKED, RowStatus.SKIPPED
SUP, W, C, L = (
    RowStatus.SUPERSEDED,
    RowStatus.WITHDRAWN,
    RowStatus.CANCELLED,
    RowStatus.LOST,
)


def _row(
    status: RowStatus = P,
    *,
    source: RowSource = RowSource.RULE,
    reason: str | None = None,
) -> RequestRow:
    rid = RowId(uuid4())
    return RequestRow(
        id=rid,
        course_account_id=CourseAccountId(uuid4()),
        course_id=MB,
        target_date=TARGET,
        timezone=TZ,
        window_earliest=time(8, 0),
        window_latest=time(10, 0),
        party_size=2,
        status=status,
        source=source,
        cutoff_at=CUTOFF_AT,
        request_id=row_request_id(rid),
        version=1,
        rule_id=RuleId(uuid4()) if source is RowSource.RULE else None,
        status_reason=reason,
    )


# --- identity helpers (§3.1 / §3.3) ---------------------------------------------------


def test_row_request_id_is_tenant_row_fingerprint() -> None:
    rid = RowId(UUID("11111111-2222-3333-4444-555555555555"))
    assert row_request_id(rid) == derive_request_id(f"tenant-row|{rid}")


def test_row_request_id_distinct_per_row() -> None:
    assert row_request_id(RowId(uuid4())) != row_request_id(RowId(uuid4()))


def test_rule_row_id_is_deterministic_per_rule_and_date() -> None:
    rule = RuleId(uuid4())
    assert rule_row_id(rule, TARGET) == rule_row_id(rule, TARGET)
    assert rule_row_id(rule, TARGET) != rule_row_id(rule, TARGET + timedelta(days=7))
    assert rule_row_id(rule, TARGET) != rule_row_id(RuleId(uuid4()), TARGET)


def test_derive_account_id_is_deterministic_per_user_and_course() -> None:
    user = UserId(uuid4())
    assert derive_account_id(user, MB) == derive_account_id(user, MB)
    assert derive_account_id(user, MB) != derive_account_id(user, CourseId("teeitup:x"))
    assert derive_account_id(user, MB) != derive_account_id(UserId(uuid4()), MB)


# --- reason vocabularies (§3.4, §7.5, §7.7, round-3 M1) --------------------------------


def test_user_withdrawn_is_not_user_terminal() -> None:
    assert (W, USER_WITHDRAW_REASON) not in USER_TERMINAL
    assert not is_user_terminal(_row(W, source=RowSource.EXPLICIT, reason=USER_WITHDRAW_REASON))


def test_cancelled_rows_are_user_terminal() -> None:
    for reason in CANCEL_REASONS:
        assert is_user_terminal(_row(C, reason=reason))
    assert not is_user_terminal(_row(W, reason="rule_deactivated"))


def test_system_and_user_withdraw_reasons_disjoint() -> None:
    assert USER_WITHDRAW_REASON not in SYSTEM_WITHDRAW_REASONS


# --- derived predicates ----------------------------------------------------------------


def test_row_is_frozen_at_cutoff_inclusive() -> None:
    row = _row()
    assert not row_is_frozen(row, now=CUTOFF_AT - timedelta(seconds=1))
    assert row_is_frozen(row, now=CUTOFF_AT)


def test_row_is_frozen_when_date_passed_in_course_tz() -> None:
    row = replace(_row(), cutoff_at=datetime(2030, 1, 1, tzinfo=UTC))
    # 2026-10-04 02:00 UTC is still 2026-10-03 22:00 in New York: not passed.
    assert not row_is_frozen(row, now=datetime(2026, 10, 4, 2, 0, tzinfo=UTC))
    assert row_is_frozen(row, now=datetime(2026, 10, 4, 5, 0, tzinfo=UTC))


def test_lease_held_only_while_unexpired() -> None:
    row = replace(_row(), lease_owner="booker", lease_expires_at=NOW + timedelta(seconds=60))
    assert lease_held(row, now=NOW)
    assert not lease_held(row, now=NOW + timedelta(seconds=60))
    assert not lease_held(_row(), now=NOW)


# --- check_create (∅ -> pending) --------------------------------------------------------


def test_create_explicit_by_web_allowed() -> None:
    check_create(_row(source=RowSource.EXPLICIT), actor=Actor.WEB, now=NOW)


def test_create_rule_row_by_materializer_allowed_pending_or_superseded() -> None:
    check_create(_row(), actor=Actor.MATERIALIZER, now=NOW)
    check_create(_row(SUP), actor=Actor.MATERIALIZER, now=NOW)


@pytest.mark.parametrize(
    ("source", "actor"),
    [
        (RowSource.EXPLICIT, Actor.MATERIALIZER),
        (RowSource.RULE, Actor.WEB),
        (RowSource.EXPLICIT, Actor.WATCHER),
        (RowSource.RULE, Actor.BOOKING_RUNNER),
    ],
)
def test_create_by_wrong_actor_refused(source: RowSource, actor: Actor) -> None:
    with pytest.raises(TransitionRefusedError):
        check_create(_row(source=source), actor=actor, now=NOW)


def test_create_refused_when_frozen() -> None:
    with pytest.raises(TransitionRefusedError, match="frozen"):
        check_create(_row(source=RowSource.EXPLICIT), actor=Actor.WEB, now=FROZEN_NOW)


def test_create_refused_in_non_initial_status() -> None:
    with pytest.raises(TransitionRefusedError):
        check_create(_row(B, source=RowSource.EXPLICIT), actor=Actor.WEB, now=NOW)


# --- check_transition: the §3.4 table, allowed legs -----------------------------------


@pytest.mark.parametrize(
    ("row", "to", "actor", "reason", "now"),
    [
        (_row(P), B, Actor.BOOKING_RUNNER, None, NOW),
        (_row(P), B, Actor.WATCHER, None, NOW),
        (_row(P), S, Actor.WEB, None, NOW),
        (_row(S), P, Actor.WEB, None, NOW),
        (_row(P), SUP, Actor.WEB, None, NOW),
        (_row(S), SUP, Actor.WEB, None, NOW),
        (_row(SUP), P, Actor.WEB, None, NOW),
        (_row(P, source=RowSource.EXPLICIT), W, Actor.WEB, USER_WITHDRAW_REASON, NOW),
        (_row(P), W, Actor.MATERIALIZER, "rule_deactivated", NOW),
        (_row(P), W, Actor.WEB, "rule_deleted", NOW),
        (_row(W, reason="rule_weekday_changed"), P, Actor.MATERIALIZER, None, NOW),
        (_row(B), B, Actor.WATCHER, None, NOW),
        (_row(B), C, Actor.WEB, "user", NOW),
        (_row(B), C, Actor.WATCHER, "external", NOW),
        (_row(P), L, Actor.WATCHER, None, FROZEN_NOW),
    ],
)
def test_check_transition_allowed(
    row: RequestRow, to: RowStatus, actor: Actor, reason: str | None, now: datetime
) -> None:
    check_transition(row, to, actor=actor, now=now, reason=reason)


# --- check_transition: refused legs ------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "to", "actor", "reason", "now", "match"),
    [
        # booked -> skipped is ALWAYS refused (cancel is a separate action)
        (_row(B), S, Actor.WEB, None, NOW, "Cancel"),
        # wrong actor on an allowed edge
        (_row(P), B, Actor.WEB, None, NOW, "may not"),
        (_row(P), S, Actor.WATCHER, None, NOW, "may not"),
        (_row(B), B, Actor.BOOKING_RUNNER, None, NOW, "may not"),
        (_row(W, reason="rule_deactivated"), P, Actor.WEB, None, NOW, "may not"),
        # not-frozen guards
        (_row(S), P, Actor.WEB, None, FROZEN_NOW, "frozen"),
        (_row(SUP), P, Actor.WEB, None, FROZEN_NOW, "frozen"),
        (_row(W, reason="rule_deactivated"), P, Actor.MATERIALIZER, None, FROZEN_NOW, "frozen"),
        # only RULE rows can be superseded
        (_row(P, source=RowSource.EXPLICIT), SUP, Actor.WEB, None, NOW, "rule rows"),
        # withdraw reason vocabulary
        (_row(P), W, Actor.WEB, USER_WITHDRAW_REASON, NOW, "reason"),
        (_row(P, source=RowSource.EXPLICIT), W, Actor.WEB, "rule_deleted", NOW, "reason"),
        (_row(P), W, Actor.MATERIALIZER, USER_WITHDRAW_REASON, NOW, "reason"),
        (_row(P), W, Actor.MATERIALIZER, None, NOW, "reason"),
        # only SYSTEM-withdrawn rows come back
        (
            _row(W, source=RowSource.EXPLICIT, reason=USER_WITHDRAW_REASON),
            P,
            Actor.MATERIALIZER,
            None,
            NOW,
            "system",
        ),
        # cancel reason vocabulary
        (_row(B), C, Actor.WEB, None, NOW, "reason"),
        (_row(B), C, Actor.WEB, "whatever", NOW, "reason"),
        # lost only once frozen
        (_row(P), L, Actor.WATCHER, None, NOW, "frozen"),
        # terminal statuses never move
        (_row(C, reason="external"), P, Actor.WEB, None, NOW, "no transition"),
        (_row(L), P, Actor.WEB, None, NOW, "no transition"),
        (_row(P), C, Actor.WEB, "user", NOW, "no transition"),
    ],
)
def test_check_transition_refused(
    row: RequestRow,
    to: RowStatus,
    actor: Actor,
    reason: str | None,
    now: datetime,
    match: str,
) -> None:
    with pytest.raises(TransitionRefusedError, match=match):
        check_transition(row, to, actor=actor, now=now, reason=reason)


# --- review round 1 (MU-5) --------------------------------------------------------------


def test_booked_to_pending_requires_needs_reconcile() -> None:
    """M2 edge: an upgrade cancelled the old slot and the rebook failed. Without the flag the
    §7.6 in-window adoption never applies and a landed rebook is adopted as unowned."""
    check_transition(_row(B), P, actor=Actor.WATCHER, now=NOW, needs_reconcile=True)
    with pytest.raises(TransitionRefusedError, match="needs_reconcile"):
        check_transition(_row(B), P, actor=Actor.WATCHER, now=NOW)
