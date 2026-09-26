"""MU-14 pages: /accounts (connect, re-verify, refresh) and Cancel on /dates (MULTIUSER_PLAN §8.2,
§8.4-§8.6, §9).

End-to-end over ASGI against a real ``InMemoryTenantStore`` + ``FakeClock``; the only fakes are
the OAuth provider's HTTP (conftest) and the ForeUP adapter at the ``AdapterFactory`` boundary.
Every write goes through the page's own form POST with the session's CSRF token.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.routing import Route

from teetime.core.clock import FakeClock
from teetime.core.models import BookingOutcome, BookingResult
from teetime.core.redaction import redact_text
from teetime.core.release_policy import ReleasePolicy
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    Actor,
    BookingSource,
    BookingState,
    CourseAccount,
    OwnedBooking,
    OwnedBookingId,
    RankedWindow,
    RequestRow,
    RowStatus,
    RuleId,
    StandingRule,
    User,
    UserId,
    UserRole,
    UserStatus,
    derive_account_id,
)
from teetime.tenant.store import RowOutcome
from teetime.web import services
from teetime.web.app import WebSettings, create_app
from teetime.web.routes import ROUTES, AuthLevel

from ..tenant.conformance import CUTOFF, MB, TZ
from .account_builders import (
    KEYRING,
    PASSWORD,
    ProbeAdapter,
    ProbeFactory,
    reservation,
    stored_account,
)
from .conftest import T0, GitHubIdentity, make_invited, mock_github, sign_in

MEMBER = "turk@example.test"
POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
OCT3 = date(2026, 10, 3)
TEE = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)
RAW = "9001"


@pytest.fixture
def factory() -> ProbeFactory:
    adapter = ProbeAdapter()
    adapter.set_existing_reservations([reservation(RAW, TEE)])
    return ProbeFactory(adapter=adapter)


@pytest.fixture
def live_settings(settings: WebSettings) -> WebSettings:
    return replace(settings, dry_run=False)


def _app(
    settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock, factory: ProbeFactory
) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        keyring=KEYRING,
        adapter_factory=factory,
        policies={str(MB): POLICY},
        cutoff=CUTOFF,
    )


@pytest.fixture
def app(
    live_settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    factory: ProbeFactory,
) -> FastAPI:
    return _app(live_settings, store, clock, factory)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@dataclass(frozen=True)
class Member:
    user: User
    account: CourseAccount | None


async def _sign_in(
    client: httpx.AsyncClient, store: InMemoryTenantStore, router: respx.MockRouter
) -> User:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(router, GitHubIdentity(subject="42", emails=[(MEMBER, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    return user


@pytest.fixture
async def newcomer(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> Member:
    """Signed in, no course account yet."""
    return Member(user=await _sign_in(client, store, provider_mock), account=None)


@pytest.fixture
async def member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> Member:
    """Signed in, with a connected account whose ciphertext really decrypts."""
    user = await _sign_in(client, store, provider_mock)
    account = stored_account(user.id)
    await store.upsert_account(account)
    return Member(user=user, account=account)


@pytest.fixture
async def mallory(store: InMemoryTenantStore) -> Member:
    user = User(
        id=UserId(uuid4()),
        oauth_provider="github",
        oauth_subject="666",
        email="mallory@example.test",
        display_name="Mallory",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    await store.upsert_user(user)
    account = stored_account(user.id, username="mallory@golf.example")
    await store.upsert_account(account)
    return Member(user=user, account=account)


def _csrf(page: httpx.Response) -> str:
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def _post(client: httpx.AsyncClient, path: str, data: dict[str, str]) -> httpx.Response:
    token = _csrf(await client.get("/accounts"))
    return await client.post(path, data={"csrf_token": token, **data})


async def _booked_row(
    store: InMemoryTenantStore, account: CourseAccount, *, owned: bool = True
) -> RequestRow:
    rule = await store.upsert_rule(
        StandingRule(
            id=RuleId(uuid4()),
            course_account_id=account.id,
            weekday=OCT3.weekday(),
            options=(
                RankedWindow(
                    1,
                    time(8, 0),
                    time(10, 0),
                ),
            ),
            party_size=2,
            active=True,
            materialized_through=None,
            version=1,
        ),
        user_id=account.user_id,
    )
    row = await store.insert_rule_row_if_absent(rule, OCT3, now=T0)
    assert row is not None
    owner = "booker:test"
    await store.claim_rows([row.id], owner=owner, until=T0 + timedelta(minutes=5), now=T0)
    booking = OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=account.id,
        course_id=row.course_id,
        target_date=OCT3,
        raw_reservation_id=RAW,
        tee_time=TEE,
        party_size=2,
        source=BookingSource.BLIND,
        state=BookingState.HELD,
    )
    await store.record_outcomes(
        [
            RowOutcome(
                row_id=row.id,
                course_account_id=account.id,
                target_date=OCT3,
                actor=Actor.BOOKING_RUNNER,
                to_status=RowStatus.BOOKED,
                last_outcome="booked",
                at=T0,
                result=BookingResult(
                    request_id=row.request_id,
                    outcome=BookingOutcome.BOOKED,
                    course_id=row.course_id,
                    slot=None,
                    confirmation_code=f"TTB:{RAW}",
                    booked_at=T0,
                    attempts=1,
                ),
                booking=booking if owned else None,
                release_lease_owner=owner,
            )
        ]
    )
    got = await store.get_row(row.id, user_id=account.user_id)
    assert got is not None and got.status is RowStatus.BOOKED
    return got


# --- the route contract --------------------------------------------------------------------------


def test_every_mu14_route_is_bound_and_idor_covered(app: FastAPI) -> None:
    bound = {(m, r.path) for r in app.routes if isinstance(r, Route) for m in (r.methods or set())}
    mu14 = [s for s in ROUTES if s.milestone == "MU-14"]
    assert {(s.method, s.path) for s in mu14} == {
        ("GET", "/accounts"),
        ("POST", "/accounts/connect"),
        ("POST", "/accounts/{id}/reverify"),
        ("POST", "/accounts/{id}/refresh"),
        ("POST", "/rows/{id}/cancel"),
    }
    covered = {"/accounts/{id}/reverify", "/accounts/{id}/refresh", "/rows/{id}/cancel"}
    for spec in mu14:
        assert (spec.method, spec.path) in bound, spec
        assert spec.auth is AuthLevel.USER, spec
        assert callable(getattr(services, spec.handler, None)), spec
        if spec.method == "POST":
            assert spec.csrf, spec
            # connect names no id: the account id is derived from the SESSION user
            assert spec.path in covered or spec.path == "/accounts/connect", spec


async def test_route_rejects_other_users_account_and_row(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    factory: ProbeFactory,
    member: Member,
    mallory: Member,
) -> None:
    """Another user's account / row id gets EXACTLY the missing-id response, and ForeUP is never
    called (IDOR, §9.1)."""
    assert mallory.account is not None
    victim_row = await _booked_row(store, mallory.account)
    cases = [
        ("/accounts/{id}/refresh", str(mallory.account.id), {}),
        ("/accounts/{id}/reverify", str(mallory.account.id), {"password": PASSWORD}),
        ("/rows/{id}/cancel", str(victim_row.id), {"confirm_unowned": "on"}),
    ]
    for path, victim_id, form in cases:
        foreign = await _post(client, path.format(id=victim_id), form)
        missing = await _post(client, path.format(id=uuid4()), form)
        garbage = await _post(client, path.format(id="not-a-uuid"), form)
        assert foreign.status_code == missing.status_code == garbage.status_code == 404, path
        assert foreign.text == missing.text
    assert factory.calls == []
    assert await store.get_row(victim_row.id, user_id=mallory.user.id) == victim_row
    assert await store.get_snapshot(mallory.account.id) is None


# --- connect -----------------------------------------------------------------------------------


async def test_connect_via_page(
    client: httpx.AsyncClient, store: InMemoryTenantStore, newcomer: Member
) -> None:
    page = await client.get("/accounts")
    assert page.status_code == 200
    assert 'action="/accounts/connect"' in page.text
    assert 'type="password"' in page.text
    r = await _post(
        client,
        "/accounts/connect",
        {"course_id": str(MB), "username": "turk@golf.example", "password": PASSWORD},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/accounts?notice=account_connected"
    account = await store.get_account(
        derive_account_id(newcomer.user.id, MB), user_id=newcomer.user.id
    )
    assert account is not None and account.username == "turk@golf.example"
    listed = await client.get(r.headers["location"])
    assert "turk@golf.example" in listed.text
    assert f'action="/accounts/{account.id}/refresh"' in listed.text


async def test_connect_rejects_unknown_course(
    client: httpx.AsyncClient, factory: ProbeFactory, newcomer: Member
) -> None:
    r = await _post(
        client,
        "/accounts/connect",
        {"course_id": "foreup:1:1", "username": "turk@golf.example", "password": PASSWORD},
    )
    assert r.status_code == 400
    assert factory.calls == []


async def test_connect_rate_limited_is_429(
    client: httpx.AsyncClient, store: InMemoryTenantStore, factory: ProbeFactory, newcomer: Member
) -> None:
    for _ in range(services.ProbeLimits().per_user_per_hour):
        await store.record_login_probe(
            user_id=newcomer.user.id, course_id=MB, username_hash="x", ok=False, at=T0
        )
    r = await _post(
        client,
        "/accounts/connect",
        {"course_id": str(MB), "username": "turk@golf.example", "password": PASSWORD},
    )
    assert r.status_code == 429
    assert "Too many login attempts" in r.text
    assert factory.calls == []


async def test_connect_password_never_logged_or_echoed(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    factory: ProbeFactory,
    newcomer: Member,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The plaintext password is never echoed back in a page (success or failure), never
    logged by any logger, never in the audit log, and never stored unencrypted (§9.2/§9.4)."""
    caplog.set_level(logging.DEBUG)
    form = {"course_id": str(MB), "username": "turk@golf.example", "password": PASSWORD}
    factory.adapter.set_auth_soft_fail()
    failed = await _post(client, "/accounts/connect", form)
    assert failed.status_code == 409
    assert "Login failed" in failed.text
    factory.adapter.set_auth_soft_fail(False)
    await clock.sleep(16 * 60)  # the failed probe counts toward the username lockout
    ok = await _post(client, "/accounts/connect", form)
    assert ok.status_code == 303
    pages = [failed.text, ok.text, (await client.get("/accounts")).text]
    for html in pages:
        assert PASSWORD not in html
    for record in caplog.records:
        assert PASSWORD not in f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
    for entry in store.audit_log:
        assert PASSWORD not in repr(entry)
    account = await store.get_account(
        derive_account_id(newcomer.user.id, MB), user_id=newcomer.user.id
    )
    assert account is not None
    assert PASSWORD not in repr(account)
    assert PASSWORD not in redact_text(f"x {PASSWORD} y")  # E7-registered for the process


# --- refresh + re-verify -------------------------------------------------------------------------


async def test_refresh_via_page(
    client: httpx.AsyncClient, store: InMemoryTenantStore, factory: ProbeFactory, member: Member
) -> None:
    assert member.account is not None
    r = await _post(client, f"/accounts/{member.account.id}/refresh", {})
    assert r.status_code == 303
    assert r.headers["location"] == "/accounts?notice=refreshed"
    snap = await store.get_snapshot(member.account.id)
    assert snap is not None and [e.raw_id for e in snap.entries] == [RAW]
    again = await _post(client, f"/accounts/{member.account.id}/refresh", {})
    assert again.status_code == 303
    assert factory.adapter.authenticate_call_count == 1  # served from the TTL cache


async def test_refresh_untrusted_via_page_is_409(
    client: httpx.AsyncClient, store: InMemoryTenantStore, factory: ProbeFactory, member: Member
) -> None:
    assert member.account is not None
    factory.adapter.trusted = False
    r = await _post(client, f"/accounts/{member.account.id}/refresh", {})
    assert r.status_code == 409
    assert "read your reservations" in r.text
    assert await store.get_snapshot(member.account.id) is None


async def test_reverify_via_page(
    client: httpx.AsyncClient, store: InMemoryTenantStore, factory: ProbeFactory, member: Member
) -> None:
    assert member.account is not None
    r = await _post(client, f"/accounts/{member.account.id}/reverify", {"password": PASSWORD})
    assert r.status_code == 303
    assert r.headers["location"] == "/accounts?notice=account_verified"
    assert factory.adapter.credentials == [(member.account.username, PASSWORD)]


# --- cancel -----------------------------------------------------------------------------------


async def test_cancel_via_dates_page(
    client: httpx.AsyncClient, store: InMemoryTenantStore, factory: ProbeFactory, member: Member
) -> None:
    assert member.account is not None
    row = await _booked_row(store, member.account)
    page = await client.get("/dates")
    assert f'action="/rows/{row.id}/cancel"' in page.text
    assert "coming soon" not in page.text
    assert 'name="confirm_unowned"' not in page.text  # owned: no confirm needed
    r = await _post(client, f"/rows/{row.id}/cancel", {})
    assert r.status_code == 303
    assert r.headers["location"] == "/dates?notice=cancelled"
    got = await store.get_row(row.id, user_id=member.user.id)
    assert got is not None and (got.status, got.status_reason) == (RowStatus.CANCELLED, "user")
    assert factory.adapter.cancel_call_count == 1
    assert "cancelled by you" in (await client.get("/dates?notice=cancelled")).text


async def test_unowned_cancel_page_requires_confirm(
    client: httpx.AsyncClient, store: InMemoryTenantStore, factory: ProbeFactory, member: Member
) -> None:
    assert member.account is not None
    row = await _booked_row(store, member.account, owned=False)
    page = await client.get("/dates")
    assert 'name="confirm_unowned"' in page.text
    assert "made by TeeTimeBooker" in page.text
    refused = await _post(client, f"/rows/{row.id}/cancel", {})
    assert refused.status_code == 409
    assert factory.calls == []
    r = await _post(client, f"/rows/{row.id}/cancel", {"confirm_unowned": "on"})
    assert r.status_code == 303
    assert factory.adapter.cancel_call_count == 1


async def test_cancel_in_dry_run_env_via_page(
    settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    factory: ProbeFactory,
    provider_mock: respx.MockRouter,
) -> None:
    assert settings.dry_run  # the default
    app = _app(settings, store, clock, factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        user = await _sign_in(client, store, provider_mock)
        account = stored_account(user.id)
        await store.upsert_account(account)
        row = await _booked_row(store, account)
        r = await _post(client, f"/rows/{row.id}/cancel", {})
    assert r.status_code == 409
    assert "dry-run" in r.text
    assert factory.calls == []


async def test_account_actions_without_keyring_are_refused(
    live_settings: WebSettings,
    store: InMemoryTenantStore,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    """An app built without the keyring / adapter factory (MU-12/13 wiring) refuses the MU-14
    actions with a message instead of crashing."""
    app = create_app(live_settings, store=store, clock=clock, policies={str(MB): POLICY})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _sign_in(client, store, provider_mock)
        r = await _post(
            client,
            "/accounts/connect",
            {"course_id": str(MB), "username": "turk@golf.example", "password": PASSWORD},
        )
    assert r.status_code == 409
    assert "not available" in r.text
