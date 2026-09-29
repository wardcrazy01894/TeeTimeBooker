"""The operator's user list on /admin/users: everyone invited, whether they have signed in, what
they have set up, and a one-click disable / enable (operator request 2026-09-29)."""

from __future__ import annotations

import re
from datetime import date, time
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RankedWindow, RuleId, StandingRule, User, UserStatus
from teetime.web.app import WebSettings, create_app

from ..tenant.conformance import MB
from .account_builders import stored_account
from .conftest import T0, GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_admin_csrf import _csrf, _sign_in_operator

OCT3 = date(2026, 10, 3)  # a Saturday inside the 21-day window from T0
_TAG = re.compile(r"<[^>]+>")


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(settings, store=store, clock=clock, course_names={str(MB): "Mangrove Bay"})


def _text(html: str) -> str:
    return " ".join(_TAG.sub(" ", html).split())


def _row(html: str, email: str) -> str:
    """The visible text of the table row that names ``email``."""
    for tr in re.findall(r"<tr\b.*?</tr>", html, re.DOTALL):
        if email in tr:
            return _text(tr)
    raise AssertionError(f"no row for {email}")


async def _member_with_a_weekly_booking(
    client: httpx.AsyncClient, store: InMemoryTenantStore, router: respx.MockRouter
) -> User:
    """Turk signs in (binding the invite), connects Mangrove Bay and has one weekly rule with
    one upcoming pending date."""
    await store.upsert_user(make_invited("turk@example.test"))
    mock_github(router, GitHubIdentity(subject="42", emails=[("turk@example.test", True)]))
    assert (await sign_in(client)).status_code == 303
    turk = await store.get_user_by_subject("github", "42")
    assert turk is not None
    account = stored_account(turk.id)
    await store.upsert_account(account)
    rule = await store.upsert_rule(
        StandingRule(
            id=RuleId(uuid4()),
            course_account_id=account.id,
            weekday=OCT3.weekday(),
            options=(RankedWindow(1, time(8, 0), time(10, 0)),),
            party_size=2,
            active=True,
            materialized_through=None,
            version=1,
        ),
        user_id=turk.id,
    )
    assert await store.insert_rule_row_if_absent(rule, OCT3, now=T0) is not None
    client.cookies.clear()
    return turk


async def test_operator_sees_every_user_and_what_they_set_up(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _member_with_a_weekly_booking(client, store, provider_mock)
    await store.upsert_user(make_invited("pending.pal@example.test"))
    await _sign_in_operator(client, provider_mock)
    page = (await client.get("/admin/users")).text

    invited = _row(page, "pending.pal@example.test")
    assert "Invited" in invited and "hasn't signed in" in invited

    turk = _row(page, "turk@example.test")
    assert "Active" in turk and "GitHub" in turk and "Member" in turk
    assert "Mangrove Bay" in turk and str(MB) not in turk  # a course NAME, never an id
    assert "1 weekly" in turk
    assert "1 pending" in turk

    me = _row(page, "operator@example.test")
    assert "Operator" in me


async def test_row_buttons_disable_and_enable_by_the_bound_identity(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    turk = await _member_with_a_weekly_booking(client, store, provider_mock)
    await store.upsert_user(make_invited("pending.pal@example.test"))
    await _sign_in_operator(client, provider_mock)
    resp = await client.get("/admin/users")
    page = resp.text
    turk_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", page, re.DOTALL) if "turk@" in tr)
    assert 'name="subject" value="42"' in turk_tr
    assert 'value="disable"' in turk_tr
    me_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", page, re.DOTALL) if "operator@" in tr)
    assert 'value="disable"' not in me_tr  # no self-disable button
    pal_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", page, re.DOTALL) if "pending.pal" in tr)
    assert 'value="disable"' not in pal_tr  # nothing bound to disable yet

    r = await client.post(
        "/admin/users",
        data={
            "csrf_token": _csrf(resp),
            "action": "disable",
            "provider": "github",
            "subject": "42",
        },
    )
    assert r.status_code == 303
    got = await store.get_user_unscoped(turk.id)
    assert got is not None and got.status is UserStatus.DISABLED
    after = (await client.get("/admin/users")).text
    assert "Disabled" in _row(after, "turk@example.test")
    turk_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", after, re.DOTALL) if "turk@" in tr)
    assert 'value="enable"' in turk_tr
