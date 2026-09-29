"""Reports also become anonymized GitHub issues in the PUBLIC repo (operator decision 2026-09-29).

The issue carries the message (in a code block, so it cannot @mention anyone or inject markup), the
page, an anonymous reporter tag and anonymized diagnostics: never the reporter's name, email, user
id, account ids or row ids. The operator's email still carries everything, plus the issue link.
Filing is best-effort: a GitHub failure never loses the report.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import respx
from fastapi import FastAPI
from teetime.web.github_issues import GitHubIssues

from teetime.core.clock import FakeClock
from teetime.core.redaction import redact_text
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.notify import FakeEmailSender
from teetime.web.app import WebSettings, create_app

from ..tenant.conformance import MB
from .account_builders import stored_account
from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_admin_csrf import _csrf

REPO = "owner/repo"
ISSUES_URL = f"https://api.github.com/repos/{REPO}/issues"
TOKEN = "github_pat_test_0123456789abcdef"
MEMBER = "turk@example.test"


@pytest.fixture
def sender() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def app(
    settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock, sender: FakeEmailSender
) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        email_sender=sender,
        github_issues=GitHubIssues(REPO, TOKEN),
        course_names={str(MB): "Mangrove Bay"},
    )


@pytest.fixture
async def member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> str:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(MEMBER, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    await store.upsert_account(stored_account(user.id))
    return str(user.id)


async def _report(client: httpx.AsyncClient, kind: str, message: str) -> httpx.Response:
    page = await client.get(f"/feedback?kind={kind}&from=/dates")
    return await client.post(
        "/feedback",
        data={"csrf_token": _csrf(page), "kind": kind, "from": "/dates", "message": message},
    )


async def test_a_bug_report_files_an_anonymized_issue_and_links_it_in_the_email(
    client: httpx.AsyncClient, provider_mock: respx.MockRouter, sender: FakeEmailSender, member: str
) -> None:
    route = provider_mock.post(ISSUES_URL).mock(
        return_value=httpx.Response(
            201, json={"html_url": "https://github.com/owner/repo/issues/7"}
        )
    )
    message = "Cancel failed @everyone\n```\n<script>x</script>"
    assert (await _report(client, "bug", message)).status_code == 303
    assert route.called
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    issue = json.loads(req.content)
    title, body = issue["title"], issue["body"]
    assert title.startswith("[Bug report] Cancel failed")
    assert "@" not in title and "\n" not in title
    # The message sits in a fence the user's own backticks cannot close.
    assert "~~~~text\n" + message + "\n~~~~" in body
    assert "Page: `/dates`" in body
    anon = "r-" + hashlib.sha256(member.encode()).hexdigest()[:8]
    assert anon in body
    assert "Mangrove Bay: active" in body
    # Nothing that identifies the reporter.
    for secret in (MEMBER, "turk", member):
        assert secret not in body and secret not in title
    mail = sender.sent[0]
    assert "GitHub issue: https://github.com/owner/repo/issues/7" in mail.body
    assert member in mail.body  # the operator's copy keeps the full diagnostics


async def test_a_course_request_files_an_issue_too(
    client: httpx.AsyncClient, provider_mock: respx.MockRouter, member: str
) -> None:
    route = provider_mock.post(ISSUES_URL).mock(
        return_value=httpx.Response(
            201, json={"html_url": "https://github.com/owner/repo/issues/8"}
        )
    )
    await _report(client, "course", "Bardmoor Golf, Largo FL")
    issue = json.loads(route.calls.last.request.content)
    assert issue["title"] == "[Course request] Bardmoor Golf, Largo FL"
    assert "Diagnostics" not in issue["body"]


async def test_a_github_failure_never_loses_the_report(
    client: httpx.AsyncClient, provider_mock: respx.MockRouter, sender: FakeEmailSender, member: str
) -> None:
    provider_mock.post(ISSUES_URL).mock(return_value=httpx.Response(500, json={}))
    r = await _report(client, "bug", "boom")
    assert r.headers["location"] == "/?notice=feedback_sent"
    assert "GitHub issue: (not filed)" in sender.sent[0].body


@pytest.mark.usefixtures("member")
async def test_the_form_warns_that_the_message_is_public(client: httpx.AsyncClient) -> None:
    page = (await client.get("/feedback?kind=bug")).text
    assert "posted publicly as a GitHub issue" in page
    assert "without your name or email" in page


async def test_the_token_is_masked_in_logs() -> None:
    GitHubIssues(REPO, TOKEN)
    assert TOKEN not in redact_text(f"auth {TOKEN}")
