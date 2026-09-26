"""MU-13: the dashboard, rules and dates pages (MULTIUSER_PLAN §8.2, §3.4, §7.4, §7.7).

End-to-end over ASGI against a real ``InMemoryTenantStore`` + ``FakeClock``: the only mock is the
OAuth provider's HTTP (conftest). Every write goes through the page's own form POST with the
session's CSRF token, and every assertion about state reads the store back.

Clock: T0 = Sat 2026-09-26 12:00 UTC = 08:00 EDT. Today's Saturday is already frozen (the 16:00
day-before cutoff), so a Saturday rule materializes 10/3, 10/10 and 10/17 (21-day horizon).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.routing import Route

from teetime.core.clock import FakeClock
from teetime.core.models import BookingOutcome, BookingResult, CourseId
from teetime.core.release_policy import ReleasePolicy
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    Actor,
    BookingSource,
    BookingState,
    CourseAccount,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    ReservationSnapshot,
    RowSource,
    RowStatus,
    RuleId,
    SnapshotEntry,
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
from .conftest import T0, GitHubIdentity, make_invited, mock_github, sign_in

MEMBER = "turk@example.test"
POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
SAT, SUN = 5, 6
OCT3, OCT10, OCT17 = date(2026, 10, 3), date(2026, 10, 10), date(2026, 10, 17)
TEE_0930 = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)  # 09:30 EDT on OCT3
TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "teetime" / "web" / "templates"


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(settings, store=store, clock=clock, policies={str(MB): POLICY}, cutoff=CUTOFF)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@dataclass(frozen=True)
class Member:
    user: User
    account: CourseAccount


def _account(user_id: UserId, *, course: CourseId = MB) -> CourseAccount:
    return CourseAccount(
        id=derive_account_id(user_id, course),
        user_id=user_id,
        course_id=course,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=f"golfer-{uuid4().hex[:8]}",
        password_ciphertext="v1:k1:nonce:ct",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )


@pytest.fixture
async def member(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> Member:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(provider_mock, GitHubIdentity(subject="42", emails=[(MEMBER, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "42")
    assert user is not None
    account = _account(user.id)
    await store.upsert_account(account)
    return Member(user=user, account=account)


@pytest.fixture
async def mallory(store: InMemoryTenantStore) -> Member:
    """Another tenant, created straight in the store: the attacker's victim."""
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
    account = _account(user.id)
    await store.upsert_account(account)
    return Member(user=user, account=account)


def _csrf(page: httpx.Response) -> str:
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


async def _token(client: httpx.AsyncClient) -> str:
    return _csrf(await client.get("/"))


async def _post(client: httpx.AsyncClient, path: str, data: dict[str, str]) -> httpx.Response:
    return await client.post(path, data={"csrf_token": await _token(client), **data})


def _rule_form(
    account: CourseAccount,
    *,
    weekday: int = SAT,
    earliest: str = "08:00",
    latest: str = "10:00",
    party: int = 2,
) -> dict[str, str]:
    return {
        "account_id": str(account.id),
        "weekday": str(weekday),
        "window_earliest": earliest,
        "window_latest": latest,
        "party_size": str(party),
    }


async def _rows(store: InMemoryTenantStore, m: Member) -> list[RequestRow]:
    return await store.list_rows_for_user(
        m.user.id, from_date=date(2026, 9, 1), to_date=date(2026, 12, 31)
    )


async def _row_on(
    store: InMemoryTenantStore, m: Member, day: date, *, source: RowSource
) -> RequestRow:
    matches = [r for r in await _rows(store, m) if r.target_date == day and r.source is source]
    assert len(matches) == 1, matches
    return matches[0]


async def _rules(store: InMemoryTenantStore, m: Member) -> list[StandingRule]:
    return await store.list_rules_for_user(m.user.id)


async def _seed_rule(store: InMemoryTenantStore, m: Member, *, weekday: int = SAT) -> StandingRule:
    """A rule + its row for OCT3, written straight to the store (the victim's data)."""
    rule = await store.upsert_rule(
        StandingRule(
            id=RuleId(uuid4()),
            course_account_id=m.account.id,
            weekday=weekday,
            window_earliest=time(8, 0),
            window_latest=time(10, 0),
            party_size=2,
            active=True,
            materialized_through=None,
            version=1,
        ),
        user_id=m.user.id,
    )
    row = await store.insert_rule_row_if_absent(rule, OCT3, now=T0)
    assert row is not None
    return rule


async def _book(store: InMemoryTenantStore, row: RequestRow, *, raw_id: str = "9001") -> RequestRow:
    """pending -> booked through the leased runner path, exactly as WRITE #2 does it."""
    owner = "booker:test"
    claimed = await store.claim_rows(
        [row.id], owner=owner, until=T0 + timedelta(minutes=20), now=T0
    )
    assert claimed == {row.id}
    tee = TEE_0930
    owned = OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=row.course_account_id,
        course_id=row.course_id,
        target_date=row.target_date,
        raw_reservation_id=raw_id,
        tee_time=tee,
        party_size=row.party_size,
        source=BookingSource.BLIND,
        state=BookingState.HELD,
    )
    await store.record_outcomes(
        [
            RowOutcome(
                row_id=row.id,
                course_account_id=row.course_account_id,
                target_date=row.target_date,
                actor=Actor.BOOKING_RUNNER,
                to_status=RowStatus.BOOKED,
                last_outcome="booked",
                at=T0,
                result=BookingResult(
                    request_id=row.request_id,
                    outcome=BookingOutcome.BOOKED,
                    course_id=row.course_id,
                    slot=None,
                    confirmation_code=f"TTB:{raw_id}",
                    booked_at=T0,
                    attempts=1,
                ),
                booking=owned,
                release_lease_owner=owner,
            )
        ]
    )
    (got,) = [
        r for r in await store.rows_for_account_date(row.course_account_id, OCT3) if r.id == row.id
    ]
    assert got.status is RowStatus.BOOKED, got
    return got


# --- IDOR: every row / rule route ------------------------------------------------------------

# (method, path template, form builder). ``{id}`` is the target id; routes that name the
# account in the FORM instead of the path get the victim's account id there.
_ID_ROUTES: list[tuple[str, dict[str, str]]] = [
    ("/rows/{id}/skip", {}),
    ("/rows/{id}/unskip", {}),
    ("/rows/{id}/withdraw", {}),
]


@pytest.mark.parametrize(("path", "extra"), _ID_ROUTES)
async def test_route_rejects_other_users_row(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    mallory: Member,
    path: str,
    extra: dict[str, str],
) -> None:
    """Another user's row id gets EXACTLY the response a missing id gets (IDOR, §9.1)."""
    await _seed_rule(store, mallory)
    victim = await _row_on(store, mallory, OCT3, source=RowSource.RULE)
    foreign = await _post(client, path.format(id=victim.id), extra)
    missing = await _post(client, path.format(id=uuid4()), extra)
    garbage = await _post(client, path.format(id="not-a-uuid"), extra)
    assert foreign.status_code == missing.status_code == garbage.status_code == 404
    assert foreign.text == missing.text
    assert await store.get_row(victim.id, user_id=mallory.user.id) == victim  # untouched


async def test_route_rejects_other_users_rule(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member, mallory: Member
) -> None:
    rule = await _seed_rule(store, mallory)
    edit = {**_rule_form(member.account, weekday=SUN), "version": str(rule.version)}
    foreign = await _post(client, f"/rules/{rule.id}", edit)
    missing = await _post(client, f"/rules/{uuid4()}", edit)
    garbage = await _post(client, "/rules/not-a-uuid", edit)
    assert foreign.status_code == missing.status_code == garbage.status_code == 404
    assert foreign.text == missing.text
    for action in ("deactivate", "activate"):
        r = await _post(client, f"/rules/{rule.id}", {"action": action, "version": "1"})
        assert r.status_code == 404
    assert await _rules(store, mallory) == [rule]


async def test_route_rejects_other_users_account(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member, mallory: Member
) -> None:
    """POST /rules and POST /rows name the account in the form: a foreign or unknown account id
    is the same 404, and nothing is written to the victim."""
    for path, form in (
        ("/rules", _rule_form(mallory.account)),
        (
            "/rows",
            {
                "account_id": str(mallory.account.id),
                "target_date": OCT10.isoformat(),
                "window_earliest": "08:00",
                "window_latest": "10:00",
                "party_size": "2",
            },
        ),
    ):
        foreign = await _post(client, path, form)
        missing = await _post(client, path, {**form, "account_id": str(uuid4())})
        garbage = await _post(client, path, {**form, "account_id": "nope"})
        assert foreign.status_code == missing.status_code == garbage.status_code == 404, path
        assert foreign.text == missing.text
    assert await _rules(store, mallory) == []
    assert await _rows(store, mallory) == []


def test_every_mu13_route_is_bound_and_idor_covered(app: FastAPI) -> None:
    """The ROUTES table is the contract: every MU-13 row is bound, user-auth, CSRF on POST, and
    every one that takes a row/rule/account id has an IDOR test above. ``handler`` names the
    ``web.services`` function the route delegates to, so it must exist there."""
    bound = {(m, r.path) for r in app.routes if isinstance(r, Route) for m in (r.methods or set())}
    mu13 = [s for s in ROUTES if s.milestone == "MU-13"]
    assert {(s.method, s.path) for s in mu13} >= {
        ("GET", "/"),
        ("GET", "/dates"),
        ("GET", "/rules"),
    }
    covered = {p for p, _ in _ID_ROUTES} | {"/rules/{id}", "/rules", "/rows"}
    for spec in mu13:
        assert (spec.method, spec.path) in bound, spec
        assert spec.auth is AuthLevel.USER, spec
        assert callable(getattr(services, spec.handler, None)), spec
        if spec.method == "POST":
            assert spec.csrf, spec
            assert spec.path in covered, spec


# --- dashboard ---------------------------------------------------------------------------------


async def test_dashboard_shows_snapshot_age(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _seed_rule(store, member)
    row = await _row_on(store, member, OCT3, source=RowSource.RULE)
    await _book(store, row, raw_id="9001")
    await store.save_snapshot(
        ReservationSnapshot(
            course_account_id=member.account.id,
            observed_at=T0 - timedelta(minutes=7),  # 07:53 EDT
            source="watcher",
            trusted=True,
            entries=(SnapshotEntry(raw_id="9001", tee_time=TEE_0930, party_size=2),),
        )
    )
    page = await client.get("/")
    assert page.status_code == 200
    assert "as of 07:53" in page.text
    assert "7 min ago" in page.text
    assert "2026-10-03" in page.text
    assert "booked" in page.text
    assert "09:30" in page.text  # the booked tee time, course-local
    assert "not seen at course" not in page.text


async def test_dashboard_without_snapshot_says_so(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _seed_rule(store, member)
    page = await client.get("/")
    assert page.status_code == 200
    assert "2026-10-03" in page.text
    assert "not checked yet" in page.text


async def test_dashboard_flags_booked_row_missing_from_trusted_snapshot(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _seed_rule(store, member)
    await _book(store, await _row_on(store, member, OCT3, source=RowSource.RULE))
    await store.save_snapshot(
        ReservationSnapshot(
            course_account_id=member.account.id,
            observed_at=T0 - timedelta(minutes=3),
            source="watcher",
            trusted=True,
            entries=(),
        )
    )
    assert "not seen at course" in (await client.get("/")).text


async def test_dashboard_shows_only_my_rows(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member, mallory: Member
) -> None:
    await _seed_rule(store, mallory)
    page = await client.get("/")
    assert page.status_code == 200
    assert "2026-10-03" not in page.text


# --- rules -------------------------------------------------------------------------------------


async def test_rule_create_materializes(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    r = await _post(client, "/rules", _rule_form(member.account))
    assert r.status_code == 303
    assert r.headers["location"].startswith("/rules")
    (rule,) = await _rules(store, member)
    assert (rule.weekday, rule.window_earliest, rule.window_latest, rule.party_size) == (
        SAT,
        time(8, 0),
        time(10, 0),
        2,
    )
    assert rule.active
    rows = await _rows(store, member)
    assert [(r.target_date, r.status, r.source) for r in rows] == [
        (d, RowStatus.PENDING, RowSource.RULE) for d in (OCT3, OCT10, OCT17)
    ]
    assert rule.materialized_through == date(2026, 10, 17)
    page = await client.get(r.headers["location"])
    assert "Edits apply from the next drop" in page.text


async def test_second_rule_same_weekday_shows_conflict(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    assert (await _post(client, "/rules", _rule_form(member.account))).status_code == 303
    r = await _post(client, "/rules", _rule_form(member.account, earliest="11:00", latest="12:00"))
    assert r.status_code == 409
    assert "already have an active rule for Saturday" in r.text
    assert len(await _rules(store, member)) == 1


async def test_rule_create_rejects_bad_input(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    for bad in (
        {"window_earliest": "10:00", "window_latest": "08:00"},
        {"window_earliest": "8am"},
        {"weekday": "7"},
        {"party_size": "0"},
        {"party_size": "9"},
    ):
        r = await _post(client, "/rules", {**_rule_form(member.account), **bad})
        assert r.status_code == 400, bad
    assert await _rules(store, member) == []


async def test_rule_window_edit_rewrites_pending_rows(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _post(client, "/rules", _rule_form(member.account))
    (rule,) = await _rules(store, member)
    edit = {**_rule_form(member.account, earliest="09:00", latest="11:00", party=3)}
    r = await _post(client, f"/rules/{rule.id}", {**edit, "version": str(rule.version)})
    assert r.status_code == 303
    rows = await _rows(store, member)
    assert {(r.window_earliest, r.window_latest, r.party_size) for r in rows} == {
        (time(9, 0), time(11, 0), 3)
    }


async def test_rule_edit_from_stale_page_shows_version_conflict(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _post(client, "/rules", _rule_form(member.account))
    (rule,) = await _rules(store, member)
    stale = rule.version - 1 if rule.version > 1 else rule.version + 5
    r = await _post(
        client, f"/rules/{rule.id}", {**_rule_form(member.account, party=4), "version": str(stale)}
    )
    assert r.status_code == 409
    assert "changed since you loaded" in r.text
    assert (await _rules(store, member))[0].party_size == 2


async def test_rule_deactivate_and_reactivate(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _post(client, "/rules", _rule_form(member.account))
    (rule,) = await _rules(store, member)
    r = await _post(
        client, f"/rules/{rule.id}", {"action": "deactivate", "version": str(rule.version)}
    )
    assert r.status_code == 303
    (rule,) = await _rules(store, member)
    assert not rule.active
    assert {r.status for r in await _rows(store, member)} == {RowStatus.WITHDRAWN}
    r = await _post(
        client, f"/rules/{rule.id}", {"action": "activate", "version": str(rule.version)}
    )
    assert r.status_code == 303
    (rule,) = await _rules(store, member)
    assert rule.active
    assert {r.status for r in await _rows(store, member)} == {RowStatus.PENDING}


# --- dates ---------------------------------------------------------------------------------


async def test_add_one_off_and_withdraw_it(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    r = await _post(
        client,
        "/rows",
        {
            "account_id": str(member.account.id),
            "target_date": OCT10.isoformat(),
            "window_earliest": "07:30",
            "window_latest": "09:00",
            "party_size": "4",
        },
    )
    assert r.status_code == 303
    one_off = await _row_on(store, member, OCT10, source=RowSource.EXPLICIT)
    assert (one_off.status, one_off.window_earliest, one_off.party_size) == (
        RowStatus.PENDING,
        time(7, 30),
        4,
    )
    page = await client.get("/dates")
    assert f"/rows/{one_off.id}/withdraw" in page.text
    assert (await _post(client, f"/rows/{one_off.id}/withdraw", {})).status_code == 303
    got = await store.get_row(one_off.id, user_id=member.user.id)
    assert got is not None and got.status is RowStatus.WITHDRAWN


async def test_one_off_on_frozen_date_is_refused(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    r = await _post(
        client,
        "/rows",
        {
            "account_id": str(member.account.id),
            "target_date": "2026-09-26",  # today: past the cutoff
            "window_earliest": "08:00",
            "window_latest": "10:00",
            "party_size": "2",
        },
    )
    assert r.status_code == 409
    assert await _rows(store, member) == []


async def test_skip_and_unskip_rule_date(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _post(client, "/rules", _rule_form(member.account))
    row = await _row_on(store, member, OCT10, source=RowSource.RULE)
    page = await client.get("/dates")
    assert f"/rows/{row.id}/skip" in page.text
    assert (await _post(client, f"/rows/{row.id}/skip", {})).status_code == 303
    got = await store.get_row(row.id, user_id=member.user.id)
    assert got is not None and got.status is RowStatus.SKIPPED
    assert f"/rows/{row.id}/unskip" in (await client.get("/dates")).text
    assert (await _post(client, f"/rows/{row.id}/unskip", {})).status_code == 303
    got = await store.get_row(row.id, user_id=member.user.id)
    assert got is not None and got.status is RowStatus.PENDING


async def test_skip_booked_shows_cancel_hint(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _seed_rule(store, member)
    booked = await _book(store, await _row_on(store, member, OCT3, source=RowSource.RULE))
    page = await client.get("/dates")
    assert f"/rows/{booked.id}/skip" not in page.text  # no skip button on a booked row
    r = await _post(client, f"/rows/{booked.id}/skip", {})
    assert r.status_code == 409
    assert "use Cancel instead" in r.text
    got = await store.get_row(booked.id, user_id=member.user.id)
    assert got == booked


async def test_skip_leased_row_says_booking_in_progress(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _seed_rule(store, member)
    row = await _row_on(store, member, OCT3, source=RowSource.RULE)
    await store.claim_rows([row.id], owner="booker:x", until=T0 + timedelta(minutes=20), now=T0)
    r = await _post(client, f"/rows/{row.id}/skip", {})
    assert r.status_code == 409
    assert "booking in progress" in r.text.lower()


async def test_unskip_after_weekday_change_shows_one_off_hint(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _post(client, "/rules", _rule_form(member.account))
    row = await _row_on(store, member, OCT10, source=RowSource.RULE)
    assert (await _post(client, f"/rows/{row.id}/skip", {})).status_code == 303
    (rule,) = await _rules(store, member)
    moved = await _post(
        client,
        f"/rules/{rule.id}",
        {**_rule_form(member.account, weekday=SUN), "version": str(rule.version)},
    )
    assert moved.status_code == 303
    r = await _post(client, f"/rows/{row.id}/unskip", {})
    assert r.status_code == 409
    assert "This rule no longer covers 2026-10-10; add it as a one-off instead" in r.text
    # ... with the "add as one-off" action, prefilled from the row.
    assert 'action="/rows"' in r.text
    assert 'value="2026-10-10"' in r.text
    form = {
        "account_id": str(row.course_account_id),
        "target_date": "2026-10-10",
        "window_earliest": "08:00",
        "window_latest": "10:00",
        "party_size": "2",
    }
    assert (await _post(client, "/rows", form)).status_code == 303
    one_off = await _row_on(store, member, OCT10, source=RowSource.EXPLICIT)
    assert one_off.status is RowStatus.PENDING


async def test_rerequest_after_external_cancel_creates_explicit_row(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _seed_rule(store, member)
    booked = await _book(store, await _row_on(store, member, OCT3, source=RowSource.RULE))
    owner = "watcher:test"
    assert await store.acquire_row_lease(
        booked.id, owner=owner, until=T0 + timedelta(minutes=5), now=T0, expected=None
    )
    await store.record_outcomes(
        [
            RowOutcome(
                row_id=booked.id,
                course_account_id=booked.course_account_id,
                target_date=OCT3,
                actor=Actor.WATCHER,
                to_status=RowStatus.CANCELLED,
                last_outcome="cancelled_external",
                at=T0,
                status_reason="external",
                release_lease_owner=owner,
            )
        ]
    )
    page = await client.get("/dates")
    assert "cancelled" in page.text
    assert "Re-request" in page.text
    r = await _post(
        client,
        "/rows",
        {
            "account_id": str(booked.course_account_id),
            "target_date": OCT3.isoformat(),
            "window_earliest": "08:00",
            "window_latest": "10:00",
            "party_size": "2",
        },
    )
    assert r.status_code == 303
    again = await _row_on(store, member, OCT3, source=RowSource.EXPLICIT)
    assert again.status is RowStatus.PENDING
    # the re-requested date no longer offers a second re-request
    assert "Re-request" not in (await client.get("/dates")).text


# --- CSRF + CSP ------------------------------------------------------------------------------


async def test_every_post_requires_csrf(
    app: FastAPI, client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    """Every bound POST route refuses a signed-in request with no / a wrong token, and writes
    nothing (the guard runs before the handler)."""
    await _post(client, "/rules", _rule_form(member.account))
    (rule,) = await _rules(store, member)
    row = await _row_on(store, member, OCT10, source=RowSource.RULE)
    before_rows, before_rules = await _rows(store, member), await _rules(store, member)
    posts = sorted(
        r.path for r in app.routes if isinstance(r, Route) and "POST" in (r.methods or set())
    )
    assert {"/rules", "/rules/{id}", "/rows", "/rows/{id}/skip"} <= set(posts)
    for path in posts:
        if path == "/logout":
            continue  # covered by test_web_admin_csrf; it would end the session here
        concrete = path.replace("{id}", str(row.id if path.startswith("/rows/") else rule.id))
        for data in ({}, {"csrf_token": "wrong"}):
            r = await client.post(concrete, data={**_rule_form(member.account), **data})
            assert r.status_code == 403, (path, data)
    assert await _rows(store, member) == before_rows
    assert await _rules(store, member) == before_rules


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
_EVENT_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
_INLINE_STYLE = re.compile(r"<style\b|\sstyle\s*=", re.IGNORECASE)


def _assert_no_inline(html: str, where: str) -> None:
    assert not _INLINE_SCRIPT.search(html), where
    assert not _EVENT_HANDLER.search(html), where
    assert not _INLINE_STYLE.search(html), where
    assert "javascript:" not in html.lower(), where


async def test_pages_have_no_inline_script(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    """The CSP forbids inline script and style (§8.3): check every rendered page, including an
    error page, AND every template source."""
    await _post(client, "/rules", _rule_form(member.account))
    for path in ("/", "/rules", "/dates"):
        page = await client.get(path)
        assert page.status_code == 200, path
        _assert_no_inline(page.text, path)
    conflict = await _post(client, "/rules", _rule_form(member.account))
    assert conflict.status_code == 409
    _assert_no_inline(conflict.text, "409 page")
    templates = sorted(TEMPLATES.glob("*.html"))
    assert {p.name for p in templates} >= {"dashboard.html", "rules.html", "dates.html"}
    for tpl in templates:
        _assert_no_inline(tpl.read_text(), tpl.name)
