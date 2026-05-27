"""Feature 1 — Cancellation Monitor / Watch Orchestrator (M-feature-1).

Red-phase tests. Each test documents one behavioral contract of WatchOrchestrator.
All raise NotImplementedError until M-feature-1.T2 is implemented.

Coverage areas:
- Polls and books when a slot appears (happy path).
- Returns None (no booking) when no slot is available.
- Suppresses polling outside configured hours (polling_start_hour/end_hour gate).
- Stops and returns None when past the watch deadline (target_date has passed).
- Respects the advisory lock (does NOT attempt to book if main run holds lock).
- Does NOT re-book if a booking already exists in list_reservations (integrates
  with §9 layer 2 short-circuit).
- Does NOT re-book if store already has BOOKED terminal for the (request_id, date).
- Notifies on successful booking.
- Does NOT raise on transient failures (network blip) — returns None.
- Re-raises CaptchaError and AuthError after notification.
"""

from __future__ import annotations

import pytest

# These imports will work once the stubs are filled.
# The test file itself must not raise ImportError.
from teetime.core.models import WatchConfig

# --- Happy path ---------------------------------------------------------


async def test_watch_check_once_books_when_slot_available() -> None:
    """When a slot is available and no existing booking exists, check_once()
    returns a BOOKED BookingResult."""
    raise NotImplementedError(
        "RED: implement WatchOrchestrator.check_once, then remove this raise. "
        "See M-feature-1.T2."
    )


async def test_watch_check_once_returns_none_when_no_slots() -> None:
    """When search() returns empty, check_once() returns None (not an error)."""
    raise NotImplementedError("RED: implement M-feature-1.T2.")


# --- Polling-hours gate -------------------------------------------------


async def test_watch_suppressed_before_polling_start_hour() -> None:
    """check_once() returns None without calling search() when current time is
    before polling_start_hour. No HTTP calls should be made."""
    raise NotImplementedError("RED: implement M-feature-1.T2 polling-hours gate.")


async def test_watch_suppressed_after_polling_end_hour() -> None:
    """check_once() returns None without calling search() when current time is
    past polling_end_hour."""
    raise NotImplementedError("RED: implement M-feature-1.T2 polling-hours gate.")


# --- Deadline gate ------------------------------------------------------


async def test_watch_stops_when_past_target_date() -> None:
    """check_once() returns None when now > target_date (course-local midnight).
    The round has passed; no point polling."""
    raise NotImplementedError("RED: implement M-feature-1.T2 deadline gate.")


# --- Idempotency guards -------------------------------------------------


async def test_watch_does_not_rebook_when_store_has_booked_terminal() -> None:
    """If store.get_terminal() returns BOOKED, check_once() returns that result
    immediately without touching the adapter."""
    raise NotImplementedError("RED: implement M-feature-1.T2 idempotency guard.")


async def test_watch_does_not_rebook_when_list_reservations_has_match() -> None:
    """If list_reservations() returns a matching reservation for the target date,
    check_once() returns ALREADY_BOOKED without POSTing again (§9 layer 2)."""
    raise NotImplementedError("RED: implement M-feature-1.T2 §9 layer 2 guard.")


# --- Error handling -----------------------------------------------------


async def test_watch_returns_none_on_transient_network_error() -> None:
    """A transient httpx.RequestError in search() must NOT propagate — check_once()
    returns None so the ACA cron can retry on the next interval."""
    raise NotImplementedError("RED: implement M-feature-1.T2 error handling.")


async def test_watch_reraises_captcha_error_after_notify() -> None:
    """CaptchaError from the adapter must be re-raised (after notifying) so the
    calling CLI/workflow can disable the watch job."""
    raise NotImplementedError("RED: implement M-feature-1.T2 error handling.")


# --- WatchConfig validation ---------------------------------------------


def test_watch_config_rejects_poll_interval_below_floor() -> None:
    """WatchConfig must raise ValueError if poll_interval_s < 300."""
    with pytest.raises(ValueError, match="300"):
        WatchConfig(poll_interval_s=299)


def test_watch_config_accepts_floor_value() -> None:
    """WatchConfig must accept poll_interval_s == 300 (the floor itself)."""
    cfg = WatchConfig(poll_interval_s=300)
    assert cfg.poll_interval_s == 300
