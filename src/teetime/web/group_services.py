"""MU-R3 (MULTIUSER_PLAN §16.2): save one ranked list as a GROUP.

A group is one explicit row (a one-off date) or one standing rule (a weekday) PER COURSE in the
list, all sharing a fresh ``group_id``. Each carries that course's options (global ranks), the
price override the user typed for that course (``None`` = the account default at run time) and
``group_rank`` = its best option rank (sorting only). Rows of one group live in different account
partitions, so a group is not one transaction: courses are written in rank order and a failure
on one course is REPORTED, not raised, leaving a smaller, still-valid group the user can re-save
(§16.2). Only when nothing at all was saved does the call raise.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from uuid import uuid4

from ..core.clock import Clock
from ..core.config import BookingCutoffConfig
from ..core.release_policy import ReleasePolicy
from ..tenant.materialize import RuleConflictError, materialize_rule
from ..tenant.models import (
    CourseAccount,
    CourseAccountId,
    RankedWindow,
    RequestRow,
    RuleId,
    StandingRule,
    TransitionRefusedError,
    UserId,
)
from ..tenant.store import RowLeaseError, TenantNotFoundError, TenantStore
from .booking_form import RankedChoice, parse_price
from .services import (
    ActionRefusedError,
    InvalidInputError,
    _conflict,
    _own_account,
    _policy_for,
    _refused,
)

__all__ = [
    "GroupFailure",
    "GroupRowsReport",
    "GroupRulesReport",
    "create_group_one_off",
    "create_group_rule",
    "set_default_price",
]


_GONE = "this course account is no longer connected"


@dataclass(frozen=True, slots=True)
class GroupFailure:
    account_id: CourseAccountId
    message: str


@dataclass(frozen=True, slots=True)
class GroupRowsReport:
    rows: tuple[RequestRow, ...]
    failures: tuple[GroupFailure, ...]


@dataclass(frozen=True, slots=True)
class GroupRulesReport:
    rules: tuple[StandingRule, ...]
    failures: tuple[GroupFailure, ...]


def _in_rank_order(
    choice: RankedChoice,
) -> list[tuple[CourseAccountId, tuple[RankedWindow, ...]]]:
    return sorted(choice.per_account.items(), key=lambda item: item[1][0].rank)


def _nothing_saved(failures: list[GroupFailure]) -> ActionRefusedError:
    return ActionRefusedError(
        failures[0].message
        if len(failures) == 1
        else "Nothing was saved: " + "; ".join(f.message for f in failures)
    )


async def create_group_one_off(
    store: TenantStore,
    *,
    user_id: UserId,
    target_date: date,
    choice: RankedChoice,
    clock: Clock,
) -> GroupRowsReport:
    """One explicit row per course for ``target_date``, sharing a fresh ``group_id``."""
    group_id = uuid4()
    rows: list[RequestRow] = []
    failures: list[GroupFailure] = []
    for account_id, options in _in_rank_order(choice):
        try:
            row = await store.create_explicit_row(
                user_id=user_id,
                account_id=account_id,
                target_date=target_date,
                options=options,
                party_size=choice.party_size,
                now=clock.now_utc(),
                max_price=choice.prices.get(account_id),
                group_id=group_id,
                group_rank=options[0].rank,
            )
        except TenantNotFoundError:
            # Ownership was checked when the form was parsed; the account vanished since.
            failures.append(GroupFailure(account_id, _GONE))
            continue
        except (RowLeaseError, TransitionRefusedError) as e:
            failures.append(GroupFailure(account_id, _refused(e).message))
            continue
        rows.append(row)
    if not rows:
        raise _nothing_saved(failures)
    return GroupRowsReport(rows=tuple(rows), failures=tuple(failures))


async def create_group_rule(
    store: TenantStore,
    *,
    user_id: UserId,
    weekday: int,
    choice: RankedChoice,
    policies: Mapping[str, ReleasePolicy],
    cutoff: BookingCutoffConfig,
    clock: Clock,
) -> GroupRulesReport:
    """One ACTIVE rule per course for ``weekday``, sharing a fresh ``group_id``, each
    materialized synchronously (§7.7). A course that already has an active rule on the weekday
    is reported (one active rule per account and weekday), the others are still saved."""
    group_id = uuid4()
    rules: list[StandingRule] = []
    failures: list[GroupFailure] = []
    for account_id, options in _in_rank_order(choice):
        account = await _own_account(store, user_id=user_id, account_id=account_id)
        policy = _policy_for(account, policies)
        rule = StandingRule(
            id=RuleId(uuid4()),
            course_account_id=account.id,
            weekday=weekday,
            options=options,
            party_size=choice.party_size,
            active=True,
            materialized_through=None,
            version=1,
            max_price=choice.prices.get(account_id),
            group_id=group_id,
            group_rank=options[0].rank,
        )
        try:
            stored = await store.upsert_rule(rule, user_id=user_id)
        except RuleConflictError:
            failures.append(GroupFailure(account_id, _conflict(rule).message))
            continue
        except TenantNotFoundError:
            failures.append(GroupFailure(account_id, _GONE))
            continue
        rules.append(stored)
        try:
            await materialize_rule(
                stored, store=store, policy=policy, cutoff=cutoff, now=clock.now_utc()
            )
        except TransitionRefusedError as e:
            # The rule is saved; the daily tick materializes it again.
            failures.append(GroupFailure(account_id, _refused(e).message))
    if not rules:
        raise _nothing_saved(failures)
    return GroupRulesReport(rules=tuple(rules), failures=tuple(failures))


async def set_default_price(
    store: TenantStore, *, user_id: UserId, account_id: CourseAccountId, raw_price: str
) -> CourseAccount:
    """The account's default per-player price cap (§16.1: $100 until changed), used by every
    row and rule of this course that has no override."""
    price = parse_price(raw_price)
    if price is None:
        raise InvalidInputError("enter a price like 85 or 85.50")
    account = await _own_account(store, user_id=user_id, account_id=account_id)
    updated = replace(account, default_max_price=price)
    await store.upsert_account(updated)
    return updated
