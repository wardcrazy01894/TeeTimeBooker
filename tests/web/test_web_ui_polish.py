"""The UI polish pass: course NAMES instead of raw course ids, a ranked form that starts with ONE
option row (the rest behind "Add another time slot", still accepted server-side), and a
same-origin progressive-enhancement script referenced without any inline script.

End-to-end over ASGI (real store + clock; only the OAuth HTTP is mocked).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from datetime import date, time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.core.models import CourseId
from teetime.courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID
from teetime.courses.names import COURSE_DISPLAY_NAMES, course_display_name
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RankedWindow
from teetime.web.app import STATIC_DIR, WebSettings, create_app, static_asset_versions
from teetime.web.booking_form import MAX_OPTIONS

from ..tenant.conformance import CUTOFF, MB, OTHER_COURSE
from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_pages import _book
from .test_web_ranked_pages import MEMBER, OCT3, POLICY, SAT, Member, _account, _post, _ranked

NAMES = {str(MB): "Mangrove Bay", str(OTHER_COURSE): "Twin Brooks"}
_TAG = re.compile(r"<[^>]+>")


def _visible_text(html: str) -> str:
    """The page as a reader sees it: tags (and so attribute values) stripped."""
    return _TAG.sub(" ", html)


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        policies={str(MB): POLICY, str(OTHER_COURSE): POLICY},
        cutoff=CUTOFF,
        course_names=NAMES,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@pytest.fixture
async def member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> Member:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(MEMBER, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    a, b = _account(user.id, MB), _account(user.id, OTHER_COURSE)
    await store.upsert_account(a)
    await store.upsert_account(b)
    return Member(user=user, a=a, b=b)


# --- the single source of course names --------------------------------------------------------


def test_mangrove_bay_has_a_display_name_and_unknown_ids_fall_back_to_the_raw_id() -> None:
    assert COURSE_DISPLAY_NAMES[MANGROVE_BAY_COURSE_ID] == "Mangrove Bay"
    assert course_display_name(MANGROVE_BAY_COURSE_ID) == "Mangrove Bay"
    assert course_display_name(CourseId("foreup:0:0")) == "foreup:0:0"


# --- course names on every main page ----------------------------------------------------------


async def test_no_raw_course_id_is_visible_on_any_main_page(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    await _post(client, "/bookings/weekly", {"weekday": str(SAT), **_ranked(member)})
    await _post(client, "/bookings/date", {"target_date": "2026-10-03", **_ranked(member)})
    rows = await store.rows_for_account_date(member.a.id, OCT3)
    await _book(store, next(r for r in rows if r.source.value != "rule"))
    for path in ("/", "/dates", "/rules", "/accounts"):
        page = await client.get(path)
        assert page.status_code == 200, path
        text = _visible_text(page.text)
        assert str(MB) not in text, path
        assert str(OTHER_COURSE) not in text, path
        assert "Mangrove Bay" in text, path
        assert "Twin Brooks" in text, path


async def test_connect_dropdown_shows_names_but_posts_the_course_id(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    # A user with NOTHING connected yet: a connected course leaves the dropdown (2026-10-01).
    await store.upsert_user(make_invited("fresh@example.test"))
    mock_github(
        provider_mock, GitHubIdentity(subject="4242", emails=[("fresh@example.test", True)])
    )
    assert (await sign_in(client)).status_code == 303
    page = await client.get("/accounts")
    assert f'<option value="{MB}">Mangrove Bay</option>' in page.text


async def test_partial_save_message_names_the_course(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore, clock: FakeClock
) -> None:
    await store.create_explicit_row(
        user_id=member.user.id,
        account_id=member.b.id,
        target_date=OCT3,
        options=(RankedWindow(1, time(7), time(8)),),
        party_size=2,
        now=clock.now_utc(),
    )
    resp = await _post(client, "/bookings/date", {"target_date": "2026-10-03", **_ranked(member)})
    assert resp.status_code == 409
    assert "Twin Brooks:" in resp.text
    assert str(OTHER_COURSE) not in _visible_text(resp.text)


# --- the one-row-default ranked form ----------------------------------------------------------


@pytest.mark.parametrize("path", ["/dates", "/rules"])
async def test_ranked_form_shows_one_option_row_up_front(
    client: httpx.AsyncClient, member: Member, path: str
) -> None:
    page = (await client.get(path)).text
    form = page[page.index('class="ranked"') :]
    up_front, _, more = form.partition('<details class="more-options"')
    assert 'name="opt1_account"' in up_front
    assert 'name="opt2_account"' not in up_front
    assert "Add another time slot" in more
    # Without script the extra rows are still in the page, behind the disclosure.
    for i in range(2, MAX_OPTIONS + 1):
        assert f'name="opt{i}_account"' in more


async def test_all_six_options_are_still_accepted(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    form = {"target_date": "2026-10-03", "party_size": "2"}
    for i in range(1, MAX_OPTIONS + 1):
        form |= {
            f"opt{i}_account": str(member.a.id),
            f"opt{i}_earliest": f"{6 + i:02d}:00",
            f"opt{i}_latest": f"{6 + i:02d}:30",
            f"opt{i}_rank": str(i),
        }
    resp = await _post(client, "/bookings/date", form)
    assert resp.status_code == 303
    (row,) = await store.rows_for_account_date(member.a.id, date(2026, 10, 3))
    assert [o.rank for o in row.options] == list(range(1, MAX_OPTIONS + 1))


# --- the progressive-enhancement script -------------------------------------------------------


async def test_script_is_served_same_origin_and_referenced_without_inline_code(
    client: httpx.AsyncClient, member: Member
) -> None:
    js = await client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "more-options" in js.text
    page = (await client.get("/dates")).text
    assert re.search(r'<script src="/static/app\.js\?v=[0-9a-f]{12}" defer></script>', page)
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.DOTALL)
    assert scripts == [""]  # one script tag, external, empty body


# --- static assets are never served stale after a deploy ---------------------------------------
# Dev showed the pre-#268 players/calendar for hours after that deploy: the static files had no
# Cache-Control, so browsers reused their old copies heuristically.


async def test_asset_links_carry_a_content_hash(client: httpx.AsyncClient, member: Member) -> None:
    page = (await client.get("/dates")).text
    for name in ("base.css", "app.js"):
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        assert f'"/static/{name}?v={digest}"' in page
        assert (await client.get(f"/static/{name}?v={digest}")).status_code == 200


async def test_static_files_are_always_revalidated(client: httpx.AsyncClient) -> None:
    for name in ("base.css", "app.js"):
        resp = await client.get(f"/static/{name}")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache"
        etag = resp.headers["etag"]
        again = await client.get(f"/static/{name}", headers={"If-None-Match": etag})
        assert again.status_code == 304  # revalidation is cheap
        assert again.headers["cache-control"] == "no-cache"


def test_asset_version_changes_with_the_content(tmp_path: Path) -> None:
    asset = tmp_path / "base.css"
    asset.write_text("a { color: red; }")
    before = static_asset_versions(tmp_path)["base.css"]
    asset.write_text("a { color: blue; }")
    after = static_asset_versions(tmp_path)["base.css"]
    assert re.fullmatch(r"[0-9a-f]{12}", before) and before != after


def test_nested_assets_are_versioned_by_their_relative_path(tmp_path: Path) -> None:
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "logo.png").write_bytes(b"png")
    assert set(static_asset_versions(tmp_path)) == {"img/logo.png"}


def test_no_template_links_a_static_file_without_its_version() -> None:
    templates = STATIC_DIR.parent / "templates"
    for tpl in templates.rglob("*.html"):
        for attr in re.findall(r"""(?:src|href)\s*=\s*["'](/static/[^"']*)""", tpl.read_text()):
            pytest.fail(f"{tpl.name} links {attr} directly; use static_url()")
