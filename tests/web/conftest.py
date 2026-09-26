"""Fixtures for the MU-12 web skeleton tests (MULTIUSER_PLAN §8).

The app is exercised end-to-end over ASGI with ``httpx.AsyncClient``; the ONLY thing mocked is
the OAuth provider's HTTP (respx), never the app, the store or the clock. ``base_url`` is
``https://`` so the ``Secure`` session cookie is actually sent back by httpx's cookie jar —
over ``http://testserver`` every authenticated test would silently look signed-out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import User, UserId, UserRole, UserStatus
from teetime.web.app import OAuthProviderSettings, WebSettings, create_app

BASE_URL = "https://teetime-web-dev.example.azurecontainerapps.io"
OPERATOR_EMAIL = "operator@example.test"
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"
GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


@pytest.fixture
def settings() -> WebSettings:
    return WebSettings(
        public_base_url=BASE_URL,
        session_secret="s" * 48,
        github=OAuthProviderSettings(client_id="gh-id", client_secret="gh-secret-0123456789"),
        google=OAuthProviderSettings(client_id="gg-id", client_secret="gg-secret-0123456789"),
        operator_email=OPERATOR_EMAIL,
    )


@pytest.fixture
def store() -> InMemoryTenantStore:
    return InMemoryTenantStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(settings, store=store, clock=clock)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as c:
        yield c


def make_invited(email: str, *, role: UserRole = UserRole.MEMBER) -> User:
    return User(
        id=UserId(uuid4()),
        oauth_provider="github",
        oauth_subject=None,
        email=email,
        display_name=email.split("@", maxsplit=1)[0],
        role=role,
        status=UserStatus.INVITED,
    )


@dataclass
class GitHubIdentity:
    """What the mocked GitHub API reports for the signing-in account."""

    subject: str = "42"
    login: str = "turk"
    name: str = "Turk"
    profile_email: str | None = None  # the PUBLIC profile field — must never be trusted
    # (address, verified) as GET /user/emails reports them
    emails: list[tuple[str, bool]] = field(default_factory=lambda: [("turk@example.test", True)])


@dataclass
class GoogleIdentity:
    subject: str = "g-1"
    email: str = "turk@example.test"
    email_verified: bool = True
    name: str = "Turk"


@pytest.fixture
def provider_mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def mock_github(router: respx.MockRouter, identity: GitHubIdentity) -> None:
    router.post(GITHUB_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "gho_test", "token_type": "bearer", "scope": "user:email"},
        )
    )
    user: dict[str, Any] = {
        "id": int(identity.subject),
        "login": identity.login,
        "name": identity.name,
        "email": identity.profile_email,
    }
    router.get(f"{GITHUB_API}/user").mock(return_value=httpx.Response(200, json=user))
    router.get(f"{GITHUB_API}/user/emails").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"email": addr, "verified": verified, "primary": i == 0, "visibility": None}
                for i, (addr, verified) in enumerate(identity.emails)
            ],
        )
    )


def mock_google(router: respx.MockRouter, identity: GoogleIdentity) -> None:
    router.get(GOOGLE_DISCOVERY).mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": "https://accounts.google.com",
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": GOOGLE_TOKEN_URL,
                "userinfo_endpoint": GOOGLE_USERINFO,
                "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile"],
                "code_challenge_methods_supported": ["S256"],
            },
        )
    )
    # No id_token in the token response: the identity comes from the userinfo endpoint,
    # which is what the app must consult for `email_verified` either way.
    router.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "ya29.test", "token_type": "Bearer", "expires_in": 3599},
        )
    )
    router.get(GOOGLE_USERINFO).mock(
        return_value=httpx.Response(
            200,
            json={
                "sub": identity.subject,
                "email": identity.email,
                "email_verified": identity.email_verified,
                "name": identity.name,
            },
        )
    )


async def sign_in(client: httpx.AsyncClient, *, provider: str = "github") -> httpx.Response:
    """Drive the real OAuth round-trip: /login/<provider> -> provider (mocked) -> callback.

    Returns the CALLBACK response (303 to "/" on success, 403 page when not invited) so tests
    can inspect its cookies / status. The provider HTTP must already be mocked.
    """
    start = await client.get(f"/login/{provider}")
    assert start.status_code == 302, start.text
    location = start.headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]
    return await client.get(f"/auth/{provider}/callback", params={"code": "code-1", "state": state})
