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
PARAM_PROD = BICEP_DIR / "main.bicepparam.prod"


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


def test_main_never_creates_acr_always_pulls_shared(main: str) -> None:
    # Dedicated shared RG: NEITHER env creates an ACR — main.bicep has no registry module and
    # always deploys the cross-RG AcrPull grant (no owner conditional).
    assert "module registry 'modules/registry.bicep'" not in main
    assert "isAcrOwner" not in main
    assert "acrOwnerEnv" not in main
    assert "module sharedAcrPull 'modules/acr-pull-cross-rg.bicep' = {" in main


def test_shared_acrpull_is_cross_rg_scoped(main: str) -> None:
    # The AcrPull grant lands in the shared ACR's dedicated RG.
    assert "scope: resourceGroup(sharedAcrResourceGroup)" in main


def test_shared_acr_params_are_required(main: str) -> None:
    # Both env deploys MUST supply the shared ACR name + RG (no defaults — there is no owner).
    assert "param sharedAcrName string\n" in main
    assert "param sharedAcrResourceGroup string\n" in main


def test_compute_depends_on_the_acr_grant(main: str) -> None:
    assert "dependsOn: [sharedAcrPull]" in main


def test_acr_login_server_output_derives_from_shared_name(main: str) -> None:
    # No registry module to read an output from — the login server is derived from the name.
    assert "output acrLoginServer string = '${sharedAcrName}.azurecr.io'" in main


def test_param_files_point_at_dedicated_shared_rg() -> None:
    for text in (PARAM_DEV.read_text(), PARAM_PROD.read_text()):
        assert "param sharedAcrResourceGroup = 'rg-teetime-shared'" in text
        assert "param sharedAcrName =" in text
        assert "param acrSku" not in text  # acrSku removed (main.bicep no longer declares it)


def test_purge_isolates_prod_and_dev_repos() -> None:
    """C1 regression guard: prod images (`teetime`) and dev images (`teetime-dev`) live in
    SEPARATE repos so dev's high-frequency churn can never evict prod's pinned tag via the
    per-repo `--keep` purge. The shared ACR's purge task must prune BOTH repos."""
    text = (BICEP_DIR / "modules" / "registry.bicep").read_text()
    assert "--filter \\'teetime:.*\\'" in text
    assert "--filter \\'teetime-dev:.*\\'" in text


def test_dev_workflow_pushes_to_separate_repo() -> None:
    """C1 regression guard (workflow side): the dev CI build pushes to `teetime-dev`, the
    prod build to `teetime` — so they never share a repo in the shared ACR."""
    wf = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "azure-iac.yml"
    ).read_text()
    assert "teetime-dev:${{ github.sha }}" in wf  # dev pushes/deploys the dev repo
    # prod build/deploy still uses the bare `teetime` repo (unchanged).
    assert 'az acr build -r "${ACR_NAME}" -t "teetime:${{ github.sha }}"' in wf
