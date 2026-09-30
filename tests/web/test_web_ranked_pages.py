"""MU-R3 (MULTIUSER_PLAN §16.1): the ranked booking form on /dates and /rules, and the default
price on /accounts. End-to-end over ASGI (real store + clock; only the OAuth HTTP is mocked).

Clock: T0 = Sat 2026-09-26 12:00 UTC (08:00 EDT); Sat 10/3 is bookable.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.core.models import CourseId
from teetime.core.release_policy import ReleasePolicy
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    CourseAccount,
    RankedWindow,
    User,
    UserId,
    derive_account_id,
)
from teetime.web import services
from teetime.web.app import WebSettings, create_app

from ..tenant.conformance import CUTOFF, MB, OTHER_COURSE, TZ
from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_pages import _book

MEMBER = "turk@example.test"
POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
OCT3 = date(2026, 10, 3)
SAT = 5


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        policies={str(MB): POLICY, str(OTHER_COURSE): POLICY},
        cutoff=CUTOFF,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@dataclass(frozen=True)
class Member:
    user: User
    a: CourseAccount
    b: CourseAccount


def _account(user_id: UserId, course: CourseId) -> CourseAccount:
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
    a, b = _account(user.id, MB), _account(user.id, OTHER_COURSE)
    await store.upsert_account(a)
    await store.upsert_account(b)
    return Member(user=user, a=a, b=b)


async def _post(client: httpx.AsyncClient, path: str, data: dict[str, str]) -> httpx.Response:
    page = await client.get("/")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    token = page.text[start : page.text.index('"', start)]
    return await client.post(path, data={"csrf_token": token, **data})


def _ranked(m: Member) -> dict[str, str]:
    """A 9-10 (1) > B 9-10 (2) > A 8-9 (3), party 4, $85 on B."""
    form = {"party_size": "4", f"price_{m.b.id}": "85"}
    for i, (acc, lo, hi) in enumerate(
        [(m.a, "09:00", "10:00"), (m.b, "09:00", "10:00"), (m.a, "08:00", "09:00")], start=1
    ):
        form |= {
            f"opt{i}_account": str(acc.id),
            f"opt{i}_earliest": lo,
            f"opt{i}_latest": hi,
            f"opt{i}_rank": str(i),
        }
    return form


async def test_dates_page_renders_the_ranked_form_with_every_account(
    client: httpx.AsyncClient, member: Member
) -> None:
    page = await client.get("/dates")
    assert page.status_code == 200
    assert 'action="/bookings/date"' in page.text
    assert 'name="opt6_account"' in page.text
    assert f'name="price_{member.a.id}"' in page.text
    assert f'name="price_{member.b.id}"' in page.text


async def test_book_a_date_saves_one_row_per_course_as_one_group(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    resp = await _post(client, "/bookings/date", {"target_date": "2026-10-03", **_ranked(member)})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dates?notice=group_saved"
    (row_a,) = await store.rows_for_account_date(member.a.id, OCT3)
    (row_b,) = await store.rows_for_account_date(member.b.id, OCT3)
    assert row_a.group_id is not None and row_a.group_id == row_b.group_id
    assert row_a.options == (RankedWindow(1, time(9), time(10)), RankedWindow(3, time(8), time(9)))
    assert row_b.max_price == Decimal("85.00")
    assert row_a.max_price is None


async def test_book_a_date_partial_save_says_which_course_was_not_saved(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore, clock: FakeClock
) -> None:
    await store.create_explicit_row(
        user_id=member.user.id,
        account_id=member.b.id,
        target_date=OCT3,
        options=(RankedWindow(1, time(7), time(8)),),
        party_size=2,
        now=clock.now_utc(),
    )
    resp = await _post(client, "/bookings/date", {"target_date": "2026-10-03", **_ranked(member)})
    assert resp.status_code == 409
    assert "Saved for 1 of 2 courses" in resp.text
    assert str(OTHER_COURSE) in resp.text
    assert len(await store.rows_for_account_date(member.a.id, OCT3)) == 1


async def test_book_weekly_saves_one_rule_per_course_as_one_group(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    resp = await _post(client, "/bookings/weekly", {"weekday": str(SAT), **_ranked(member)})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/rules?notice=group_rule_saved"
    rules = await store.list_rules_for_user(member.user.id)
    assert len(rules) == 2
    assert len({r.group_id for r in rules}) == 1
    page = await client.get("/rules")
    assert 'action="/bookings/weekly"' in page.text


async def test_bad_ranked_form_is_a_400_on_the_source_page(
    client: httpx.AsyncClient, member: Member
) -> None:
    form = _ranked(member) | {"opt2_rank": "1"}
    resp = await _post(client, "/bookings/date", {"target_date": "2026-10-03", **form})
    assert resp.status_code == 400
    assert "different rank" in resp.text


async def test_ranked_form_with_a_foreign_account_is_the_uniform_404(
    client: httpx.AsyncClient, member: Member
) -> None:
    form = _ranked(member) | {"opt1_account": str(uuid4())}
    resp = await _post(client, "/bookings/date", {"target_date": "2026-10-03", **form})
    assert resp.status_code == 404


async def test_account_default_price_can_be_changed_from_the_accounts_page(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    page = await client.get("/accounts")
    assert f'action="/accounts/{member.a.id}/price"' in page.text
    assert 'value="100.00"' in page.text
    resp = await _post(client, f"/accounts/{member.a.id}/price", {"default_max_price": "70"})
    assert resp.status_code == 303
    stored = await store.get_account(member.a.id, user_id=member.user.id)
    assert stored is not None and stored.default_max_price == Decimal("70.00")


async def test_a_booked_group_row_shows_which_option_it_got(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    await _post(client, "/bookings/date", {"target_date": "2026-10-03", **_ranked(member)})
    (row_a,) = await store.rows_for_account_date(member.a.id, OCT3)
    await _book(store, row_a)  # 09:30 EDT: inside option 1 (A 09:00-10:00)
    page = await client.get("/dates")
    assert "got option 1" in page.text


async def test_a_ranked_rule_shows_its_options_and_no_single_window_edit(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    await _post(client, "/bookings/weekly", {"weekday": str(SAT), **_ranked(member)})
    page = await client.get("/rules")
    assert "09:00\u201310:00, 08:00\u201309:00" in page.text
    assert 'name="window_earliest"' not in page.text
    assert 'value="deactivate"' in page.text


# --- form controls (operator request 2026-09-29) ------------------------------------------------


def _party_radios(page: str) -> list[tuple[str, bool]]:
    return [
        (m.group(1), "checked" in m.group(0))
        for m in re.finditer(r'<input type="radio" name="party_size" value="(\d)"[^>]*>', page)
    ]


@pytest.mark.parametrize("path", ["/dates", "/rules"])
async def test_players_is_a_one_to_four_button_choice_defaulting_to_two(
    client: httpx.AsyncClient, member: Member, path: str
) -> None:
    """Players is a row of 1 / 2 / 3 / 4 buttons (radio inputs, so it works without script),
    never a typed number."""
    page = (await client.get(path)).text
    assert _party_radios(page) == [("1", False), ("2", True), ("3", False), ("4", False)]
    assert not re.search(r'type="number"[^>]*name="party_size"', page)


async def test_the_date_field_is_enhanced_but_stays_a_native_date_input(
    client: httpx.AsyncClient, member: Member
) -> None:
    """Without script it is the browser's date box; app.js turns ``.datepick`` into the
    side-to-side month calendar."""
    page = (await client.get("/dates")).text
    assert re.search(r'<input type="date" name="target_date" class="datepick"[^>]*required', page)


def test_earliest_bookable_date_is_today_in_the_course_zone_not_utc() -> None:
    """02:00 UTC on Sep 27 is still Sep 26 at a New York course: the calendar must not grey out
    the course's today because the server (or the visitor) is already on the next UTC day."""
    now = datetime(2026, 9, 27, 2, 0, tzinfo=UTC)
    accounts = [_account(UserId(uuid4()), MB)]
    got = services.earliest_bookable_date(accounts, {str(MB): POLICY}, now=now)
    assert got == date(2026, 9, 26)
    # No policy for any account (or no accounts): fall back to the UTC date.
    assert services.earliest_bookable_date([], {}, now=now) == date(2026, 9, 27)


async def test_the_date_field_carries_the_server_computed_min(
    client: httpx.AsyncClient, member: Member, clock: FakeClock
) -> None:
    page = (await client.get("/dates")).text
    today = clock.now_utc().astimezone(ZoneInfo(TZ)).date()
    assert re.search(
        rf'<input type="date" name="target_date" class="datepick" min="{today.isoformat()}"', page
    )


@pytest.mark.parametrize("path", ["/dates", "/rules"])
async def test_both_booking_forms_explain_how_a_time_is_picked(
    client: httpx.AsyncClient, member: Member, path: str
) -> None:
    page = (await client.get(path)).text
    panel = page[page.index('class="how-picked"') :]
    panel = panel[: panel.index("</details>")]
    assert "How the bot picks your tee time" in panel
    assert "middle" in panel
    chips = panel[panel.index('class="pick-order"') :]
    order = [chips.index(t) for t in ("9:00 AM", "9:07 AM", "8:52 AM")]
    assert order == sorted(order)  # the chips list them best first
    assert "Example at Mangrove Bay" in panel
    assert "before the booking cutoff (4 PM the day before)" in panel  # from BookingCutoffConfig
    assert "<svg" in panel and "style=" not in panel  # CSP: no inline style, even in the SVG
