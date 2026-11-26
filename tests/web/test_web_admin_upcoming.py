"""The operator's upcoming-tee-times page, ``/admin/upcoming`` (operator request 2026-11-26: "a
page I can go to to see all upcoming bookings across all accounts, similar to how I can see all
historical ones").

Every signed-in person's dates from today on (the 21 days each person's own dashboard shows):
the tee times already booked, soonest first, and the dates the bot is still looking for.
Clock: T0 = Sat 2026-09-26 08:00 EDT, so OCT3 / OCT10 / OCT17 are all ahead.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx
from fastapi import FastAPI
from starlette.routing import Route

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.web import admin_upcoming, services
from teetime.web.routes import ROUTES, AuthLevel

from .conftest import sign_in
from .test_web_admin_csrf import _sign_in_operator
from .test_web_history import (
    OCT20_NOON,
    TEE_OCT3,
    TEE_OCT10,
    _book,
    _main,
    _move_clock_to,
    _row_for,
)
from .test_web_pages import OCT3, OCT10, OCT17, Member, _seed_rule
from .test_web_pages import app as app  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import client as client  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import mallory as mallory  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import member as member  # noqa: PLC0414 — pytest fixture re-export

TEE_OCT3_EARLY = datetime(2026, 10, 3, 12, 7, tzinfo=UTC)  # 08:07 EDT


async def _as_operator(
    client: httpx.AsyncClient, router: respx.MockRouter, path: str
) -> httpx.Response:
    client.cookies.clear()
    await _sign_in_operator(client, router)
    return await client.get(path)


async def _two_people(store: InMemoryTenantStore, member: Member, mallory: Member) -> None:
    """Turk: Oct 3 booked 09:30, Oct 10 booked 08:15, Oct 17 still pending. Mallory: Oct 3
    booked 08:07 (earlier the same day than Turk's)."""
    rule = await _seed_rule(store, member)
    await _book(store, await _row_for(store, member, rule, OCT3), TEE_OCT3)
    await _book(store, await _row_for(store, member, rule, OCT10), TEE_OCT10)
    await _row_for(store, member, rule, OCT17)
    theirs = await _seed_rule(store, mallory)
    await _book(store, await _row_for(store, mallory, theirs, OCT3), TEE_OCT3_EARLY)


def test_the_route_is_in_the_contract_table_and_operator_only(app: FastAPI) -> None:
    (spec,) = [s for s in ROUTES if s.path == "/admin/upcoming"]
    assert (spec.method, spec.auth, spec.csrf) == ("GET", AuthLevel.OPERATOR, False)
    bound = {(m, r.path) for r in app.routes if isinstance(r, Route) for m in (r.methods or set())}
    assert ("GET", "/admin/upcoming") in bound


async def test_the_page_lists_every_persons_booked_tee_times_soonest_first(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    mallory: Member,
    provider_mock: respx.MockRouter,
) -> None:
    await _two_people(store, member, mallory)
    page = await _as_operator(client, provider_mock, "/admin/upcoming")
    assert page.status_code == 200
    text = _main(page.text)
    assert "3 tee times booked" in text and "1 date still open" in text
    booked = text[text.index("Booked") : text.index("Still looking")]
    # Soonest first; on one day, the earlier tee time first.
    order = [booked.index(t) for t in ("08:07", "09:30", "08:15")]
    assert order == sorted(order)
    assert booked.index("mallory@example.test") < booked.index("turk@example.test")
    assert "Oct 17" not in booked
    looking = text[text.index("Still looking") :]
    assert "Oct 17" in looking and "turk@example.test" in looking and "08:00" in looking
    assert page.text.count("<form") == 1  # read-only: only the top bar's Sign out


async def test_the_report_is_soonest_first_then_by_tee_time_then_by_person(
    store: InMemoryTenantStore, member: Member, mallory: Member, clock: FakeClock
) -> None:
    await _two_people(store, member, mallory)
    report = await admin_upcoming.upcoming_report(store, clock=clock)
    assert [(b.user.email, b.entry.row.target_date) for b in report.booked] == [
        ("mallory@example.test", OCT3),
        ("turk@example.test", OCT3),
        ("turk@example.test", OCT10),
    ]
    assert [(b.user.email, b.entry.row.target_date) for b in report.pending] == [
        ("turk@example.test", OCT17)
    ]
    assert report.players == 6


async def test_a_date_that_has_passed_is_no_longer_upcoming(
    store: InMemoryTenantStore, member: Member, mallory: Member, clock: FakeClock
) -> None:
    await _two_people(store, member, mallory)
    await _move_clock_to(clock, OCT20_NOON)
    report = await admin_upcoming.upcoming_report(store, clock=clock)
    assert report.booked == () and report.pending == ()


async def test_skipped_dates_are_left_out(
    store: InMemoryTenantStore, member: Member, mallory: Member, clock: FakeClock
) -> None:
    await _two_people(store, member, mallory)
    (pending,) = (await admin_upcoming.upcoming_report(store, clock=clock)).pending
    await services.skip_row(store, user_id=member.user.id, row_id=pending.entry.row.id, clock=clock)
    report = await admin_upcoming.upcoming_report(store, clock=clock)
    assert report.pending == () and len(report.booked) == 3


async def test_nothing_upcoming_says_so(
    client: httpx.AsyncClient, member: Member, provider_mock: respx.MockRouter
) -> None:
    page = await _as_operator(client, provider_mock, "/admin/upcoming")
    text = _main(page.text)
    assert "No tee times booked" in text and "Nothing still open" in text


async def test_the_page_is_operator_only_and_linked_from_users(
    client: httpx.AsyncClient, member: Member, provider_mock: respx.MockRouter
) -> None:
    assert (await client.get("/admin/upcoming")).status_code == 403  # a signed-in member
    client.cookies.clear()
    signed_out = await client.get("/admin/upcoming")
    assert signed_out.status_code == (await client.get("/admin/users")).status_code != 200
    users = await _as_operator(client, provider_mock, "/admin/users")
    assert 'href="/admin/upcoming"' in users.text
    assert 'href="/admin/upcoming"' in (await client.get("/admin/bookings")).text


async def test_a_member_session_cannot_reach_it_after_signing_in_again(
    client: httpx.AsyncClient, member: Member
) -> None:
    assert (await sign_in(client)).status_code == 303
    assert (await client.get("/admin/upcoming")).status_code == 403
