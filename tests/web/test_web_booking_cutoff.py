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

import httpx
import pytest
from fastapi import FastAPI

from teetime.core.booking_cutoff import cutoff_instant
from teetime.core.clock import FakeClock
from teetime.core.config import BookingCutoffConfig
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RankedWindow, RequestRow
from teetime.web.app import WebSettings, create_app
from teetime.web.routes import ROUTES

from ..tenant.conformance import CUTOFF, MB, OTHER_COURSE, TZ
from .test_web_course_info import NAMES, POLICY
from .test_web_ranked_pages import OCT3, Member, _post

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
