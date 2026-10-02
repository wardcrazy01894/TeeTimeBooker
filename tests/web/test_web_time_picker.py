"""The booking forms' time pickers list only a course's tee-sheet hours (operator request
2026-10-02), end-to-end over ASGI: a ``<select>`` per bound (no ``<input type="time">`` anywhere),
the union of the person's courses before a course is chosen (script off), the chosen course's own
bound with script (``data-first`` / ``data-last`` on the course option, read by ``app.js``), a
stored off-grid window still selectable on its edit form, and a 400 naming the course and its hours
for a window outside them.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.courses.names import TeeSheetHours
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RankedWindow, RuleId, StandingRule
from teetime.web.app import STATIC_DIR, WebSettings, create_app

from ..tenant.conformance import CUTOFF, MB, OTHER_COURSE
from .conftest import GitHubIdentity, make_invited, mock_github, sign_in
from .test_web_pages import _rule_form
from .test_web_ranked_pages import MEMBER, OCT3, POLICY, SAT, Member, _account, _post, _ranked

NAMES = {str(MB): "Mangrove Bay", str(OTHER_COURSE): "Twin Brooks"}
HOURS = {
    str(MB): TeeSheetHours(first=time(6, 30), last=time(19, 0)),
    str(OTHER_COURSE): TeeSheetHours(first=time(7, 0), last=time(18, 0)),
}
TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "teetime" / "web" / "templates"


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(
        settings,
        store=store,
        clock=clock,
        policies={str(MB): POLICY, str(OTHER_COURSE): POLICY},
        cutoff=CUTOFF,
        course_names=NAMES,
        tee_sheet_hours=HOURS,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


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


def _select(html: str, name: str) -> str:
    m = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', html, re.S)
    assert m, f"no <select name={name!r}>"
    return m.group(1)


def _values(select_html: str) -> list[str]:
    return re.findall(r'<option value="([^"]*)"', select_html)


def _selected(select_html: str) -> str | None:
    m = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', select_html)
    return m.group(1) if m else None


def test_no_template_uses_a_free_text_time_input() -> None:
    offenders = [p.name for p in TEMPLATES.glob("*.html") if 'type="time"' in p.read_text()]
    assert offenders == []


@pytest.mark.parametrize("path", ["/dates", "/rules"])
async def test_ranked_form_lists_the_union_of_the_persons_courses(
    client: httpx.AsyncClient, member: Member, path: str
) -> None:
    html = (await client.get(path)).text
    for bound, default in (("earliest", "08:00"), ("latest", "10:00")):
        sel = _select(html, f"opt1_{bound}")
        values = _values(sel)
        assert values[0] == "06:30" and values[-1] == "19:00", bound  # MB + Twin Brooks
        assert "04:00" not in values and "21:00" not in values
        assert all(int(v[3:]) % 15 == 0 for v in values)
        assert _selected(sel) == default
    assert "6:30 AM" in html and "7:00 PM" in html  # labels read like a clock


async def test_course_options_carry_their_own_hours_for_the_script(
    client: httpx.AsyncClient, member: Member
) -> None:
    html = (await client.get("/dates")).text
    course = _select(html, "opt1_account")
    assert f'value="{member.a.id}" data-first="06:30" data-last="19:00"' in course
    assert f'value="{member.b.id}" data-first="07:00" data-last="18:00"' in course
    assert "data-first" in (STATIC_DIR / "app.js").read_text()  # the script reads them


async def test_rule_edit_form_is_bounded_to_its_course_and_keeps_an_off_grid_window(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    await store.upsert_rule(
        StandingRule(
            id=RuleId(uuid4()),
            course_account_id=member.b.id,
            weekday=SAT,
            options=(RankedWindow(1, time(9, 22), time(10, 7)),),
            party_size=2,
            active=True,
            materialized_through=None,
            version=1,
        ),
        user_id=member.user.id,
    )
    html = (await client.get("/rules")).text
    earliest, latest = _select(html, "window_earliest"), _select(html, "window_latest")
    assert _values(earliest)[0] == "07:00" and _values(earliest)[-1] == "18:00"  # Twin Brooks
    assert _selected(earliest) == "09:22" and _selected(latest) == "10:07"
    assert _values(earliest) == sorted(_values(earliest))


async def test_ranked_window_outside_the_courses_hours_is_refused_with_the_hours(
    client: httpx.AsyncClient, member: Member
) -> None:
    form = _ranked(member) | {"opt2_earliest": "06:30", "opt2_latest": "08:00"}  # B opens 07:00
    r = await _post(client, "/bookings/weekly", {"weekday": str(SAT), **form})
    assert r.status_code == 400
    assert "Twin Brooks" in r.text and "7:00 AM" in r.text and "6:00 PM" in r.text
    assert "6:30 AM to 8:00 AM" in r.text
    ok = await _post(client, "/bookings/weekly", {"weekday": str(SAT), **_ranked(member)})
    assert ok.status_code == 303


async def test_refusal_is_worded_by_course_and_times_whatever_the_row_number(
    client: httpx.AsyncClient, member: Member
) -> None:
    """Row 3 ranked 1 (ranks are renumbered 1..N): the message must not say "option 1" or
    "option 3", only the course and the times, which the person recognises either way."""
    form = _ranked(member) | {
        "opt3_rank": "1",
        "opt1_rank": "2",
        "opt2_rank": "3",
        "opt3_earliest": "05:00",
        "opt3_latest": "08:00",  # A (Mangrove Bay) opens 06:30
    }
    r = await _post(client, "/bookings/date", {"target_date": OCT3.isoformat(), **form})
    assert r.status_code == 400
    alert = re.search(r'<div class="error" role="alert">\s*<p>(.*?)</p>', r.text, re.S)
    assert alert, "no error block"
    assert "Mangrove Bay" in alert.group(1) and "5:00 AM to 8:00 AM" in alert.group(1)
    assert "option" not in alert.group(1)


async def test_one_off_re_request_outside_hours_is_refused(
    client: httpx.AsyncClient, member: Member
) -> None:
    """The hidden-input forms (re-request, the add-as-one-off follow-up) post to /rows."""
    r = await _post(
        client,
        "/rows",
        {
            "account_id": str(member.b.id),
            "target_date": OCT3.isoformat(),
            "window_earliest": "18:30",
            "window_latest": "19:30",  # Twin Brooks closes 18:00
            "party_size": "2",
        },
    )
    assert r.status_code == 400 and "Twin Brooks" in r.text and "6:00 PM" in r.text


async def test_single_rule_create_and_edit_outside_hours_are_refused(
    client: httpx.AsyncClient, member: Member, store: InMemoryTenantStore
) -> None:
    r = await _post(client, "/rules", _rule_form(member.a, earliest="05:00", latest="08:00"))
    assert r.status_code == 400 and "Mangrove Bay" in r.text and "6:30 AM" in r.text
    r = await _post(client, "/rules", _rule_form(member.a, earliest="08:00", latest="10:00"))
    assert r.status_code == 303
    (rule,) = await store.list_rules_for_user(member.user.id)
    r = await _post(
        client,
        f"/rules/{rule.id}",
        {
            "action": "save",
            "version": str(rule.version),
            "weekday": str(SAT),
            "window_earliest": "18:00",
            "window_latest": "20:00",
            "party_size": "2",
        },
    )
    assert r.status_code == 400 and "7:00 PM" in r.text
