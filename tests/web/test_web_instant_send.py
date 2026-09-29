"""Invite / Resend invite / Report a bug respond at once (operator report 2026-09-29: the page
hung 5-20 s while ACS polled the send status and GitHub filed the issue). The email (and the
issue) go out AFTER the 303 as a background job on ``app.state.background_jobs``; the audit entry
is written by that job once the send has finished, so it stays truthful."""

from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.notify import EmailMessage, EmailSendResult, FakeEmailSender
from teetime.web.app import WebSettings, create_app
from teetime.web.background import BackgroundJobs

from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_admin_csrf import _csrf, _sign_in_operator

FRIEND = "pal@example.test"
MEMBER = "turk@example.test"
STATIC = Path(__file__).resolve().parents[2] / "src" / "teetime" / "web" / "static"
APP_JS = STATIC / "app.js"
BASE_CSS = STATIC / "base.css"


class HangingSender:
    def __init__(self) -> None:
        self.started = 0

    async def send(self, message: EmailMessage) -> EmailSendResult:
        self.started += 1
        await asyncio.Event().wait()  # never set
        raise AssertionError("unreachable")


def _jobs(app: FastAPI) -> BackgroundJobs:
    jobs = app.state.background_jobs
    assert isinstance(jobs, BackgroundJobs)
    return jobs


def _audit(store: InMemoryTenantStore, action: str) -> list[dict[str, object]]:
    return [dict(e.detail) for e in store.audit_log if e.action == action]


# --- BackgroundJobs ----------------------------------------------------------------------------


async def test_background_jobs_run_after_spawn_and_drain_waits_for_them() -> None:
    jobs = BackgroundJobs()
    done: list[int] = []

    async def work() -> None:
        await asyncio.sleep(0)
        done.append(1)

    jobs.spawn(work(), name="work")
    assert jobs.pending == 1
    await jobs.drain(timeout_s=1)
    assert done == [1]
    assert jobs.pending == 0


async def test_drain_cancels_what_is_still_running_at_the_timeout() -> None:
    jobs = BackgroundJobs()
    jobs.spawn(asyncio.Event().wait(), name="hang")
    await jobs.drain(timeout_s=0.05)
    assert jobs.pending == 0


async def test_a_failing_job_is_logged_by_class_name_only(caplog: pytest.LogCaptureFixture) -> None:
    jobs = BackgroundJobs()

    async def boom() -> None:
        raise RuntimeError("secret detail")

    with caplog.at_level(logging.WARNING):
        jobs.spawn(boom(), name="boom")
        await jobs.drain(timeout_s=1)
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


# --- invite / resend ---------------------------------------------------------------------------


async def _admin_post(client: httpx.AsyncClient, data: dict[str, str]) -> httpx.Response:
    token = _csrf(await client.get("/admin/users"))
    return await client.post("/admin/users", data={"csrf_token": token, **data})


async def test_a_hanging_sender_does_not_delay_the_invite(
    settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    sender = HangingSender()
    app = create_app(settings, store=store, clock=clock, email_sender=sender)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _sign_in_operator(client, provider_mock)
        r = await asyncio.wait_for(
            _admin_post(client, {"action": "invite", "email": FRIEND, "role": "member"}),
            timeout=1,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/users?notice=invited"
        # The invite row is written BEFORE the response.
        assert any(u.email == FRIEND for u in await store.list_users())
        await asyncio.sleep(0)
        assert _jobs(app).pending == 1
        assert sender.started == 1
        await _jobs(app).drain(timeout_s=0.05)
        assert _jobs(app).pending == 0


async def test_the_invite_audit_is_written_after_the_send(
    settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    sender = FakeEmailSender()
    app = create_app(settings, store=store, clock=clock, email_sender=sender)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _sign_in_operator(client, provider_mock)
        await _admin_post(client, {"action": "invite", "email": FRIEND, "role": "member"})
        await _jobs(app).drain(timeout_s=1)
        assert [m.to for m in sender.sent] == [FRIEND]
        (detail,) = _audit(store, "admin_invite")
        assert detail["sent"] is True

        sender.fail = True
        pal = next(u for u in await store.list_users() if u.email == FRIEND)
        r = await _admin_post(client, {"action": "resend", "user_id": str(pal.id)})
        assert r.headers["location"] == "/admin/users?notice=resent"
        await _jobs(app).drain(timeout_s=1)
        (detail,) = _audit(store, "admin_resend_invite")
        assert detail["sent"] is False


async def test_the_invite_notices_say_the_email_is_on_its_way(
    settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    app = create_app(settings, store=store, clock=clock, email_sender=FakeEmailSender())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _sign_in_operator(client, provider_mock)
        invited = (await client.get("/admin/users?notice=invited")).text
        resent = (await client.get("/admin/users?notice=resent")).text
    # The page cannot know whether the email arrived (it is sent after the response), so the
    # notice says what to do if it doesn't (review of #288).
    assert (
        "Invite created; the invitation email is on its way. It binds on their first sign-in with Google. If it doesn't arrive within a few minutes, use Resend invite."
        in html.unescape(invited)
    )
    assert (
        "Invitation email is on its way. If it still doesn't arrive, tell them to sign in with Google using that address."
        in html.unescape(resent)
    )


# --- feedback ----------------------------------------------------------------------------------


async def _sign_in_member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(MEMBER, True)]))
    assert (await sign_in(client)).status_code == 303


async def _report(client: httpx.AsyncClient) -> httpx.Response:
    page = await client.get("/feedback?kind=bug&from=/dates")
    return await client.post(
        "/feedback",
        data={"csrf_token": _csrf(page), "kind": "bug", "from": "/dates", "message": "broken"},
    )


async def test_a_hanging_sender_does_not_delay_a_bug_report(
    settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    sender = HangingSender()
    app = create_app(settings, store=store, clock=clock, email_sender=sender)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _sign_in_member(client, store, provider_mock)
        r = await asyncio.wait_for(_report(client), timeout=1)
        assert r.status_code == 303
        assert r.headers["location"] == "/?notice=feedback_sent"
        for _ in range(20):
            await asyncio.sleep(0)
        assert _jobs(app).pending == 1
        await _jobs(app).drain(timeout_s=0.05)
        assert _jobs(app).pending == 0
        assert _audit(store, "feedback") == []  # the send never finished


@pytest.mark.parametrize("fail", [False, True])
async def test_the_feedback_audit_records_the_send_outcome(
    settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
    fail: bool,
) -> None:
    sender = FakeEmailSender(fail=fail)
    app = create_app(settings, store=store, clock=clock, email_sender=sender)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _sign_in_member(client, store, provider_mock)
        r = await _report(client)
        # Always thanked: a failure is logged + audited, never shown.
        assert r.headers["location"] == "/?notice=feedback_sent"
        await _jobs(app).drain(timeout_s=1)
    assert len(sender.sent) == 1
    (detail,) = _audit(store, "feedback")
    assert detail["sent"] is (not fail)
    assert detail["issue"] is False


# --- client side (operator: EVERY button gives immediate feedback when clicked) ---------------
# app.js is plain progressive enhancement with no JS test runner here; these pin that the handlers
# and the styles they rely on exist (the behaviour was checked by hand in a browser).


def test_app_js_marks_every_submitted_form_busy() -> None:
    js = APP_JS.read_text()
    assert 'document.addEventListener("submit", onSubmit)' in js
    assert ".disabled = true" in js
    assert 'setAttribute("aria-busy", "true")' in js
    assert "event.submitter" in js
    # Only after the browser's own validation passed (it fires no submit event otherwise), and
    # never for a submit another handler cancelled.
    assert "event.defaultPrevented" in js


def test_app_js_marks_button_links_busy_but_not_new_tab_clicks() -> None:
    js = APP_JS.read_text()
    assert 'document.addEventListener("click", onButtonLinkClick)' in js
    assert 'closest("a.button")' in js
    for modifier in ("event.button !== 0", "event.ctrlKey", "event.metaKey", "event.shiftKey"):
        assert modifier in js
    assert "preventDefault" not in js.split("function onButtonLinkClick", 1)[1].split("\n  }", 1)[0]


def test_app_js_never_leaves_a_button_stuck_busy() -> None:
    """Review of #288: clear on EVERY pageshow (Firefox keeps `disabled` across a soft reload or a
    non-bfcache Back), re-enable after a 30 s safety timeout (a POST that never navigates), and
    never mark a form that submits into another tab/window (this page stays)."""
    js = APP_JS.read_text()
    assert 'window.addEventListener("pageshow", clearBusy)' in js
    assert "event.persisted" not in js
    assert "BUSY_SAFETY_MS = 30000" in js
    assert 'form.target && form.target !== "_self"' in js


def test_base_css_has_the_busy_spinner_and_a_pressed_state() -> None:
    css = BASE_CSS.read_text()
    assert ".is-busy::after" in css
    assert "@keyframes spin" in css
    assert "prefers-reduced-motion" in css
    for selector in ("button:active", "a.button:active", "summary.button:active"):
        assert selector in css
