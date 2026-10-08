"""A person's own booking cutoff (operator request 2026-10-08): "4 PM the day before" suits the
operator, but a friend may want noon the day before, or 4 PM two days before. The "Your account"
page (``/me``) has a Booking cutoff form; saving it rewrites the person's live dates, and every
page that words the cutoff (the "How the bot picks" panel, each course's release cycle) uses
THEIR cutoff. End-to-end over ASGI.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from teetime.core.booking_cutoff import cutoff_instant
from teetime.core.clock import FakeClock
from teetime.core.config import BookingCutoffConfig
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.materialize import materialize_rule
from teetime.tenant.models import RankedWindow, RequestRow, RuleId, StandingRule
from teetime.web.app import WebSettings, create_app
from teetime.web.routes import ROUTES

from ..tenant.conformance import CUTOFF, MB, OTHER_COURSE, TZ
from .test_web_course_info import NAMES, POLICY
from .test_web_ranked_pages import OCT3, Member, _post
from .test_web_ranked_pages import member as member  # noqa: PLC0414 — pytest fixture re-export

NOON_TWO_DAYS = BookingCutoffConfig(days_before=2, time_of_day=time(12, 0))


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        policies={str(MB): POLICY, str(OTHER_COURSE): POLICY},
        cutoff=CUTOFF,
        course_names=NAMES,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


def _selected(html: str, name: str) -> str:
    box = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.DOTALL)
    assert box, f"no {name} select"
    chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', box.group(1))
    assert chosen, f"nothing selected in {name}"
    return chosen.group(1)


async def _pending_row(store: InMemoryTenantStore, member: Member, clock: FakeClock) -> RequestRow:
    return await store.create_explicit_row(
        user_id=member.user.id,
        account_id=member.a.id,
        target_date=OCT3,
        options=(RankedWindow(1, time(8), time(10)),),
        party_size=2,
        now=clock.now_utc(),
    )


def test_the_cutoff_form_is_in_the_route_contract() -> None:
    (spec,) = [r for r in ROUTES if r.path == "/me/cutoff"]
    assert (spec.method, spec.auth.value, spec.csrf) == ("POST", "user", True)


async def test_your_account_shows_the_cutoff_form_with_the_site_default_selected(
    client: httpx.AsyncClient, member: Member
) -> None:
    html = (await client.get("/me")).text
    assert "Booking cutoff" in html and 'action="/me/cutoff"' in html
    assert _selected(html, "cutoff_time") == "16:00"
    assert _selected(html, "cutoff_days_before") == "1"
    assert "4 PM the day before" in html  # the setting in words
    assert "better" in html.lower() and "final" in html.lower()  # what the cutoff means


async def test_saving_a_cutoff_rewrites_live_dates_and_rewords_every_page(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore, clock: FakeClock
) -> None:
    row = await _pending_row(store, member, clock)
    assert row.cutoff_at == cutoff_instant(OCT3, timezone=TZ, cutoff=CUTOFF)
    r = await _post(client, "/me/cutoff", {"cutoff_time": "12:00", "cutoff_days_before": "2"})
    assert r.status_code == 303 and r.headers["location"] == "/me?notice=cutoff_saved"
    html = (await client.get(r.headers["location"])).text
    assert "Booking cutoff saved" in html
    assert _selected(html, "cutoff_time") == "12:00"
    assert _selected(html, "cutoff_days_before") == "2"
    assert "12 PM, 2 days before" in html
    saved = await store.get_user_unscoped(member.user.id)
    assert saved is not None and saved.booking_cutoff == NOON_TWO_DAYS
    (after,) = await store.rows_for_account_date(member.a.id, OCT3)
    assert after.cutoff_at == cutoff_instant(OCT3, timezone=TZ, cutoff=NOON_TWO_DAYS)
    assert after.version == row.version + 1
    # Every page that words the cutoff uses THEIRS, not the site default.
    for path in ("/rules", "/dates"):
        page = (await client.get(path)).text
        assert "before the booking cutoff (12 PM, 2 days before)" in page
        assert "until 12 PM, 2 days before" in page  # course facts (release cycle tip)
        assert "4 PM the day before" not in page
    lines = await store.recent_audit(member.user.id, limit=10)
    assert "cutoff_set" in [line.action for line in lines]


async def test_a_bad_cutoff_is_refused_with_the_page_re_rendered(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    for form in (
        {"cutoff_time": "12:30", "cutoff_days_before": "1"},  # not on the hour
        {"cutoff_time": "16:00", "cutoff_days_before": "9"},  # too far out
        {"cutoff_time": "16:00", "cutoff_days_before": "-1"},
        {"cutoff_time": "", "cutoff_days_before": "1"},
        {"cutoff_time": "noon", "cutoff_days_before": "1"},
    ):
        r = await _post(client, "/me/cutoff", form)
        assert r.status_code == 400, form
        assert "Booking cutoff" in r.text  # the account page, with the error
    saved = await store.get_user_unscoped(member.user.id)
    assert saved is not None and saved.booking_cutoff is None


async def test_a_date_the_bot_is_checking_keeps_the_old_cutoff_and_the_page_says_so(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore, clock: FakeClock
) -> None:
    row = await _pending_row(store, member, clock)
    now = clock.now_utc()
    assert await store.acquire_row_lease(
        row.id, owner="watcher:run-1", until=now.replace(year=now.year + 1), now=now, expected=None
    )
    r = await _post(client, "/me/cutoff", {"cutoff_time": "12:00", "cutoff_days_before": "2"})
    assert r.status_code == 303 and r.headers["location"] == "/me?notice=cutoff_saved_partly"
    html = (await client.get(r.headers["location"])).text
    assert "save again" in html.lower()
    saved = await store.get_user_unscoped(member.user.id)
    assert saved is not None and saved.booking_cutoff == NOON_TWO_DAYS
    (after,) = await store.rows_for_account_date(member.a.id, OCT3)
    assert after.cutoff_at == row.cutoff_at  # untouched under the lease


# --- a cutoff per booking (2026-10-08, the same day) --------------------------------------------
# "Friday at noon for Saturday and Friday at 4 PM for Sunday, or Thursday, whatever": the weekly
# and one-date forms carry their own Stop-looking pickers (the Day select opens with "my
# account's cutoff"), a single-window rule's edit form too, the rules page words a rule's own,
# Dates shows each date's, and the person's save leaves such bookings alone.

FRIDAY_NOON = BookingCutoffConfig(days_before=1, time_of_day=time(12, 0))


def _ranked_one(member: Member) -> dict[str, str]:
    return {
        "party_size": "2",
        "opt1_account": str(member.a.id),
        "opt1_earliest": "09:00",
        "opt1_latest": "10:00",
        "opt1_rank": "1",
    }


def _picker_opens_on_the_account(html: str, form_action: str) -> None:
    form = re.search(rf'<form[^>]*action="{form_action}"[^>]*>(.*?)</form>', html, re.DOTALL)
    assert form, f"no form posting to {form_action}"
    assert "Stop looking" in form.group(1)
    assert _selected(form.group(1), "cutoff_days_before") == ""
    assert "my account" in form.group(1).lower()


async def test_the_weekly_form_offers_a_cutoff_of_its_own_and_saves_it_on_every_rule(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    html = (await client.get("/rules")).text
    _picker_opens_on_the_account(html, "/bookings/weekly")
    form = {
        **_ranked_one(member),
        "opt2_account": str(member.b.id),
        "opt2_earliest": "09:00",
        "opt2_latest": "10:00",
        "opt2_rank": "2",
        "weekday": "5",
        "cutoff_days_before": "1",
        "cutoff_time": "12:00",
    }
    r = await _post(client, "/bookings/weekly", form)
    assert r.status_code == 303, r.text
    rules = await store.list_rules_for_user(member.user.id)
    assert [rule.booking_cutoff for rule in rules] == [FRIDAY_NOON, FRIDAY_NOON]
    for rule in rules:
        (row,) = await store.rows_for_account_date(rule.course_account_id, OCT3)
        assert row.cutoff_at == cutoff_instant(OCT3, timezone=TZ, cutoff=FRIDAY_NOON)
        assert row.booking_cutoff is None  # a rule row follows its rule
    page = (await client.get("/rules")).text
    assert "stops looking 12 PM the day before" in page
    # A weekly booking left on the account's cutoff says nothing of its own.
    r = await _post(client, "/bookings/weekly", {**_ranked_one(member), "weekday": "6"})
    assert r.status_code == 303, r.text
    sunday = [rule for rule in await store.list_rules_for_user(member.user.id) if rule.weekday == 6]
    assert len(sunday) == 1 and sunday[0].booking_cutoff is None
    page = (await client.get("/rules")).text
    assert page.count("stops looking") == 2  # the two Saturday rules only


async def test_a_bad_cutoff_on_the_weekly_form_is_refused(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    form = {
        **_ranked_one(member),
        "weekday": "5",
        "cutoff_days_before": "1",
        "cutoff_time": "12:30",
    }
    r = await _post(client, "/bookings/weekly", form)
    assert r.status_code == 400
    assert await store.list_rules_for_user(member.user.id) == []


async def test_the_one_date_form_offers_a_cutoff_of_its_own_and_dates_shows_each_dates(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    html = (await client.get("/dates")).text
    _picker_opens_on_the_account(html, "/bookings/date")
    form = {
        **_ranked_one(member),
        "target_date": OCT3.isoformat(),
        "cutoff_days_before": "2",
        "cutoff_time": "16:00",
    }
    r = await _post(client, "/bookings/date", form)
    assert r.status_code == 303, r.text
    (row,) = await store.rows_for_account_date(member.a.id, OCT3)
    thursday_four = BookingCutoffConfig(days_before=2, time_of_day=time(16, 0))
    assert row.booking_cutoff == thursday_four
    assert row.cutoff_at == cutoff_instant(OCT3, timezone=TZ, cutoff=thursday_four)
    page = (await client.get("/dates")).text
    assert "Thu Oct 1, 4 PM" in page  # the date's own cutoff, on the course's clock


async def test_a_single_window_rules_edit_form_carries_its_own_cutoff(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore, clock: FakeClock
) -> None:
    """The in-place edit form pre-selects the rule's own cutoff (even one equal to the
    account's: own is own), saving a cutoff moves the rule's live rows, and clearing it puts
    them back under the account's."""
    rule = await store.upsert_rule(
        StandingRule(
            id=RuleId(uuid4()),
            course_account_id=member.a.id,
            weekday=5,
            options=(RankedWindow(1, time(8), time(10)),),
            party_size=2,
            active=True,
            materialized_through=None,
            version=1,
        ),
        user_id=member.user.id,
    )
    await materialize_rule(rule, store=store, policy=POLICY, cutoff=CUTOFF, now=clock.now_utc())
    (before,) = await store.rows_for_account_date(member.a.id, OCT3)
    html = (await client.get("/rules")).text
    _picker_opens_on_the_account(html, f"/rules/{rule.id}")
    base = {"action": "save", "weekday": "5", "window_earliest": "08:00", "window_latest": "10:00"}
    r = await _post(
        client,
        f"/rules/{rule.id}",
        {
            **base,
            "party_size": "2",
            "version": "1",
            "cutoff_days_before": "1",
            "cutoff_time": "16:00",
        },
    )
    assert r.status_code == 303, r.text
    stored = await store.get_rule_unscoped(rule.id)
    assert stored is not None and stored.booking_cutoff == CUTOFF  # own, though equal
    html = (await client.get("/rules")).text
    form = re.search(rf'<form[^>]*action="/rules/{rule.id}"[^>]*>(.*?)</form>', html, re.DOTALL)
    assert form and _selected(form.group(1), "cutoff_days_before") == "1"
    assert _selected(form.group(1), "cutoff_time") == "16:00"
    # Now Friday noon: the pending row moves with the rule.
    r = await _post(
        client,
        f"/rules/{rule.id}",
        {
            **base,
            "party_size": "2",
            "version": "2",
            "cutoff_days_before": "1",
            "cutoff_time": "12:00",
        },
    )
    assert r.status_code == 303, r.text
    (after,) = await store.rows_for_account_date(member.a.id, OCT3)
    assert after.cutoff_at == cutoff_instant(OCT3, timezone=TZ, cutoff=FRIDAY_NOON)
    assert after.version > before.version
    # Back to the account's: with the person on noon-two-days, the row follows THAT.
    assert (
        await _post(client, "/me/cutoff", {"cutoff_time": "12:00", "cutoff_days_before": "2"})
    ).status_code == 303
    assert (await store.rows_for_account_date(member.a.id, OCT3))[0].cutoff_at == after.cutoff_at
    r = await _post(
        client,
        f"/rules/{rule.id}",
        {**base, "party_size": "2", "version": "3", "cutoff_days_before": ""},
    )
    assert r.status_code == 303, r.text
    stored = await store.get_rule_unscoped(rule.id)
    assert stored is not None and stored.booking_cutoff is None
    (cleared,) = await store.rows_for_account_date(member.a.id, OCT3)
    assert cleared.cutoff_at == cutoff_instant(OCT3, timezone=TZ, cutoff=NOON_TWO_DAYS)


async def test_the_account_save_leaves_a_booking_with_its_own_cutoff_alone_and_says_so(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore, clock: FakeClock
) -> None:
    own = await store.create_explicit_row(
        user_id=member.user.id,
        account_id=member.a.id,
        target_date=OCT3,
        options=(RankedWindow(1, time(8), time(10)),),
        party_size=2,
        now=clock.now_utc(),
        booking_cutoff=FRIDAY_NOON,
    )
    r = await _post(client, "/me/cutoff", {"cutoff_time": "12:00", "cutoff_days_before": "2"})
    assert r.status_code == 303 and r.headers["location"] == "/me?notice=cutoff_saved_kept_own"
    html = (await client.get(r.headers["location"])).text
    assert "its own cutoff" in html
    assert "keeps it" in html  # the hint on the page, in every state
    (after,) = await store.rows_for_account_date(member.a.id, OCT3)
    assert after == own
