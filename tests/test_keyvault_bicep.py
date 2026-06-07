"""Static assertions for Key Vault audit logging in keyvault.bicep.

The `az bicep build` step is the compile gate; pytest is the contract gate. Pins the
diagnosticSettings wiring that ships Key Vault AuditEvent logs to Log Analytics (the
forensic record for a suspected credential leak — full-repo-scan security finding).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BICEP_DIR = Path(__file__).resolve().parent.parent / "infra" / "bicep"
KEYVAULT_BICEP = _BICEP_DIR / "modules" / "keyvault.bicep"
MAIN_BICEP = _BICEP_DIR / "main.bicep"


@pytest.fixture(scope="module")
def keyvault() -> str:
    return KEYVAULT_BICEP.read_text()


@pytest.fixture(scope="module")
def main() -> str:
    return MAIN_BICEP.read_text()


def test_keyvault_declares_diagnostic_settings(keyvault: str) -> None:
    """A Microsoft.Insights/diagnosticSettings resource scoped to the vault must exist."""
    assert "Microsoft.Insights/diagnosticSettings" in keyvault
    assert "scope: keyVault" in keyvault


def test_keyvault_audit_logs_enabled(keyvault: str) -> None:
    """AuditEvent (categoryGroup 'audit') must be ENABLED — assert the `enabled: true` flag
    co-occurs with the audit category block, so a stray `enabled: true` elsewhere can't mask
    the audit log being turned off."""
    assert re.search(r"categoryGroup:\s*'audit'\s+enabled:\s*true", keyvault), (
        "audit categoryGroup must be immediately followed by enabled: true"
    )


def test_keyvault_diagnostics_send_no_metrics(keyvault: str) -> None:
    """Cost guard: only audit LOGS are shipped — no metrics block (AllMetrics would add
    ingestion). A future accidental metrics add must trip this test."""
    assert "AllMetrics" not in keyvault
    assert "metrics:" not in keyvault


def test_keyvault_diagnostics_target_the_workspace_param(keyvault: str) -> None:
    """Logs must flow to the injected Log Analytics workspace, not a hardcoded id."""
    assert "param logAnalyticsWorkspaceId string" in keyvault
    assert "workspaceId: logAnalyticsWorkspaceId" in keyvault


def test_main_wires_logs_workspace_into_keyvault(main: str) -> None:
    """main.bicep must pass the logs workspace id into the keyvault module (this also
    creates the implicit dependency so logs deploys before keyvault)."""
    assert "logAnalyticsWorkspaceId: logs.outputs.workspaceId" in main
