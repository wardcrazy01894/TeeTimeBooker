"""The operator's used-tee-times report, ``/admin/bookings`` (operator request 2026-11-16: "I
might start charging people for the tee times they use, and I want a way to easily just go see
what was booked and used").

Every person's PAST dates that still hold a tee time, in a chosen window, with a count per
person; booked-then-cancelled dates are hidden unless asked for. Clock: the month of golf from
``test_web_history`` (Oct 3 booked 09:30, Oct 10 booked 08:15 then cancelled at the course,
Oct 17 lost), seen from Tue Oct 20 2026 12:00 EDT unless a test moves it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.routing import Route

from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RowId
from teetime.web import admin_bookings, services
from teetime.web.routes import ROUTES, AuthLevel

from .conftest import sign_in
from .test_web_admin_csrf import _sign_in_operator
from .test_web_history import (
    OCT20_NOON,
    TEE_OCT3,
    _a_month_of_golf,
    _book,
    _cancel_at_course,
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
    assert "Last 30 days" in text and "Sep 20" in text and "Oct 20" in text  # the window, dated
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


async def test_a_tee_time_counts_as_used_from_its_tee_time_on_the_same_day(
    store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """Operator, 2026-11-27: "we shouldn't need to wait til the next day". A tee time still
    held when it starts is used from that minute; a minute earlier it is still to come."""
    rule = await _seed_rule(store, member)
    await _book(store, await _row_for(store, member, rule, OCT3), TEE_OCT3)
    await _move_clock_to(clock, TEE_OCT3 - timedelta(minutes=1))
    early = await admin_bookings.usage_report(
        store, clock=clock, window="30d", include_cancelled=True
    )
    assert (early.last, early.used, early.bookings) == (OCT3, 0, ())
    await _move_clock_to(clock, TEE_OCT3)
    for window in ("30d", "this-month"):
        report = await admin_bookings.usage_report(
            store, clock=clock, window=window, include_cancelled=False
        )
        assert (report.last, report.used) == (OCT3, 1)
        assert [(b.entry.row.target_date, b.billable) for b in report.bookings] == [(OCT3, True)]
    # The person's own /history still starts yesterday: today is on their dashboard.
    assert await services.history(store, user_id=member.user.id, clock=clock) == []


async def test_a_tee_time_cancelled_on_its_day_before_it_started_is_not_used(
    store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """A row the watcher wrote cancelled BEFORE the tee time (09:30) was a cancel, even on the
    day itself; only one written at or after the tee time was a check-in."""
    await _a_month_of_golf(store, member, clock)
    (oct3,) = await store.list_rows_for_user(member.user.id, from_date=OCT3, to_date=OCT3)
    await _cancel_at_course(store, oct3, at=TEE_OCT3 - timedelta(minutes=1))
    report = await admin_bookings.usage_report(
        store, clock=clock, window="30d", include_cancelled=True
    )
    assert [b.billable for b in report.bookings] == [False, False]
    (total,) = report.totals
    assert (total.used, total.cancelled) == (0, 2)


async def test_a_tee_time_the_course_dropped_from_its_list_on_the_day_counts_as_used(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    """Operator, 2026-11-27: "I'm pretty sure brad used his tee time this morning", and the
    report had it cancelled. The course drops a reservation from the upcoming list at check-in,
    which the watcher wrote CANCELLED(external) on the day; that is a used tee time. Oct 10,
    cancelled before its date, is still not."""
    await _a_month_of_golf(store, member, clock)
    (oct3,) = await store.list_rows_for_user(member.user.id, from_date=OCT3, to_date=OCT3)
    await _cancel_at_course(store, oct3, at=TEE_OCT3 + timedelta(minutes=40))
    report = await admin_bookings.usage_report(
        store, clock=clock, window="30d", include_cancelled=True
    )
    assert {b.entry.row.target_date: b.billable for b in report.bookings} == {
        OCT10: False,
        OCT3: True,
    }
    (total,) = report.totals
    assert (total.used, total.players, total.cancelled) == (1, 2, 1)
    text = _main((await _as_operator(client, provider_mock, "/admin/bookings")).text)
    assert "1 tee time used" in text and "Oct 3" in text and "Oct 10" not in text


@pytest.mark.parametrize(
    ("window", "first", "last", "has_oct3"),
    [
        ("30d", date(2026, 10, 6), date(2026, 11, 5), False),
        ("90d", date(2026, 8, 7), date(2026, 11, 5), True),
        ("365d", date(2025, 11, 5), date(2026, 11, 5), True),
        ("this-month", date(2026, 11, 1), date(2026, 11, 5), False),
        ("last-month", date(2026, 10, 1), date(2026, 10, 31), True),
        ("garbage", date(2026, 10, 6), date(2026, 11, 5), False),  # unknown: the default
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
    """Seen from Thu Nov 5: day windows and this month end TODAY (a tee time counts from its
    tee time on, the same day) and months are calendar months on the operator's (Eastern) clock."""
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


@pytest.mark.parametrize(
    ("today", "window", "first", "last"),
    [
        # The 1st: this month is that one day.
        (date(2026, 11, 1), "this-month", date(2026, 11, 1), date(2026, 11, 1)),
        (date(2026, 11, 1), "last-month", date(2026, 10, 1), date(2026, 10, 31)),
        (date(2027, 1, 9), "last-month", date(2026, 12, 1), date(2026, 12, 31)),  # across a year
        (date(2028, 3, 1), "last-month", date(2028, 2, 1), date(2028, 2, 29)),  # a leap February
        (date(2028, 2, 29), "365d", date(2027, 3, 1), date(2028, 2, 29)),
    ],
)
def test_window_edges(today: date, window: str, first: date, last: date) -> None:
    assert admin_bookings.window_dates(window, today=today) == (first, last)


async def test_a_booked_row_with_no_reservation_id_still_counts_as_used(
    store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """A BOOKED row the bot's own run wrote but whose reservation id was never learned must
    not slide into "not booked by the bot" and go uncharged: How is blank, the count is 1."""
    rule = await _seed_rule(store, member)
    row = await _row_for(store, member, rule, OCT3)
    await _book(store, row, TEE_OCT3)
    booked = store._rows[row.id]
    store._rows[row.id] = replace(booked, booked_raw_id=None)
    await _move_clock_to(clock, OCT20_NOON)
    report = await admin_bookings.usage_report(
        store, clock=clock, window="30d", include_cancelled=False
    )
    (booking,) = report.bookings
    assert (booking.entry.how, booking.billable, report.used) == (None, True, 1)


async def test_two_bookings_of_one_ranked_request_are_charged_once(
    store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """One ranked request (a group) is one tee time. If its collapse failed and the date passed
    with two courses still booked, the report counts the best-ranked one and flags the other."""
    rule = await _seed_rule(store, member)
    row = await _row_for(store, member, rule, OCT3)
    await _book(store, row, TEE_OCT3)
    group = uuid4()
    first_choice = replace(store._rows[row.id], group_id=group, group_rank=1)
    store._rows[row.id] = first_choice
    other = replace(first_choice, id=RowId(uuid4()), group_rank=2, booked_raw_id="other-1")
    store._rows[other.id] = other
    store._ledger[(other.course_account_id, other.course_id, "other-1")] = replace(
        next(iter(store._ledger.values())), row_id=other.id, raw_reservation_id="other-1"
    )
    await _move_clock_to(clock, OCT20_NOON)
    report = await admin_bookings.usage_report(
        store, clock=clock, window="30d", include_cancelled=False
    )
    assert report.used == 1
    assert {b.entry.row.id: (b.billable, b.duplicate) for b in report.bookings} == {
        row.id: (True, False),
        other.id: (False, True),
    }
    (total,) = report.totals
    assert (total.used, total.duplicates) == (1, 1)
