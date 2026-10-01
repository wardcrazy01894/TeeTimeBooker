"""Connected Courses after the first new user (operator requests 2026-10-01):

* a course already connected leaves the Connect dropdown (he saw blank username/password boxes
  under his connected course and wondered whether to do it again); with every course connected
  the form goes away and Re-verify is pointed to;
* the Connect form says you are NOT creating an account: it is the login you already have at the
  course, with a link to the course's booking site to create one first;
* every course states its release cycle (Mangrove Bay: 7 days ahead at 6:00 AM Eastern, so the
  most effective pick is a date 7+ days out), on its card and next to both booking forms.

End-to-end over ASGI (real store + clock; only the OAuth HTTP is mocked), plus the pure text.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import time

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.core.release_policy import ReleasePolicy
from teetime.courses.foreup.mangrove_bay import (
    MANGROVE_BAY_BOOKING_PAGE_URL,
    MANGROVE_BAY_COURSE_ID,
)
from teetime.courses.names import course_signup_url
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import User
from teetime.web.app import WebSettings, create_app
from teetime.web.course_info import release_cycle, zone_label

from ..tenant.conformance import CUTOFF, MB, OTHER_COURSE, TZ
from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_ranked_pages import _account

MEMBER = "andrew@example.test"
POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
CHICAGO = ReleasePolicy(advance_days=15, release_time=time(6, 30), timezone="America/Chicago")
NAMES = {str(MB): "Mangrove Bay", str(OTHER_COURSE): "Twin Brooks"}
_TAG = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return " ".join(_TAG.sub(" ", html).split())


def _connect_section(html: str) -> str:
    start = html.index("<h2>Connect a course</h2>")
    return html[start:]


# --- the words ---------------------------------------------------------------------------------


def test_release_cycle_says_when_tee_times_open_and_when_to_book() -> None:
    cycle = release_cycle(POLICY, cutoff_text="4 PM the day before")
    assert cycle.opens == "Tee times open 7 days ahead, at 6:00 AM Eastern."
    assert "7 or more days out" in cycle.tip
    assert "4 PM the day before" in cycle.tip  # the watcher keeps trying until the cutoff
    assert "cancellation" in cycle.tip


def test_release_cycle_in_another_zone_and_at_a_half_hour() -> None:
    cycle = release_cycle(CHICAGO, cutoff_text="4 PM the day before")
    assert cycle.opens == "Tee times open 15 days ahead, at 6:30 AM Central."
    assert "15 or more days out" in cycle.tip


def test_release_cycle_same_day_has_no_days_out_advice() -> None:
    cycle = release_cycle(
        ReleasePolicy(advance_days=0, release_time=time(7, 0), timezone=TZ),
        cutoff_text="4 PM the day before",
    )
    assert cycle.opens == "Tee times open the same day, at 7:00 AM Eastern."
    assert "days out" not in cycle.tip


def test_zone_labels_name_us_zones_and_fall_back_to_the_city() -> None:
    assert zone_label("America/New_York") == "Eastern"
    assert zone_label("America/Chicago") == "Central"
    assert zone_label("America/Phoenix") == "Arizona"
    assert zone_label("Europe/Dublin") == "Dublin time"


def test_mangrove_bay_has_a_signup_url_and_unknown_courses_have_none() -> None:
    assert course_signup_url(MANGROVE_BAY_COURSE_ID) == MANGROVE_BAY_BOOKING_PAGE_URL
    assert course_signup_url("foreup:nowhere") is None


# --- the pages ---------------------------------------------------------------------------------


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        policies={str(MB): POLICY, str(OTHER_COURSE): CHICAGO},
        cutoff=CUTOFF,
        course_names=NAMES,
        # The conformance suite's MB is a test id; give it Mangrove Bay's real booking page.
        course_signup_urls={str(MB): MANGROVE_BAY_BOOKING_PAGE_URL},
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@dataclass(frozen=True)
class Newcomer:
    user: User


@pytest.fixture
async def newcomer(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> Newcomer:
    await store.upsert_user(make_invited(MEMBER))
    mock_github(provider_mock, GitHubIdentity(subject="77", emails=[(MEMBER, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", "77")
    assert user is not None
    return Newcomer(user=user)


async def test_connect_says_it_is_your_existing_course_login_and_links_to_sign_up(
    client: httpx.AsyncClient, newcomer: Newcomer
) -> None:
    section = _connect_section((await client.get("/accounts")).text)
    text = _text(section)
    assert "This doesn't create an account" in text
    assert "username and password you already use" in text
    assert "create one" in text.lower()
    assert f'href="{MANGROVE_BAY_BOOKING_PAGE_URL}"' in section
    assert "Twin Brooks login" not in text  # no signup page known for it: no link, no guess
    # The release cycle of each course you could connect, right where you choose it.
    assert "Mangrove Bay" in text and "Tee times open 7 days ahead, at 6:00 AM Eastern." in text
    assert "Twin Brooks" in text and "Tee times open 15 days ahead, at 6:30 AM Central." in text


async def test_a_connected_course_leaves_the_connect_dropdown(
    client: httpx.AsyncClient, store: InMemoryTenantStore, newcomer: Newcomer
) -> None:
    before = _connect_section((await client.get("/accounts")).text)
    assert f'<option value="{MB}">Mangrove Bay</option>' in before
    assert f'<option value="{OTHER_COURSE}">Twin Brooks</option>' in before

    await store.upsert_account(_account(newcomer.user.id, MB))
    after = (await client.get("/accounts")).text
    section = _connect_section(after)
    assert f'<option value="{MB}">' not in section
    assert f'<option value="{OTHER_COURSE}">Twin Brooks</option>' in section
    assert 'data-course="' + str(MB) + '"' not in section  # its facts went with it
    # The connected course's own card still shows its login and the Re-verify form.
    assert "Re-verify" in after


async def test_with_every_course_connected_the_form_is_gone_and_reverify_is_pointed_to(
    client: httpx.AsyncClient, store: InMemoryTenantStore, newcomer: Newcomer
) -> None:
    await store.upsert_account(_account(newcomer.user.id, MB))
    await store.upsert_account(_account(newcomer.user.id, OTHER_COURSE))
    page = (await client.get("/accounts")).text
    section = _connect_section(page)
    assert 'action="/accounts/connect"' not in section
    assert 'name="username"' not in section
    assert "Every course this site supports is connected" in _text(section)
    assert "Re-verify" in _text(section)
    assert "Request a course" in _text(section)  # still the way to ask for a new one


async def test_each_connected_course_card_states_its_release_cycle(
    client: httpx.AsyncClient, store: InMemoryTenantStore, newcomer: Newcomer
) -> None:
    await store.upsert_account(_account(newcomer.user.id, MB))
    page = (await client.get("/accounts")).text
    card = page[page.index('<section class="account">') : page.index("<h2>Connect a course</h2>")]
    text = _text(card)
    assert "Tee times open 7 days ahead, at 6:00 AM Eastern." in text
    assert "7 or more days out" in text


@pytest.mark.parametrize("path", ["/dates", "/rules"])
async def test_both_booking_forms_state_each_courses_release_cycle(
    client: httpx.AsyncClient, store: InMemoryTenantStore, newcomer: Newcomer, path: str
) -> None:
    assert "Tee times open" not in (await client.get(path)).text  # nothing connected yet
    await store.upsert_account(_account(newcomer.user.id, MB))
    await store.upsert_account(_account(newcomer.user.id, OTHER_COURSE))
    text = _text((await client.get(path)).text)
    assert "Mangrove Bay: Tee times open 7 days ahead, at 6:00 AM Eastern." in text
    assert "Twin Brooks: Tee times open 15 days ahead, at 6:30 AM Central." in text
