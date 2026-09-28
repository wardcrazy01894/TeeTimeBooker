"""Canonical-host redirect (custom domain, 2026-09-28).

The OAuth ``state`` lives in the session cookie of the host the sign-in STARTED on, and the
callback always returns to ``public_base_url``. A visitor on ``www.`` or the old
``*.azurecontainerapps.io`` host would therefore fail sign-in. With
``canonical_host_redirect`` on, every request to another host is redirected to the same path on
``public_base_url`` (the target is always the configured origin: never an open redirect).
``/healthz`` is exempt so a platform probe on any host keeps working.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.web.app import (
    WebConfigError,
    WebSettings,
    _CanonicalHostMiddleware,
    create_app,
    load_web_settings,
)

CANONICAL = "https://spicyteetimebooker.com"


@pytest.fixture
async def canonical_client(
    settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        replace(settings, public_base_url=CANONICAL, canonical_host_redirect=True),
        store=store,
        clock=clock,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=CANONICAL, follow_redirects=False
    ) as c:
        yield c


async def test_www_is_redirected_permanently_to_the_same_path_on_the_apex(
    canonical_client: httpx.AsyncClient,
) -> None:
    resp = await canonical_client.get(
        "https://www.spicyteetimebooker.com/dates?week=2",
        headers={"host": "www.spicyteetimebooker.com"},
    )
    assert resp.status_code == 301
    assert resp.headers["location"] == f"{CANONICAL}/dates?week=2"


async def test_the_old_container_apps_host_is_redirected(
    canonical_client: httpx.AsyncClient,
) -> None:
    old = "teetime-web-prod.wittydesert-02f9f0cd.eastus2.azurecontainerapps.io"
    resp = await canonical_client.get(f"https://{old}/login", headers={"host": old})
    assert resp.status_code == 301
    assert resp.headers["location"] == f"{CANONICAL}/login"


async def test_a_post_to_another_host_keeps_its_method(
    canonical_client: httpx.AsyncClient,
) -> None:
    resp = await canonical_client.post(
        "https://www.spicyteetimebooker.com/logout", headers={"host": "www.spicyteetimebooker.com"}
    )
    assert resp.status_code == 308  # 308 keeps the method and body; 301 may turn POST into GET
    assert resp.headers["location"] == f"{CANONICAL}/logout"


async def test_the_canonical_host_is_served_normally(canonical_client: httpx.AsyncClient) -> None:
    resp = await canonical_client.get("/login")
    assert resp.status_code == 200
    # Host matching ignores case and an explicit default port.
    resp = await canonical_client.get("/login", headers={"host": "SpicyTeeTimeBooker.com:443"})
    assert resp.status_code == 200


async def test_healthz_is_never_redirected(canonical_client: httpx.AsyncClient) -> None:
    resp = await canonical_client.get("/healthz", headers={"host": "10.0.0.7:8000"})
    assert resp.status_code == 200


async def test_a_spoofed_host_cannot_steer_the_redirect_target(
    canonical_client: httpx.AsyncClient,
) -> None:
    resp = await canonical_client.get("/login", headers={"host": "evil.example"})
    assert resp.status_code == 301
    assert resp.headers["location"].startswith(f"{CANONICAL}/")


async def test_redirect_is_off_by_default(client: httpx.AsyncClient) -> None:
    """Dev and every existing deployment keep answering on whatever host they are reached by."""
    resp = await client.get("/login", headers={"host": "some-other-host.example"})
    assert resp.status_code == 200


def _env(**extra: str) -> dict[str, str]:
    return {
        "TEETIME_PUBLIC_BASE_URL": CANONICAL,
        "WEB_SESSION_SECRET": "x" * 48,
        "OAUTH_GOOGLE_CLIENT_ID": "gg",
        "OAUTH_GOOGLE_CLIENT_SECRET": "gg-secret-0123456789",
        **extra,
    }


def test_load_web_settings_reads_the_redirect_flag() -> None:
    assert load_web_settings(_env()).canonical_host_redirect is False
    on = load_web_settings(_env(TEETIME_CANONICAL_HOST_REDIRECT="true"))
    assert on.canonical_host_redirect is True
    with pytest.raises(WebConfigError, match="TEETIME_CANONICAL_HOST_REDIRECT"):
        load_web_settings(_env(TEETIME_CANONICAL_HOST_REDIRECT="yes"))


async def test_a_redirect_carries_the_security_headers(canonical_client: httpx.AsyncClient) -> None:
    """Pins the middleware order: the security headers wrap the canonical-host redirect."""
    resp = await canonical_client.get("/login", headers={"host": "www.spicyteetimebooker.com"})
    assert resp.status_code == 301
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in resp.headers["content-security-policy"]


async def test_control_bytes_never_reach_the_location_header() -> None:
    """Defence in depth: a raw path or query carrying CR/LF (normally rejected upstream) falls
    back to the origin's root instead of being echoed into ``Location``."""
    sent: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:  # never reached
        raise AssertionError("the redirect must short-circuit")

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    middleware = _CanonicalHostMiddleware(app, origin=CANONICAL)  # type: ignore[arg-type]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "raw_path": b"/x\r\nSet-Cookie: pwned=1",
        "query_string": b"a=1\r\nb",
        "headers": [(b"host", b"www.spicyteetimebooker.com")],
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]
    start = sent[0]
    headers = dict(start["headers"])  # type: ignore[arg-type]
    assert headers[b"location"] == f"{CANONICAL}/".encode()
