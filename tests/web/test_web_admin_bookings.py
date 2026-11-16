"""The operator's used-tee-times report, ``/admin/bookings`` (operator request 2026-11-16: "I
might start charging people for the tee times they use, and I want a way to easily just go see
what was booked and used").

Every person's PAST dates that still hold a tee time, in a chosen window, with a count per
person; booked-then-cancelled dates are hidden unless asked for. Clock: the month of golf from
``test_web_history`` (Oct 3 booked 09:30, Oct 10 booked 08:15 then cancelled at the course,
Oct 17 lost), seen from Tue Oct 20 2026 12:00 EDT unless a test moves it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.routing import Route

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.web import admin_bookings
from teetime.web.routes import ROUTES, AuthLevel

from .conftest import sign_in
from .test_web_admin_csrf import _sign_in_operator
from .test_web_history import (
    TEE_OCT3,
    _a_month_of_golf,
    _book,
    _main,
    _move_clock_to,
    _row_for,
)
from .test_web_pages import OCT3, OCT10, Member, _seed_rule
from .test_web_pages import app as app  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import client as client  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import mallory as mallory  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import member as member  # noqa: PLC0414 — pytest fixture re-export

NOV5_NOON = datetime(2026, 11, 5, 17, 0, tzinfo=UTC)


async def _as_operator(
    client: httpx.AsyncClient, router: respx.MockRouter, path: str
) -> httpx.Response:
    client.cookies.clear()
    await _sign_in_operator(client, router)
    return await client.get(path)


def test_the_report_route_is_in_the_contract_table_and_operator_only(app: FastAPI) -> None:
    (spec,) = [s for s in ROUTES if s.path == "/admin/bookings"]
    assert (spec.method, spec.auth, spec.csrf) == ("GET", AuthLevel.OPERATOR, False)
    bound = {(m, r.path) for r in app.routes if isinstance(r, Route) for m in (r.methods or set())}
    assert ("GET", "/admin/bookings") in bound


async def test_the_report_lists_used_tee_times_and_counts_them_per_person(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    await _a_month_of_golf(store, member, clock)
    page = await _as_operator(client, provider_mock, "/admin/bookings")
    assert page.status_code == 200
    text = _main(page.text)
    assert "Last 30 days" in text and "Sep 20" in text and "Oct 19" in text  # the window, dated
    # The used one: who, when, what time, how many, how.
    used = text[text.index("Oct 3") :]
    assert "turk@example.test" in used and "09:30" in used and "when tee times opened" in used
    # Cancelled and never-booked dates are not "used".
    assert "Oct 10" not in text and "08:15" not in text and "Oct 17" not in text
    assert "1 tee time used" in text
    assert page.text.count("<form") == 1  # read-only: only the top bar's Sign out


async def test_cancelled_tee_times_are_there_when_asked_for_and_lost_dates_never(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    await _a_month_of_golf(store, member, clock)
    hidden = await _as_operator(client, provider_mock, "/admin/bookings")
    assert 'href="/admin/bookings?window=30d&amp;cancelled=1"' in hidden.text  # the toggle
    text = _main((await client.get("/admin/bookings?window=30d&cancelled=1")).text)
    cancelled = text[text.index("Oct 10") : text.index("Oct 3")]
    assert "cancelled at the course" in cancelled and "08:15" in cancelled
    assert "Oct 17" not in text
    assert "1 tee time used" in text  # a cancelled one is listed, never counted as used


@pytest.mark.parametrize(
    ("window", "first", "last", "has_oct3"),
    [
        ("30d", date(2026, 10, 6), date(2026, 11, 4), False),
        ("90d", date(2026, 8, 7), date(2026, 11, 4), True),
        ("365d", date(2025, 11, 5), date(2026, 11, 4), True),
        ("this-month", date(2026, 11, 1), date(2026, 11, 4), False),
        ("last-month", date(2026, 10, 1), date(2026, 10, 31), True),
        ("garbage", date(2026, 10, 6), date(2026, 11, 4), False),  # unknown: the default
    ],
)
async def test_each_window_covers_its_dates(
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    window: str,
    first: date,
    last: date,
    has_oct3: bool,
) -> None:
    """Seen from Thu Nov 5: windows end YESTERDAY (a tee time counts once its date has passed)
    and months are calendar months on the operator's (Eastern) clock."""
    await _a_month_of_golf(store, member, clock)
    await _move_clock_to(clock, NOV5_NOON)
    report = await admin_bookings.usage_report(
        store, clock=clock, window=window, include_cancelled=False
    )
    assert (report.first, report.last) == (first, last)
    assert [b.entry.row.target_date for b in report.bookings] == ([OCT3] if has_oct3 else [])


async def test_totals_are_per_person_and_leave_out_what_the_bot_did_not_book(
    store: InMemoryTenantStore, member: Member, mallory: Member, clock: FakeClock
) -> None:
    """Turk: one used tee time for two players, one cancelled. Mallory: a reservation on her
    date that the bot never owned, which is listed but is not the site's to charge for."""
    theirs = await _seed_rule(store, mallory)
    await _book(store, await _row_for(store, mallory, theirs, OCT3), TEE_OCT3)
    store._ledger.clear()  # Mallory's reservation has no ownership record
    await _a_month_of_golf(store, member, clock)
    report = await admin_bookings.usage_report(
        store, clock=clock, window="30d", include_cancelled=True
    )
    totals = {t.user.email: (t.used, t.players, t.cancelled, t.not_ours) for t in report.totals}
    assert totals == {"turk@example.test": (1, 2, 1, 0), "mallory@example.test": (0, 0, 0, 1)}
    assert report.used == 1
    by_person = {(b.user.email, b.entry.row.target_date): b.billable for b in report.bookings}
    assert by_person == {
        ("turk@example.test", OCT10): False,
        ("turk@example.test", OCT3): True,
        ("mallory@example.test", OCT3): False,
    }
    # Without the cancelled ones, the count of them is still reported.
    lean = await admin_bookings.usage_report(
        store, clock=clock, window="30d", include_cancelled=False
    )
    assert OCT10 not in [b.entry.row.target_date for b in lean.bookings]
    assert {t.user.email: t.cancelled for t in lean.totals}["turk@example.test"] == 1


async def test_an_empty_window_says_so(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    await _a_month_of_golf(store, member, clock)
    page = await _as_operator(client, provider_mock, "/admin/bookings?window=last-month")
    assert "No tee times used in this window" in _main(page.text)  # September: nothing yet


async def test_the_report_is_operator_only_and_linked_from_users(
    client: httpx.AsyncClient, member: Member, provider_mock: respx.MockRouter
) -> None:
    assert (await client.get("/admin/bookings")).status_code == 403  # a signed-in member
    client.cookies.clear()
    signed_out = await client.get("/admin/bookings")
    assert signed_out.status_code == (await client.get("/admin/users")).status_code != 200
    users = await _as_operator(client, provider_mock, "/admin/users")
    assert 'href="/admin/bookings"' in users.text


async def test_a_member_session_cannot_reach_it_after_signing_in_again(
    client: httpx.AsyncClient, member: Member
) -> None:
    assert (await sign_in(client)).status_code == 303
    assert (await client.get("/admin/bookings?window=365d&cancelled=1")).status_code == 403
