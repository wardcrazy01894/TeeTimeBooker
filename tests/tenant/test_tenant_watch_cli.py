"""The ``teetime tenant-watch`` entrypoint (MULTIUSER_PLAN §7.1, §7.9, §9.4; MU-10b): logging
order, fail-closed keyring, E7 registration of the keyring material, the in-memory WARNING, and
the §7.9 exit status."""

from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import re
from typing import Any

import pytest
from click.testing import CliRunner

from teetime import __main__ as entry
from teetime.core.redaction import redact_text
from teetime.tenant.crypto import KEYRING_ENV_VAR
from teetime.tenant.runner import WatchReport

KEY_B64 = base64.b64encode(os.urandom(32)).decode()
GOOD_KEYRING = json.dumps({"active": "k1", "keys": {"k1": KEY_B64}})


def test_tenant_watch_cli_installs_redaction_after_basicconfig() -> None:
    """Source-position pin (like tests/test_log_redaction.py): basicConfig CREATES the handler,
    so installing the redaction filter first would attach it to nothing."""
    callback = entry.tenant_watch_cmd.callback
    assert callback is not None
    src = inspect.getsource(callback)
    assert src.index("logging.basicConfig(") < src.index("install_log_redaction()")
    assert len(re.findall(r"logging\.basicConfig\(", src)) == 1


def test_tenant_watch_help_exits_zero() -> None:
    result = CliRunner().invoke(entry.cli, ["tenant-watch", "--help"])
    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output


def test_tenant_watch_fails_closed_without_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEYRING_ENV_VAR, raising=False)
    result = CliRunner().invoke(entry.cli, ["tenant-watch", "--dry-run", "true"])
    assert result.exit_code != 0
    assert KEYRING_ENV_VAR in result.output


def test_tenant_watch_runs_on_the_in_memory_store_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(KEYRING_ENV_VAR, GOOD_KEYRING)
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(entry.cli, ["tenant-watch", "--dry-run", "true"])
    assert result.exit_code == 0, result.output
    assert "IN-MEMORY" in caplog.text
    # E7: the keyring's key material is masked from now on.
    assert KEY_B64 not in redact_text(f"key={KEY_B64}")


def _report(**overrides: Any) -> WatchReport:
    fields: dict[str, Any] = {
        "rows_loaded": 0,
        "searches": 0,
        "logins": 0,
        "booked": (),
        "upgraded": (),
        "lost": (),
        "rate_limited": False,
        "systemic_error": None,
    }
    return WatchReport(**(fields | overrides))


@pytest.mark.parametrize(
    ("report", "exit_code"),
    [
        (_report(rate_limited=True), 0),
        (_report(captcha_error=True), 1),
        (_report(systemic_error="load_watch_rows: ConnectionError"), 1),
    ],
)
def test_tenant_watch_exit_code_follows_the_watch_exit_status(
    monkeypatch: pytest.MonkeyPatch, report: WatchReport, exit_code: int
) -> None:
    async def fake_run(**kwargs: Any) -> WatchReport:
        assert kwargs["dry_run"] is True
        return report

    monkeypatch.setenv(KEYRING_ENV_VAR, GOOD_KEYRING)
    monkeypatch.setattr(entry, "run_tenant_watch", fake_run)
    result = CliRunner().invoke(entry.cli, ["tenant-watch", "--dry-run", "true"])
    assert result.exit_code == exit_code, result.output
