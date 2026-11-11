"""Every page ends with the site footer's credit line, "A Spicy Al Software Solution": it lives
in the shared layout (`base.html`), after `<main>`, as the last thing in `<body>`, so no page can
miss it. Checked signed out (sign-in page), signed in (dashboard) and on an error page (403)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import respx

from teetime.tenant.in_memory_store import InMemoryTenantStore

from .conftest import GitHubIdentity, make_invited, mock_github, sign_in

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "teetime" / "web" / "templates"
CREDIT = "A Spicy Al Software Solution"
# The footer is the LAST element of the page: after </main>, nothing but </body></html> follows.
_PAGE_END = re.compile(
    r"</main>\s*<footer class=\"site-footer\">\s*<p>"
    + re.escape(CREDIT)
    + r"</p>\s*</footer>\s*</body>\s*</html>\s*\Z"
)


def _assert_ends_with_credit(html: str) -> None:
    assert html.count(CREDIT) == 1
    assert _PAGE_END.search(html), html[-400:]


async def test_the_sign_in_page_ends_with_the_credit_line(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    _assert_ends_with_credit(response.text)


async def test_a_signed_in_page_ends_with_the_credit_line(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await store.upsert_user(make_invited("turk@example.test"))
    mock_github(provider_mock, GitHubIdentity(emails=[("turk@example.test", True)]))
    assert (await sign_in(client)).status_code == 303
    response = await client.get("/")
    assert response.status_code == 200
    _assert_ends_with_credit(response.text)


async def test_an_error_page_ends_with_the_credit_line(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await store.upsert_user(make_invited("turk@example.test"))
    mock_github(provider_mock, GitHubIdentity(emails=[("turk@example.test", True)]))
    assert (await sign_in(client)).status_code == 303
    # A member asking for an operator page gets the app's own error page (forbidden.html).
    response = await client.get("/admin/users")
    assert response.status_code == 403
    _assert_ends_with_credit(response.text)


def test_every_page_template_uses_the_shared_layout() -> None:
    """The credit is on every page only because every page extends `base.html`; a template that
    builds its own `<html>` would silently drop it. Partials (`_name.html`) are included, not
    served."""
    pages = [p for p in TEMPLATES.glob("*.html") if not p.name.startswith("_")]
    assert len(pages) > 5
    for page in pages:
        if page.name == "base.html":
            continue
        assert page.read_text().lstrip().startswith('{% extends "base.html" %}'), page.name
