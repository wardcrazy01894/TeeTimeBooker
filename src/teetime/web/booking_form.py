"""MU-R3 (MULTIUSER_PLAN §16.1): the ranked booking form, parsed. Pure: no store, no I/O.

One form serves both a one-off date and a weekly rule. The user lists up to ``MAX_OPTIONS``
(course, time window) options and gives each a rank; courses may repeat and interleave. The page
has no script (the CSP forbids it), so ordering is a rank number per row rather than drag or
move buttons. A price box per connected course overrides that account's default cap; blank means
"use the account default".
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from uuid import UUID

from ..tenant.models import CourseAccountId, RankedWindow
from .services import InvalidInputError, WebNotFoundError, parse_party_size

MAX_OPTIONS = 6
# A per-player green fee above this is a typo, not a preference.
MAX_PRICE = Decimal("1000")


@dataclass(frozen=True, slots=True)
class RankedChoice:
    """The parsed form: this group's options per course account (global ranks, ascending), the
    party size, and the per-course price overrides the user filled in."""

    party_size: int
    per_account: dict[CourseAccountId, tuple[RankedWindow, ...]]
    prices: dict[CourseAccountId, Decimal]


def _time(raw: str, label: str) -> time:
    try:
        return time.fromisoformat(raw.strip()).replace(second=0, microsecond=0, tzinfo=None)
    except ValueError as e:
        raise InvalidInputError(f"{label} must be a time like 08:30") from e


def _account(raw: str, own_accounts: Collection[CourseAccountId]) -> CourseAccountId:
    try:
        account = CourseAccountId(UUID(raw))
    except (ValueError, AttributeError, TypeError) as e:
        raise WebNotFoundError from e
    if account not in own_accounts:
        raise WebNotFoundError  # IDOR (§9.1): a foreign id is the same 404 as a bad one
    return account


def parse_price(raw: str) -> Decimal | None:
    """A per-player price in dollars, or None for a blank field."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as e:
        raise InvalidInputError("a price must be a number like 85 or 85.50") from e
    if not value.is_finite() or value < 0 or value > MAX_PRICE:
        raise InvalidInputError(f"a price must be between 0 and {MAX_PRICE}")
    return value.quantize(Decimal("0.01"))


def parse_ranked_form(
    form: Mapping[str, str], *, own_accounts: Collection[CourseAccountId]
) -> RankedChoice:
    """Read ``opt<i>_account/earliest/latest/rank`` for i in 1..MAX_OPTIONS (a blank account
    skips the row), ``party_size`` and ``price_<account_id>``. Ranks must be distinct; they are
    renumbered 1..N in the user's order so a group's ranks are always contiguous (§16.2)."""
    party = parse_party_size(form)
    picked: list[tuple[int, CourseAccountId, time, time]] = []
    for i in range(1, MAX_OPTIONS + 1):
        raw_account = form.get(f"opt{i}_account", "").strip()
        if not raw_account:
            continue
        account = _account(raw_account, own_accounts)
        earliest = _time(form.get(f"opt{i}_earliest", ""), f"option {i} earliest")
        latest = _time(form.get(f"opt{i}_latest", ""), f"option {i} latest")
        if earliest >= latest:
            raise InvalidInputError(f"option {i}: the earliest time must be before the latest")
        try:
            rank = int(form.get(f"opt{i}_rank", "").strip() or i)
        except ValueError as e:
            raise InvalidInputError(f"option {i}: rank must be a number") from e
        picked.append((rank, account, earliest, latest))
    if not picked:
        raise InvalidInputError("add at least one course and time window")
    ranks = [p[0] for p in picked]
    if len(set(ranks)) != len(ranks):
        raise InvalidInputError("each option needs a different rank")
    per_account: dict[CourseAccountId, list[RankedWindow]] = {}
    for new_rank, (_, account, earliest, latest) in enumerate(sorted(picked), start=1):
        per_account.setdefault(account, []).append(RankedWindow(new_rank, earliest, latest))
    prices: dict[CourseAccountId, Decimal] = {}
    for account in per_account:
        price = parse_price(form.get(f"price_{account}", ""))
        if price is not None:
            prices[account] = price
    return RankedChoice(
        party_size=party,
        per_account={a: tuple(opts) for a, opts in per_account.items()},
        prices=prices,
    )
