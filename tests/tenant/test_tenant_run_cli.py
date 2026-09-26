"""The ``teetime tenant-run`` / ``teetime tenant-plan`` entrypoints (MULTIUSER_PLAN §4, §11.2;
MU-9b): logging order, the in-memory WARNING, the unknown-event refusal, the exit code, and
``tenant-plan`` making no ForeUP call."""

from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import re

import pytest
import respx
from click.testing import CliRunner

from teetime import __main__ as entry
from teetime.core.redaction import RedactingLogFilter
from teetime.tenant.crypto import KEYRING_ENV_VAR

GOOD_KEYRING = json.dumps(
    {"active": "k1", "keys": {"k1": base64.b64encode(os.urandom(32)).decode()}}
)


@pytest.mark.parametrize("command", ["tenant_run_cmd", "tenant_plan_cmd"])
def test_tenant_run_cli_installs_redaction_after_basicconfig(command: str) -> None:
    """Source-position pin (like tests/test_log_redaction.py): basicConfig CREATES the handler,
    so installing the redaction filter first would attach it to nothing."""
    callback = getattr(entry, command).callback
    assert callback is not None
    src = inspect.getsource(callback)
    assert src.index("logging.basicConfig(") < src.index("install_log_redaction()")
    assert len(re.findall(r"logging\.basicConfig\(", src)) == 1


def test_tenant_run_help_lists_the_flags() -> None:
    result = CliRunner().invoke(entry.cli, ["tenant-run", "--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--event", "--dry-run", "--wait / --no-wait"):
        assert flag in result.output


def test_tenant_run_refuses_an_unknown_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEYRING_ENV_VAR, GOOD_KEYRING)
    result = CliRunner().invoke(entry.cli, ["tenant-run", "--event", "nope", "--no-wait"])
    assert result.exit_code != 0
    assert "mb0600et" in result.output


def test_tenant_run_on_the_empty_in_memory_store_exits_zero_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(KEYRING_ENV_VAR, GOOD_KEYRING)
    caplog.set_level(logging.INFO)
    result = CliRunner().invoke(
        entry.cli, ["tenant-run", "--event", "mb0600et", "--dry-run", "true", "--no-wait"]
    )
    assert result.exit_code == 0, result.output
    assert "tenant store is IN-MEMORY" in caplog.text
    # The redaction filter is attached to the root handlers by the entrypoint.
    assert any(
        isinstance(f, RedactingLogFilter) for h in logging.getLogger().handlers for f in h.filters
    )


def test_tenant_run_without_a_keyring_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEYRING_ENV_VAR, raising=False)
    result = CliRunner().invoke(
        entry.cli, ["tenant-run", "--event", "mb0600et", "--dry-run", "true", "--no-wait"]
    )
    assert result.exit_code == 1


@respx.mock  # no routes: ANY HTTP request raises
def test_tenant_plan_cli_makes_no_foreup_call() -> None:
    result = CliRunner().invoke(entry.cli, ["tenant-plan", "--event", "mb0600et"])
    assert result.exit_code == 0, result.output
    assert "tenant-plan mb0600et: release 06:00 America/New_York" in result.output
    assert "0 pending row(s)" in result.output
    assert not respx.calls
