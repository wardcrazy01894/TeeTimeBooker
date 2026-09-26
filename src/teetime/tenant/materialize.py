"""Standing-rule materializer (MULTIUSER_PLAN §7.7, §3.4).

Turns each active ``StandingRule`` into dated PENDING rows 2-3 weeks ahead, idempotent on
UNIQUE(rule_id, target_date). Owners: the web (synchronously on rule create/edit/reactivate) and
the watcher (a cheap tick every run). The booking runner NEVER materializes, so it stays read +
claim only.

Resurrection is decided by the (account, date) HISTORY, never by a document-id collision (round-2
M1): see ``classify_date_history``. A user-terminal row (``models.USER_TERMINAL``) blocks the date
for every rule, including a new rule_id created after an external cancel. A system-withdrawn row
(``models.SYSTEM_WITHDRAW_REASONS``) is reactivated when a rule applies to the date again (weekday
flipped back, rule reactivated).

Rules: no row for a date already frozen (``core.booking_cutoff.frozen_reason`` in the COURSE
timezone) or in the past. A collision with an active explicit row is inserted SUPERSEDED. A
window/party edit rewrites only PENDING, unleased, not-frozen rows (never BOOKED/SKIPPED/
SUPERSEDED). Deactivation, deletion and a weekday change WITHDRAW the rule's PENDING **and
SUPERSEDED** rows (system reason; round-4 D1); BOOKED rows are never touched. Order (§7.7):
``reset_materialized_through`` FIRST, then withdraw the unleased rows, then reset AGAIN (a
concurrent tick may have re-advanced it), then write the rule inactive, so a crash part-way
leaves the tick work to do. Reactivation and a weekday change need no separate reset:
``upsert_rule`` clears the watermark itself in those writes (round-5). Rows skipped because they
were leased are swept later by the tick via ``rows_no_longer_covered`` (missing, inactive or
weekday-moved rules); until then ``load_event_rows`` /
``load_watch_rows`` never offer them and ``finalize_lost`` withdraws them instead of LOST.
Reactivation goes through ``TenantStore.reactivate_rule_row`` only, which restores the row's
pre-supersede status (``superseded_from``: SKIPPED stays SKIPPED, round-5) else PENDING. The
materializer NEVER writes superseded -> pending (only the web's one-off withdraw restores a
superseded row, round-4 D2).

Rule DELETION is not a store operation yet. When MU-8b/MU-13 add one, it MUST clear the rule doc
AND its ``ruleday|<weekday>`` pointer in ONE batch — a dangling pointer blocks every new rule on
that weekday forever (``RuleConflictError``). Until then a vanished rule's rows are withdrawn
``rule_deleted`` by the tick sweep and ``finalize_lost``.

Every entry point is a pure function of its arguments plus the store: ``now`` is the injected
clock reading (tz-aware), course-local "today" and the frozen check come from the course's
``ReleasePolicy.timezone``. Store errors are SURFACED, never swallowed: ``RuleConflictError`` /
``VersionConflictError`` (from ``upsert_rule``), ``RuleNoLongerCoversError`` /
``TransitionRefusedError`` (a stale rule or a row that moved) propagate out of
``materialize_rule`` / ``apply_rule_edit``. Only ``materialize_tick`` contains a failure — per
RULE, logged with the rule id — so one bad rule cannot stop the others from being materialized.
The only reads a rule edit skips on purpose are LEASED rows (``RowLeaseError`` ->
``MaterializeReport.skipped_leased``); the tick's sweep picks them up once the lease is gone.

Rule DELETION has no ``TenantStore`` method yet (MU-5 left it out), so the row side of a delete
(withdraw with ``rule_deleted``) is not exposed here; a rule that vanished from the store has its
rows withdrawn ``rule_deleted`` by the tick's sweep and by ``finalize_lost``.

Implemented in MULTIUSER_PLAN MU-6.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from ..core.booking_cutoff import frozen_reason
from ..core.config import BookingCutoffConfig
from ..core.release_policy import ReleasePolicy
from .models import (
    ACTIVE_ROW_STATUSES,
    SYSTEM_WITHDRAW_REASONS,
    Actor,
    RequestRow,
    RowId,
    RowSource,
    RowStatus,
    StandingRule,
    TransitionRefusedError,
    UserId,
    is_user_terminal,
    row_is_frozen,
)
from .store import RowLeaseError, TenantStore

log = logging.getLogger(__name__)

# Floor on the materialization horizon (days ahead of course-local today). The effective horizon
# is max(this, advance_days + 7), which gives >= 1 week of slack before a drop finds no row if the
# watcher (the tick owner) is down.
MIN_HORIZON_DAYS = 21

# The system withdraw reasons a rule edit writes (§3.4); ``rule_deleted`` is the sweep's only.
_REASON_DEACTIVATED = "rule_deactivated"
_REASON_WEEKDAY_CHANGED = "rule_weekday_changed"
_REASON_DELETED = "rule_deleted"

# Statuses a rule edit (deactivate / weekday move) withdraws (round-4 D1). Never BOOKED (the user
# cancels explicitly) and never SKIPPED (a skip survives rule edits, round-5).
_WITHDRAWABLE = frozenset({RowStatus.PENDING, RowStatus.SUPERSEDED})


class RuleConflictError(ValueError):
    """Raised at rule create/edit when the account already has an ACTIVE rule for that weekday.

    v1 allows ONE active rule per (account, weekday) (round-3 SF1, §3.4): a second rule's rows
    would be created ``superseded`` and nothing re-classifies them when the first rule is later
    removed — a silent missed drop. Window-preference lists per weekday are a follow-up.
    """


class DateAction(StrEnum):
    SKIP_USER_TERMINAL = "skip_user_terminal"
    SKIP_FROZEN = "skip_frozen"
    # own row already pending/booked/skipped, or superseded (then the slot is held by the
    # explicit row, and D1 guarantees its rule is active; the materializer never un-supersedes)
    NOTHING = "nothing"
    # own row system-withdrawn and the slot is free -> ``reactivate_rule_row`` (restores
    # ``superseded_from`` or PENDING). NEVER a superseded row (round-4 D1).
    REACTIVATE = "reactivate"
    CREATE = "create"  # no own row; the slot is free
    CREATE_SUPERSEDED = "create_superseded"  # no own row; another active row holds the slot


def _own_row(rule: StandingRule, history: Sequence[RequestRow]) -> RequestRow | None:
    """This rule's row for the date (at most one: ``rule_row_id`` is deterministic)."""
    for row in history:
        if row.source is RowSource.RULE and row.rule_id == rule.id:
            return row
    return None


def _slot_held(history: Sequence[RequestRow]) -> bool:
    """True iff some row holds the date's ``slot|<date>`` pointer (§3.2)."""
    return any(row.status in ACTIVE_ROW_STATUSES for row in history)


def classify_date_history(
    rule: StandingRule,
    history: Sequence[RequestRow],
    *,
    frozen: bool,
) -> DateAction:
    """Pure (round-2 M1, §7.7): decide what the materializer does for one date given every row the
    account has for it. Order: frozen -> user-terminal (any rule) -> own row -> slot state."""
    if frozen:
        return DateAction.SKIP_FROZEN
    if any(is_user_terminal(row) for row in history):
        return DateAction.SKIP_USER_TERMINAL
    own = _own_row(rule, history)
    slot_held = _slot_held(history)
    if own is None:
        return DateAction.CREATE_SUPERSEDED if slot_held else DateAction.CREATE
    if (
        own.status is RowStatus.WITHDRAWN
        and own.status_reason in SYSTEM_WITHDRAW_REASONS
        and not slot_held
    ):
        return DateAction.REACTIVATE
    # Own row active (pending/booked/skipped), superseded (the one-off holds the slot; the
    # web's withdraw batch restores it, never the materializer), system-withdrawn with the slot
    # held (same: round-6, the one-off's withdraw restores it), or lost.
    return DateAction.NOTHING


@dataclass(frozen=True, slots=True)
class MaterializeReport:
    """What one materializer call did. ``materialized_through`` is None when nothing was
    materialized (a deactivation, or an edit of a dormant rule)."""

    rule_id: str
    inserted: tuple[RowId, ...]
    superseded_on_insert: tuple[RowId, ...]
    skipped_frozen: tuple[date, ...]
    skipped_user_terminal: tuple[date, ...]
    reactivated: tuple[RowId, ...]
    materialized_through: date | None
    # rule edits (``apply_rule_edit``): rows withdrawn (deactivate / weekday move), rows
    # rewritten in place (window/party edit), and rows either had to skip because they were
    # LEASED (the tick's sweep / the next unleased edit gets them).
    withdrawn: tuple[RowId, ...] = ()
    rewritten: tuple[RowId, ...] = ()
    skipped_leased: tuple[RowId, ...] = ()


def _empty_report(rule: StandingRule) -> MaterializeReport:
    return MaterializeReport(
        rule_id=str(rule.id),
        inserted=(),
        superseded_on_insert=(),
        skipped_frozen=(),
        skipped_user_terminal=(),
        reactivated=(),
        materialized_through=None,
    )


def horizon_days(policy: ReleasePolicy) -> int:
    """``max(MIN_HORIZON_DAYS, policy.advance_days + 7)``."""
    return max(MIN_HORIZON_DAYS, policy.advance_days + 7)


def dates_for_rule(rule: StandingRule, *, today: date, horizon: int) -> list[date]:
    """Pure: every date in ``[today, today + horizon]`` with ``weekday == rule.weekday``."""
    first_offset = (rule.weekday - today.weekday()) % 7
    return [today + timedelta(days=offset) for offset in range(first_offset, horizon + 1, 7)]


def _local_today(policy: ReleasePolicy, now: datetime) -> date:
    """Course-local calendar date of ``now`` — never the UTC date (§6)."""
    return now.astimezone(ZoneInfo(policy.timezone)).date()


def _horizon_end(policy: ReleasePolicy, now: datetime) -> date:
    return _local_today(policy, now) + timedelta(days=horizon_days(policy))


def _is_frozen(
    day: date, *, policy: ReleasePolicy, cutoff: BookingCutoffConfig, now: datetime
) -> bool:
    return frozen_reason(
        now, day, timezone=policy.timezone, cutoff=cutoff
    ) is not None or day < _local_today(policy, now)


@dataclass(slots=True)
class _Tally:
    inserted: list[RowId]
    superseded_on_insert: list[RowId]
    skipped_frozen: list[date]
    skipped_user_terminal: list[date]
    reactivated: list[RowId]

    @classmethod
    def empty(cls) -> _Tally:
        return cls([], [], [], [], [])


async def _materialize_date(
    rule: StandingRule,
    day: date,
    *,
    store: TenantStore,
    frozen: bool,
    now: datetime,
    tally: _Tally,
) -> None:
    history = await store.rows_for_account_date(rule.course_account_id, day)
    action = classify_date_history(rule, history, frozen=frozen)
    if action is DateAction.SKIP_FROZEN:
        tally.skipped_frozen.append(day)
    elif action is DateAction.SKIP_USER_TERMINAL:
        tally.skipped_user_terminal.append(day)
    elif action in (DateAction.CREATE, DateAction.CREATE_SUPERSEDED):
        created = await store.insert_rule_row_if_absent(rule, day, now=now)
        if created is None:
            # A row appeared between the history read and the create (a concurrent web
            # materialize): it EXISTS, which is all the None means (round-2 M1). The next
            # call re-reads the history and classifies it properly.
            log.warning("materializer: rule %s already has a row for %s; skipping", rule.id, day)
        elif created.status is RowStatus.SUPERSEDED:
            tally.superseded_on_insert.append(created.id)
        else:
            tally.inserted.append(created.id)
    elif action is DateAction.REACTIVATE:
        own = _own_row(rule, history)
        assert own is not None  # REACTIVATE implies an own row
        revived = await store.reactivate_rule_row(own, rule, now=now)
        tally.reactivated.append(revived.id)


async def materialize_rule(
    rule: StandingRule,
    *,
    store: TenantStore,
    policy: ReleasePolicy,
    cutoff: BookingCutoffConfig,
    now: datetime,
) -> MaterializeReport:
    """Insert missing rows for ``rule`` up to the horizon, skipping frozen dates, then advance
    ``materialized_through``. Safe to call repeatedly (idempotent). Materialize the FULL horizon
    ``[today, today + horizon]``, never only the dates after the watermark: the daily tick is what
    repairs withdrawn rows left by an interrupted deactivation (§7.7).

    ``rule`` must be the STORED version (the store IfMatches it on every row write, round-5), so
    pass what ``upsert_rule`` / ``rules_needing_materialization`` / ``get_rule_unscoped``
    returned, never a copy edited in memory. An inactive rule is refused by the store.
    """
    today = _local_today(policy, now)
    through = today + timedelta(days=horizon_days(policy))
    tally = _Tally.empty()
    for day in dates_for_rule(rule, today=today, horizon=horizon_days(policy)):
        frozen = _is_frozen(day, policy=policy, cutoff=cutoff, now=now)
        await _materialize_date(rule, day, store=store, frozen=frozen, now=now, tally=tally)
    await store.set_materialized_through(rule.id, through)
    log.info(
        "materializer: rule %s through %s: %d inserted, %d superseded, %d reactivated, "
        "%d frozen, %d user-terminal",
        rule.id,
        through,
        len(tally.inserted),
        len(tally.superseded_on_insert),
        len(tally.reactivated),
        len(tally.skipped_frozen),
        len(tally.skipped_user_terminal),
    )
    return MaterializeReport(
        rule_id=str(rule.id),
        inserted=tuple(tally.inserted),
        superseded_on_insert=tuple(tally.superseded_on_insert),
        skipped_frozen=tuple(tally.skipped_frozen),
        skipped_user_terminal=tuple(tally.skipped_user_terminal),
        reactivated=tuple(tally.reactivated),
        materialized_through=through,
    )


def _uncovered_reason(row: RequestRow, rule: StandingRule | None) -> str:
    """The system withdraw reason for a rule row its STORED rule no longer covers (§7.7)."""
    if rule is None or rule.course_account_id != row.course_account_id:
        return _REASON_DELETED
    if not rule.active:
        return _REASON_DEACTIVATED
    return _REASON_WEEKDAY_CHANGED


async def _sweep_uncovered(store: TenantStore, *, now: datetime) -> None:
    """Withdraw the unleased PENDING / SUPERSEDED rule rows their stored rule no longer covers —
    the stragglers a deactivation or weekday move had to skip while they were leased (§7.7)."""
    for row in await store.rows_no_longer_covered(now=now):
        rule = await store.get_rule_unscoped(row.rule_id) if row.rule_id is not None else None
        reason = _uncovered_reason(row, rule)
        try:
            await store.transition_row(
                row.id,
                user_id=None,
                to=RowStatus.WITHDRAWN,
                actor=Actor.MATERIALIZER,
                reason=reason,
                now=now,
            )
        except (RowLeaseError, TransitionRefusedError) as exc:
            # Leased or moved between the query and the write: the next tick retries.
            log.warning("materializer sweep: row %s (%s) not withdrawn: %s", row.id, reason, exc)
            continue
        log.info("materializer sweep: withdrew row %s for %s (%s)", row.id, row.target_date, reason)


async def _policy_for(
    rule: StandingRule, *, store: TenantStore, policies: dict[str, ReleasePolicy]
) -> ReleasePolicy | None:
    account = await store.get_account_unscoped(rule.course_account_id)
    if account is None:
        log.warning("materializer tick: rule %s has no account; skipping", rule.id)
        return None
    policy = policies.get(str(account.course_id))
    if policy is None:
        log.warning(
            "materializer tick: rule %s: no release policy for course %s; skipping",
            rule.id,
            account.course_id,
        )
    return policy


async def materialize_tick(
    *,
    store: TenantStore,
    policies: dict[str, ReleasePolicy],
    cutoff: BookingCutoffConfig,
    now: datetime,
) -> list[MaterializeReport]:
    """Watcher entry: one indexed query (rules with ``materialized_through`` short of the
    horizon), a no-op on most runs, plus the ``rows_no_longer_covered`` sweep that withdraws rows a
    deactivation had to skip while they were leased (§7.7). ``policies`` is keyed by CourseId
    string.

    The query is ONE superset read (through the FARTHEST course horizon); each due rule is then
    materialized to ITS OWN course's horizon, and a rule already at its horizon is left alone. A
    failure on one rule is logged (with the rule id) and does not stop the others.
    """
    reports: list[MaterializeReport] = []
    if policies:
        farthest = max(_horizon_end(policy, now) for policy in policies.values())
        for rule in await store.rules_needing_materialization(through=farthest):
            # The WHOLE per-rule body is isolated, including the policy lookup (a store read):
            # a transient error on one rule must neither stop the others nor skip the sweep.
            try:
                policy = await _policy_for(rule, store=store, policies=policies)
                if policy is None:
                    continue
                through = _horizon_end(policy, now)
                if rule.materialized_through is not None and rule.materialized_through >= through:
                    continue  # the superset query returned it; its own horizon is covered
                reports.append(
                    await materialize_rule(rule, store=store, policy=policy, cutoff=cutoff, now=now)
                )
            except Exception:
                log.exception(
                    "materializer tick: rule %s failed; continuing with the rest", rule.id
                )
    await _sweep_uncovered(store, now=now)
    return reports


async def _withdraw_rule_rows(
    rule: StandingRule,
    *,
    reason: str,
    store: TenantStore,
    policy: ReleasePolicy,
    now: datetime,
) -> tuple[list[RowId], list[RowId]]:
    """Withdraw ``rule``'s PENDING and SUPERSEDED rows on its (old) weekday across the horizon
    (round-4 D1). Returns ``(withdrawn, skipped_leased)``; a leased row is left for the sweep
    (§3.4 "rule edits never touch leased rows"). BOOKED and SKIPPED rows are never touched."""
    withdrawn: list[RowId] = []
    skipped: list[RowId] = []
    today = _local_today(policy, now)
    for day in dates_for_rule(rule, today=today, horizon=horizon_days(policy)):
        own = _own_row(rule, await store.rows_for_account_date(rule.course_account_id, day))
        if own is None or own.status not in _WITHDRAWABLE:
            continue
        try:
            await store.transition_row(
                own.id,
                user_id=None,
                to=RowStatus.WITHDRAWN,
                actor=Actor.MATERIALIZER,
                reason=reason,
                now=now,
            )
        except RowLeaseError:
            log.info(
                "materializer: row %s for %s is leased; the sweep will withdraw it", own.id, day
            )
            skipped.append(own.id)
            continue
        withdrawn.append(own.id)
    return withdrawn, skipped


async def _rewrite_pending_rows(
    rule: StandingRule, *, store: TenantStore, policy: ReleasePolicy, now: datetime
) -> tuple[list[RowId], list[RowId]]:
    """Rewrite ``rule``'s PENDING, unleased, not-frozen rows in place from the (stored) rule.
    Returns ``(rewritten, skipped_leased)``. A frozen row is silently left as it is."""
    rewritten: list[RowId] = []
    skipped: list[RowId] = []
    today = _local_today(policy, now)
    for day in dates_for_rule(rule, today=today, horizon=horizon_days(policy)):
        own = _own_row(rule, await store.rows_for_account_date(rule.course_account_id, day))
        if own is None or own.status is not RowStatus.PENDING or row_is_frozen(own, now=now):
            continue
        try:
            await store.rewrite_pending_rule_row(
                own.id, rule=rule, expected_version=own.version, now=now
            )
        except RowLeaseError:
            log.info(
                "materializer: row %s for %s is leased; keeps its window this week", own.id, day
            )
            skipped.append(own.id)
            continue
        rewritten.append(own.id)
    return rewritten, skipped


async def _deactivate(
    old: StandingRule,
    new: StandingRule,
    *,
    store: TenantStore,
    policy: ReleasePolicy,
    now: datetime,
    user_id: UserId,
) -> MaterializeReport:
    # §7.7 order: reset -> withdraw -> reset AGAIN -> write inactive. A crash part-way leaves an
    # ACTIVE rule with a cleared watermark, so the next tick re-materializes (reactivating the
    # withdrawn rows) instead of skipping it.
    await store.reset_materialized_through(old.id)
    withdrawn, skipped = await _withdraw_rule_rows(
        old, reason=_REASON_DEACTIVATED, store=store, policy=policy, now=now
    )
    await store.reset_materialized_through(old.id)
    await store.upsert_rule(new, user_id=user_id)
    return replace(_empty_report(old), withdrawn=tuple(withdrawn), skipped_leased=tuple(skipped))


async def _activate_or_move(
    old: StandingRule,
    new: StandingRule,
    *,
    store: TenantStore,
    policy: ReleasePolicy,
    cutoff: BookingCutoffConfig,
    now: datetime,
    user_id: UserId,
) -> MaterializeReport:
    # ``upsert_rule`` clears the watermark for a weekday change / re-activation in the same write
    # (round-5 SF-2), so no separate reset; a RuleConflictError surfaces before any row moves.
    stored = await store.upsert_rule(new, user_id=user_id)
    withdrawn: list[RowId] = []
    skipped: list[RowId] = []
    if old.active and old.weekday != new.weekday:
        withdrawn, skipped = await _withdraw_rule_rows(
            old, reason=_REASON_WEEKDAY_CHANGED, store=store, policy=policy, now=now
        )
    report = await materialize_rule(stored, store=store, policy=policy, cutoff=cutoff, now=now)
    return replace(report, withdrawn=tuple(withdrawn), skipped_leased=tuple(skipped))


async def _edit_window(
    new: StandingRule,
    *,
    store: TenantStore,
    policy: ReleasePolicy,
    cutoff: BookingCutoffConfig,
    now: datetime,
    user_id: UserId,
) -> MaterializeReport:
    stored = await store.upsert_rule(new, user_id=user_id)
    rewritten, skipped = await _rewrite_pending_rows(stored, store=store, policy=policy, now=now)
    # The web materializes on every edit (§7.7 owners); idempotent, and it keeps the horizon full.
    report = await materialize_rule(stored, store=store, policy=policy, cutoff=cutoff, now=now)
    return replace(report, rewritten=tuple(rewritten), skipped_leased=tuple(skipped))


async def apply_rule_edit(
    old: StandingRule,
    new: StandingRule,
    *,
    store: TenantStore,
    policy: ReleasePolicy,
    cutoff: BookingCutoffConfig,
    now: datetime,
    user_id: UserId,
) -> MaterializeReport:
    """Window/party change: rewrite PENDING unleased not-frozen rule rows in place via
    ``TenantStore.rewrite_pending_rule_row`` (leased rows are skipped for that week). Weekday
    change: ``upsert_rule`` (clears the watermark), withdraw old-weekday PENDING and SUPERSEDED
    rows (``rule_weekday_changed``), then materialize the new weekday. Deactivate
    (``new.active is False``): withdraw PENDING and SUPERSEDED rows (``rule_deactivated``)
    after ``reset_materialized_through`` and BEFORE writing the rule inactive (§7.7); BOOKED rows
    are untouched (round-4 D1). Reactivate: ``upsert_rule`` (clears the watermark) then
    materialize, which REACTIVATES the system-withdrawn rows whose slot is free.

    ``old`` is the STORED rule the caller read (its ``version`` is what ``upsert_rule``
    IfMatches: a stale copy is ``VersionConflictError``); ``new`` is the edited copy with the
    same id. ``user_id`` scopes the rule write (the web's IDOR defence, §9.1). An edit of a
    dormant rule that stays dormant only writes the rule.
    """
    if old.id != new.id:
        raise ValueError(f"apply_rule_edit: {old.id} and {new.id} are different rules")
    if old.active and not new.active:
        return await _deactivate(old, new, store=store, policy=policy, now=now, user_id=user_id)
    if not new.active:
        await store.upsert_rule(new, user_id=user_id)
        return _empty_report(new)
    if not old.active or old.weekday != new.weekday:
        return await _activate_or_move(
            old, new, store=store, policy=policy, cutoff=cutoff, now=now, user_id=user_id
        )
    return await _edit_window(
        new, store=store, policy=policy, cutoff=cutoff, now=now, user_id=user_id
    )
