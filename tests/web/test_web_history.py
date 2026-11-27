"""Booking history (operator request 2026-11-16): ``/history`` lists a person's PAST dates with
what became of each (booked with its tee time, cancelled and by whom, lost, skipped), and the
operator's per-person page shows the same list.

End-to-end over ASGI like ``test_web_pages.py``. Clock: T0 = Sat 2026-09-26 08:00 EDT; the rows
are written at T0 and the clock is then moved past their dates.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.routing import Route

from teetime.core.clock import FakeClock
from teetime.core.models import BookingOutcome, BookingResult
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    Actor,
    BookingSource,
    BookingState,
    CourseAccountId,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    ReservationSnapshot,
    RowStatus,
    StandingRule,
)
from teetime.tenant.store import RowOutcome
from teetime.web import services
from teetime.web.routes import ROUTES, AuthLevel

from .conftest import T0, sign_in
from .test_web_admin_csrf import _sign_in_operator
from .test_web_pages import OCT3, OCT10, OCT17, Member, _seed_rule
from .test_web_pages import app as app  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import client as client  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import mallory as mallory  # noqa: PLC0414 — pytest fixture re-export
from .test_web_pages import member as member  # noqa: PLC0414 — pytest fixture re-export

OCT24 = date(2026, 10, 24)
TEE_OCT3 = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)  # 09:30 EDT
TEE_OCT10 = datetime(2026, 10, 10, 12, 15, tzinfo=UTC)  # 08:15 EDT
OCT20_NOON = datetime(2026, 10, 20, 16, 0, tzinfo=UTC)
_TAG = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return " ".join(_TAG.sub(" ", html).split())


def _main(html: str) -> str:
    """The visible text of the page body, without the top bar (whose nav names every page)."""
    return _text(html[html.index("<main>") :])


async def _move_clock_to(clock: FakeClock, when: datetime) -> None:
    await clock.sleep((when - clock.now_utc()).total_seconds())


async def _history(client: httpx.AsyncClient) -> httpx.Response:
    """GET /history on a fresh session: the tests move the clock by weeks, and a session lives
    12 hours (the member fixture's provider mock is still in place)."""
    assert (await sign_in(client)).status_code == 303
    return await client.get("/history")


async def _row_for(
    store: InMemoryTenantStore, m: Member, rule: StandingRule, day: date, *, now: datetime = T0
) -> RequestRow:
    if day != OCT3:  # _seed_rule already made OCT3's
        assert await store.insert_rule_row_if_absent(rule, day, now=now) is not None
    (row,) = await store.list_rows_for_user(m.user.id, from_date=day, to_date=day)
    return row


async def _book(
    store: InMemoryTenantStore,
    row: RequestRow,
    tee: datetime,
    *,
    source: BookingSource = BookingSource.BLIND,
) -> None:
    """pending -> booked through the leased runner path (WRITE #2)."""
    owner = "booker:test"
    assert await store.claim_rows(
        [row.id], owner=owner, until=T0 + timedelta(minutes=20), now=T0
    ) == {row.id}
    raw_id = uuid4().hex[:6]
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
                booking=OwnedBooking(
                    id=OwnedBookingId(uuid4()),
                    row_id=row.id,
                    course_account_id=row.course_account_id,
                    course_id=row.course_id,
                    target_date=row.target_date,
                    raw_reservation_id=raw_id,
                    tee_time=tee,
                    party_size=row.party_size,
                    source=source,
                    state=BookingState.HELD,
                ),
                release_lease_owner=owner,
            )
        ]
    )


async def _cancel_at_course(
    store: InMemoryTenantStore, row: RequestRow, *, at: datetime = T0
) -> None:
    """booked -> cancelled(external), as the watcher writes a vanished reservation."""
    owner = "watcher:test"
    assert await store.acquire_row_lease(
        row.id, owner=owner, until=at + timedelta(minutes=5), now=at, expected=None
    )
    await store.record_outcomes(
        [
            RowOutcome(
                row_id=row.id,
                course_account_id=row.course_account_id,
                target_date=row.target_date,
                actor=Actor.WATCHER,
                to_status=RowStatus.CANCELLED,
                last_outcome="cancelled_external",
                at=at,
                status_reason="external",
                release_lease_owner=owner,
            )
        ]
    )


async def _a_month_of_golf(store: InMemoryTenantStore, m: Member, clock: FakeClock) -> None:
    """Oct 3 booked 09:30, Oct 10 booked 08:15 then cancelled at the course, Oct 17 never
    booked (lost), and, seen from Oct 20, Oct 24 still to come."""
    rule = await _seed_rule(store, m)
    await _book(store, await _row_for(store, m, rule, OCT3), TEE_OCT3)
    oct10 = await _row_for(store, m, rule, OCT10)
    await _book(store, oct10, TEE_OCT10)
    await _cancel_at_course(store, oct10)
    await _row_for(store, m, rule, OCT17)
    await _move_clock_to(clock, OCT20_NOON)
    assert [r.target_date for r in await store.finalize_lost(now=clock.now_utc())] == [OCT17]
    await _row_for(store, m, rule, OCT24, now=clock.now_utc())


def test_the_history_route_is_in_the_contract_table(app: FastAPI) -> None:
    (spec,) = [s for s in ROUTES if s.path == "/history"]
    assert (spec.method, spec.auth, spec.csrf) == ("GET", AuthLevel.USER, False)
    assert callable(getattr(services, spec.handler, None))
    bound = {(m, r.path) for r in app.routes if isinstance(r, Route) for m in (r.methods or set())}
    assert ("GET", "/history") in bound


async def test_history_lists_past_dates_newest_first_with_what_became_of_each(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    await _a_month_of_golf(store, member, clock)
    page = await _history(client)
    assert page.status_code == 200
    text = _main(page.text)
    oct17, oct10, oct3 = text.index("Oct 17"), text.index("Oct 10"), text.index("Oct 3")
    assert oct17 < oct10 < oct3  # newest first
    assert "lost" in text[oct17:oct10]
    # A cancelled date says who cancelled it and keeps the tee time it had.
    assert "cancelled at the course" in text[oct10:oct3] and "08:15" in text[oct10:oct3]
    assert "booked" in text[oct3:] and "09:30" in text[oct3:]
    assert "Oct 24" not in text  # still to come: the dashboard's, not history's
    assert "1 booked" in text and "1 cancelled" in text and "1 lost" in text
    # Read-only: the only form on the page is the top bar's Sign out.
    assert page.text.count("<form") == 1


@pytest.mark.parametrize(
    ("source", "how"),
    [
        (BookingSource.BLIND, "when tee times opened"),
        (BookingSource.SEARCH, "when tee times opened"),
        (BookingSource.WATCH, "found later"),
        (BookingSource.UPGRADE, "upgraded to a better time"),
        (BookingSource.ADOPTED_RECONCILE, "confirmed later"),
        (BookingSource.ADOPTED_OWNED, "adopted"),
    ],
)
async def test_history_says_how_a_date_was_booked(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    source: BookingSource,
    how: str,
) -> None:
    """The How column comes from the ownership ledger entry of the reservation the row holds."""
    rule = await _seed_rule(store, member)
    await _book(store, await _row_for(store, member, rule, OCT3), TEE_OCT3, source=source)
    await _move_clock_to(clock, OCT20_NOON)
    text = _main((await _history(client)).text)
    assert "<th>How</th>" in (await client.get("/history")).text
    assert how in text[text.index("Oct 3") :]


async def test_a_cancelled_date_still_says_how_it_was_booked_and_a_lost_one_says_nothing(
    store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    await _a_month_of_golf(store, member, clock)
    lost, cancelled, booked = await services.history(store, user_id=member.user.id, clock=clock)
    assert (lost.row.status, lost.how) == (RowStatus.LOST, None)
    assert (cancelled.row.status, cancelled.how) == (RowStatus.CANCELLED, "when tee times opened")
    assert booked.how == "when tee times opened"


async def test_a_reservation_the_bot_did_not_make_says_so(
    store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """A BOOKED row whose reservation is not in the ledger (a manual booking the bot found on
    the date) is not the bot's work, and history must not claim it."""
    rule = await _seed_rule(store, member)
    row = await _row_for(store, member, rule, OCT3)
    await _book(store, row, TEE_OCT3)
    store._ledger.clear()  # the same row, with no ownership record behind it
    await _move_clock_to(clock, OCT20_NOON)
    (entry,) = await services.history(store, user_id=member.user.id, clock=clock)
    assert entry.how == "not booked by the bot"


async def test_a_failed_ledger_read_blanks_that_dates_how_and_nothing_else(
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The How column is one ledger read per date: one of them failing must cost that date
    its How (shown as a dash, never the false "not booked by the bot"), not the page."""
    await _a_month_of_golf(store, member, clock)
    real = store.list_owned_bookings

    async def flaky(account_id: CourseAccountId, *, target_date: date) -> list[OwnedBooking]:
        if target_date == OCT10:
            raise RuntimeError("cosmos blip")
        return await real(account_id, target_date=target_date)

    monkeypatch.setattr(store, "list_owned_bookings", flaky)
    _, cancelled, booked = await services.history(store, user_id=member.user.id, clock=clock)
    assert cancelled.how is None and cancelled.tee_time_local == "08:15"
    assert booked.how == "when tee times opened"
    assert "history: ledger read failed" in caplog.text


async def test_history_is_reachable_from_the_top_bar_and_the_dashboard(
    client: httpx.AsyncClient, member: Member
) -> None:
    home = (await client.get("/")).text
    assert home.count('href="/history"') == 2  # the nav and the dashboard's own link
    assert 'href="/history" aria-current="page"' in (await client.get("/history")).text


async def test_history_with_nothing_past_says_so(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member
) -> None:
    await _seed_rule(store, member)  # OCT3 is still ahead of T0
    text = _main((await client.get("/history")).text)
    assert "No past dates yet" in text
    assert "Oct 3" not in text


async def test_history_shows_only_my_rows(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    mallory: Member,
    clock: FakeClock,
) -> None:
    mine = await _seed_rule(store, member)
    await _book(store, await _row_for(store, member, mine, OCT3), TEE_OCT3)
    await _a_month_of_golf(store, mallory, clock)
    text = _main((await _history(client)).text)
    assert "Oct 3" in text and "1 booked" in text  # mine shows
    assert "Oct 10" not in text and "Oct 17" not in text  # Mallory's dates
    assert "08:15" not in text and "lost" not in text  # her tee time, her miss


async def test_history_needs_a_sign_in(client: httpx.AsyncClient) -> None:
    signed_out, dates = await client.get("/history"), await client.get("/dates")
    assert signed_out.status_code == dates.status_code != 200
    assert signed_out.headers.get("location") == dates.headers.get("location")


async def test_history_names_the_year_of_an_older_date_and_stops_after_twelve_months(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    await _a_month_of_golf(store, member, clock)
    assert "Oct 3, 2026" not in _main((await _history(client)).text)  # this year: no year
    await _move_clock_to(clock, datetime(2027, 1, 15, 16, 0, tzinfo=UTC))
    assert "Oct 3, 2026" in _main((await _history(client)).text)
    await _move_clock_to(clock, datetime(2027, 10, 9, 16, 0, tzinfo=UTC))  # Oct 3 is 371 days ago
    text = _main((await _history(client)).text)
    assert "Oct 3, 2026" not in text and "Oct 10, 2026" in text


async def test_history_leaves_out_withdrawn_rows(
    store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """A withdrawn or superseded row is a request the person took back or replaced, not an
    outcome: the dashboard hides it and so does history."""
    rule = await _seed_rule(store, member)
    row = await _row_for(store, member, rule, OCT3)
    await store.transition_row(
        row.id,
        user_id=None,
        to=RowStatus.WITHDRAWN,
        actor=Actor.MATERIALIZER,
        reason="rule_deactivated",
        now=T0,
    )
    await _move_clock_to(clock, OCT20_NOON)
    assert await services.history(store, user_id=member.user.id, clock=clock) == []


async def test_the_operators_page_for_a_person_shows_their_past_dates(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    member: Member,
    clock: FakeClock,
    provider_mock: respx.MockRouter,
) -> None:
    await _a_month_of_golf(store, member, clock)
    client.cookies.clear()
    await _sign_in_operator(client, provider_mock)
    text = _main((await client.get(f"/admin/users/{member.user.id}")).text)
    past = text[text.index("Past dates") :]
    assert "Oct 17" in past and "lost" in past
    assert "cancelled at the course" in past and "08:15" in past
    assert "Oct 3" in past and "09:30" in past
    assert "Oct 24" not in past  # upcoming: in the section above


async def test_a_tee_time_dropped_from_the_courses_list_on_its_own_day_reads_as_booked(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """Prod, 2026-10-31 to 2026-11-27: the watcher wrote seven PLAYED tee times
    CANCELLED(external), because the course drops a reservation from the upcoming list at
    check-in. A row cancelled that way on its own date was held to the day: history says
    booked. One cancelled before its date (Oct 10, in ``_a_month_of_golf``) stays cancelled."""
    await _a_month_of_golf(store, member, clock)
    (oct3,) = await store.list_rows_for_user(member.user.id, from_date=OCT3, to_date=OCT3)
    await _cancel_at_course(store, oct3, at=TEE_OCT3 + timedelta(minutes=40))  # 10:10 EDT
    rows = await services.history(store, user_id=member.user.id, clock=clock)
    by_date = {r.row.target_date: r for r in rows}
    assert (by_date[OCT3].played, by_date[OCT10].played) == (True, False)
    assert dict(services.history_summary(rows)) == {"booked": 1, "cancelled": 1, "lost": 1}
    text = _main((await _history(client)).text)
    oct3_line = text[text.index("Oct 3") : text.index("Dates from the last")]
    assert "booked 09:30" in oct3_line and "cancelled" not in oct3_line


async def test_the_dashboard_does_not_flag_a_tee_time_missing_from_the_list_on_its_own_day(
    client: httpx.AsyncClient, store: InMemoryTenantStore, member: Member, clock: FakeClock
) -> None:
    """On the day of play "not seen at course" would be said of every checked-in tee time."""
    rule = await _seed_rule(store, member)
    await _book(store, await _row_for(store, member, rule, OCT3), TEE_OCT3)
    for at in (TEE_OCT3 - timedelta(days=1), TEE_OCT3 + timedelta(minutes=40)):
        await _move_clock_to(clock, at + timedelta(minutes=5))
        await store.save_snapshot(
            ReservationSnapshot(
                course_account_id=member.account.id,
                observed_at=at,
                source="watcher",
                trusted=True,
                entries=(),
            )
        )
        assert (await sign_in(client)).status_code == 303
        page = (await client.get("/")).text
        assert ("not seen at course" in page) is (at < TEE_OCT3)
