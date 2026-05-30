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

import pytest

from teetime.core.models import CourseCredentials
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter

pytestmark = pytest.mark.integration

_HAVE_CREDS = bool(os.environ.get("MB_USERNAME") and os.environ.get("MB_PASSWORD"))


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
