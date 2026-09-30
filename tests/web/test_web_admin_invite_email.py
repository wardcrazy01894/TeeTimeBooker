"""Invitation email on /admin/users (operator request 2026-09-29): Invite emails the invitee,
still-invited people get a Resend button, and a mail problem never loses the invite."""

from __future__ import annotations

import logging
import re
from dataclasses import replace

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import UserStatus
from teetime.tenant.notify import FakeEmailSender
from teetime.web.app import WebSettings, create_app

from .conftest import drain_jobs, make_invited
from .test_web_admin_csrf import _csrf, _sign_in_operator
from .test_web_admin_users_list import _member_with_a_weekly_booking

FRIEND = "pal@example.test"


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
    return create_app(settings, store=store, clock=clock, email_sender=sender)


async def _post(client: httpx.AsyncClient, data: dict[str, str]) -> httpx.Response:
    token = _csrf(await client.get("/admin/users"))
    return await client.post("/admin/users", data={"csrf_token": token, **data})


async def test_invite_emails_the_invitee(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    sender: FakeEmailSender,
    settings: WebSettings,
) -> None:
    await _sign_in_operator(client, provider_mock)
    r = await _post(client, {"action": "invite", "email": FRIEND, "role": "member"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users?notice=invited"
    (mail,) = sender.sent
    assert mail.to == FRIEND
    assert mail.subject == "You're invited to Spicy's Tee Time Booker!"
    assert f"Go to {settings.public_base_url}" in mail.body
    assert f"({FRIEND})" in mail.body
    page = (await client.get(r.headers["location"])).text
    assert "the invitation email is on its way" in page
    assert store.audit_log[-1].detail["sent"] is True


async def test_a_failed_email_keeps_the_invite_and_says_so(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    sender: FakeEmailSender,
) -> None:
    sender.fail = True
    await _sign_in_operator(client, provider_mock)
    r = await _post(client, {"action": "invite", "email": FRIEND, "role": "member"})
    # Sent after the response: the notice cannot know; the audit records the failure.
    assert r.headers["location"] == "/admin/users?notice=invited"
    assert any(u.email == FRIEND for u in await store.list_users())
    assert store.audit_log[-1].action == "admin_invite"
    assert store.audit_log[-1].detail["sent"] is False


async def test_without_email_configured_the_invite_still_works(
    settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    app = create_app(settings, store=store, clock=clock)  # no email_sender
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _sign_in_operator(client, provider_mock)
        r = await _post(client, {"action": "invite", "email": FRIEND, "role": "member"})
        await drain_jobs(app)
    assert r.headers["location"] == "/admin/users?notice=invited"
    assert any(u.email == FRIEND for u in await store.list_users())
    assert store.audit_log[-1].detail["sent"] is False


async def test_resend_is_offered_to_still_invited_people_only_and_sends_again(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    sender: FakeEmailSender,
) -> None:
    await _member_with_a_weekly_booking(client, store, provider_mock)  # an ACTIVE member
    pal = make_invited(FRIEND)
    await store.upsert_user(pal)
    await _sign_in_operator(client, provider_mock)
    page = (await client.get("/admin/users")).text
    rows = re.findall(r"<tr\b.*?</tr>", page, re.DOTALL)
    pal_row = next(tr for tr in rows if FRIEND in tr)
    assert 'value="resend"' in pal_row and f'name="user_id" value="{pal.id}"' in pal_row
    assert not any('value="resend"' in tr for tr in rows if "turk@" in tr)

    r = await _post(client, {"action": "resend", "user_id": str(pal.id)})
    assert r.headers["location"] == "/admin/users?notice=resent"
    assert [m.to for m in sender.sent] == [FRIEND]


async def test_resend_refuses_someone_who_already_signed_in_or_does_not_exist(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    sender: FakeEmailSender,
) -> None:
    turk = await _member_with_a_weekly_booking(client, store, provider_mock)
    assert turk.status is UserStatus.ACTIVE
    await _sign_in_operator(client, provider_mock)
    assert (await _post(client, {"action": "resend", "user_id": str(turk.id)})).status_code == 400
    missing = "00000000-0000-4000-8000-000000000000"
    assert (await _post(client, {"action": "resend", "user_id": missing})).status_code == 404
    assert (await _post(client, {"action": "resend", "user_id": "nope"})).status_code == 400
    assert sender.sent == []


# --- uninvite (operator request 2026-09-29) --------------------------------------------------


async def test_uninvite_is_offered_to_still_invited_people_only_and_asks_first(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _member_with_a_weekly_booking(client, store, provider_mock)  # an ACTIVE member
    pal = make_invited(FRIEND)
    await store.upsert_user(pal)
    await _sign_in_operator(client, provider_mock)
    page = (await client.get("/admin/users")).text
    rows = re.findall(r"<tr\b.*?</tr>", page, re.DOTALL)
    pal_row = next(tr for tr in rows if FRIEND in tr)
    # A disclosure: one click reveals the real (confirming) button, so a stray tap can't uninvite.
    assert '<summary class="button small danger">Uninvite</summary>' in pal_row
    assert 'value="uninvite"' in pal_row and f'name="user_id" value="{pal.id}"' in pal_row
    assert not any('value="uninvite"' in tr for tr in rows if "turk@" in tr)


async def test_uninvite_removes_the_invite_and_audits_it(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    pal = make_invited(FRIEND)
    await store.upsert_user(pal)
    await _sign_in_operator(client, provider_mock)
    r = await _post(client, {"action": "uninvite", "user_id": str(pal.id)})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users?notice=uninvited"
    assert await store.get_user_unscoped(pal.id) is None
    assert not any(u.email == FRIEND for u in await store.list_users())
    entry = store.audit_log[-1]
    assert (entry.action, entry.detail["uninvited_user_id"]) == ("admin_uninvite", str(pal.id))
    assert "uninvited" in (await client.get(r.headers["location"])).text.lower()


async def test_uninvite_refuses_someone_who_signed_in_or_does_not_exist(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    turk = await _member_with_a_weekly_booking(client, store, provider_mock)
    await _sign_in_operator(client, provider_mock)
    r = await _post(client, {"action": "uninvite", "user_id": str(turk.id)})
    assert r.status_code == 400 and "Disable" in r.text
    assert await store.get_user_unscoped(turk.id) is not None
    missing = "00000000-0000-4000-8000-000000000000"
    assert (await _post(client, {"action": "uninvite", "user_id": missing})).status_code == 404
    assert (await _post(client, {"action": "uninvite", "user_id": "nope"})).status_code == 400


async def test_an_undelivered_invitation_logs_the_acs_error(
    client: httpx.AsyncClient,
    provider_mock: respx.MockRouter,
    sender: FakeEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender.fail = True
    caplog.set_level(logging.WARNING, logger="teetime.web.app")
    await _sign_in_operator(client, provider_mock)
    await _post(client, {"action": "invite", "email": FRIEND, "role": "member"})
    (line,) = [r.getMessage() for r in caplog.records if "invitation email not" in r.getMessage()]
    assert "error=" in line


@pytest.mark.parametrize(
    ("status", "notice", "text"),
    [
        (UserStatus.INVITED, "already_invited", "Use Resend invite"),
        (UserStatus.ACTIVE, "already_member", "already has an account"),
        (UserStatus.DISABLED, "already_member", "use Enable"),
    ],
)
async def test_inviting_an_address_already_on_the_list_creates_no_second_row(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    sender: FakeEmailSender,
    status: UserStatus,
    notice: str,
    text: str,
) -> None:
    """Scan 2026-09-30: a second INVITED row for one address outlived an Uninvite of the first
    (whichever row sorts first binds), so "can no longer sign in" was false."""
    existing = replace(make_invited(FRIEND), status=status)
    await store.upsert_user(existing)
    await _sign_in_operator(client, provider_mock)
    r = await _post(client, {"action": "invite", "email": FRIEND.upper(), "role": "member"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/users?notice={notice}"
    assert [u.id for u in await store.list_users() if u.email == FRIEND] == [existing.id]
    assert sender.sent == []
    page = (await client.get(r.headers["location"])).text
    assert text in page


async def test_invite_refuses_an_unknown_role(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _sign_in_operator(client, provider_mock)
    r = await _post(client, {"action": "invite", "email": FRIEND, "role": "overlord"})
    assert r.status_code == 400
    assert not any(u.email == FRIEND for u in await store.list_users())
