"""Operator requests 2026-09-29: the dashboard does not show the sign-in subject ID, a user with
no course connected gets an obvious "Connect a course" button, and the page for course logins is
called "Connected courses" (the URL stays /accounts)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.web import pages
from teetime.web.app import WebSettings, create_app

from ..tenant.conformance import MB
from .account_builders import stored_account
from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_admin_csrf import _sign_in_operator

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "teetime" / "web" / "templates"
SUBJECT = "90210777"
CONNECT_BUTTON = re.compile(
    r'<a class="button primary large" href="/accounts">Connect a course</a>'
)


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(settings, store=store, clock=clock, course_names={str(MB): "Mangrove Bay"})


async def _sign_in_member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, router: respx.MockRouter
) -> None:
    await store.upsert_user(make_invited("turk@example.test"))
    mock_github(router, GitHubIdentity(subject=SUBJECT, emails=[("turk@example.test", True)]))
    assert (await sign_in(client)).status_code == 303


async def test_dashboard_says_who_you_are_without_the_subject_id(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_member(client, store, provider_mock)
    page = (await client.get("/")).text
    assert "turk@example.test" in page
    assert "via GitHub" in page  # the provider's real name, not "Github"
    assert SUBJECT not in page
    assert " ID " not in re.sub(r"<[^>]+>", " ", page)


async def test_a_new_user_gets_a_big_connect_button_until_a_course_is_connected(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_member(client, store, provider_mock)
    assert CONNECT_BUTTON.search((await client.get("/")).text)
    user = await store.get_user_by_subject("github", SUBJECT)
    assert user is not None
    await store.upsert_account(stored_account(user.id))
    assert not CONNECT_BUTTON.search((await client.get("/")).text)


async def test_the_course_logins_page_is_called_connected_courses(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_member(client, store, provider_mock)
    page = (await client.get("/accounts")).text
    assert '<a href="/accounts" aria-current="page">Connected courses</a>' in page
    assert "<h1>Connected courses</h1>" in page
    assert "<title>Connected courses · TeeTimeBooker</title>" in page


STALE = ("Accounts page", "Go to Accounts", "Accounts ·", "Course accounts", "course account")


def test_no_template_calls_it_the_accounts_page_any_more() -> None:
    for tpl in TEMPLATES.rglob("*.html"):
        text = re.sub(r"<[^>]+>", " ", tpl.read_text())  # visible text only, not URLs
        for stale in STALE:
            assert stale.casefold() not in text.casefold(), (tpl.name, stale)


def test_no_page_notice_says_course_account_either() -> None:
    for message in pages._NOTICES.values():
        for stale in STALE:
            assert stale.casefold() not in message.casefold(), (message, stale)


async def test_the_operator_still_sees_each_users_subject_for_debugging(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_member(client, store, provider_mock)
    client.cookies.clear()
    await _sign_in_operator(client, provider_mock)
    page = (await client.get("/admin/users")).text
    turk = next(tr for tr in re.findall(r"<tr\b.*?</tr>", page, re.DOTALL) if "turk@" in tr)
    assert f"ID {SUBJECT}" in re.sub(r"<[^>]+>", " ", turk)
