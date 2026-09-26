"""MU-R2 (MULTIUSER_PLAN §16.2): every engine request the tenant path builds carries the row's
per-player cap — its own override, else its course account's default ($100 unless changed).
Before this, tenant requests had NO cap (``max_price_per_player=None``)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from teetime.tenant import runner, watch_runner
from teetime.tenant.watcher import SearchGroupKey

from .watcher_builders import TARGET, account, event, row


def test_booker_request_uses_account_default_cap() -> None:
    acct = account(0)
    request = runner._request_for(row(acct), acct, dry_run=False)
    assert request.max_price_per_player == Decimal("100.00")


def test_booker_request_uses_row_override() -> None:
    acct = replace(account(0), default_max_price=Decimal("90.00"))
    request = runner._request_for(replace(row(acct), max_price=Decimal("60")), acct, dry_run=False)
    assert request.max_price_per_player == Decimal("60")


def test_watcher_row_request_carries_cap() -> None:
    acct = replace(account(0), default_max_price=Decimal("75.00"))
    request = watch_runner._request_for(row(acct), acct, dry_run=True)
    assert request.max_price_per_player == Decimal("75.00")


def test_watcher_group_search_uses_highest_member_cap() -> None:
    """The shared search is permissive (the highest cap); each row re-filters with its own."""
    low = replace(account(0), default_max_price=Decimal("50.00"))
    high = replace(account(1), default_max_price=Decimal("120.00"))
    members = [event(low), event(high)]
    key = SearchGroupKey(course_id=low.course_id, target_date=TARGET, party_size=4)
    request = watch_runner._group_request(key, members, dry_run=True)
    assert request.max_price_per_player == Decimal("120.00")
