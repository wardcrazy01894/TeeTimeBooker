"""Live ForeUP API-drift canary (integration-only; skipped in CI and by default).

The original plan (§19.2) promised "vcrpy cassettes go red loud" when ForeUP changes
its API, but cassettes were never recorded (S1), and respx unit tests only assert what
the bot *sends* — they cannot detect that ForeUP changed its *response* shape. This test
closes that gap: it authenticates against live ForeUP and parses the login-cache
reservation list, so an upstream response-shape change fails loudly instead of silently
losing a 6 AM booking.

It does NOT book. Run manually before a weekend cron:

    set -a && source .env && set +a
    uv run pytest -m integration tests/test_foreup_canary.py -v

Skipped automatically when MB_USERNAME / MB_PASSWORD are absent, and excluded from CI
(which runs `-m "not integration"`).
"""

from __future__ import annotations

import os
from datetime import date, time, timedelta
from uuid import uuid4

import pytest

from teetime.core.models import (
    BookingRequest,
    CourseCredentials,
    Player,
    RequestId,
    TimeWindow,
)
from teetime.courses.foreup.mangrove_bay import (
    BLIND_POST_TEMPLATE,
    MANGROVE_BAY_COURSE_ID,
    MangroveBayAdapter,
)

pytestmark = pytest.mark.integration

_HAVE_CREDS = bool(os.environ.get("MB_USERNAME") and os.environ.get("MB_PASSWORD"))

# Fields in BLIND_POST_TEMPLATE that legitimately VARY per slot/date/price and so must
# NOT be value-compared against a live slot (they would false-alarm). `time`/`start_front`
# are the documented per-slot placeholders; `available_spots*` track live inventory;
# the fee/tax/special/trade fields move with pricing and promos (and book() overlays the
# real green_fee/total onto slot.raw anyway, so a stale template price never reaches the
# POST). What remains in the value check is the slot SHAPE + course/schedule identity.
_VOLATILE_TEMPLATE_KEYS = frozenset(
    {
        "time",
        "start_front",
        "available_spots",
        "available_spots_9",
        "available_spots_18",
        "has_special",
        "special_id",
        "special_discount_percentage",
        "special_was_price",
        "group_id",
        "booking_class_id",
        "foreup_discount",
        "green_fee",
        "green_fee_9",
        "green_fee_18",
        "guest_green_fee",
        "guest_green_fee_9",
        "guest_green_fee_18",
        "cart_fee",
        "cart_fee_9",
        "cart_fee_18",
        "guest_cart_fee",
        "guest_cart_fee_9",
        "guest_cart_fee_18",
        "cart_fee_18_hole",
        "cart_fee_9_hole",
        "green_fee_tax",
        "green_fee_tax_9",
        "green_fee_tax_18",
        "guest_green_fee_tax",
        "guest_green_fee_tax_9",
        "guest_green_fee_tax_18",
        "cart_fee_tax",
        "cart_fee_tax_9",
        "cart_fee_tax_18",
        "guest_cart_fee_tax",
        "guest_cart_fee_tax_9",
        "guest_cart_fee_tax_18",
        "trade_available_players",
        "teesheet_logo",
    }
)


@pytest.mark.skipif(not _HAVE_CREDS, reason="MB_USERNAME/MB_PASSWORD unset; live canary skipped")
async def test_foreup_authenticate_and_list_reservations_parses_live() -> None:
    """authenticate() + list_reservations() against live ForeUP must parse cleanly.

    A failure here means ForeUP changed its login/reservation response shape — fix the
    adapter before the next cron run rather than discovering it at 06:00:00.
    """
    adapter = MangroveBayAdapter(captcha_provider=None)
    creds = CourseCredentials(
        username=os.environ["MB_USERNAME"],
        password=os.environ["MB_PASSWORD"],
        extra={"booking_class_id": "2149"},
    )

    await adapter.authenticate(creds)
    reservations = await adapter.list_reservations()

    # The contract: a list of ExistingReservation (possibly empty) — parsed, not raised.
    assert isinstance(reservations, list)


@pytest.mark.skipif(not _HAVE_CREDS, reason="MB_USERNAME/MB_PASSWORD unset; live canary skipped")
async def test_blind_post_template_matches_live_searched_slot() -> None:
    """BLIND_POST_TEMPLATE must still match the SHAPE + identity of a live ForeUP slot.

    Blind-POST (BLIND_POST_PLAN.md) fires book POSTs synthesized from BLIND_POST_TEMPLATE
    WITHOUT a live search — only `time` + `start_front` are filled per slot. If ForeUP
    changes its slot/teesheet response shape (drops/renames a field, or changes a
    course/schedule identity value), the synthesized POST would silently drift from what
    ForeUP expects and fail at 06:00:00. This canary diffs the frozen template against a
    real searched slot so that drift fails loudly BEFORE a drop, not during one.

    Mornings inside the 7-day window sell out, so we search a WIDE window across the next
    several days to land on live (afternoon) inventory. If the course is genuinely empty,
    skip rather than false-fail.
    """
    adapter = MangroveBayAdapter(captcha_provider=None)
    creds = CourseCredentials(
        username=os.environ["MB_USERNAME"],
        password=os.environ["MB_PASSWORD"],
        extra={"booking_class_id": "2149"},
    )
    await adapter.authenticate(creds)

    today = date.today()
    request = BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=tuple(today + timedelta(days=offset) for offset in range(1, 7)),
        time_windows=(TimeWindow(earliest=time(6, 0), latest=time(19, 0)),),
        players=(Player(first_name="Canary", last_name="Drift", email="canary@x.test"),),
        course_preferences=(MANGROVE_BAY_COURSE_ID,),
        holes=18,
    )

    slots = await adapter.search(request)
    if not slots:
        pytest.skip("no live Mangrove Bay inventory in the next 6 days; cannot diff template")

    live_raw = slots[0].raw

    # 1. SHAPE drift: every key the blind template carries must still exist in the live
    #    slot. A missing key means ForeUP dropped/renamed a field our POST relies on.
    missing = sorted(set(BLIND_POST_TEMPLATE) - set(live_raw))
    assert not missing, (
        f"BLIND_POST_TEMPLATE keys absent from live slot (ForeUP shape drift): {missing}"
    )

    # 2. IDENTITY drift: the date/slot/price-independent fields must still match by value.
    #    (Volatile inventory/fee/special fields are excluded — see _VOLATILE_TEMPLATE_KEYS.)
    mismatched = {
        key: {"template": BLIND_POST_TEMPLATE[key], "live": live_raw[key]}
        for key in BLIND_POST_TEMPLATE
        if key not in _VOLATILE_TEMPLATE_KEYS and BLIND_POST_TEMPLATE[key] != live_raw[key]
    }
    assert not mismatched, f"BLIND_POST_TEMPLATE static fields drifted from live slot: {mismatched}"
