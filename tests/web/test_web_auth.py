"""OAuth sign-in, invite-only binding and sessions (MULTIUSER_PLAN §8.3, SF10).

Every test drives the REAL app over ASGI; only the provider's HTTP is mocked (respx).
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import respx

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import UserRole, UserStatus
from teetime.web.app import WebSettings

from .conftest import (
    BASE_URL,
    OPERATOR_EMAIL,
    GitHubIdentity,
    GoogleIdentity,
    make_invited,
    mock_github,
    mock_google,
    sign_in,
)

INVITED = "turk@example.test"


async def _add_invite(store: InMemoryTenantStore, email: str = INVITED) -> None:
    await store.upsert_user(make_invited(email))


# --- binding ---------------------------------------------------------------------------------


async def test_non_invited_subject_403(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    mock_github(provider_mock, GitHubIdentity(subject="99", emails=[("nobody@x.test", True)]))
    r = await sign_in(client)
    assert r.status_code == 403
    assert "not invited" in r.text
    # An audit doc is written (SF10) — identity only; the email is PII and stays out.
    actions = [e.action for e in store.audit_log]
    assert actions == ["signin_rejected_not_invited"]
    detail = store.audit_log[0].detail
    assert detail["provider"] == "github" and detail["subject"] == "99"
    assert store.audit_log[0].user_id is None
    # ...and NO session was established.
    assert (await client.get("/")).status_code == 303


async def test_invited_verified_email_binds_subject(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED.upper(), True)]))
    r = await sign_in(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    assert user.status is UserStatus.ACTIVE
    assert user.email == INVITED  # the invite row's email, not the provider's casing
    page = await client.get("/")
    assert page.status_code == 200
    assert INVITED in page.text


async def test_unverified_github_email_never_matches_invite(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(
        provider_mock,
        GitHubIdentity(subject="42", emails=[(INVITED, False), ("other@x.test", True)]),
    )
    r = await sign_in(client)
    assert r.status_code == 403
    invited = [u for u in store._users.values() if u.email == INVITED]
    assert invited[0].status is UserStatus.INVITED and invited[0].oauth_subject is None


async def test_github_invite_matches_only_verified_email(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    """SF10: the profile's public `email` field is attacker-controlled and must never count."""
    await _add_invite(store)
    mock_github(
        provider_mock,
        GitHubIdentity(subject="42", profile_email=INVITED, emails=[("other@x.test", True)]),
    )
    assert (await sign_in(client)).status_code == 403


async def test_second_signin_uses_subject_not_email(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 303
    bound = await store.get_user_by_subject("github", "42")
    assert bound is not None
    await client.post("/logout", data={"csrf_token": _csrf(await client.get("/"))})

    # The provider now reports a DIFFERENT verified email for the same subject.
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[("renamed@x.test", True)]))
    assert (await sign_in(client)).status_code == 303
    again = await store.get_user_by_subject("github", "42")
    assert again is not None and again.id == bound.id and again.email == INVITED

    # And a DIFFERENT subject presenting the invited email cannot take the row over.
    await client.post("/logout", data={"csrf_token": _csrf(await client.get("/"))})
    mock_github(provider_mock, GitHubIdentity(subject="43", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 403
    assert await store.get_user_by_subject("github", "43") is None


async def test_google_signin_binds_by_verified_email(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_google(provider_mock, GoogleIdentity(subject="g-1", email=INVITED))
    r = await sign_in(client, provider="google")
    assert r.status_code == 303
    user = await store.get_user_by_subject("google", "g-1")
    assert user is not None and user.status is UserStatus.ACTIVE


async def test_google_unverified_email_is_403(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_google(provider_mock, GoogleIdentity(subject="g-1", email=INVITED, email_verified=False))
    assert (await sign_in(client, provider="google")).status_code == 403


async def test_operator_email_is_implicitly_invited(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    """The first operator must be able to sign in to an EMPTY site: the configured operator
    email counts as an invite, and the resulting row carries the operator role."""
    mock_github(provider_mock, GitHubIdentity(subject="1", emails=[(OPERATOR_EMAIL, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "1")
    assert user is not None
    assert user.role is UserRole.OPERATOR and user.status is UserStatus.ACTIVE
    assert (await client.get("/admin/users")).status_code == 200


async def test_disabled_user_cannot_sign_in(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 303
    await client.post("/logout", data={"csrf_token": _csrf(await client.get("/"))})
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    await store.upsert_user(replace(user, status=UserStatus.DISABLED))

    r = await sign_in(client)
    assert r.status_code == 403 and "disabled" in r.text
    assert store.audit_log[-1].action == "signin_rejected_disabled"
    assert store.audit_log[-1].user_id == user.id
    assert (await client.get("/")).status_code == 303


async def test_callback_with_bad_state_is_400(
    client: httpx.AsyncClient, provider_mock: respx.MockRouter
) -> None:
    mock_github(provider_mock, GitHubIdentity())
    r = await client.get("/auth/github/callback", params={"code": "c", "state": "forged"})
    assert r.status_code == 400
    assert not provider_mock.calls  # nothing was exchanged


# --- session -----------------------------------------------------------------------------------


def _csrf(page: httpx.Response) -> str:
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def test_session_cookie_flags(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    r = await sign_in(client)
    cookie = r.headers["set-cookie"]
    lowered = cookie.lower()
    assert lowered.startswith("teetime_session=")
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered


async def test_session_absolute_expiry(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    clock: FakeClock,
    settings: WebSettings,
    provider_mock: respx.MockRouter,
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 303
    await clock.sleep(settings.session_max_age_s - 1)
    assert (await client.get("/")).status_code == 200  # activity never extends it...
    await clock.sleep(2)
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await client.get("/")).status_code == 303  # ...and the cookie is gone for good


async def test_disabled_user_403_on_next_request_despite_valid_cookie(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 303
    assert (await client.get("/")).status_code == 200
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    await store.upsert_user(replace(user, status=UserStatus.DISABLED))
    r = await client.get("/")
    assert r.status_code == 403
    assert "disabled" in r.text


async def test_disabled_user_session_rejected_next_request(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    """SF10: the 403 also CLEARS the session, so re-enabling does not resurrect the cookie."""
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    await store.upsert_user(replace(user, status=UserStatus.DISABLED))
    assert (await client.get("/")).status_code == 403
    await store.upsert_user(user)  # re-enabled
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_logout_clears_session(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 303
    page = await client.get("/")
    r = await client.post("/logout", data={"csrf_token": _csrf(page)})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await client.get("/")).status_code == 303


async def test_login_page_redirects_home_when_signed_in(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _add_invite(store)
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(INVITED, True)]))
    assert (await sign_in(client)).status_code == 303
    r = await client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/"


# --- redirect URI ---------------------------------------------------------------------------


async def test_oauth_redirect_uses_configured_base_url(
    client: httpx.AsyncClient, provider_mock: respx.MockRouter
) -> None:
    mock_google(provider_mock, GoogleIdentity())
    for provider, host in (("github", "github.com"), ("google", "accounts.google.com")):
        r = await client.get(f"/login/{provider}")
        assert r.status_code == 302, r.text
        loc = urlparse(r.headers["location"])
        assert loc.hostname == host
        q = parse_qs(loc.query)
        # Derived from the ONE public_base_url setting — never from the request's Host header.
        assert q["redirect_uri"] == [f"{BASE_URL}/auth/{provider}/callback"]
        assert q["state"] and q["client_id"]
        assert q.get("code_challenge_method") == ["S256"]
