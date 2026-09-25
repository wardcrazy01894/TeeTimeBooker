"""E6 (MULTIUSER_PLAN §2.3 / §7.5): ForeUP reports whether the last login produced a
TRUSTWORTHY reservation snapshot.

`list_reservations()` reads a cache built from the POST /login response body, and three
quiet degradations leave that cache empty (or stale) while looking healthy: (a) a soft
login failure (400/401/rejected body), (b) a 200 whose body is not JSON, (c) a JSON
success whose `reservations` is missing or not a list (round-2 SF3). A tenant watcher
that read `[]` from any of them as "the booking vanished" would mark it externally
cancelled or re-book it (double booking). `snapshot_trusted` is True ONLY when a real
list was parsed from the latest login. `list_reservations()` itself is unchanged.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from teetime.core.adapter import ReservationSnapshotHealth
from teetime.core.models import CourseCredentials, CourseId
from teetime.courses.foreup.base import FOREUP_BASE_URL, LOGIN_PATH, ForeUpAdapter
from teetime.dev.fake_adapter import FakeAdapter

CID = CourseId("foreup:mangrove_bay")
CREDS = CourseCredentials(username="user@example.com", password="secret")
WARMUP_URL = f"{FOREUP_BASE_URL}/index.php/booking/19671/2149"
LOGIN_URL = f"{FOREUP_BASE_URL}{LOGIN_PATH}"

_RES = {
    "TTID": "TTID_05271417087kr17",
    "teetime_id": "TTID_05271417087kr17",
    "type": "teetime",
    "start_datetime": "2026-06-03 14:15:00",
    "player_count": "4",
    "course_id": "19671",
}


def _adapter(client: httpx.AsyncClient) -> ForeUpAdapter:
    return ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
        timezone="America/New_York",
        http_client=client,
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=FOREUP_BASE_URL)


async def test_foreup_satisfies_reservation_snapshot_health() -> None:
    async with _client() as client:
        assert isinstance(_adapter(client), ReservationSnapshotHealth)


def test_fake_adapter_does_not_claim_snapshot_health() -> None:
    """Opt-in: an adapter without the property is not a ReservationSnapshotHealth."""
    assert not isinstance(FakeAdapter(course_id=CID), ReservationSnapshotHealth)


async def test_snapshot_untrusted_before_authenticate() -> None:
    async with _client() as client:
        assert _adapter(client).snapshot_trusted is False


@respx.mock
@pytest.mark.parametrize("reservations", [[], [_RES]])
async def test_foreup_json_login_with_reservations_list_is_trusted(
    reservations: list[dict[str, str]],
) -> None:
    """A real list — INCLUDING an empty one (a genuine "no reservations") — is trusted."""
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "reservations": reservations})
    )
    async with _client() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter.snapshot_trusted is True
        assert len(await adapter.list_reservations()) == len(reservations)


@respx.mock
async def test_foreup_non_json_login_marks_snapshot_untrusted() -> None:
    """(b) A 200 whose body is not JSON: `_logged_in` flips True but there is no cache."""
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, text="<html>waf</html>"))
    async with _client() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter.is_authenticated is True
        assert adapter.snapshot_trusted is False
        assert await adapter.list_reservations() == []  # behaviour unchanged


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"success": True, "msg": "ok"},  # key missing
        {"success": True, "reservations": False},  # the lazy-load flag shape
        {"success": True, "reservations": None},
        {"success": True, "reservations": {"TTID": "x"}},  # a dict, not a list
        [],  # JSON, but not an object at all
    ],
)
async def test_foreup_json_login_without_reservations_list_marks_untrusted(
    body: object,
) -> None:
    """(c) round-2 SF3: a JSON success whose `reservations` is missing or not a list."""
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=body))
    async with _client() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter.is_authenticated is True
        assert adapter.snapshot_trusted is False
        assert await adapter.list_reservations() == []


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"success": False, "msg": "invalid"}),
        httpx.Response(400, json={"success": False, "msg": "bad"}),
        httpx.Response(200, json={"success": False, "msg": "rejected", "reservations": []}),
    ],
)
async def test_foreup_soft_failed_login_marks_snapshot_untrusted(
    response: httpx.Response,
) -> None:
    """(a) A soft login failure (400/401/rejected body) — even one echoing a list."""
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    respx.post(LOGIN_URL).mock(return_value=response)
    async with _client() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter.is_authenticated is False
        assert adapter.snapshot_trusted is False


@respx.mock
async def test_foreup_login_http_error_leaves_snapshot_untrusted() -> None:
    """A login that RAISES (5xx) after an earlier trusted login must not leave the flag
    claiming the stale snapshot is trustworthy."""
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    respx.post(LOGIN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "reservations": [_RES]}),
            httpx.Response(503, text="down"),
        ]
    )
    async with _client() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter.snapshot_trusted is True
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.refresh_reservations(CREDS)
        assert adapter.snapshot_trusted is False


@respx.mock
async def test_refresh_to_degraded_login_flips_trust_off() -> None:
    """trusted → forced refresh returns a non-JSON 200 → untrusted. (The stale cache from
    the first login is still what list_reservations() returns — unchanged behaviour, which
    is exactly why the flag must say "don't infer from this".)"""
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    respx.post(LOGIN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "reservations": [_RES]}),
            httpx.Response(200, text="<html>interstitial</html>"),
        ]
    )
    async with _client() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter.snapshot_trusted is True
        await adapter.refresh_reservations(CREDS)
        assert adapter.snapshot_trusted is False


@respx.mock
async def test_idempotent_reauth_keeps_trust() -> None:
    """A second authenticate() short-circuits (no login POST), so the snapshot — and its
    trust — are those of the last real login."""
    respx.get(WARMUP_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    login = respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "reservations": []})
    )
    async with _client() as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        await adapter.authenticate(CREDS)
        assert login.call_count == 1
        assert adapter.snapshot_trusted is True
