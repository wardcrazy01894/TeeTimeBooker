"""M1.T3 tests: CLI commands.

`teetime show-config` prints redacted config.
`teetime run --use-fake-adapter --dry-run true` exits 0 with a DRY_RUN line.
`teetime run --use-fake-adapter --dry-run false` exits 0 with a BOOKED line.
`teetime run` (no fake adapter) errors with a helpful message.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import click
import pytest
from click.testing import CliRunner

import teetime.__main__ as main_mod
from teetime.__main__ import _resolve_creds, cli
from teetime.core.adapter import RateLimitError
from teetime.core.clock import FakeClock
from teetime.core.config import TimeWindowConfig
from teetime.core.config import load as _load
from teetime.core.models import BookingOutcome, BookingResult, RequestId

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
}


@pytest.fixture(autouse=True)
def env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def pin_run_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the `run` command's clock to a fixed Saturday.

    `teetime run` derives its target date as today + offset (offset 7, i.e. the
    SAME weekday) from the live clock. example.toml configures only Sat/Sun
    windows, so these CLI tests passed ONLY when the real wall-clock day was a
    Saturday or Sunday and were silently RED Mon-Fri (today+7 lands on a weekday
    with no time window, so the run errors with exit 1). Every prior merge landed
    on a weekend, masking it. Pinning RealClock to 2026-06-13 (a Saturday) makes
    the default target a Saturday deterministically, decoupling the suite from
    the day it happens to run. Tests that need a specific date pass it
    explicitly and are unaffected (they never build a clock)."""

    # noon UTC = 08:00 ET -- unambiguously still Saturday in America/New_York.
    saturday = _dt.datetime(2026, 6, 13, 12, 0, tzinfo=_dt.UTC)
    monkeypatch.setattr(main_mod, "RealClock", lambda **_kw: FakeClock(start=saturday))


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


def test_run_nonzero_exit_on_no_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """L6 (full-repo-scan): when no course books, the `run` command must exit NON-ZERO via a
    ClickException — the operator's signal a drop was missed. NO_INVENTORY is deliberately NOT
    in the success set {booked, dry_run, already_booked}. The orchestrator side (recording the
    NO_INVENTORY terminal) is well-tested; this pins the CLI's terminal→exit-code translation
    (__main__ run_cmd), which had no coverage. Stub the Orchestrator collaborator (not the SUT,
    which is the CLI command) to force a NO_INVENTORY result regardless of the fake adapter."""

    class _NoInventoryOrchestrator:
        def __init__(self, **_: object) -> None: ...

        async def run(self, request: object) -> BookingResult:
            return BookingResult(
                request_id=RequestId(uuid4()),
                outcome=BookingOutcome.NO_INVENTORY,
                course_id=None,
                slot=None,
                confirmation_code=None,
                booked_at=None,
                attempts=0,
            )

    monkeypatch.setattr(main_mod, "Orchestrator", _NoInventoryOrchestrator)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "false", "--use-fake-adapter"],
    )
    assert result.exit_code != 0, result.output
    assert "no_inventory" in result.output.lower()


def test_run_logs_exception_on_orchestrator_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Full-repo-scan observability finding: if `orch.run()` RAISES (CaptchaError/AuthError/
    an uncaught HTTPStatusError on the ForeUP book POST), the booking CLI must log the failure
    WITH a traceback (exc_info) before the process exits — otherwise the once-a-day 06:00
    failure cause never reaches Log Analytics. The exception must still propagate (non-zero
    exit); we log-and-re-raise, never swallow. Stub the Orchestrator collaborator to raise."""

    class _RaisingOrchestrator:
        def __init__(self, **_: object) -> None: ...

        async def run(self, request: object) -> BookingResult:
            raise RuntimeError("boom: captcha pipeline broken")

    monkeypatch.setattr(main_mod, "Orchestrator", _RaisingOrchestrator)
    runner = CliRunner()
    with caplog.at_level(logging.ERROR):
        result = runner.invoke(
            cli,
            ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "false", "--use-fake-adapter"],
        )
    assert result.exit_code != 0, result.output
    rec = next(
        (r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info is not None),
        None,
    )
    assert rec is not None, "expected an ERROR log with exc_info when the orchestrator raises"
    assert rec.exc_info is not None and "boom: captcha pipeline broken" in str(rec.exc_info[1])


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


# ---------------------------------------------------------------------------
# _resolve_creds(): *_env key resolution and collision detection
# ---------------------------------------------------------------------------


def test_resolve_creds_env_key_resolves_to_env_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """card_number_env = "SM_CARD_NUMBER" resolves to the env var value."""
    monkeypatch.setenv("SM_CARD_NUMBER", "4111111111111111")
    toml = tmp_path / "t.toml"
    toml.write_text(
        """
[[courses]]
id = "teeitup:sydney_marovitz"
adapter = "teeitup.sydney_marovitz"
username_env = "MB_USERNAME"
password_env = "MB_PASSWORD"
extra = { card_number_env = "SM_CARD_NUMBER" }

[request]
target_offsets = [7]
holes = 9
course_preferences = ["teeitup:sydney_marovitz"]

[[request.players]]
first_name = "A"
last_name = "B"
email_env = "PLAYER1_EMAIL"

[[request.time_windows]]
weekday  = "sunday"
earliest = "07:00:00"
latest   = "10:00:00"

[scheduler]
timezone = "America/Chicago"
fire_time = "06:00:00"
early_arrival_ms = 0
poll_interval_ms = 100
max_poll_seconds = 1

[notifier]
backend = "console"

[watcher]
enabled = false

[one_booking_policy]
enabled = false
"""
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["show-config", "--config", str(toml)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # card_number_env is an env-var NAME — shown as-is
    assert payload["courses"][0]["extra"]["card_number_env"] == "SM_CARD_NUMBER"
    # The resolved card value must NOT appear in show-config output
    assert "4111111111111111" not in result.output


def test_resolve_creds_collision_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Having both card_number (literal) and card_number_env in the same block errors."""
    monkeypatch.setenv("SM_CARD_NUMBER", "4111111111111111")
    toml = tmp_path / "t.toml"
    toml.write_text(
        """
[[courses]]
id = "teeitup:sydney_marovitz"
adapter = "teeitup.sydney_marovitz"
username_env = "MB_USERNAME"
password_env = "MB_PASSWORD"
[courses.extra]
card_number = "literal"
card_number_env = "SM_CARD_NUMBER"

[request]
target_offsets = [7]
holes = 9
course_preferences = ["teeitup:sydney_marovitz"]

[[request.players]]
first_name = "A"
last_name = "B"
email_env = "PLAYER1_EMAIL"

[[request.time_windows]]
weekday  = "sunday"
earliest = "07:00:00"
latest   = "10:00:00"

[scheduler]
timezone = "America/Chicago"
fire_time = "06:00:00"
early_arrival_ms = 0
poll_interval_ms = 100
max_poll_seconds = 1

[notifier]
backend = "console"

[watcher]
enabled = false

[one_booking_policy]
enabled = false
"""
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--config", str(toml), "--dry-run", "true"])
    assert result.exit_code != 0
    assert "card_number" in result.output
    assert "ambiguity" in result.output


def test_resolve_creds_literal_sensitive_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone literal sensitive key (e.g. card_number) — no _env counterpart — must be
    rejected: card/credential fields MUST use the *_env form so a raw PAN never sits in a
    config file. (Non-sensitive literals like booking_class_id stay allowed.)"""
    monkeypatch.setenv("MB_USERNAME", "u")
    monkeypatch.setenv("MB_PASSWORD", "p")
    monkeypatch.setenv("PLAYER1_EMAIL", "a@x.test")
    toml = tmp_path / "t.toml"
    toml.write_text(
        """
[[courses]]
id = "teeitup:sydney_marovitz"
adapter = "teeitup.sydney_marovitz"
username_env = "MB_USERNAME"
password_env = "MB_PASSWORD"
[courses.extra]
card_number = "4111111111111111"

[request]
target_offsets = [7]
holes = 9
course_preferences = ["teeitup:sydney_marovitz"]

[[request.players]]
first_name = "A"
last_name = "B"
email_env = "PLAYER1_EMAIL"

[[request.time_windows]]
weekday  = "sunday"
earliest = "07:00:00"
latest   = "10:00:00"

[scheduler]
timezone = "America/Chicago"
fire_time = "06:00:00"
early_arrival_ms = 0
poll_interval_ms = 100
max_poll_seconds = 1

[notifier]
backend = "console"

[watcher]
enabled = false

[one_booking_policy]
enabled = false
"""
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--config", str(toml), "--dry-run", "true"])
    assert result.exit_code != 0
    assert "card_number" in result.output
    assert "card_number_env" in result.output  # the message names the required env form
    # the raw PAN must never be echoed in the error
    assert "4111111111111111" not in result.output


def test_resolve_creds_allows_literal_billing_country(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """billing_country is NOT secret (2-letter code, defaults to "US") — a literal is
    allowed, no *_env required. Pins the SECRET_EXTRA_KEYS exclusion. Calls _resolve_creds
    directly (the guard's home) so no live booking flow is triggered."""
    monkeypatch.setenv("MB_USERNAME", "u")
    monkeypatch.setenv("MB_PASSWORD", "p")
    monkeypatch.setenv("PLAYER1_EMAIL", "a@x.test")
    monkeypatch.setenv("SM_CARD_NUMBER", "4111111111111111")
    toml = tmp_path / "t.toml"
    toml.write_text(
        """
[[courses]]
id = "teeitup:sydney_marovitz"
adapter = "teeitup.sydney_marovitz"
username_env = "MB_USERNAME"
password_env = "MB_PASSWORD"
[courses.extra]
card_number_env = "SM_CARD_NUMBER"
billing_country = "CA"

[request]
target_offsets = [7]
holes = 9
course_preferences = ["teeitup:sydney_marovitz"]

[[request.players]]
first_name = "A"
last_name = "B"
email_env = "PLAYER1_EMAIL"

[[request.time_windows]]
weekday  = "sunday"
earliest = "07:00:00"
latest   = "10:00:00"

[scheduler]
timezone = "America/Chicago"
fire_time = "06:00:00"
early_arrival_ms = 0
poll_interval_ms = 100
max_poll_seconds = 1

[notifier]
backend = "console"

[watcher]
enabled = false

[one_booking_policy]
enabled = false
"""
    )
    cfg = _load(toml)
    creds = _resolve_creds(cfg)  # must NOT raise on the literal billing_country
    (cred,) = creds.values()
    assert cred.extra["billing_country"] == "CA"
    assert cred.extra["card_number"] == "4111111111111111"  # *_env still resolved


# ---------------------------------------------------------------------------
# M6 PR1: `teetime run --wait/--no-wait` execution-mode selector,
#          NTP-offset re-gating (on `wait`, not `dry_run`), and the dev/test
#          `--fire-time` override. The SUT is the CLI wiring — collaborators
#          (Orchestrator.run, measure_ntp_offset) are mocked so no real
#          busy-wait or network call happens.
# ---------------------------------------------------------------------------


class _SpyOrchestrator:
    """Captures init kwargs and no-ops .run() so the CLI wiring is exercised
    without a real T0 busy-wait or network call."""

    last_kwargs: ClassVar[dict] = {}
    last_request: ClassVar[object] = None

    def __init__(self, **kwargs: object) -> None:
        _SpyOrchestrator.last_kwargs = dict(kwargs)

    async def run(self, request: object) -> object:
        _SpyOrchestrator.last_request = request
        return SimpleNamespace(outcome=SimpleNamespace(value="dry_run"))


@pytest.fixture
def spy_run(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace Orchestrator with a spy; spy on _local_demo_scheduler and
    measure_ntp_offset. TEETIME_WAIT is cleared so tests start from a known state."""
    monkeypatch.delenv("TEETIME_WAIT", raising=False)
    _SpyOrchestrator.last_kwargs = {}
    _SpyOrchestrator.last_request = None
    demo_calls: list = []
    ntp_calls: list = []

    orig_demo = main_mod._local_demo_scheduler

    def demo_spy(base: object) -> object:
        demo_calls.append(base)
        return orig_demo(base)

    def ntp_spy(*_a: object, **_k: object) -> _dt.timedelta:
        ntp_calls.append(1)
        return _dt.timedelta(0)

    monkeypatch.setattr(main_mod, "Orchestrator", _SpyOrchestrator)
    monkeypatch.setattr(main_mod, "_local_demo_scheduler", demo_spy)
    monkeypatch.setattr(main_mod, "measure_ntp_offset", ntp_spy)
    return SimpleNamespace(
        kwargs=lambda: _SpyOrchestrator.last_kwargs,
        demo_calls=demo_calls,
        ntp_calls=ntp_calls,
    )


def test_run_no_wait_uses_demo_scheduler(spy_run: SimpleNamespace) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--config",
            str(EXAMPLE_TOML),
            "--dry-run",
            "true",
            "--use-fake-adapter",
            "--no-wait",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(spy_run.demo_calls) == 1  # immediate/demo timing
    assert spy_run.kwargs()["scheduler"].early_arrival_ms == 0
    assert spy_run.ntp_calls == []  # no NTP probe off the wait path


def test_run_wait_uses_real_scheduler(spy_run: SimpleNamespace, gate_spy: SimpleNamespace) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter", "--wait"],
    )
    assert result.exit_code == 0, result.output
    assert spy_run.demo_calls == []  # real cfg.scheduler, NOT the demo
    sched = spy_run.kwargs()["scheduler"]
    assert sched.fire_time == _dt.time(6, 0, 0)
    assert sched.early_arrival_ms == 500  # verbatim from example.toml
    assert spy_run.ntp_calls == []  # fake adapter suppresses the NTP probe


def test_run_default_is_no_wait(spy_run: SimpleNamespace) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter"],
    )
    assert result.exit_code == 0, result.output
    assert len(spy_run.demo_calls) == 1  # neither flag, env unset -> no-wait


def test_run_env_wait_fallback_and_flag_override(
    spy_run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEETIME_WAIT", "1")
    runner = CliRunner()
    # No flag -> TEETIME_WAIT fallback -> real scheduler (wait).
    r1 = runner.invoke(
        cli, ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter"]
    )
    assert r1.exit_code == 0, r1.output
    assert spy_run.demo_calls == []  # env enabled wait
    # Explicit --no-wait overrides the env.
    r2 = runner.invoke(
        cli,
        [
            "run",
            "--config",
            str(EXAMPLE_TOML),
            "--dry-run",
            "true",
            "--use-fake-adapter",
            "--no-wait",
        ],
    )
    assert r2.exit_code == 0, r2.output
    assert len(spy_run.demo_calls) == 1  # flag overrode env


def test_fire_time_override_refused_when_live() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--config",
            str(EXAMPLE_TOML),
            "--dry-run",
            "false",
            "--use-fake-adapter",
            "--fire-time",
            "12:00:00",
        ],
    )
    assert result.exit_code != 0
    assert "dev/test" in result.output.lower()  # our guard, not click's "no such option"


def test_fire_time_override_sets_scheduler_fire_time(
    spy_run: SimpleNamespace, gate_spy: SimpleNamespace
) -> None:
    # gate_spy keeps the --wait DST gate proceeding regardless of wall-clock hour;
    # here we prove the --fire-time override reaches the scheduler the orchestrator runs.
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--config",
            str(EXAMPLE_TOML),
            "--dry-run",
            "true",
            "--use-fake-adapter",
            "--wait",
            "--fire-time",
            "12:00:00",
        ],
    )
    assert result.exit_code == 0, result.output
    assert spy_run.kwargs()["scheduler"].fire_time == _dt.time(12, 0, 0)


def test_wait_measures_ntp_on_real_adapter_even_in_dry_run(spy_run: SimpleNamespace) -> None:
    # Reviewer item 3: NTP gated on `wait and not use_fake_adapter`, NOT on dry_run,
    # so the dev `--wait --dry-run true` run probes UDP:123 before the first prod Sunday.
    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--wait"]
    )
    assert result.exit_code == 0, result.output
    assert spy_run.ntp_calls == [1]  # measured despite dry-run


def test_no_wait_real_adapter_does_not_measure_ntp(spy_run: SimpleNamespace) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--no-wait"]
    )
    assert result.exit_code == 0, result.output
    assert spy_run.ntp_calls == []


# ---------------------------------------------------------------------------
# M6 PR2: the DST-half gate is invoked ONLY on the --wait path. The --no-wait
# path (manual/local/on-demand) bypasses it, matching the old book.yml
# workflow_dispatch always-proceed semantics. (Predicate matrix: test_dst_gate.py.)
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_spy(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace the DST gate with a recorder. `.proceed` controls its return."""
    holder = SimpleNamespace(calls=[], proceed=True)

    def fake_gate(clock: object, *, timezone: str, fire_time: object) -> bool:
        holder.calls.append((timezone, fire_time))
        return holder.proceed

    monkeypatch.setattr(main_mod, "should_proceed", fake_gate)
    return holder


def test_gate_bypassed_on_no_wait_path(spy_run: SimpleNamespace, gate_spy: SimpleNamespace) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--config",
            str(EXAMPLE_TOML),
            "--dry-run",
            "true",
            "--use-fake-adapter",
            "--no-wait",
        ],
    )
    assert result.exit_code == 0, result.output
    assert gate_spy.calls == []  # gate never evaluated off the wait path


def test_gate_invoked_on_wait_path(spy_run: SimpleNamespace, gate_spy: SimpleNamespace) -> None:
    gate_spy.proceed = True
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter", "--wait"],
    )
    assert result.exit_code == 0, result.output
    assert len(gate_spy.calls) == 1  # gate evaluated on the wait path
    assert spy_run.kwargs()  # proceeded → orchestrator was constructed/run


def test_gate_skip_exits_zero_without_booking(
    spy_run: SimpleNamespace, gate_spy: SimpleNamespace
) -> None:
    gate_spy.proceed = False  # wrong-season cron
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter", "--wait"],
    )
    assert result.exit_code == 0, result.output  # a wrong-season cron is NOT an error
    assert spy_run.kwargs() == {}  # orchestrator never constructed → no booking attempt


# --- _build_request: wanted-days derived from per-day windows ----


def test_build_request_does_not_crash(env_set: None) -> None:
    # _build_request builds the RequestId/players/windows; its target_dates is a placeholder
    # (both callers override it). Just assert it builds a non-empty request without raising.
    cfg = _load(EXAMPLE_TOML)
    req = main_mod._build_request(cfg, dry_run=True)
    assert req.target_dates  # non-empty placeholder


def _cfg_distinct_windows() -> object:
    """A config with DIFFERENT windows on Sat vs Sun, to prove per-date scoping picks the
    right day's windows."""
    cfg = _load(EXAMPLE_TOML)
    cfg.request.time_windows = [
        TimeWindowConfig(weekday="saturday", earliest=_dt.time(8, 0), latest=_dt.time(9, 0)),
        TimeWindowConfig(weekday="sunday", earliest=_dt.time(17, 0), latest=_dt.time(19, 0)),
    ]
    return cfg


def test_build_booking_request_scopes_windows_to_date_weekday(env_set: None) -> None:
    cfg = _cfg_distinct_windows()
    # 2026-06-13 is a Saturday → the booking request must carry ONLY Saturday's window.
    req = main_mod._build_booking_request(cfg, dry_run=True, target_date=_dt.date(2026, 6, 13))
    assert len(req.time_windows) == 1
    assert req.time_windows[0].earliest == _dt.time(8, 0)  # Sat window, not Sun's 17:00


def test_scope_request_to_date_narrows_windows(env_set: None) -> None:
    cfg = _cfg_distinct_windows()
    base = main_mod._build_request(cfg, dry_run=True)
    # 2026-06-14 is a Sunday → only Sunday's window.
    scoped = main_mod._scope_request_to_date(base, cfg, _dt.date(2026, 6, 14))
    assert scoped.target_dates == (_dt.date(2026, 6, 14),)
    assert len(scoped.time_windows) == 1
    assert scoped.time_windows[0].earliest == _dt.time(17, 0)  # Sun window


def test_windows_for_date_errors_on_windowless_weekday(env_set: None) -> None:
    cfg = _cfg_distinct_windows()  # only Sat + Sun windows
    # 2026-06-15 is a Monday → no window → hard error (Q2).
    with pytest.raises(Exception, match="no time window configured"):
        main_mod._windows_for_date(cfg, _dt.date(2026, 6, 15))


# ---------------------------------------------------------------------------
# MULTIDAY PR2: the booking-day gate runs on the --wait path AFTER the DST gate
# and BEFORE the busy-wait. A non-wanted target weekday exits 0 without booking;
# a wanted target proceeds and the booking request is pinned to that SINGLE date.
# ---------------------------------------------------------------------------


@pytest.fixture
def booking_gate_spy(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace the booking-day gate with a recorder. `.book` controls its return."""
    holder = SimpleNamespace(calls=[], book=True)

    def fake_gate(
        clock: object,
        *,
        timezone: str,
        target_offset: int,
        wanted_weekdays: object,
        skip_dates: object = frozenset(),
        cutoff: object = None,
    ) -> bool:
        holder.calls.append((timezone, target_offset, wanted_weekdays))
        return holder.book

    monkeypatch.setattr(main_mod, "should_book_today", fake_gate)
    return holder


def test_run_wait_dst_first_then_booking_day_gate(
    spy_run: SimpleNamespace, gate_spy: SimpleNamespace, booking_gate_spy: SimpleNamespace
) -> None:
    # Wrong-season cron: the DST gate short-circuits BEFORE the booking-day gate is consulted.
    gate_spy.proceed = False
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter", "--wait"],
    )
    assert result.exit_code == 0, result.output
    assert gate_spy.calls  # DST gate evaluated
    assert booking_gate_spy.calls == []  # booking-day gate NOT reached (DST first)
    assert _SpyOrchestrator.last_kwargs == {}  # never booked


def test_run_wait_booking_day_skip_exits_zero_no_book(
    spy_run: SimpleNamespace, gate_spy: SimpleNamespace, booking_gate_spy: SimpleNamespace
) -> None:
    gate_spy.proceed = True  # correct season
    booking_gate_spy.book = False  # today+offset is not a wanted weekday
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter", "--wait"],
    )
    assert result.exit_code == 0, result.output  # not-a-booking-day is NOT an error
    assert len(booking_gate_spy.calls) == 1  # gate evaluated
    assert _SpyOrchestrator.last_kwargs == {}  # orchestrator never constructed/run
    assert _SpyOrchestrator.last_request is None  # never reached the booking request build


def test_run_wait_booking_day_proceed_books_single_date(
    spy_run: SimpleNamespace, gate_spy: SimpleNamespace, booking_gate_spy: SimpleNamespace
) -> None:
    gate_spy.proceed = True
    booking_gate_spy.book = True
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter", "--wait"],
    )
    assert result.exit_code == 0, result.output
    req = _SpyOrchestrator.last_request
    assert req is not None
    # Single-date target (reviewer must-fix 4 corollary: a multi-date booking request would
    # let another day's reservation vacuously pass the pre-book guard).
    assert len(req.target_dates) == 1


def test_run_no_wait_bypasses_booking_day_gate(
    spy_run: SimpleNamespace, gate_spy: SimpleNamespace, booking_gate_spy: SimpleNamespace
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--config",
            str(EXAMPLE_TOML),
            "--dry-run",
            "true",
            "--use-fake-adapter",
            "--no-wait",
        ],
    )
    assert result.exit_code == 0, result.output
    assert booking_gate_spy.calls == []  # bypassed off the wait path (always-proceed)
    assert gate_spy.calls == []  # DST gate also bypassed
    # --no-wait still books a single date (today + offset).
    assert _SpyOrchestrator.last_request is not None
    assert len(_SpyOrchestrator.last_request.target_dates) == 1


def test_booking_day_skip_log_emitted(
    spy_run: SimpleNamespace,
    gate_spy: SimpleNamespace,
    booking_gate_spy: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verification surface (MULTIDAY PR6): a non-booking-day --wait run emits a clear
    'booking-day gate: ... not a wanted booking day' INFO line so an operator can confirm
    the daily cron fast-exited on purpose (vs failing)."""

    gate_spy.proceed = True  # correct season
    booking_gate_spy.book = False  # today+offset is not a wanted weekday
    with caplog.at_level(logging.INFO):
        result = CliRunner().invoke(
            cli,
            [
                "run",
                "--config",
                str(EXAMPLE_TOML),
                "--dry-run",
                "true",
                "--use-fake-adapter",
                "--wait",
            ],
        )
    assert result.exit_code == 0, result.output
    assert any("not a wanted booking day" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Cost/etiquette: the DST + booking-day gates must short-circuit BEFORE the
# (real-adapter) site-key fetch / adapter / credential resolution. _resolve_site_keys
# makes a live ForeUP GET per course in prod; a wrong-season or non-booking-day cron
# fires DAILY and must not pay that cost (or touch ForeUP) on the ~5/7 idle mornings.
# The gates are pure functions of the clock, so they can gate the expensive work.
# ---------------------------------------------------------------------------


@pytest.fixture
def resolve_spy(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Count calls to the (potentially expensive) resolution helpers, delegating to
    the originals so the real-adapter path still builds correctly when it proceeds."""
    holder = SimpleNamespace(build_adapters=0, resolve_creds=0, site_keys=0)
    orig_build = main_mod._build_adapters
    orig_creds = main_mod._resolve_creds
    orig_site = main_mod._resolve_site_keys

    def build_spy(*a: object, **k: object) -> object:
        holder.build_adapters += 1
        return orig_build(*a, **k)

    def creds_spy(*a: object, **k: object) -> object:
        holder.resolve_creds += 1
        return orig_creds(*a, **k)

    async def site_spy(*a: object, **k: object) -> object:
        holder.site_keys += 1
        return await orig_site(*a, **k)

    monkeypatch.setattr(main_mod, "_build_adapters", build_spy)
    monkeypatch.setattr(main_mod, "_resolve_creds", creds_spy)
    monkeypatch.setattr(main_mod, "_resolve_site_keys", site_spy)
    return holder


def test_dst_skip_does_not_resolve_adapters(
    spy_run: SimpleNamespace, gate_spy: SimpleNamespace, resolve_spy: SimpleNamespace
) -> None:
    gate_spy.proceed = False  # wrong-season cron
    result = CliRunner().invoke(
        cli, ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--wait"]
    )
    assert result.exit_code == 0, result.output
    assert resolve_spy.build_adapters == 0  # gated out before adapter construction
    assert resolve_spy.resolve_creds == 0
    assert resolve_spy.site_keys == 0  # no live ForeUP GET on a wrong-season cron


def test_booking_day_skip_does_not_resolve_adapters(
    spy_run: SimpleNamespace,
    gate_spy: SimpleNamespace,
    booking_gate_spy: SimpleNamespace,
    resolve_spy: SimpleNamespace,
) -> None:
    gate_spy.proceed = True  # correct season
    booking_gate_spy.book = False  # today+offset is not a wanted weekday
    result = CliRunner().invoke(
        cli, ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--wait"]
    )
    assert result.exit_code == 0, result.output
    assert resolve_spy.build_adapters == 0  # non-booking-day cron skips resolution
    assert resolve_spy.resolve_creds == 0
    assert resolve_spy.site_keys == 0


def test_gate_proceed_resolves_adapters(
    spy_run: SimpleNamespace,
    gate_spy: SimpleNamespace,
    booking_gate_spy: SimpleNamespace,
    resolve_spy: SimpleNamespace,
) -> None:
    # Happy path: when both gates proceed, resolution still happens exactly once.
    gate_spy.proceed = True
    booking_gate_spy.book = True
    result = CliRunner().invoke(
        cli, ["run", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--wait"]
    )
    assert result.exit_code == 0, result.output
    assert resolve_spy.build_adapters == 1
    assert resolve_spy.resolve_creds == 1
    assert resolve_spy.site_keys == 0  # --dry-run true short-circuits the live ForeUP GET


# ---------------------------------------------------------------------------
# Live mode requires a 2captcha key. The Playwright inline solver was dropped
# (the prod image has no browser), so a live ForeUP build with no
# TWOCAPTCHA_API_KEY must fail fast with a clear error rather than silently
# selecting a solver that crashes at browser launch mid-booking.
# ---------------------------------------------------------------------------


def test_build_adapters_live_requires_2captcha_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWOCAPTCHA_API_KEY", raising=False)
    cfg = _load(EXAMPLE_TOML)  # first course is the Mangrove Bay ForeUP adapter
    with pytest.raises(click.ClickException, match="TWOCAPTCHA_API_KEY"):
        main_mod._build_adapters(cfg, dry_run=False, site_keys={})


def test_build_adapters_live_builds_with_2captcha_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWOCAPTCHA_API_KEY", "test-key")
    cfg = _load(EXAMPLE_TOML)
    adapters = main_mod._build_adapters(cfg, dry_run=False, site_keys={})
    assert adapters  # ForeUP adapter builds fine once the key is present


# ---------------------------------------------------------------------------
# A 429 (RateLimitError) during a watch run is a back-off, not a crash: the
# watch command must still exit 0 (the 10-min cron is the retry). Non-zero exit
# is reserved for Captcha/Auth (operator action). PLAN §12.
# ---------------------------------------------------------------------------


def test_watch_rate_limited_exits_zero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _RateLimitedWatch:
        def __init__(self, **_kw: object) -> None:
            pass

        async def check_once(self, *_a: object, **_k: object) -> None:
            raise RateLimitError("throttled by ForeUP", retry_after_s=45.0)

    # example.toml ships with the watcher off (it's the template); force it on so the
    # command reaches the check loop where the 429 is raised.
    real_load = main_mod.load

    def _load_watch_enabled(path: object) -> object:
        cfg = real_load(path)
        cfg.watcher.enabled = True
        return cfg

    monkeypatch.setattr(main_mod, "load", _load_watch_enabled)
    monkeypatch.setattr(main_mod, "WatchOrchestrator", _RateLimitedWatch)
    runner = CliRunner()
    with caplog.at_level(logging.WARNING, logger="teetime.__main__"):
        result = runner.invoke(
            cli,
            ["watch", "--config", str(EXAMPLE_TOML), "--dry-run", "true", "--use-fake-adapter"],
        )
    # 429 → clean exit 0 (the cron is the retry), NOT a non-zero crash.
    assert result.exit_code == 0, result.output
    assert result.exception is None  # not a propagated RateLimitError
    assert any("backing off" in r.message.lower() for r in caplog.records)
