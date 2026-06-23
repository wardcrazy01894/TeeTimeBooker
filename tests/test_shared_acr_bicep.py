"""Static assertions over the shared-ACR consolidation (full-repo-scan cost finding):
both envs pull from ONE shared ACR (prod-owned) instead of one ACR per env (~$5/mo saved).

Bicep is not pytest-importable; CI's `az bicep build` / `bicep lint` is the compile gate.
These pin the INTENT so a future edit can't silently re-introduce a per-env ACR or break the
cross-RG AcrPull grant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BICEP_DIR = Path(__file__).resolve().parent.parent / "infra" / "bicep"
MAIN = BICEP_DIR / "main.bicep"
CROSS_RG = BICEP_DIR / "modules" / "acr-pull-cross-rg.bicep"
PARAM_DEV = BICEP_DIR / "main.bicepparam.dev"


@pytest.fixture(scope="module")
def main() -> str:
    return MAIN.read_text()


@pytest.fixture(scope="module")
def cross_rg() -> str:
    return CROSS_RG.read_text()


def test_cross_rg_module_grants_acrpull_on_existing_acr(cross_rg: str) -> None:
    # References an EXISTING ACR (the shared one in the owner RG), not a new one.
    assert "Microsoft.ContainerRegistry/registries@2023-07-01' existing" in cross_rg
    # Grants the AcrPull built-in role (same GUID registry.bicep uses) to the job MI.
    assert "7f951dda-4ed3-4680-a7ca-43fe172d538d" in cross_rg
    assert "Microsoft.Authorization/roleAssignments" in cross_rg
    assert "param jobPrincipalId string" in cross_rg


def test_main_creates_acr_only_for_owner_env(main: str) -> None:
    # The registry (ACR-creating) module is gated on the owner; non-owner gets a cross-RG pull.
    assert "var isAcrOwner = envName == acrOwnerEnv" in main
    assert "module registry 'modules/registry.bicep' = if (isAcrOwner)" in main
    assert "module sharedAcrPull 'modules/acr-pull-cross-rg.bicep' = if (!isAcrOwner)" in main


def test_shared_acrpull_is_cross_rg_scoped(main: str) -> None:
    # The non-owner's AcrPull grant lands in the shared ACR's (owner's) RG.
    assert "scope: resourceGroup(sharedAcrResourceGroup)" in main


def test_owner_defaults_to_prod(main: str) -> None:
    assert "param acrOwnerEnv string = 'prod'" in main


def test_compute_depends_on_the_right_acr_grant(main: str) -> None:
    # Owner waits on its registry module; non-owner on the cross-RG pull grant.
    assert "dependsOn: isAcrOwner ? [registry] : [sharedAcrPull]" in main


def test_acr_login_server_output_is_safe_for_non_owner(main: str) -> None:
    # Safe-deref so the un-deployed registry module on the non-owner path resolves cleanly.
    assert "registry.?outputs.loginServer ?? '${sharedAcrName}.azurecr.io'" in main


def test_dev_param_file_points_at_shared_acr() -> None:
    text = PARAM_DEV.read_text()
    assert "param sharedAcrResourceGroup = 'rg-teetime-prod'" in text
    assert "param sharedAcrName =" in text
