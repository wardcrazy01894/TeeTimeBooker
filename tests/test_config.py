"""M1.T2 tests: TOML config loader + env-var secret resolution.

PLAN.md §4: secrets never live in TOML; the file references env-var NAMES and
the loader resolves them at config-load time. Missing envs fail loudly.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from teetime.core.config import (
    AppConfig,
    MissingEnvVarError,
    PlayerConfig,
    RequestConfig,
    TimeWindowConfig,
    load,
    redact,
)


def _rc(**overrides: object) -> RequestConfig:
    """Build a minimal valid RequestConfig, overriding any field (for validator tests)."""
    base: dict[str, object] = {
        "target_offsets": [7],
        "time_windows": [
            TimeWindowConfig(weekday="sunday", earliest=time(8, 45), latest=time(10, 0))
        ],
        "players": [PlayerConfig(first_name="A", last_name="B")],
        "course_preferences": ["foreup:mangrove_bay"],
    }
    base.update(overrides)
    return RequestConfig(**base)  # type: ignore[arg-type]


EXAMPLE_TOML = Path(__file__).resolve().parent.parent / "config" / "example.toml"
CONTAINER_TOML = Path(__file__).resolve().parent.parent / "config" / "container.toml"


# Minimal env set required by `config/example.toml`.
_REQUIRED_ENV = {
    "MB_USERNAME": "test_mb_user",
    "MB_PASSWORD": "test_mb_pass",
    "PLAYER1_EMAIL": "alex@example.test",
    "PLAYER1_PHONE": "555-0001",
    "PLAYER1_MB_MEMBER": "12345",
    "PLAYER2_EMAIL": "guest@example.test",
    "PLAYER3_EMAIL": "guest3@example.test",
    "PLAYER4_EMAIL": "guest4@example.test",
}


@pytest.fixture
def env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)


def test_loads_example_toml(env_set: None) -> None:
    cfg = load(EXAMPLE_TOML)
    assert isinstance(cfg, AppConfig)
    assert len(cfg.courses) == 1
    assert cfg.courses[0].id == "foreup:mangrove_bay"
    assert cfg.request.target_offsets == [7]
    assert cfg.request.holes == 18
    assert cfg.request.max_price_per_player == Decimal("55.00")
    assert len(cfg.request.players) == 4


def test_container_config_enables_watcher(env_set: None) -> None:
    """M6 PR4: the shipped container config (deployed image) enables the watcher so
    the dev/prod watch job actually polls. With --dry-run true it looks but never books;
    the orchestrator-level dry-run guard is covered in test_watch_orchestrator.py."""
    cfg = load(CONTAINER_TOML)
    assert cfg.watcher.enabled is True
    assert cfg.watcher.poll_interval_s == 600
    # to_watch_config() round-trips the knobs the WatchOrchestrator consumes. The
    # polling-hours gate was removed (MULTIDAY PR4) — the watcher polls every run.
    wc = cfg.watcher.to_watch_config()
    assert wc.poll_interval_s == 600
    assert not hasattr(wc, "polling_start_hour")


def test_container_config_enables_one_booking_policy(env_set: None) -> None:
    """Auto-upgrade: with one_booking_policy.enabled the watcher cancels the current
    booking and rebooks a higher-ranked (closer-to-midpoint) slot. Real effect only in
    prod (dryRun=false); dev's dry-run suppresses the cancel/book POSTs. Safe to enable
    now that the watch target is anchored to the booked Sunday (PR7, not a rolling
    today+7). Empty priority_slots → uses course_preferences + time_windows[0] ranking."""
    cfg = load(CONTAINER_TOML)
    assert cfg.one_booking_policy.enabled is True


def test_player_email_env_resolves_to_email_value(env_set: None) -> None:
    cfg = load(EXAMPLE_TOML)
    p1 = cfg.request.players[0]
    assert p1.email == "alex@example.test"
    assert p1.phone == "555-0001"
    assert p1.member_number == "12345"


def test_time_windows_parsed_as_time(env_set: None) -> None:
    """Per-day windows: one 08:45-10:00 morning window for Saturday and one for Sunday."""
    cfg = load(EXAMPLE_TOML)
    windows = cfg.request.time_windows
    assert len(windows) == 2
    assert {w.weekday for w in windows} == {"saturday", "sunday"}
    for w in windows:
        assert w.earliest == time(8, 45)
        assert w.latest == time(10, 0)


def test_missing_required_env_raises_missing_env_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One required env unset → loader fails with the variable name in the message."""
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PLAYER1_EMAIL", raising=False)
    with pytest.raises(MissingEnvVarError, match="PLAYER1_EMAIL"):
        load(EXAMPLE_TOML)


def test_missing_player3_email_raises_missing_env_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Players 3 and 4 have email_env set — their env vars are required at
    load time, same as Player 1. Verify the loader fails with the right var name."""
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PLAYER3_EMAIL", raising=False)
    with pytest.raises(MissingEnvVarError, match="PLAYER3_EMAIL"):
        load(EXAMPLE_TOML)


def test_optional_env_unset_leaves_field_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Player2 in example.toml has no phone_env / member_number_env at all —
    those fields should remain None (not raise)."""
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    cfg = load(EXAMPLE_TOML)
    p2 = cfg.request.players[1]
    assert p2.email == "guest@example.test"
    assert p2.phone is None
    assert p2.member_number is None


def test_redact_strips_secrets(env_set: None) -> None:
    """Helper used by `teetime show-config` MUST scrub resolved secrets back to
    masked sentinel before display. The TOML never had them; the resolved
    AppConfig does -- show-config must not expose them."""
    cfg = load(EXAMPLE_TOML)
    redacted = redact(cfg)
    p1_dump = redacted.request.players[0].model_dump()
    assert p1_dump["email"] == "***"
    assert p1_dump["phone"] == "***"
    assert p1_dump["member_number"] == "***"


def test_example_toml_windows_are_per_day(env_set: None) -> None:
    # Per-day windows (PERDAY_WINDOWS_PLAN): example.toml has one tagged window per wanted day.
    cfg = load(EXAMPLE_TOML)
    assert {w.weekday for w in cfg.request.time_windows} == {"saturday", "sunday"}
    assert cfg.request.wanted_weekday_indices == frozenset({5, 6})


def _win(weekday: str, e: time = time(8, 45), latest: time = time(10, 0)) -> TimeWindowConfig:
    return TimeWindowConfig(weekday=weekday, earliest=e, latest=latest)


def test_wanted_weekdays_derived_from_windows() -> None:
    rc = _rc(time_windows=[_win("saturday"), _win("sunday")])
    assert rc.wanted_weekday_indices == frozenset({5, 6})


def test_wanted_weekdays_dedupes_multiple_windows_same_day() -> None:
    rc = _rc(
        time_windows=[
            _win("sunday", time(9, 0), time(10, 0)),
            _win("sunday", time(17, 0), time(19, 0)),
        ]
    )
    assert rc.wanted_weekday_indices == frozenset({6})  # Sunday once


def test_windows_for_returns_that_days_windows_in_order() -> None:
    morning = _win("sunday", time(9, 0), time(10, 0))
    afternoon = _win("sunday", time(17, 0), time(19, 0))
    rc = _rc(time_windows=[afternoon, morning, _win("saturday")])
    sun = rc.windows_for(6)  # Sunday
    assert [w.earliest for w in sun] == [time(9, 0), time(17, 0)]  # normalised earliest-first
    assert len(rc.windows_for(5)) == 1  # Saturday
    assert rc.windows_for(0) == ()  # Monday — no windows


def test_window_requires_weekday() -> None:
    with pytest.raises(ValueError, match="invalid weekday"):
        _win("someday")


def test_window_earliest_after_latest_rejected() -> None:
    with pytest.raises(ValueError, match="after latest"):
        _win("sunday", time(10, 0), time(9, 0))


def test_empty_time_windows_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _rc(time_windows=[])


def test_removed_target_weekdays_key_rejected() -> None:
    # Hard cutover: the multi-day re-arch's target_weekdays is gone — fail loudly.
    with pytest.raises(ValueError, match="have been removed"):
        _rc(time_windows=[_win("sunday")], target_weekdays=["sunday"])


def test_removed_target_weekday_alias_rejected() -> None:
    with pytest.raises(ValueError, match="have been removed"):
        _rc(time_windows=[_win("sunday")], target_weekday="sunday")


def test_untagged_window_in_toml_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An un-migrated config (window without a weekday) must fail loudly, not silently."""
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    toml = tmp_path / "bad.toml"
    toml.write_text(
        """
[[courses]]
id = "foreup:mangrove_bay"
adapter = "foreup.mangrove_bay"
username_env = "MB_USERNAME"
password_env = "MB_PASSWORD"

[request]
target_offsets = [7]
course_preferences = ["foreup:mangrove_bay"]

[[request.players]]
first_name = "A"
last_name = "B"
email_env = "PLAYER1_EMAIL"

[[request.time_windows]]
earliest = "08:45:00"
latest = "10:00:00"

[scheduler]
timezone = "America/New_York"
fire_time = "06:00:00"
"""
    )
    with pytest.raises(Exception, match="weekday"):
        load(toml)
