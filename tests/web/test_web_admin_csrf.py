"""CSRF on every POST and the operator-only /admin/users page (MULTIUSER_PLAN §8.2, §8.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import UserRole, UserStatus

from .conftest import OPERATOR_EMAIL, GitHubIdentity, make_invited, mock_github, sign_in

MEMBER = "turk@example.test"


def _csrf(page: httpx.Response) -> str:
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def _sign_in_member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, router: respx.MockRouter
) -> None:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(router, GitHubIdentity(subject="42", emails=[(MEMBER, True)]))
    assert (await sign_in(client)).status_code == 303


async def _sign_in_operator(client: httpx.AsyncClient, router: respx.MockRouter) -> None:
    mock_github(router, GitHubIdentity(subject="1", name="Op", emails=[(OPERATOR_EMAIL, True)]))
    assert (await sign_in(client)).status_code == 303


@pytest.fixture
async def other_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser (separate cookie jar) against the same app + store."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


# --- CSRF --------------------------------------------------------------------------------------


async def test_post_without_csrf_403(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_member(client, store, provider_mock)
    assert (await client.post("/logout")).status_code == 403
    assert (await client.post("/logout", data={"csrf_token": "wrong"})).status_code == 403
    assert (await client.post("/logout", headers={"X-CSRF-Token": "wrong"})).status_code == 403
    assert (await client.get("/")).status_code == 200  # still signed in: nothing happened


async def test_csrf_token_accepted_from_form_or_header(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_member(client, store, provider_mock)
    token = _csrf(await client.get("/"))
    r = await client.post("/logout", headers={"X-CSRF-Token": token})
    assert r.status_code == 303
    # a fresh session gets a fresh token (rotation on login)
    assert (await sign_in(client)).status_code == 303
    token2 = _csrf(await client.get("/"))
    assert token2 != token
    assert (await client.post("/logout", data={"csrf_token": token2})).status_code == 303


async def test_csrf_token_is_per_session(
    client: httpx.AsyncClient,
    other_client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
) -> None:
    await _sign_in_member(client, store, provider_mock)
    victim_token = _csrf(await client.get("/"))
    await _sign_in_operator(other_client, provider_mock)
    # the attacker's own token is worthless against the victim's session
    attacker_token = _csrf(await other_client.get("/"))
    assert (await client.post("/logout", data={"csrf_token": attacker_token})).status_code == 403
    assert (await client.post("/logout", data={"csrf_token": victim_token})).status_code == 303


# --- /admin/users ------------------------------------------------------------------------------


async def test_admin_users_operator_only(
    client: httpx.AsyncClient,
    other_client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
) -> None:
    await _sign_in_member(client, store, provider_mock)
    assert (await client.get("/admin/users")).status_code == 403
    token = _csrf(await client.get("/"))
    r = await client.post(
        "/admin/users", data={"csrf_token": token, "action": "invite", "email": "x@y.test"}
    )
    assert r.status_code == 403
    assert not [u for u in store._users.values() if u.email == "x@y.test"]

    await _sign_in_operator(other_client, provider_mock)
    assert (await other_client.get("/admin/users")).status_code == 200


async def test_admin_invite_creates_invited_row_then_signin_binds(
    client: httpx.AsyncClient,
    other_client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
) -> None:
    await _sign_in_operator(client, provider_mock)
    token = _csrf(await client.get("/admin/users"))
    r = await client.post(
        "/admin/users",
        data={"csrf_token": token, "action": "invite", "email": " New@Example.test "},
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/users")
    invited = [u for u in store._users.values() if u.email == "new@example.test"]
    assert len(invited) == 1
    assert invited[0].status is UserStatus.INVITED and invited[0].role is UserRole.MEMBER
    assert invited[0].oauth_subject is None
    assert store.audit_log[-1].action == "admin_invite"
    assert store.audit_log[-1].user_id is not None  # the operator who acted

    mock_github(provider_mock, GitHubIdentity(subject="777", emails=[("new@example.test", True)]))
    assert (await sign_in(other_client)).status_code == 303
    assert (await other_client.get("/")).status_code == 200


async def test_admin_invite_operator_role(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_operator(client, provider_mock)
    token = _csrf(await client.get("/admin/users"))
    data = {
        "csrf_token": token,
        "action": "invite",
        "email": "two@example.test",
        "role": "operator",
    }
    assert (await client.post("/admin/users", data=data)).status_code == 303
    row = next(u for u in store._users.values() if u.email == "two@example.test")
    assert row.role is UserRole.OPERATOR


async def test_admin_invite_rejects_bad_email_and_unknown_action(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_operator(client, provider_mock)
    token = _csrf(await client.get("/admin/users"))
    before = len(store._users)
    r = await client.post(
        "/admin/users", data={"csrf_token": token, "action": "invite", "email": "not-an-email"}
    )
    assert r.status_code == 400
    r = await client.post("/admin/users", data={"csrf_token": token, "action": "explode"})
    assert r.status_code == 400
    assert len(store._users) == before


async def test_admin_disable_and_enable_by_subject(
    client: httpx.AsyncClient,
    other_client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
) -> None:
    await _sign_in_member(other_client, store, provider_mock)  # github/42
    await _sign_in_operator(client, provider_mock)
    token = _csrf(await client.get("/admin/users"))
    base = {"csrf_token": token, "provider": "github", "subject": "42"}

    r = await client.post("/admin/users", data={**base, "action": "disable"})
    assert r.status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None and user.status is UserStatus.DISABLED
    assert store.audit_log[-1].action == "admin_disable"
    assert store.audit_log[-1].detail["subject"] == "42"
    # the member's still-valid cookie is rejected on their very next request (SF10)
    assert (await other_client.get("/")).status_code == 403

    r = await client.post("/admin/users", data={**base, "action": "enable"})
    assert r.status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None and user.status is UserStatus.ACTIVE
    assert store.audit_log[-1].action == "admin_enable"


async def test_admin_disable_unknown_subject_is_404(
    client: httpx.AsyncClient, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_operator(client, provider_mock)
    token = _csrf(await client.get("/admin/users"))
    data = {"csrf_token": token, "action": "disable", "provider": "github", "subject": "nope"}
    assert (await client.post("/admin/users", data=data)).status_code == 404


async def test_operator_cannot_disable_self(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_operator(client, provider_mock)  # github/1
    token = _csrf(await client.get("/admin/users"))
    data = {"csrf_token": token, "action": "disable", "provider": "github", "subject": "1"}
    assert (await client.post("/admin/users", data=data)).status_code == 400
    me = await store.get_user_by_subject("github", "1")
    assert me is not None and me.status is UserStatus.ACTIVE
