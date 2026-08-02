"""Unit tests for ForeUpAdapter using respx to mock httpx.

Tests cover: authenticate, search (filtering), book, list_reservations,
aclose, and the two parse helpers. No real network traffic.
"""

from __future__ import annotations

import json as stdlib_json
import logging
from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from teetime.core.adapter import (
    CancelError,
    CaptchaError,
    CourseAdapter,
    InventoryNotPublishedError,
    OtpChallengeError,
    RateLimitError,
    ReservationCacheRefreshable,
    SlotGoneError,
)
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    CourseCredentials,
    CourseId,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.courses.foreup.base import (
    FOREUP_BASE_URL,
    LOGIN_PATH,
    RESERVATION_PATH,
    TIMES_PATH,
    ForeUpAdapter,
    _parse_reservation,
    _parse_slot,
)
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter

# All injected test clients need base_url so relative paths resolve correctly.
_CLIENT_KWARGS = {"base_url": FOREUP_BASE_URL}

ET = ZoneInfo("America/New_York")
CID = CourseId("foreup:mangrove_bay")
CREDS = CourseCredentials(username="user@example.com", password="secret")
TARGET_DATE = date(2026, 5, 13)

_RAW_SLOT = {
    "teesheet_id": 99001,
    "time": "09:30:00",
    "holes": 18,
    "available_spots": 4,
    "green_fee": "45.00",
    "rate_type": "walking",
    "course_id": 19671,
    "schedule_id": 2149,
}

_RAW_RESERVATION = {
    "id": "RES-123",
    "tee_time": "2026-05-13 08:00:00",
    "players": 2,
}

# Matches the actual ForeUP login-response shape discovered via live API probe.
# Field names differ from the legacy GET-endpoint shape above.
_RAW_FOREUP_LOGIN_RESERVATION = {
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


def _retry_adapter(client: httpx.AsyncClient, *, max_retries: int = 2) -> ForeUpAdapter:
    """Adapter with retries enabled and zero backoff (no real sleeps in tests)."""
    return ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
        timezone="America/New_York",
        http_client=client,
        max_retries=max_retries,
        retry_backoff_s=0.0,
    )


def _request(*, dry_run: bool = False) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        dry_run=dry_run,
    )


# --- Protocol structural check -------------------------------------------


def test_mangrove_bay_adapter_satisfies_protocol() -> None:
    assert isinstance(MangroveBayAdapter(), CourseAdapter)


# --- authenticate --------------------------------------------------------


@respx.mock
async def test_authenticate_success() -> None:
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "msg": "ok"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)  # should not raise


@respx.mock
async def test_authenticate_bad_password_soft_fails() -> None:
    """401 login is a soft-fail: PHPSESSID alone still allows search().
    book() will refuse until _logged_in=True."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(
            401,
            json={"success": False, "msg": "Username or password is invalid"},
            headers={"content-type": "application/json"},
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)  # must not raise
        assert adapter._logged_in is False
        # AuthStateReportable contract (RACE_PREWARM_PLAN §3.1 SF#1): is_authenticated
        # must report the soft failure so the race pre-warm skips recording this course.
        assert adapter.is_authenticated is False


@respx.mock
async def test_is_authenticated_reflects_logged_in() -> None:
    """`is_authenticated` is the AuthStateReportable signal the race pre-warm reads to
    distinguish a real login from ForeUP's soft-fail. It must track `_logged_in`."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "msg": "ok"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        assert adapter.is_authenticated is False  # before login
        await adapter.authenticate(CREDS)
        assert adapter.is_authenticated is True  # after a successful login


@respx.mock
async def test_authenticate_is_idempotent_skips_relogin_when_already_logged_in() -> None:
    """Defensive `_logged_in` guard (RACE_PREWARM_PLAN §3.1): a second authenticate()
    after a successful login is a no-op — it does NOT re-issue the warm-up GET or login
    POST. (Hygiene: a real ForeUP re-login is wasteful; the orchestrator already skips
    via prewarmed_course_ids, but the adapter honors the Protocol's documented idempotency.)"""
    warm = respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    login = respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "msg": "ok"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        await adapter.authenticate(CREDS)  # second call must short-circuit
        assert adapter._logged_in is True
        assert warm.call_count == 1, "second authenticate() must not re-warm"
        assert login.call_count == 1, "second authenticate() must not re-login"


@respx.mock
async def test_refresh_reservations_forces_relogin_and_rebuilds_cache() -> None:
    """`refresh_reservations()` (ReservationCacheRefreshable) must FORCE a fresh login even
    when already logged in, so `list_reservations()` reflects the CURRENT server state.

    This is the blind-POST re-guard must-fix: `authenticate()` is idempotent (no-op when
    `_logged_in`), so a landed-but-uncertain reservation created during the T0 burst would be
    invisible if the re-guard only re-authenticated. The first login returns an EMPTY list; the
    second (forced) login returns the now-landed reservation. Without the forced relogin the
    cache would stay empty and the orchestrator would double-book."""
    warm = respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    login = respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "msg": "ok", "reservations": []}),
            httpx.Response(
                200,
                json={
                    "success": True,
                    "msg": "ok",
                    "reservations": [_RAW_FOREUP_LOGIN_RESERVATION],
                },
            ),
        ]
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter._reservations_from_login == []  # nothing booked yet

        # A second plain authenticate() would short-circuit (idempotent) — but
        # refresh_reservations() must force the warm-up GET + login POST again.
        await adapter.refresh_reservations(CREDS)

        assert warm.call_count == 2, "refresh_reservations must re-warm"
        assert login.call_count == 2, "refresh_reservations must re-login"
        assert adapter._logged_in is True
        assert adapter._reservations_from_login == [_RAW_FOREUP_LOGIN_RESERVATION]


async def test_foreup_adapter_is_reservation_cache_refreshable() -> None:
    """ForeUpAdapter structurally satisfies the ReservationCacheRefreshable capability so
    the orchestrator's `isinstance` branch routes it to the forced refresh."""

    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        assert isinstance(adapter, ReservationCacheRefreshable)


@respx.mock
async def test_authenticate_retries_after_soft_login_failure() -> None:
    """A soft login failure (401) leaves `_logged_in` False, so a later authenticate()
    DOES re-issue the warm-up GET + login POST (the guard only skips on a real success)."""
    warm = respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    login = respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        side_effect=[
            httpx.Response(
                401,
                json={"success": False, "msg": "invalid"},
                headers={"content-type": "application/json"},
            ),
            httpx.Response(200, json={"success": True, "msg": "ok"}),
        ]
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
        assert adapter._logged_in is False
        await adapter.authenticate(CREDS)  # retries because the first was a soft-fail
        assert adapter._logged_in is True
        assert warm.call_count == 2
        assert login.call_count == 2


@respx.mock
async def test_authenticate_captcha_raises_captcha_error() -> None:
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(
            400,
            json={"success": False, "msg": "Refresh required.", "openNewWindow": True},
            headers={"content-type": "application/json"},
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(CaptchaError):
            await adapter.authenticate(CREDS)


@respx.mock
async def test_authenticate_non_json_200_does_not_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HTTP 200 with a non-JSON body must not raise UnboundLocalError, but it MUST warn.

    The prior bug: `data` was unbound after `ValueError` from `r.json()`, causing
    `if isinstance(data, dict)` to raise UnboundLocalError. The fix initializes
    `data = {}` before the try block. A non-JSON 200 is treated as a successful
    login (status 200 is trusted), but no JWT or reservations are extracted — so this
    branch flips `_logged_in` True on a body we couldn't parse (e.g. a WAF interstitial).
    That must be logged, or a session with no JWT/reservation cache looks healthy.
    """
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, text="<html>error page</html>")
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level("WARNING"):
            await adapter.authenticate(CREDS)  # must not raise UnboundLocalError
    # 200 status → _logged_in=True; no JWT or reservations extracted from HTML body
    assert adapter._auth_token is None
    assert adapter._reservations_from_login == []
    assert "not JSON" in caplog.text


@respx.mock
async def test_authenticate_soft_fail_clears_reservations_cache() -> None:
    """A 401 soft-fail resets _reservations_from_login so a stale prior cache is never returned."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(401, json={"success": False})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        # Simulate a stale cache from a previous successful login
        adapter._reservations_from_login = [_RAW_RESERVATION]
        await adapter.authenticate(CREDS)  # soft-fail
    # Cache must be cleared — not left as stale data
    assert adapter._reservations_from_login == []


async def test_list_reservations_raises_if_not_authenticated() -> None:
    """list_reservations() must raise RuntimeError when authenticate() was never called.
    Guards PLAN §9 layer-2: silent empty list would pass the pre-book check vacuously."""
    adapter = ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
    )
    with pytest.raises(RuntimeError, match="authenticate"):
        await adapter.list_reservations()


# --- search --------------------------------------------------------------


@respx.mock
async def test_search_returns_matching_slots() -> None:
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(_request())
    assert len(slots) == 1
    assert slots[0].slot_id == SlotId("99001")
    assert slots[0].price_per_player == Decimal("45.00")
    assert slots[0].holes == 18
    assert slots[0].available_spots == 4


@respx.mock
async def test_search_logs_matched_tee_times_for_retroactive_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The search logs each date's matched (in-window) tee times, not just a count, so
    that after a real 06:00 drop the blind-POST derived grid (see test_mangrove_bay_blind_post)
    can be diffed against the actual morning inventory to detect grid drift. PII-free."""
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT])  # 09:30, in 09:00-10:30
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level("INFO"):
            await adapter.search(_request())
    assert "matched tee times" in caplog.text
    assert "09:30" in caplog.text


@respx.mock
async def test_search_filters_out_of_window_slots() -> None:
    out_of_window = {**_RAW_SLOT, "time": "12:00:00"}  # noon, outside 09:00-10:30
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[out_of_window])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(_request())
    assert slots == []


@respx.mock
async def test_search_filters_by_max_price() -> None:
    expensive = {**_RAW_SLOT, "green_fee": "100.00"}
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[expensive])
    )
    req = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        max_price_per_player=Decimal("55.00"),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(req)
    assert slots == []


# --- 0-match search diagnostics -------------------------------------------
# When a search returns inventory but NOTHING matches, the old logs said only
# "got 27 raw slot(s) ... 0 slot(s) match filters" — which cannot distinguish a genuinely
# blocked/sold-out window from a filter bug (a wrong window, a price ceiling that excludes
# everything, a holes mismatch). That ambiguity cost real diagnosis time after the
# 2026-08-01 miss: answering it required hitting the live ForeUP API by hand.
#
# LEVEL SPLIT (deliberate): a purely out-of-window miss is the ROUTINE case — Mangrove Bay
# mornings sell out, and the watcher searches ~300x/day — so it logs at INFO, where its
# neighbouring `got N raw slot(s)` / `matched tee times: []` lines already live. A rejection
# on any OTHER leg (price/holes/spots) implies a misconfiguration and logs at WARNING. This
# keeps the `dropped N/M unparseable slot(s)` schema-break canary — the only other WARNING in
# search() — from being buried under hundreds of routine sell-out lines.

_DIAG = "teetime.courses.foreup.base"


def _diag_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Records carrying the 0-match diagnostics line (matched by content, not substring luck)."""
    return [r for r in caplog.records if "matched filters" in r.getMessage()]


@respx.mock
async def test_search_zero_match_logs_out_of_window_breakdown_and_span(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-08-01 shape: inventory exists, but all of it sits outside the window.

    The log alone must show WHY (out-of-window) and WHAT was on offer (16:07-17:45), so a
    course-level block is distinguishable from a broken filter without touching the live API.
    """
    afternoon = [
        {**_RAW_SLOT, "time": t, "teesheet_id": i}
        for i, t in enumerate(["16:07:00", "16:52:00", "17:45:00"])
    ]
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=afternoon)
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level(logging.INFO):
            slots = await adapter.search(_request())  # window 09:00-10:30
    assert slots == []
    (rec,) = _diag_records(caplog)
    text = rec.getMessage()
    assert "out-of-window=3" in text, f"missing rejection breakdown: {text}"
    # The span of what WAS available — the single most diagnostic fact.
    assert "16:07" in text and "17:45" in text, f"missing available-time span: {text}"
    # And the window we were looking for, so the two can be compared at a glance.
    assert "09:00" in text and "10:30" in text, f"missing requested window: {text}"


@respx.mock
async def test_search_sold_out_window_logs_at_info_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sold-out morning is routine (~300 searches/day), so it must not cry WARNING.

    Otherwise it drowns the `dropped N/M unparseable slot(s)` schema-break canary, which is
    the only other WARNING search() emits.
    """
    afternoon = [{**_RAW_SLOT, "time": "16:07:00"}]
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=afternoon)
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level(logging.INFO):
            await adapter.search(_request())
    (rec,) = _diag_records(caplog)
    assert rec.levelno == logging.INFO, f"sold-out window should be INFO, got {rec.levelname}"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@respx.mock
async def test_search_zero_match_breaks_down_price_and_spots_and_holes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each filter leg is counted separately, so a config error names itself.

    A non-window rejection means the request itself is likely misconfigured — that IS
    actionable, so it escalates to WARNING.
    """
    in_window = "09:30:00"
    raws = [
        {**_RAW_SLOT, "teesheet_id": 1, "time": in_window, "green_fee": "500.00"},
        {**_RAW_SLOT, "teesheet_id": 2, "time": in_window, "available_spots": 0},
        {**_RAW_SLOT, "teesheet_id": 3, "time": in_window, "holes": 9},
    ]
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(return_value=httpx.Response(200, json=raws))
    req = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE,),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        max_price_per_player=Decimal("55.00"),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level(logging.INFO):
            slots = await adapter.search(req)
    assert slots == []
    (rec,) = _diag_records(caplog)
    text = rec.getMessage()
    assert rec.levelno == logging.WARNING, f"filter-bug shape should be WARNING: {text}"
    assert "over-price=1" in text, text
    assert "insufficient-spots=1" in text, text
    assert "wrong-holes=1" in text, text


@respx.mock
async def test_search_diagnostics_are_scoped_per_target_date(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The tally must reset per date — it lives inside the target_dates loop.

    A refactor hoisting `rejected`/`offered` above the loop would silently mis-report every
    date after the first (carrying date 1's counts and times into date 2's line), and nothing
    else in the suite would notice. Date 1 matches, date 2 does not.
    """
    d1, d2 = date(2026, 5, 13), date(2026, 5, 14)
    responses = [
        httpx.Response(200, json=[_RAW_SLOT]),  # 09:30 — matches
        httpx.Response(200, json=[{**_RAW_SLOT, "time": "16:07:00"}]),  # out of window
    ]
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(side_effect=responses)
    req = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(d1, d2),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level(logging.INFO):
            slots = await adapter.search(req)
    assert len(slots) == 1  # only date 1 matched
    (rec,) = _diag_records(caplog)  # exactly one diagnostics line, for date 2 only
    text = rec.getMessage()
    assert str(d2) in text and str(d1) not in text, f"diagnostics named the wrong date: {text}"
    assert "0/1" in text, f"date-1 slots leaked into date-2's denominator: {text}"
    assert "16:07" in text and "09:30" not in text, f"date-1 times leaked: {text}"


@respx.mock
async def test_search_all_unparseable_does_not_crash_or_diagnose(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`offered` is empty when every slot fails to parse — min()/max() must never run.

    If that guard ever broke, `min()` on an empty list would raise ValueError out of
    `search()`; on the T0 blind-POST 0-booked fallback that loses the week's booking.
    """
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[{"nope": 1}, {"nope": 2}])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level(logging.INFO):
            slots = await adapter.search(_request())
    assert slots == []
    assert not _diag_records(caplog), "no inventory parsed — nothing to diagnose"
    assert any("dropped 2/2 unparseable" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_search_does_not_diagnose_when_something_matched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No noise on the happy path — the watcher runs this every 10 minutes, all year."""
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level(logging.INFO):
            slots = await adapter.search(_request())
    assert len(slots) == 1
    assert not _diag_records(caplog)


@respx.mock
async def test_search_does_not_diagnose_when_course_returned_no_inventory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty teesheet needs no breakdown — and an unpublished date legitimately returns [].

    Diagnosing here would fire on every watcher cycle for a date the course has not opened
    yet, drowning the signal this line exists to carry.
    """
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(return_value=httpx.Response(200, json=[]))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level(logging.INFO):
            slots = await adapter.search(_request())
    assert slots == []
    assert not _diag_records(caplog)


@respx.mock
async def test_search_rate_limited_raises() -> None:
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(RateLimitError) as exc_info:
            await adapter.search(_request())
    assert exc_info.value.retry_after_s == 30.0


@respx.mock
async def test_search_empty_list_returns_no_slots() -> None:
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(return_value=httpx.Response(200, json=[]))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        slots = await adapter.search(_request())
    assert slots == []


@respx.mock
async def test_search_unexpected_shape_redacts_pii_in_error() -> None:
    """A non-list /times body is echoed into InventoryNotPublishedError for diagnosis,
    but any account-holder email in it must be redacted before it reaches the log."""
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json={"error": "denied for player@example.com"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(InventoryNotPublishedError) as exc_info:
            await adapter.search(_request())
    msg = str(exc_info.value)
    assert "player@example.com" not in msg
    assert "<redacted-email>" in msg


@respx.mock
async def test_search_logs_dropped_unparseable_slots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A /times list whose items fail to parse (a ForeUP schema drift) must NOT empty the
    result silently. search() backs the 06:00 booking decision: if every slot is unparseable
    it returns [] → the bot reports NO_INVENTORY, indistinguishable from a genuinely empty
    teesheet. Mirror the list_reservations() parse-drop log: a PII-free aggregate at WARNING
    (count + sample keys), not per-slot spam. One valid + one broken slot → 1 returned, 1 logged."""
    broken = {k: v for k, v in _RAW_SLOT.items() if k != "time"}  # missing 'time' → KeyError
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT, broken])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with caplog.at_level("WARNING"):
            slots = await adapter.search(_request())

    assert len(slots) == 1  # the valid slot still comes through
    warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and "unparseable" in r.message
    ]
    assert warnings, "expected a WARNING about dropped unparseable slots"
    text = caplog.text
    assert "1" in text  # dropped count
    # PII-free: the slot's KEYS may be logged for schema-drift diagnosis, never values.
    assert "09:30" not in text  # no tee-time value leaks
    assert "teesheet_id" in text  # sample keys present


# --- leading courtesy-sleep trim, RACE PATH ONLY (Change D / PR3) ---------


def _multi_date_request() -> BookingRequest:
    """A two-date search request (mirrors a multi-date search call)."""
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET_DATE, date(2026, 5, 14)),
        time_windows=(TimeWindow(earliest=time(9, 0), latest=time(10, 30)),),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
    )


@respx.mock
async def test_search_skips_leading_sleep_when_skip_initial_spacing_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single date + skip_initial_spacing=True → the GET fires with NO leading sleep."""
    sleeps: list[float] = []

    async def _spy(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("teetime.courses.foreup.base.asyncio.sleep", _spy)
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.search(_request(), skip_initial_spacing=True)
    assert sleeps == []  # no courtesy delay before the leading GET


@respx.mock
async def test_search_keeps_leading_sleep_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (watcher path): one date per call DOES sleep before the GET.

    This is the watcher's real call pattern; the leading sleep is its only
    inter-date-check spacing, so it must be preserved when the flag is unset.
    """
    sleeps: list[float] = []

    async def _spy(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("teetime.courses.foreup.base.asyncio.sleep", _spy)
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.search(_request())
    assert sleeps == [0.25]


@respx.mock
async def test_search_spaces_subsequent_requests_even_when_skipping_initial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two dates + skip_initial_spacing=True: 1st GET no sleep, 2nd GET IS spaced."""
    sleeps: list[float] = []

    async def _spy(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("teetime.courses.foreup.base.asyncio.sleep", _spy)
    respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(200, json=[_RAW_SLOT])
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.search(_multi_date_request(), skip_initial_spacing=True)
    assert sleeps == [0.25]  # only the 2nd iteration spaces


# --- transient-error retry (idempotent calls only) -----------------------


@respx.mock
async def test_search_retries_transient_timeout_then_succeeds() -> None:
    """A single httpx.ReadTimeout on the /times GET is retried, not abandoned.

    Reproduces the prod failure mode: ForeUP occasionally read-times-out for one
    poll while the server is up (adjacent polls succeed). The watcher's per-course
    catch turned that into a wasted 10-minute cycle. With retry, the call recovers
    in-run. See the prod log incident 2026-06-01."""
    route = respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        side_effect=[httpx.ReadTimeout("upstream slow"), httpx.Response(200, json=[_RAW_SLOT])]
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        slots = await adapter.search(_request())
    assert route.call_count == 2  # 1 timeout + 1 successful retry
    assert len(slots) == 1


@respx.mock
async def test_search_raises_after_retries_exhausted() -> None:
    """A persistent transport failure (server genuinely unreachable) still raises
    after the bounded retries — we do not retry forever."""
    route = respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        side_effect=httpx.ReadTimeout("upstream down")
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client, max_retries=2)
        with pytest.raises(httpx.TransportError):
            await adapter.search(_request())
    assert route.call_count == 3  # 1 initial attempt + 2 retries


@respx.mock
async def test_authenticate_retries_transient_connect_blip() -> None:
    """The warm-up GET and login POST are idempotent and retried on a connect blip."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        side_effect=[httpx.ConnectError("blip"), httpx.Response(200, text="<html/>")]
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"success": True})]
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        await adapter.authenticate(CREDS)  # must not raise
    assert adapter._logged_in is True


@respx.mock
async def test_http_status_errors_are_not_retried() -> None:
    """Retry is scoped to transport failures. A 500 surfaces via raise_for_status
    on the first attempt — it must NOT be retried (it isn't a transient transport blip)."""
    route = respx.get(f"{FOREUP_BASE_URL}{TIMES_PATH}").mock(
        return_value=httpx.Response(500, text="server error")
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.search(_request())
    assert route.call_count == 1  # no retry on HTTP status errors


@respx.mock
async def test_book_is_not_retried_on_transient_error() -> None:
    """book()'s POST is single-attempt: a transport error propagates and is NEVER
    retried (§9 double-booking defense — a timed-out book is the UNCERTAIN case the
    watcher reconciles asynchronously, not a safe in-run re-fire)."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _retry_adapter(client)
        adapter._logged_in = True  # simulate successful authenticate()
        with pytest.raises(httpx.TransportError):
            await adapter.book(slot, _request())
    assert route.call_count == 1  # book is NOT retried


# --- book ----------------------------------------------------------------


@respx.mock
async def test_book_success_returns_booked_result() -> None:
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-42"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True  # simulate successful authenticate()
        result = await adapter.book(slot, _request())
    assert result.outcome == BookingOutcome.BOOKED
    # MF-1 (Option A): confirmation_code is stamped with the TTB: managed-booking
    # prefix. The raw ForeUP id "CONF-42" is stored as "TTB:CONF-42".
    assert result.confirmation_code == "TTB:CONF-42"
    assert result.course_id == CID


@respx.mock
async def test_book_extracts_teetime_id() -> None:
    """A flat ForeUP book response carrying only `teetime_id` (the real Mangrove Bay
    shape) is extracted into the confirmation_code. PR0 (BLIND_POST_PLAN): blind-POST
    cancel-extras needs the per-reservation id, which used to be dropped (the chain
    missed teetime_id/TTID, so conf was None on every real MB booking)."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"teetime_id": 123})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        result = await adapter.book(slot, _request())
    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code == "TTB:123"


@respx.mock
async def test_book_extracts_ttid() -> None:
    """A flat book response carrying only `TTID` is extracted into confirmation_code."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"TTID": "abc"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        result = await adapter.book(slot, _request())
    assert result.confirmation_code == "TTB:abc"


@respx.mock
async def test_book_prefers_pending_reservation_id_over_teetime_id() -> None:
    """No regression: when the established id fields AND the new teetime_id/TTID are
    all present, the established field (pending_reservation_id) still wins."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "reservation": {"pending_reservation_id": "PRI-1"},
                "teetime_id": "TT-2",
                "TTID": "TTID-3",
            },
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        result = await adapter.book(slot, _request())
    assert result.confirmation_code == "TTB:PRI-1"


@respx.mock
async def test_book_ttid_wins_over_teetime_id_when_both_present() -> None:
    """Ordering invariant (reviewer should-fix): when ONLY the two new flat fields
    are present and they DIFFER, `TTID` wins over `teetime_id` — matching
    `_parse_reservation`'s relative order. If the two book() lines were ever
    reordered, cancel-extras would target a different reservation than the parser
    resolves; this test fails loudly on that drift."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"TTID": "X", "teetime_id": "Y"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        result = await adapter.book(slot, _request())
    assert result.confirmation_code == "TTB:X"


@respx.mock
async def test_cancel_strips_ttb_from_teetime_id_conf() -> None:
    """End-to-end: a teetime_id-sourced confirmation_code (TTB:<teetime_id>) is
    accepted by cancel_reservation, which strips TTB: and DELETEs the raw id — the
    blind-POST cancel-extras round-trip (book → returned conf → cancel)."""
    respx.delete(f"{FOREUP_BASE_URL}{RESERVATION_PATH}/123").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.cancel_reservation("TTB:123")  # must not raise
    assert respx.calls.last.request.url.path == f"{RESERVATION_PATH}/123"


@respx.mock
async def test_book_slot_gone_raises() -> None:
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(return_value=httpx.Response(409))
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True  # simulate successful authenticate()
        with pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())


@respx.mock
async def test_book_400_raises_slot_gone_so_orchestrator_tries_next() -> None:
    """A 400 from the reservation POST means ForeUP definitively rejected the booking
    (no reservation created — the usual cause is the slot was claimed between search and
    book). It must surface as SlotGoneError so the orchestrator's candidate loop tries the
    next-ranked slot instead of crashing with an uncaught HTTPStatusError (the 2026-06-07
    prod failure: a 400 killed the job and the 5 backup slots were never attempted)."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(400, json={"msg": "That time is no longer available."})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())


def test_otp_challenge_error_is_operator_loud() -> None:
    """Contract pin: OtpChallengeError subclasses CaptchaError, so every existing
    operator-loud path fires for free — the booking run() does NOT catch it (clean
    non-zero exit, like a broken CAPTCHA pipeline) and the watcher notify+re-raises
    it. It must never be treatable as a benign try-next-slot signal."""
    assert issubclass(OtpChallengeError, CaptchaError)


@respx.mock
async def test_book_otp_challenge_raises_otp_error_not_slot_gone() -> None:
    """MB email-OTP (announced 2026-07-15): the challenge is UI-only today — the live
    recon confirmed the direct API book POST is unchallenged — but if ForeUP ever
    extends enforcement to the API, the rejection must surface as OtpChallengeError
    (operator action: wire in the OtpSource), NOT SlotGoneError. SlotGone cascades
    into try-next-slot → every candidate "gone" → a clean NO_INVENTORY terminal,
    silently reporting "no tee times" on every drop while the real problem is the
    OTP gate."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            400,
            json={
                "success": False,
                "msg": "Please enter the booking code sent to your email to complete your reservation.",
            },
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with pytest.raises(OtpChallengeError):
            await adapter.book(slot, _request())


@respx.mock
async def test_book_body_matching_captcha_and_otp_markers_classifies_captcha() -> None:
    """Ordering pin: _guard_captcha runs BEFORE _guard_otp_challenge in book(), so a
    body that somehow matches BOTH marker sets classifies as the plain CaptchaError
    (correct: the captcha wall must be cleared regardless of any OTP wording, and
    OtpChallengeError subclasses CaptchaError so operator handling is equivalent)."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            400,
            json={
                "success": False,
                "msg": "Captcha required before we can send your booking code.",
            },
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with pytest.raises(CaptchaError) as excinfo:
            await adapter.book(slot, _request())
        assert not isinstance(excinfo.value, OtpChallengeError)


@respx.mock
async def test_book_400_one_per_day_still_slot_gone_not_otp() -> None:
    """No false positive: the known burst-sibling rejection ("1 online reservation per
    day", observed live 2026-07-11) must stay SlotGoneError — only code-challenge
    wording trips the OTP guard, otherwise a routine rejection would crash the run."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            400,
            json={
                "success": False,
                "msg": "You are only allowed to have 1 online reservation per day.",
            },
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())


@respx.mock
async def test_book_429_raises_rate_limit_not_httpstatuserror() -> None:
    """A 429 on the booking POST means ForeUP throttled us BEFORE creating a reservation.
    It must surface as RateLimitError (parity with search()/cancel) so run()'s per-course
    loop skips the course cleanly, NOT a raw HTTPStatusError that crashes the sequential /
    blind-fallback book path (_book_from_candidates only catches SlotGoneError).
    full-repo-scan correctness #2."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(429, headers={"retry-after": "45"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with pytest.raises(RateLimitError) as exc_info:
            await adapter.book(slot, _request())
    assert exc_info.value.retry_after_s == 45.0


@respx.mock
async def test_book_logs_response_body_on_error(caplog: pytest.LogCaptureFixture) -> None:
    """On any non-2xx reservation POST, the response body must be logged (it used to be
    thrown away by raise_for_status, leaving us blind to WHY ForeUP rejected the booking —
    see the 2026-06-07 prod 400). The distinctive server message must appear in the logs."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(400, json={"msg": "unique-server-rejection-reason"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with caplog.at_level("WARNING"), pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())
    assert "unique-server-rejection-reason" in caplog.text


@respx.mock
async def test_book_logs_server_date_header_on_rejection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Early-arrival diagnostic (2026-07-18 miss): the booking job fires early_arrival_ms
    (500 ms) BEFORE T0 to offset network latency. If the POST ARRIVES before the 06:00 ET
    window opens, ForeUP rejects with 400 "Time not available." — indistinguishable from a
    genuine slot-race loss by body alone. Logging ForeUP's Date response header (its server
    clock) disambiguates: a 400 stamped 05:59:59 = pre-open rejection; 06:00:00 = race loss.
    The header value must reach the logs on the rejection path."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            400,
            json={"msg": "Time not available."},
            headers={"Date": "Sat, 25 Jul 2026 05:59:59 GMT"},
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with caplog.at_level("INFO"), pytest.raises(SlotGoneError):
            await adapter.book(slot, _request())
    assert "05:59:59" in caplog.text


@respx.mock
async def test_book_logs_server_date_header_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The server Date header is logged on the WINNING book POST too, so a booked drop's
    server clock can be compared against a rejected sibling's (see the early-arrival
    diagnostic above)."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={"id": "CONF-42"},
            headers={"Date": "Sat, 25 Jul 2026 06:00:00 GMT"},
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with caplog.at_level("INFO"):
            result = await adapter.book(slot, _request())
    assert result.outcome == BookingOutcome.BOOKED
    assert "06:00:00" in caplog.text


@respx.mock
async def test_book_includes_captchaid_when_provider_given() -> None:
    """book() must include captchaid in the POST body when captcha_provider is set."""
    captcha_token = "test-captcha-token-xyz"

    async def fake_provider() -> str:
        return captcha_token

    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-99"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = ForeUpAdapter(
            course_id=CID,
            course_pk=19671,
            booking_class_id=2149,
            schedule_id=2149,
            timezone="America/New_York",
            http_client=client,
            captcha_provider=fake_provider,
        )
        adapter._logged_in = True
        result = await adapter.book(slot, _request())
    # MF-1 (Option A): confirmation_code is stamped with the TTB: prefix.
    assert result.confirmation_code == "TTB:CONF-99"
    body = stdlib_json.loads(route.calls[0].request.content)
    assert body.get("captchaid") == captcha_token


@respx.mock
async def test_book_sends_false_for_player_list() -> None:
    """ForeUP booking does not require player details — player_list must be False.
    The website confirms the booking with just a player count, no individual data."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-42"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        await adapter.book(slot, _request())
    body = stdlib_json.loads(route.calls[0].request.content)
    assert body.get("player_list") is False


@respx.mock
async def test_book_omits_captchaid_without_provider() -> None:
    """book() must NOT send captchaid when no captcha_provider is given."""
    route = respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(200, json={"id": "CONF-42"})
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        await adapter.book(slot, _request())
    body = stdlib_json.loads(route.calls[0].request.content)
    assert "captchaid" not in body


@respx.mock
async def test_book_captcha_timeout_raises_captcha_error() -> None:
    """A TimeoutError from the inline CAPTCHA solve must surface as CaptchaError.

    Prod impact 2026-06-14: 2captcha was degraded; the inline solve in book() raised
    TimeoutError which propagated uncaught through _run_course → run() → job crash with
    a Python traceback. Fix: book() catches TimeoutError from _captcha_provider() and
    re-raises as CaptchaError so the job exits cleanly with a non-zero code.
    No HTTP booking POST should be made when the CAPTCHA solve fails.
    """

    async def _timing_out() -> str:
        raise TimeoutError("2captcha did not solve CAPTCHA within 180s")

    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = ForeUpAdapter(
            course_id=CID,
            course_pk=19671,
            booking_class_id=2149,
            schedule_id=2149,
            timezone="America/New_York",
            http_client=client,
            captcha_provider=_timing_out,
        )
        adapter._logged_in = True
        with pytest.raises(CaptchaError):
            await adapter.book(slot, _request())


async def test_prepare_book_captcha_timeout_raises_captcha_error() -> None:
    """A TimeoutError from the CAPTCHA provider in prepare_book() must surface as CaptchaError.

    Symmetry with book(): both paths translate TimeoutError so callers always see
    CaptchaError, never a raw TimeoutError, regardless of which path triggered the solve.
    """

    async def _timing_out() -> str:
        raise TimeoutError("2captcha did not solve CAPTCHA within 120s")

    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = ForeUpAdapter(
            course_id=CID,
            course_pk=19671,
            booking_class_id=2149,
            schedule_id=2149,
            timezone="America/New_York",
            http_client=client,
            captcha_provider=_timing_out,
        )
        with pytest.raises(CaptchaError):
            await adapter.prepare_book(slot=None, request=_request())


# --- list_reservations ---------------------------------------------------


async def test_list_reservations_returns_parsed_items() -> None:
    """list_reservations() returns items from _reservations_from_login (no HTTP call)."""
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._reservations_from_login = [_RAW_RESERVATION]
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1
    assert reservations[0].confirmation_code == "RES-123"
    assert reservations[0].party_size == 2


async def test_list_reservations_skips_unparseable_items(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Items that can't be parsed are skipped AND logged at WARNING — a silent drop
    here would hide a ForeUP schema change that empties the double-booking guard."""
    bad = {"id": "X"}  # no tee_time field
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._reservations_from_login = [bad, _RAW_RESERVATION]
        with caplog.at_level("WARNING"):
            reservations = await adapter.list_reservations()
    assert len(reservations) == 1  # bad item skipped, good item kept
    assert "unparseable reservation" in caplog.text
    # the logged context names the keys (for schema-drift diagnosis) but never values/PII
    assert "id" in caplog.text


async def test_list_reservations_unparseable_log_never_leaks_field_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The skip-log must carry the exception TYPE + keys, never a field VALUE. A parse error
    can embed a value in its message (ValueError(f"Cannot parse tee_time: {raw_t!r}")), so we
    log type(exc).__name__, not exc — otherwise PII/booking data leaks into the app log."""
    canary = "LEAK-CANARY-DO-NOT-LOG-9f3a"
    bad = {"tee_time": canary}  # unparseable → ValueError carrying the value in its message
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._reservations_from_login = [bad, _RAW_RESERVATION]
        with caplog.at_level("WARNING"):
            reservations = await adapter.list_reservations()
    assert len(reservations) == 1  # bad item still skipped
    assert "unparseable reservation" in caplog.text
    assert "ValueError" in caplog.text  # the exception TYPE is logged (diagnostic)
    assert "tee_time" in caplog.text  # the KEY is logged (schema-drift signal)
    assert canary not in caplog.text  # the VALUE is NOT logged (the leak guard)


async def test_list_reservations_returns_foreup_login_shape() -> None:
    """list_reservations() correctly parses the actual ForeUP login-response field names."""
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._reservations_from_login = [_RAW_FOREUP_LOGIN_RESERVATION]
        reservations = await adapter.list_reservations()
    assert len(reservations) == 1
    r = reservations[0]
    assert r.confirmation_code == "TTID_05271417087kr17"
    assert r.tee_time == datetime(2026, 6, 3, 14, 15, 0, tzinfo=ET)
    assert r.party_size == 4


@respx.mock
async def test_authenticate_populates_reservations_from_login_response() -> None:
    """Login response with reservations list → cached in _reservations_from_login."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "logged_in": True,
                "reservations": [_RAW_FOREUP_LOGIN_RESERVATION],
            },
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
    assert adapter._reservations_from_login == [_RAW_FOREUP_LOGIN_RESERVATION]


@respx.mock
async def test_authenticate_stores_jwt_field_name() -> None:
    """Login response with a 'jwt' field (actual ForeUP name) → stored as _auth_token."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"logged_in": True, "jwt": "real-jwt-xyz"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
    assert adapter._auth_token == "real-jwt-xyz"


# --- cancel_reservation --------------------------------------------------


CANCEL_RESERVATION_ID = "TTID_05271410334cux8"
CANCEL_URL = f"{FOREUP_BASE_URL}{RESERVATION_PATH}/{CANCEL_RESERVATION_ID}"


@respx.mock
async def test_authenticate_stores_jwt_from_login_response() -> None:
    """login response with a 'token' field → stored as _auth_token for cancel auth."""
    respx.get(f"{FOREUP_BASE_URL}/index.php/booking/19671/2149").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.post(f"{FOREUP_BASE_URL}{LOGIN_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "token": "fake-jwt-abc"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.authenticate(CREDS)
    assert adapter._auth_token == "fake-jwt-abc"


@respx.mock
async def test_cancel_reservation_success() -> None:
    """DELETE returning 200 {"success":true,...} returns normally (no exception)."""
    respx.delete(CANCEL_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "msg": "Reservation Cancelled"})
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._auth_token = "test-token"
        await adapter.cancel_reservation(CANCEL_RESERVATION_ID)  # must not raise


@respx.mock
async def test_cancel_reservation_404_is_idempotent() -> None:
    """DELETE returning 404 (already cancelled) returns normally — idempotent."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.cancel_reservation(CANCEL_RESERVATION_ID)  # must not raise


@respx.mock
async def test_cancel_reservation_non404_error_raises_cancel_error() -> None:
    """DELETE returning a non-404 error status raises CancelError."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(400, text="Bad Request"))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(CancelError):
            await adapter.cancel_reservation(CANCEL_RESERVATION_ID)


@respx.mock
async def test_cancel_reservation_400_cant_find_is_idempotent() -> None:
    """ForeUP signals "reservation doesn't exist" on cancel as a 400 with
    "We can't find that teetime...", NOT the 404 the idempotent-cancel contract keys
    on (observed live 2026-07-15 cancelling an already-expired UI hold). The
    already-cancelled post-condition is satisfied, so this must return normally —
    otherwise a double-cancel (watcher reconcile retry, upgrade race) raises
    CancelError for a booking that is already gone."""
    respx.delete(CANCEL_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "success": False,
                "msg": (
                    "We can't find that teetime, refresh the page and try again, "
                    "or contact the course."
                ),
            },
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        await adapter.cancel_reservation(CANCEL_RESERVATION_ID)  # must not raise


@respx.mock
async def test_cancel_reservation_400_other_msg_still_raises_cancel_error() -> None:
    """A 400 whose msg is NOT the can't-find signal keeps raising CancelError — the
    booking may still be live and the caller must know the cancel failed."""
    respx.delete(CANCEL_URL).mock(
        return_value=httpx.Response(
            400, json={"success": False, "msg": "Something else went wrong."}
        )
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        with pytest.raises(CancelError):
            await adapter.cancel_reservation(CANCEL_RESERVATION_ID)


@respx.mock
async def test_cancel_reservation_strips_ttb_prefix() -> None:
    """TTB:-prefixed confirmation_code is stripped; raw id is used in the DELETE path."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        # Pass TTB:-prefixed code as stored in BookingResult.confirmation_code
        await adapter.cancel_reservation(f"TTB:{CANCEL_RESERVATION_ID}")  # must not raise
    # Verify the DELETE was called with the raw id (no TTB: prefix)
    assert respx.calls.last.request.url.path == f"{RESERVATION_PATH}/{CANCEL_RESERVATION_ID}"


@respx.mock
async def test_cancel_reservation_sends_authorization_header() -> None:
    """When _auth_token is set, DELETE includes x-authorization: Bearer <token>."""
    respx.delete(CANCEL_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._auth_token = "my-jwt-token"
        await adapter.cancel_reservation(CANCEL_RESERVATION_ID)
    assert respx.calls.last.request.headers["x-authorization"] == "Bearer my-jwt-token"


# --- aclose --------------------------------------------------------------


async def test_aclose_releases_owned_client() -> None:
    adapter = ForeUpAdapter(
        course_id=CID,
        course_pk=19671,
        booking_class_id=2149,
        schedule_id=2149,
    )
    adapter._client = httpx.AsyncClient()
    await adapter.aclose()
    assert adapter._client is None


# --- parse helpers -------------------------------------------------------


def test_parse_slot_maps_fields_correctly() -> None:
    tz = ZoneInfo("America/New_York")
    slot = _parse_slot(_RAW_SLOT, TARGET_DATE, CID, tz)
    assert slot.slot_id == SlotId("99001")
    assert slot.tee_time == datetime(2026, 5, 13, 9, 30, 0, tzinfo=tz)
    assert slot.price_per_player == Decimal("45.00")
    assert slot.holes == 18
    assert not slot.cart_included


def test_parse_reservation_maps_fields_correctly() -> None:
    tz = ZoneInfo("America/New_York")
    res = _parse_reservation(_RAW_RESERVATION, CID, tz)
    assert res.confirmation_code == "RES-123"
    assert res.tee_time == datetime(2026, 5, 13, 8, 0, 0, tzinfo=tz)
    assert res.party_size == 2


def test_parse_reservation_foreup_login_shape() -> None:
    # _parse_reservation handles login-response field names: TTID, start_datetime, player_count
    tz = ZoneInfo("America/New_York")
    res = _parse_reservation(_RAW_FOREUP_LOGIN_RESERVATION, CID, tz)
    assert res.confirmation_code == "TTID_05271417087kr17"
    assert res.tee_time == datetime(2026, 6, 3, 14, 15, 0, tzinfo=tz)
    assert res.party_size == 4


@respx.mock
async def test_book_does_not_log_response_pii(caplog: pytest.LogCaptureFixture) -> None:
    """A successful book must NOT log the full ForeUP response body — it echoes the account
    holder's name/email/phone, and ACA ships stdout to Log Analytics. Only the confirmation
    id (already safe) should be logged. Security review High finding."""
    respx.post(f"{FOREUP_BASE_URL}{RESERVATION_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "reservation": {"id": "CONF-42", "email": "leak@example.test", "phone": "555-LEAK"}
            },
        )
    )
    slot = TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId("99001"),
        tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=ET),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=False,
        raw=dict(_RAW_SLOT),
    )
    async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
        adapter = _adapter(client)
        adapter._logged_in = True
        with caplog.at_level("INFO"):
            result = await adapter.book(slot, _request())
    assert result.confirmation_code == "TTB:CONF-42"
    assert "CONF-42" in caplog.text  # the confirmation id IS logged (safe)
    assert "leak@example.test" not in caplog.text  # PII must NOT be logged
    assert "555-LEAK" not in caplog.text
