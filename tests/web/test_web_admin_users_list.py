"""The operator's user list on /admin/users: everyone invited, whether they have signed in, what
they have set up, and a one-click disable / enable (operator request 2026-09-29)."""

from __future__ import annotations

import asyncio
import re
from datetime import date, time
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RankedWindow, RuleId, StandingRule, User, UserStatus
from teetime.web import app as app_module
from teetime.web.app import WebSettings, create_app

from ..tenant.conformance import MB
from .account_builders import stored_account
from .conftest import T0, GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_admin_csrf import _csrf, _sign_in_operator

OCT3 = date(2026, 10, 3)  # a Saturday inside the 21-day window from T0
_TAG = re.compile(r"<[^>]+>")


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(settings, store=store, clock=clock, course_names={str(MB): "Mangrove Bay"})


def _text(html: str) -> str:
    return " ".join(_TAG.sub(" ", html).split())


def _row(html: str, email: str) -> str:
    """The visible text of the table row that names ``email``."""
    for tr in re.findall(r"<tr\b.*?</tr>", html, re.DOTALL):
        if email in tr:
            return _text(tr)
    raise AssertionError(f"no row for {email}")


async def _member_with_a_weekly_booking(
    client: httpx.AsyncClient, store: InMemoryTenantStore, router: respx.MockRouter
) -> User:
    """Turk signs in (binding the invite), connects Mangrove Bay and has one weekly rule with
    one upcoming pending date."""
    await store.upsert_user(make_invited("turk@example.test"))
    mock_github(router, GitHubIdentity(subject="42", emails=[("turk@example.test", True)]))
    assert (await sign_in(client)).status_code == 303
    turk = await store.get_user_by_subject("github", "42")
    assert turk is not None
    account = stored_account(turk.id)
    await store.upsert_account(account)
    rule = await store.upsert_rule(
        StandingRule(
            id=RuleId(uuid4()),
            course_account_id=account.id,
            weekday=OCT3.weekday(),
            options=(RankedWindow(1, time(8, 0), time(10, 0)),),
            party_size=2,
            active=True,
            materialized_through=None,
            version=1,
        ),
        user_id=turk.id,
    )
    assert await store.insert_rule_row_if_absent(rule, OCT3, now=T0) is not None
    client.cookies.clear()
    return turk


async def test_operator_sees_every_user_and_what_they_set_up(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _member_with_a_weekly_booking(client, store, provider_mock)
    await store.upsert_user(make_invited("pending.pal@example.test"))
    await _sign_in_operator(client, provider_mock)
    page = (await client.get("/admin/users")).text

    invited = _row(page, "pending.pal@example.test")
    assert "Invited" in invited and "hasn't signed in" in invited

    turk = _row(page, "turk@example.test")
    assert "Active" in turk and "GitHub" in turk and "Member" in turk
    assert "Mangrove Bay" in turk and str(MB) not in turk  # a course NAME, never an id
    assert "1 weekly" in turk
    assert "1 pending" in turk

    me = _row(page, "operator@example.test")
    assert "Operator" in me


async def test_row_buttons_disable_and_enable_by_the_bound_identity(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    turk = await _member_with_a_weekly_booking(client, store, provider_mock)
    await store.upsert_user(make_invited("pending.pal@example.test"))
    await _sign_in_operator(client, provider_mock)
    resp = await client.get("/admin/users")
    page = resp.text
    turk_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", page, re.DOTALL) if "turk@" in tr)
    assert 'name="subject" value="42"' in turk_tr
    assert 'value="disable"' in turk_tr
    me_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", page, re.DOTALL) if "operator@" in tr)
    assert 'value="disable"' not in me_tr  # no self-disable button
    pal_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", page, re.DOTALL) if "pending.pal" in tr)
    assert 'value="disable"' not in pal_tr  # nothing bound to disable yet

    r = await client.post(
        "/admin/users",
        data={
            "csrf_token": _csrf(resp),
            "action": "disable",
            "provider": "github",
            "subject": "42",
        },
    )
    assert r.status_code == 303
    got = await store.get_user_unscoped(turk.id)
    assert got is not None and got.status is UserStatus.DISABLED
    after = (await client.get("/admin/users")).text
    assert "Disabled" in _row(after, "turk@example.test")
    turk_tr = next(tr for tr in re.findall(r"<tr\b.*?</tr>", after, re.DOTALL) if "turk@" in tr)
    assert 'value="enable"' in turk_tr


# --- uninvited sign-in attempts ------------------------------------------------------------------


async def _stranger_tries(
    client: httpx.AsyncClient, router: respx.MockRouter, *, subject: str = "777"
) -> httpx.Response:
    mock_github(
        router,
        GitHubIdentity(
            subject=subject,
            name="Stranger Danger",
            emails=[("stranger@example.test", True), ("unverified@example.test", False)],
        ),
    )
    resp = await sign_in(client)
    client.cookies.clear()
    return resp


async def test_uninvited_signin_is_remembered_with_its_verified_emails_only(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    assert (await _stranger_tries(client, provider_mock)).status_code == 403
    assert (await _stranger_tries(client, provider_mock)).status_code == 403
    (rec,) = await store.list_rejected_signins(now=T0)
    assert (rec.provider, rec.subject, rec.attempts) == ("github", "777", 2)
    assert rec.emails == ("stranger@example.test",)  # an unverified address is never kept


async def test_a_failure_to_remember_never_changes_the_403(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**_: object) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "record_rejected_signin", boom)
    assert (await _stranger_tries(client, provider_mock)).status_code == 403


async def test_operator_sees_who_tried_and_can_invite_them(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    await _stranger_tries(client, provider_mock)
    await _stranger_tries(client, provider_mock)
    await _sign_in_operator(client, provider_mock)
    resp = await client.get("/admin/users")
    section = resp.text[resp.text.index("Tried to sign in") :]
    row = _row(section, "stranger@example.test")
    assert "GitHub" in row and "Stranger Danger" in row and "2" in row
    assert "unverified@example.test" not in resp.text
    assert 'name="action" value="invite"' in section
    assert 'name="email" value="stranger@example.test"' in section

    r = await client.post(
        "/admin/users",
        data={
            "csrf_token": _csrf(resp),
            "action": "invite",
            "email": "stranger@example.test",
            "role": "member",
        },
    )
    assert r.status_code == 303
    # Invited now: the attempt drops off the list (the person is on the People table instead).
    after = (await client.get("/admin/users")).text
    assert "Invited" in _row(after, "stranger@example.test")
    assert "stranger@example.test" not in after[after.index("Tried to sign in") :]


async def test_a_pathological_profile_is_capped_before_it_is_stored(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    emails = [(f"e{i}@example.test", True) for i in range(20)]
    mock_github(provider_mock, GitHubIdentity(subject="888", name="N" * 5000, emails=emails))
    assert (await sign_in(client)).status_code == 403
    (rec,) = await store.list_rejected_signins(now=T0)
    assert len(rec.display_name) == app_module.REJECTED_NAME_MAX_LEN
    assert rec.emails == tuple(e for e, _ in emails[: app_module.REJECTED_EMAILS_MAX])


async def test_a_hung_store_never_holds_up_the_403(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang(**_: object) -> None:
        await asyncio.Event().wait()  # never set

    monkeypatch.setattr(store, "record_rejected_signin", hang)
    monkeypatch.setattr(app_module, "REJECTED_SIGNIN_WRITE_TIMEOUT_S", 0.05)
    resp = await asyncio.wait_for(_stranger_tries(client, provider_mock), timeout=5)
    assert resp.status_code == 403


async def test_repeat_uninvited_signins_write_one_audit_doc_per_cooldown(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    """Scan 2026-09-30: anyone can script the OAuth round trip, and each rejection used to add a
    400-day audit doc. The rejected_signin record still counts every attempt; the audit trail
    (SF10) records a subject at most once per cooldown."""
    for _ in range(5):
        assert (await _stranger_tries(client, provider_mock)).status_code == 403
    audits = [e for e in store.audit_log if e.action == "signin_rejected_not_invited"]
    assert len(audits) == 1
    (rec,) = await store.list_rejected_signins(now=T0)
    assert rec.attempts == 5

    await _stranger_tries(client, provider_mock, subject="778")  # another subject: audited
    await clock.sleep(app_module.REJECTED_AUDIT_COOLDOWN.total_seconds())
    await _stranger_tries(client, provider_mock)  # the cooldown passed: audited again
    audits = [e for e in store.audit_log if e.action == "signin_rejected_not_invited"]
    assert [a.detail["subject"] for a in audits] == ["777", "778", "777"]


async def test_a_hung_audit_write_never_holds_up_the_403(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang(**_: object) -> None:
        await asyncio.Event().wait()  # never set

    monkeypatch.setattr(store, "append_audit", hang)
    monkeypatch.setattr(app_module, "REJECTED_SIGNIN_WRITE_TIMEOUT_S", 0.05)
    resp = await asyncio.wait_for(_stranger_tries(client, provider_mock), timeout=5)
    assert resp.status_code == 403


async def test_a_rejected_signin_is_logged_without_the_subject_or_emails(
    client: httpx.AsyncClient,
    provider_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="teetime.web.app")
    await _stranger_tries(client, provider_mock)
    lines = [r.getMessage() for r in caplog.records if "signin rejected" in r.getMessage()]
    assert lines == ["signin rejected provider=github reason=not_invited"]


async def test_enable_shows_its_notice(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    turk = await _member_with_a_weekly_booking(client, store, provider_mock)
    await _sign_in_operator(client, provider_mock)
    for action, notice, text in (
        ("disable", "disabled", "User disabled."),
        ("enable", "enabled", "User enabled."),
    ):
        resp = await client.get("/admin/users")
        r = await client.post(
            "/admin/users",
            data={
                "csrf_token": _csrf(resp),
                "action": action,
                "provider": "github",
                "subject": "42",
            },
        )
        assert r.headers["location"] == f"/admin/users?notice={notice}"
        assert text in (await client.get(r.headers["location"])).text
    got = await store.get_user_unscoped(turk.id)
    assert got is not None and got.status is UserStatus.ACTIVE
