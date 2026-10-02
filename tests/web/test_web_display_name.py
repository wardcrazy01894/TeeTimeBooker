"""A person can set their own name (operator request 2026-10-02): the name the invite or the
sign-in gave us is the default, and a "Your name" form on the dashboard replaces it. The name
is what the top bar shows, what the emails open with ("Hi Alex,") and what the operator's user
list shows. End-to-end over ASGI.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import User
from teetime.tenant.notify import first_name
from teetime.web.app import WebSettings, create_app
from teetime.web.services import DISPLAY_NAME_MAX_LEN

from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_admin_csrf import _sign_in_operator
from .test_web_ranked_pages import _post

MEMBER = "turk@example.test"


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(settings, store=store, clock=clock)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@pytest.fixture
async def member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> User:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(
        provider_mock, GitHubIdentity(subject="42", emails=[(MEMBER, True)], name="turkgolf99")
    )
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    return user


def _name_box(html: str) -> str:
    m = re.search(r'<input[^>]*name="display_name"[^>]*>', html)
    assert m, "no display_name box"
    return m.group(0)


async def test_dashboard_offers_the_name_prefilled_with_the_providers_name(
    client: httpx.AsyncClient, member: User
) -> None:
    html = (await client.get("/")).text
    # An invited person keeps the invite's name (the email's local part) at first sign-in.
    assert member.display_name == "turk"
    assert f'value="{member.display_name}"' in _name_box(html)
    assert f'maxlength="{DISPLAY_NAME_MAX_LEN}"' in _name_box(html)
    assert 'action="/me/name"' in html
    assert "emails" in html.lower()  # says where the name is used


async def test_saving_a_name_changes_the_top_bar_the_emails_and_the_operator_list(
    client: httpx.AsyncClient,
    member: User,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
) -> None:
    r = await _post(client, "/me/name", {"display_name": "  Turk   Golfer "})
    assert r.status_code == 303 and r.headers["location"] == "/?notice=name_saved"
    html = (await client.get(r.headers["location"])).text
    assert "Name saved." in html
    assert re.search(r'class="who">\s*Turk Golfer\s*<', html)  # whitespace collapsed
    saved = await store.get_user_unscoped(member.id)
    assert saved is not None and saved.display_name == "Turk Golfer"
    assert first_name(saved.display_name) == "Turk"  # "Hi Turk," in every email
    assert replace(saved, display_name=member.display_name) == member  # nothing else changed
    client.cookies.clear()
    await _sign_in_operator(client, provider_mock)
    assert "Turk Golfer" in (await client.get("/admin/users")).text


async def test_control_and_format_characters_are_dropped_from_a_name(
    client: httpx.AsyncClient, member: User, store: InMemoryTenantStore
) -> None:
    """A bidi override or a zero-width joiner could make a name read as another in the
    operator's list; they are dropped (the visible letters stay)."""
    r = await _post(client, "/me/name", {"display_name": "Turk\u202e Golfer\u200d\x07"})
    assert r.status_code == 303
    saved = await store.get_user_unscoped(member.id)
    assert saved is not None and saved.display_name == "Turk Golfer"


@pytest.mark.parametrize("bad", ["", "   ", "x" * (DISPLAY_NAME_MAX_LEN + 1), "two\nlines"])
async def test_a_blank_overlong_or_multiline_name_is_refused(
    client: httpx.AsyncClient, member: User, store: InMemoryTenantStore, bad: str
) -> None:
    r = await _post(client, "/me/name", {"display_name": bad})
    assert r.status_code == 400
    saved = await store.get_user_unscoped(member.id)
    assert saved is not None and saved.display_name == member.display_name
