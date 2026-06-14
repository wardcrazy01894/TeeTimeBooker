"""M6 PR4: `teetime watch` honours `watcher.enabled`.

`enabled = true` runs the WatchOrchestrator (look-but-don't-book under `--dry-run true`);
`enabled = false` logs a warning and exits 0 without polling. The orchestrator-level
"look but don't book" guarantee (DRY_RUN outcome, `book_call_count == 0`) is covered by
`tests/test_watch_orchestrator.py::test_watch_dry_run_returns_dry_run_outcome_without_booking`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

import teetime.__main__ as main_mod
from teetime.__main__ import cli
from teetime.core.target_date import next_occurrences_within_horizon

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


def _config(tmp_path: Path, *, enabled: bool, skip_dates_env: bool = False) -> Path:
    skip_line = 'skip_dates_env = "TEETIME_SKIP_DATES"\n' if skip_dates_env else ""
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
{skip_line}
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
    # Default config has windows on Sat + Sun → both weekdays must be covered.
    # When today is itself Sat or Sun, next_occurrences_within_horizon also returns today+7
    # (the same weekday next week), so the spy may see 2 or 3 dates depending on the day
    # the test runs. What matters: both weekdays are represented.
    cfg = _config(tmp_path, enabled=True)
    result = CliRunner().invoke(
        cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
    )
    assert result.exit_code == 0, result.output
    assert len(watch_spy.seen) >= 2
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
    # Even when the first date returns a result, ALL remaining dates are STILL checked (no break).
    # Count is >= 2: Mon-Fri gives exactly 2 (next Sat + next Sun); Sat/Sun gives 3 because
    # next_occurrences_within_horizon also returns today+7.
    watch_spy.result = SimpleNamespace(outcome="dry_run", confirmation_code=None)
    cfg = _config(tmp_path, enabled=True)
    result = CliRunner().invoke(
        cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
    )
    assert result.exit_code == 0, result.output
    assert len(watch_spy.seen) >= 2  # every target date checked despite a result on the first


# --- LEADTIME_SKIP_PLAN PR4: skip dates honored by the watch CLI -------------


def test_watch_cli_drops_skipped_dates_before_poll(
    tmp_path: Path, watch_spy: type[_SpyWatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped upcoming wanted date is dropped BEFORE polling — check_once runs only for
    the unskipped ones.

    When today is itself Sat or Sun, the horizon function returns 3 dates (today, next
    other-weekday, today+7). We skip all but the last one and assert only that one is polled.
    """
    today = datetime.now(tz=ZoneInfo("America/New_York")).date()
    upcoming = sorted(next_occurrences_within_horizon(today, frozenset({5, 6}), 7))
    assert len(upcoming) >= 2, f"expected at least 2 upcoming wanted days, got {upcoming}"
    # Skip everything except the last date; that one alone should be polled.
    skip_dates = upcoming[:-1]
    keep = upcoming[-1]
    monkeypatch.setenv("TEETIME_SKIP_DATES", ",".join(str(d) for d in skip_dates))
    cfg = _config(tmp_path, enabled=True, skip_dates_env=True)
    result = CliRunner().invoke(
        cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
    )
    assert result.exit_code == 0, result.output
    assert watch_spy.seen == [keep]  # only the unskipped date was polled


def test_watch_cli_date_override_skipped_refuses(
    tmp_path: Path, watch_spy: type[_SpyWatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`watch --date <skipped>` is refused with a clear error (Edge E12) — never silently booked."""
    monkeypatch.setenv("TEETIME_SKIP_DATES", "2026-06-14")
    cfg = _config(tmp_path, enabled=True, skip_dates_env=True)
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
    assert result.exit_code != 0
    assert "TEETIME_SKIP_DATES" in result.output
    assert watch_spy.seen == []  # never polled


def test_watch_cli_date_override_unskipped_ok(
    tmp_path: Path, watch_spy: type[_SpyWatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`watch --date <unskipped wanted>` proceeds even when OTHER dates are skipped."""
    monkeypatch.setenv("TEETIME_SKIP_DATES", "2026-06-14")  # a different (Sunday) date
    cfg = _config(tmp_path, enabled=True, skip_dates_env=True)
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
            "2026-06-13",
        ],
    )
    assert result.exit_code == 0, result.output
    assert watch_spy.seen == [date(2026, 6, 13)]


# --- Regression: watcher must check the same weekday 7 days out (2026-06-14 incident) ---


def test_watch_targets_include_7day_out_on_same_weekday(
    tmp_path: Path, watch_spy: type[_SpyWatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for 2026-06-14 prod incident.

    Booking job failed for Sunday June 21. The watcher ran all day on Sunday June 14 but
    its targets were ['2026-06-20'] only — June 21 was never checked. Root cause:
    next_occurrences_within_horizon returned delta=0 for Sunday → today (June 14), and after
    _is_past_watch_deadline dropped June 14, next Sunday was silently unmonitored.

    The fix: when delta+7 <= horizon, also include today+7. This test pins "today" to June 14
    (Sunday) by patching next_occurrences_within_horizon in __main__ to use a fixed date,
    then asserts both June 20 (Sat) and June 21 (Sun, today+7) appear in check_once calls.
    """
    fixed_sunday = date(2026, 6, 14)
    real_fn = main_mod.next_occurrences_within_horizon

    def _pinned(today: date, wanted_weekdays: frozenset, horizon_days: int) -> tuple:
        return real_fn(fixed_sunday, wanted_weekdays, horizon_days)

    monkeypatch.setattr(main_mod, "next_occurrences_within_horizon", _pinned)
    cfg = _config(tmp_path, enabled=True)
    result = CliRunner().invoke(
        cli, ["watch", "--config", str(cfg), "--dry-run", "true", "--use-fake-adapter"]
    )
    assert result.exit_code == 0, result.output
    seen = set(watch_spy.seen)
    assert date(2026, 6, 20) in seen, "next Saturday must be in watch targets"
    assert date(2026, 6, 21) in seen, (
        "next Sunday (today+7) must be in watch targets — regression for 2026-06-14 "
        "prod incident where Sunday booking was never recovered by watcher"
    )
