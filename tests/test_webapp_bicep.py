"""MU-15a: `webapp.bicep` (the `teetime-web-<env>` Container App) + its gating in `main.bicep`.

Static text assertions (bicep is compile-validated by CI's `az bicep build`, not
pytest-importable — see the other `test_*_bicep.py` files).
"""

from __future__ import annotations

from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules"
MAIN_BICEP = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicep"
WEBAPP_BICEP = MODULES / "webapp.bicep"
DEV_PARAMS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.dev"
PROD_PARAMS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.prod"


@pytest.fixture(scope="module")
def main_bicep() -> str:
    return MAIN_BICEP.read_text()


@pytest.fixture(scope="module")
def webapp_bicep() -> str:
    return WEBAPP_BICEP.read_text()


def test_webapp_module_file_exists() -> None:
    assert WEBAPP_BICEP.exists()


def test_webapp_module_gated_on_deploy_web_app_param(main_bicep: str) -> None:
    assert "module webapp 'modules/webapp.bicep' = if (deployWebApp)" in main_bicep


def test_deploy_web_app_param_defaults_false(main_bicep: str) -> None:
    assert "param deployWebApp bool = false" in main_bicep


def test_deploy_web_app_defaults_false_in_both_param_files() -> None:
    assert "param deployWebApp = false" in DEV_PARAMS.read_text()
    assert "param deployWebApp = false" in PROD_PARAMS.read_text()


def test_webapp_is_container_app_not_a_job(webapp_bicep: str) -> None:
    assert "Microsoft.App/containerApps@" in webapp_bicep
    assert "Microsoft.App/jobs@" not in webapp_bicep


def test_webapp_scales_to_zero(webapp_bicep: str) -> None:
    assert "minReplicas: 0" in webapp_bicep


def test_webapp_ingress_disabled_when_killswitch_fired(webapp_bicep: str) -> None:
    # SF8 (MULTIUSER_PLAN §10.1): enableIngress=false (killswitch fired, or enableSchedules=false)
    # must disable ingress AND cap maxReplicas at 0, so a CI redeploy after the killswitch fires
    # cannot bring the site back up.
    assert "param enableIngress bool" in webapp_bicep
    assert "ingress: enableIngress ? {" in webapp_bicep
    assert "maxReplicas: enableIngress ? 1 : 0" in webapp_bicep


def test_webapp_wired_from_effective_enable_schedules(main_bicep: str) -> None:
    # The web app's ingress latch must be the SAME variable that silences the ACA Jobs' cron
    # schedules (killswitchFired safety latch), not a re-derived copy that could desync.
    webapp_block_start = main_bicep.index("module webapp 'modules/webapp.bicep'")
    webapp_block_end = main_bicep.index("\n}\n", webapp_block_start)
    webapp_block = main_bicep[webapp_block_start:webapp_block_end]
    assert "enableIngress: effectiveEnableSchedules" in webapp_block


def test_webapp_secrets_are_google_oauth_only(webapp_bicep: str) -> None:
    assert "OAUTH-GOOGLE-CLIENT-ID" in webapp_bicep
    assert "OAUTH-GOOGLE-CLIENT-SECRET" in webapp_bicep
    assert "WEB-SESSION-SECRET" in webapp_bicep
    # GitHub OAuth is deliberately not wired (operator decision: Google only).
    assert "GITHUB" not in webapp_bicep
