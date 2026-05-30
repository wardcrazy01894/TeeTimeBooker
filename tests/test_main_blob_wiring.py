"""Tests for BlobStateManager wiring in the CLI entrypoint (AZURE_PLAN.md §6.4).

When AZURE_STORAGE_ACCOUNT_NAME is present, the run/watch execution is wrapped in
a BlobStateManager bound to container 'teetime-state' and blob 'teetime.db', and
the yielded local path overrides the configured sqlite_path. When the env var is
absent (local dev / v0), the manager is NOT constructed and existing behavior is
unchanged.

The manager itself is fully unit-tested elsewhere; here we patch it and assert on
the wiring decision only.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from teetime import __main__ as main_mod


def test_state_path_uses_blob_manager_when_env_present(monkeypatch, tmp_path):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "teetimedevsa")

    fake_mgr = mock.MagicMock(name="BlobStateManager_instance")
    fake_mgr.__enter__.return_value = tmp_path / "downloaded.db"
    fake_mgr.__exit__.return_value = None
    ctor = mock.MagicMock(name="BlobStateManager", return_value=fake_mgr)
    monkeypatch.setattr(main_mod, "BlobStateManager", ctor)

    with main_mod._state_context("/some/configured/path.db") as path:
        assert path == tmp_path / "downloaded.db"

    ctor.assert_called_once()
    args = ctor.call_args.args
    # account, container, blob_name passed positionally per BlobStateManager signature.
    assert args[0] == "teetimedevsa"
    assert "teetime-state" in args
    assert "teetime.db" in args
    fake_mgr.__enter__.assert_called_once()
    fake_mgr.__exit__.assert_called_once()


def test_state_path_local_when_env_absent(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_NAME", raising=False)

    ctor = mock.MagicMock(name="BlobStateManager")
    monkeypatch.setattr(main_mod, "BlobStateManager", ctor)

    with main_mod._state_context("/configured/local.db") as path:
        assert path == Path("/configured/local.db")

    ctor.assert_not_called()
