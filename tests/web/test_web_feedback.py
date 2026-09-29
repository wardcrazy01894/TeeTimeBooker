"""Report a bug / Request a course (operator request 2026-09-29): a link on every signed-in page
and wherever a course is chosen, one small form, and an email to the operator."""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.notify import FakeEmailSender
from teetime.web import feedback as feedback_module
from teetime.web.app import WebSettings, create_app

from ..tenant.conformance import MB
from .account_builders import stored_account
from .conftest import OPERATOR_EMAIL, T0, GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_admin_csrf import _csrf

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "teetime" / "web" / "templates"
MEMBER = "turk@example.test"


@pytest.fixture
def sender() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def client(draining_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Sends run after the response; wait for them before asserting what was sent."""
    return draining_client


@pytest.fixture
def app(
    settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock, sender: FakeEmailSender
) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        email_sender=sender,
        course_names={str(MB): "Mangrove Bay"},
    )


@pytest.fixture
async def member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(
        provider_mock, GitHubIdentity(subject="42", name="Turk Golfer", emails=[(MEMBER, True)])
    )
    assert (await sign_in(client)).status_code == 303


@pytest.mark.usefixtures("member")
@pytest.mark.parametrize("path", ["/", "/dates", "/rules", "/accounts"])
async def test_every_signed_in_page_has_a_report_a_bug_link(
    client: httpx.AsyncClient, path: str
) -> None:
    page = (await client.get(path)).text
    # In the top bar, next to Sign out (operator request: more visible than the footer).
    header = page[page.index('<header class="site">') : page.index("</header>")]
    assert f'class="button small report-bug" href="/feedback?kind=bug&amp;from={path}"' in header
    assert ">Report a bug<" in header
    assert "<footer" not in page


def test_the_login_page_has_no_report_link() -> None:
    """Only signed-in people can send one (the form posts as the user)."""
    assert "/feedback" not in (TEMPLATES / "login.html").read_text()


@pytest.mark.usefixtures("member")
async def test_request_a_course_is_offered_where_courses_are_chosen(
    client: httpx.AsyncClient,
) -> None:
    for path in ("/accounts", "/"):  # Connect a course; the dashboard's Start-here card
        page = (await client.get(path)).text
        assert f'href="/feedback?kind=course&amp;from={path}"' in page, path
        assert "Request a course" in page, path


@pytest.mark.usefixtures("member")
@pytest.mark.parametrize(
    ("kind", "heading", "subject"),
    [
        ("bug", "Report a bug", "Bug report from turk"),
        ("course", "Request a course", "Course request from turk"),
    ],
)
async def test_the_form_emails_the_operator_and_thanks_the_user(
    client: httpx.AsyncClient, sender: FakeEmailSender, kind: str, heading: str, subject: str
) -> None:
    form_page = await client.get(f"/feedback?kind={kind}&from=/dates")
    assert f"<h1>{heading}</h1>" in form_page.text
    r = await client.post(
        "/feedback",
        data={
            "csrf_token": _csrf(form_page),
            "kind": kind,
            "from": "/dates",
            "message": "The Sunday times look off.\nAlso Bardmoor please!",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?notice=feedback_sent"
    (mail,) = sender.sent
    assert mail.to == OPERATOR_EMAIL
    assert mail.subject == f"[Spicy's Tee Time Booker] {subject}"
    assert "From: turk <turk@example.test>" in mail.body
    assert "Page: /dates" in mail.body
    assert "The Sunday times look off.\nAlso Bardmoor please!" in mail.body
    thanks = html.unescape((await client.get(r.headers["location"])).text)
    assert "Thanks! Spicy Al will take a look." in thanks


@pytest.mark.usefixtures("member")
async def test_a_mail_problem_still_thanks_them_and_is_audited(
    client: httpx.AsyncClient, sender: FakeEmailSender, store: InMemoryTenantStore
) -> None:
    """The email goes out after the response, so the thanks cannot depend on it; the audit
    entry records the outcome."""
    sender.fail = True
    page = await client.get("/feedback?kind=bug")
    r = await client.post(
        "/feedback", data={"csrf_token": _csrf(page), "kind": "bug", "message": "broken"}
    )
    assert r.headers["location"] == "/?notice=feedback_sent"
    assert store.audit_log[-1].action == "feedback"
    assert store.audit_log[-1].detail["sent"] is False


@pytest.mark.usefixtures("member")
@pytest.mark.parametrize(
    "data",
    [
        {"kind": "bug", "message": "   "},  # empty
        {"kind": "bug", "message": "x" * 4001},  # too long
        {"kind": "spam", "message": "hi"},  # unknown kind
    ],
)
async def test_bad_input_is_refused_and_nothing_is_sent(
    client: httpx.AsyncClient, sender: FakeEmailSender, data: dict[str, str]
) -> None:
    page = await client.get("/feedback?kind=bug")
    r = await client.post("/feedback", data={"csrf_token": _csrf(page), **data})
    assert r.status_code == 400
    assert sender.sent == []


@pytest.mark.usefixtures("member")
async def test_the_from_page_is_only_ever_a_local_path(
    client: httpx.AsyncClient, sender: FakeEmailSender
) -> None:
    page = await client.get("/feedback?kind=bug&from=https://evil.example/x")
    assert "evil.example" not in page.text
    r = await client.post(
        "/feedback",
        data={
            "csrf_token": _csrf(page),
            "kind": "bug",
            "from": "https://evil.example/x",
            "message": "hi",
        },
    )
    assert r.status_code == 303
    assert "Page: (unknown)" in sender.sent[0].body


@pytest.mark.usefixtures("member")
async def test_a_name_cannot_inject_a_header_into_the_subject(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    sender: FakeEmailSender,
) -> None:
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    await store.upsert_user(replace(user, display_name="Evil\r\nBcc: x@y.test"))
    page = await client.get("/feedback?kind=bug")
    await client.post("/feedback", data={"csrf_token": _csrf(page), "kind": "bug", "message": "hi"})
    assert "\r" not in sender.sent[0].subject and "\n" not in sender.sent[0].subject


def test_connected_courses_is_capitalized_everywhere() -> None:
    for tpl in TEMPLATES.rglob("*.html"):
        text = re.sub(r"<[^>]+>", " ", tpl.read_text())
        assert "Connected courses" not in text, tpl.name


@pytest.mark.usefixtures("member")
async def test_at_most_five_reports_an_hour_per_person(
    client: httpx.AsyncClient, sender: FakeEmailSender, clock: FakeClock
) -> None:
    page = await client.get("/feedback?kind=bug")
    token = _csrf(page)

    async def post() -> httpx.Response:
        return await client.post(
            "/feedback", data={"csrf_token": token, "kind": "bug", "message": "hi"}
        )

    for _ in range(5):
        assert (await post()).status_code == 303
    refused = await post()
    assert refused.status_code == 429
    assert len(sender.sent) == 5
    await clock.sleep(timedelta(hours=1, seconds=1).total_seconds())
    assert (await post()).status_code == 303
    assert len(sender.sent) == 6


@pytest.mark.usefixtures("member")
async def test_a_huge_name_is_capped_in_the_subject(
    client: httpx.AsyncClient, store: InMemoryTenantStore, sender: FakeEmailSender
) -> None:
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    await store.upsert_user(replace(user, display_name="N" * 5000))
    page = await client.get("/feedback?kind=bug")
    await client.post("/feedback", data={"csrf_token": _csrf(page), "kind": "bug", "message": "hi"})
    assert len(sender.sent[0].subject) < 150


# --- diagnostics on bug reports (operator request 2026-09-29) ----------------------------------


async def _post_bug(client: httpx.AsyncClient) -> httpx.Response:
    page = await client.get("/feedback?kind=bug&from=/dates")
    return await client.post(
        "/feedback",
        data={"csrf_token": _csrf(page), "kind": "bug", "from": "/dates", "message": "broken"},
        headers={"User-Agent": "TestBrowser/1.0"},
    )


@pytest.mark.usefixtures("member")
async def test_a_bug_report_carries_diagnostics(
    client: httpx.AsyncClient, store: InMemoryTenantStore, sender: FakeEmailSender
) -> None:
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    await store.upsert_account(stored_account(user.id))
    await store.append_audit(
        user_id=user.id, action="row.skip", row_id=None, detail={}, at=T0 - timedelta(minutes=5)
    )
    assert (await _post_bug(client)).status_code == 303
    body = sender.sent[0].body
    diag = body[body.index("--- Diagnostics ---") :]
    assert f"User id: {user.id}" in diag
    assert "Browser: TestBrowser/1.0" in diag
    assert "Environment: (unset) · build (unset)" in diag
    assert "Mangrove Bay: active" in diag  # the course by name, with its status
    assert "row.skip" in diag  # recent actions
    assert "Next 21 days:" in diag


@pytest.mark.usefixtures("member")
async def test_a_course_request_carries_no_diagnostics(
    client: httpx.AsyncClient, sender: FakeEmailSender
) -> None:
    page = await client.get("/feedback?kind=course")
    await client.post(
        "/feedback", data={"csrf_token": _csrf(page), "kind": "course", "message": "Bardmoor"}
    )
    assert "Diagnostics" not in sender.sent[0].body


@pytest.mark.usefixtures("member")
async def test_diagnostics_that_fail_never_block_the_report(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    sender: FakeEmailSender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "list_accounts_for_user", boom)
    assert (await _post_bug(client)).status_code == 303
    assert "(diagnostics unavailable: RuntimeError)" in sender.sent[0].body


@pytest.mark.usefixtures("member")
async def test_slow_diagnostics_never_hold_up_the_report(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    sender: FakeEmailSender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang(*_: object, **__: object) -> None:
        await asyncio.Event().wait()  # never set

    monkeypatch.setattr(store, "list_accounts_for_user", hang)
    monkeypatch.setattr(feedback_module, "DIAGNOSTICS_TIMEOUT_S", 0.05)
    r = await asyncio.wait_for(_post_bug(client), timeout=5)
    assert r.status_code == 303
    assert "(diagnostics unavailable: TimeoutError)" in sender.sent[0].body
