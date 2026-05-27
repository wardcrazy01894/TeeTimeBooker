"""M1.T3 tests: CLI commands.

`teetime show-config` prints redacted config.
`teetime run --use-fake-adapter --dry-run true` exits 0 with a DRY_RUN line.
`teetime run --use-fake-adapter --dry-run false` exits 0 with a BOOKED line.
`teetime run` (no fake adapter) errors with a helpful message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from teetime.__main__ import cli

EXAMPLE_TOML = Path(__file__).resolve().parent.parent / "config" / "example.toml"


_REQUIRED_ENV = {
    "MB_USERNAME": "u",
    "MB_PASSWORD": "p",
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


@pytest.fixture(autouse=True)
def env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)


def test_show_config_redacts_secrets() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show-config", "--config", str(EXAMPLE_TOML)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # All four players must be present.
    assert len(payload["request"]["players"]) == 4
    p1 = payload["request"]["players"][0]
    assert p1["email"] == "***"
    assert p1["phone"] == "***"
    assert p1["member_number"] == "***"
    # Real env-var NAMES (not values) are still shown.
    assert p1["email_env"] == "PLAYER1_EMAIL"


def test_show_config_does_not_leak_resolved_values() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show-config", "--config", str(EXAMPLE_TOML)])
    assert "alex@example.test" not in result.output
    assert "555-0001" not in result.output


def test_run_dry_run_with_fake_adapter_succeeds() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter"],
    )
    assert result.exit_code == 0, result.output
    assert "dry_run" in result.output.lower()


def test_run_no_dry_run_with_fake_adapter_books() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "false", "--use-fake-adapter"],
    )
    assert result.exit_code == 0, result.output
    assert "booked" in result.output.lower()


def test_run_without_fake_adapter_fails_on_missing_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --use-fake-adapter the CLI uses the real adapter. Missing course
    credentials must produce a clear error, not an unhandled exception."""
    monkeypatch.delenv("MB_USERNAME", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true"],
    )
    assert result.exit_code != 0
    assert "MB_USERNAME" in result.output


def test_show_config_missing_env_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAYER1_EMAIL", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["show-config", "--config", str(EXAMPLE_TOML)])
    assert result.exit_code != 0
    assert "PLAYER1_EMAIL" in result.output
