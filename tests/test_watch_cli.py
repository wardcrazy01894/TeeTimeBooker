"""M6 PR4: `teetime watch` honours `watcher.enabled`.

`enabled = true` runs the WatchOrchestrator (look-but-don't-book under `--dry-run true`);
`enabled = false` logs a warning and exits 0 without polling. The orchestrator-level
"look but don't book" guarantee (DRY_RUN outcome, `book_call_count == 0`) is covered by
`tests/test_watch_orchestrator.py::test_watch_dry_run_returns_dry_run_outcome_without_booking`.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from click.testing import CliRunner

import teetime.__main__ as main_mod
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
weekday = "saturday"
earliest = "08:45:00"
latest = "10:00:00"

[[request.time_windows]]
weekday = "sunday"
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


# --- MULTIDAY PR4: multi-date watch loop -----------------------------------


class _SpyWatch:
    """Records the target_date of each check_once call; returns the scripted result."""

    seen: ClassVar[list] = []
    result: ClassVar[object] = None

    def __init__(self, **kwargs: object) -> None:
        pass

    async def check_once(self, request: object, target_date: date) -> object:
        _SpyWatch.seen.append(target_date)
        return _SpyWatch.result


@pytest.fixture
def watch_spy(monkeypatch: pytest.MonkeyPatch) -> type[_SpyWatch]:
    _SpyWatch.seen = []
    _SpyWatch.result = None
    monkeypatch.setattr(main_mod, "WatchOrchestrator", _SpyWatch)
    return _SpyWatch


def test_watch_checks_each_wanted_day(tmp_path: Path, watch_spy: type[_SpyWatch]) -> None:
    # Default target_weekdays = [saturday, sunday] → check_once for the next Sat AND Sun.
    cfg = _config(tmp_path, enabled=True)
    result = CliRunner().invoke(
        cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
    )
    assert result.exit_code == 0, result.output
    assert len(watch_spy.seen) == 2
    assert {d.weekday() for d in watch_spy.seen} == {5, 6}  # Sat + Sun


def test_watch_date_override_single(tmp_path: Path, watch_spy: type[_SpyWatch]) -> None:
    cfg = _config(tmp_path, enabled=True)
    result = CliRunner().invoke(
        cli,
        [
            "watch",
            "--config",
            str(cfg),
            "--dry-run",
            "true",
            "--use-fake-adapter",
            "--date",
            "2026-06-14",
        ],
    )
    assert result.exit_code == 0, result.output
    assert watch_spy.seen == [date(2026, 6, 14)]  # exactly one, the override


def test_watch_loop_continues_after_result(tmp_path: Path, watch_spy: type[_SpyWatch]) -> None:
    # Even when the first date returns a result, the second date is STILL checked (no break).
    watch_spy.result = SimpleNamespace(outcome="dry_run", confirmation_code=None)
    cfg = _config(tmp_path, enabled=True)
    result = CliRunner().invoke(
        cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
    )
    assert result.exit_code == 0, result.output
    assert len(watch_spy.seen) == 2  # both dates checked despite a result on the first
