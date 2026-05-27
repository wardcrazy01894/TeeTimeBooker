"""M1.T2 tests: TOML config loader + env-var secret resolution.

PLAN.md §4: secrets never live in TOML; the file references env-var NAMES and
the loader resolves them at config-load time. Missing envs fail loudly.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from teetime.core.config import AppConfig, MissingEnvVarError, load, redact

EXAMPLE_TOML = Path(__file__).resolve().parent.parent / "config" / "example.toml"


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
    "SMTP_HOST": "smtp.example.test",
    "SMTP_USER": "smtp-user",
    "SMTP_PASS": "smtp-secret",
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


def test_player_email_env_resolves_to_email_value(env_set: None) -> None:
    cfg = load(EXAMPLE_TOML)
    p1 = cfg.request.players[0]
    assert p1.email == "alex@example.test"
    assert p1.phone == "555-0001"
    assert p1.member_number == "12345"


def test_time_windows_parsed_as_time(env_set: None) -> None:
    """Single morning window: 09:00-10:30 ET. Afternoon window removed when
    the schedule shifted to weekend-only Saturday/Sunday bookings."""
    cfg = load(EXAMPLE_TOML)
    windows = cfg.request.time_windows
    assert len(windows) == 1
    assert windows[0].earliest == time(9, 0)
    assert windows[0].latest == time(10, 30)


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
