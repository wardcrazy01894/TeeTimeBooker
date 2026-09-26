"""App-level guarantees that need no sign-in: headers on every response, /healthz touches no
DB, and no route serves without auth except the allowlist (MULTIUSER_PLAN §8.2, §8.3, §10.1)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Mount, Route

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.web.app import WebSettings, create_app
from teetime.web.security import security_headers

from .conftest import T0

PUBLIC_PATHS = {"/healthz", "/login", "/login/{provider}", "/auth/{provider}/callback"}
PUBLIC_MOUNTS = {"/static"}


class SpyStore:
    """Records every store METHOD call, delegating to a real InMemoryTenantStore."""

    def __init__(self) -> None:
        self.inner = InMemoryTenantStore()
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def recorded(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return attr(*args, **kwargs)

        return recorded


async def test_healthz_no_db(settings: WebSettings) -> None:
    spy = SpyStore()
    app = create_app(settings, store=spy, clock=FakeClock(start=T0))  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert spy.calls == []
    assert "set-cookie" not in r.headers  # liveness must not mint sessions


async def test_csp_header_present(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "unsafe-inline" not in csp


@pytest.mark.parametrize("path", ["/healthz", "/login", "/", "/nope"])
async def test_security_headers_on_every_response(client: httpx.AsyncClient, path: str) -> None:
    """Including redirects, 403s and 404s — a header middleware, not per-handler discipline."""
    r = await client.get(path)
    for name, value in security_headers().items():
        assert r.headers.get(name) == value, (path, name, r.status_code)


async def test_no_route_serves_without_auth_except_allowlist(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """Walk the app's OWN route table so a future route cannot be added unguarded."""
    seen_public: set[str] = set()
    checked = 0
    for route in app.routes:
        if isinstance(route, Mount):
            assert route.path in PUBLIC_MOUNTS, route.path
            seen_public.add(route.path)
            continue
        assert isinstance(route, Route), route
        if route.path in PUBLIC_PATHS:
            seen_public.add(route.path)
            continue
        assert not route.path.startswith(("/docs", "/redoc", "/openapi")), (
            "API docs must not be served"
        )
        concrete = route.path.replace("{provider}", "github").replace("{id}", "x")
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            r = await client.request(method, concrete)
            checked += 1
            assert r.status_code in (303, 401, 403), (method, concrete, r.status_code)
            if r.status_code == 303:
                assert r.headers["location"] == "/login"
    assert checked >= 3
    assert seen_public == PUBLIC_PATHS | PUBLIC_MOUNTS


async def test_login_page_lists_only_enabled_providers(
    settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock
) -> None:
    app = create_app(replace(settings, google=None), store=store, clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        r = await c.get("/login")
    assert r.status_code == 200
    assert "/login/github" in r.text
    assert "/login/google" not in r.text


async def test_unknown_provider_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/login/facebook")).status_code == 404
    assert (await client.get("/auth/facebook/callback")).status_code == 404
