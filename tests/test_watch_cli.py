"""M6 PR4: `teetime watch` honours `watcher.enabled`.

`enabled = true` runs the WatchOrchestrator (look-but-don't-book under `--dry-run true`);
`enabled = false` logs a warning and exits 0 without polling. The orchestrator-level
"look but don't book" guarantee (DRY_RUN outcome, `book_call_count == 0`) is covered by
`tests/test_watch_orchestrator.py::test_watch_dry_run_returns_dry_run_outcome_without_booking`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from teetime.__main__ import cli

_ENV = {
    "MB_USERNAME": "u",
    "MB_PASSWORD": "p",
    "PLAYER1_EMAIL": "a@e.test",
    "PLAYER1_PHONE": "555-0001",
    "PLAYER1_MB_MEMBER": "1",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


def _config(tmp_path: Path, *, enabled: bool) -> Path:
    toml = tmp_path / "w.toml"
    toml.write_text(
        f"""
[[courses]]
id = "foreup:mangrove_bay"
adapter = "foreup.mangrove_bay"
username_env = "MB_USERNAME"
password_env = "MB_PASSWORD"
extra = {{ booking_class_id = "2149" }}

[request]
target_offsets = [7]
holes = 18
course_preferences = ["foreup:mangrove_bay"]

[[request.players]]
first_name = "A"
last_name = "B"
email_env = "PLAYER1_EMAIL"
phone_env = "PLAYER1_PHONE"
member_number_env = "PLAYER1_MB_MEMBER"

[[request.time_windows]]
earliest = "08:45:00"
latest = "10:00:00"

[scheduler]
timezone = "America/New_York"
fire_time = "06:00:00"

[notifier]
backend = "console"

[watcher]
enabled = {str(enabled).lower()}
poll_interval_s = 600
polling_start_hour = 7
polling_end_hour = 22
"""
    )
    return toml


def test_watch_enabled_runs_orchestrator_under_dry_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _config(tmp_path, enabled=True)
    with caplog.at_level(logging.INFO):
        result = CliRunner().invoke(
            cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
        )
    assert result.exit_code == 0, result.output
    msgs = [r.message.lower() for r in caplog.records]
    # The orchestrator actually ran (not the disabled early-exit): a "Watch check" line
    # is emitted, and the "disabled" warning is NOT.
    assert any("watch check" in m for m in msgs)
    assert not any("disabled" in m for m in msgs)


def test_watch_disabled_warns_and_exits_clean(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _config(tmp_path, enabled=False)
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
        )
    assert result.exit_code == 0, result.output
    assert any("disabled" in r.message.lower() for r in caplog.records)
