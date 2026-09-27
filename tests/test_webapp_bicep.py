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


def test_deploy_web_app_is_on_in_both_envs() -> None:
    """MU-17 turned the web app on in dev; MU-18 stage A turns it on in prod."""
    assert "param deployWebApp = true" in DEV_PARAMS.read_text()
    assert "param deployWebApp = true" in PROD_PARAMS.read_text()


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


# --- MU-16a: the web's tenant backend --------------------------------------------------------


def _between(text: str, start: str) -> str:
    i = text.index(start)
    return text[i : text.index("]", i)]


def test_webapp_gets_the_tenant_backend_env_when_cosmos_is_configured(webapp_bicep: str) -> None:
    """`teetime web` needs the durable store (Cosmos endpoint + database + the MI client id),
    the credential keyring (connect / refresh / cancel) and ACS (user email) — the names the
    wiring reads (tenant/wiring.py, tenant/crypto.py, tenant/acs_email.py)."""
    env = _between(webapp_bicep, "var webTenantEnv = [")
    for name in (
        "TENANT_COSMOS_ENDPOINT",
        "TENANT_COSMOS_DATABASE",
        "AZURE_CLIENT_ID",
        "TENANT_CREDS_KEYRING",
        "ACS_EMAIL_CONNECTION",
        "ACS_EMAIL_SENDER",
    ):
        assert f"'{name}'" in env, name
    # Secrets only by Key Vault reference, never a plain value.
    assert "{ name: 'TENANT_CREDS_KEYRING',  secretRef: 'tenant-creds-keyring' }" in env
    assert "{ name: 'ACS_EMAIL_CONNECTION',  secretRef: 'acs-email-connection' }" in env
    secrets = _between(webapp_bicep, "var webTenantSecrets = [")
    assert "secrets/TENANT-CREDS-KEYRING" in secrets
    assert "secrets/ACS-EMAIL-CONNECTION" in secrets


def test_webapp_tenant_backend_is_gated_on_the_cosmos_endpoint(webapp_bicep: str) -> None:
    # Without an endpoint the web runs on its in-memory store and must NOT reference the
    # tenant KV secrets (ACA validates KV refs at create time).
    assert "var tenantBackend = !empty(tenantCosmosEndpoint)" in webapp_bicep
    assert (
        "secrets: concat(webSecrets, operatorEmailSecrets, tenantBackend ? webTenantSecrets : [])"
        in webapp_bicep
    )
    assert "env: tenantBackend ? concat(webEnv, webTenantEnv) : webEnv" in webapp_bicep


def test_main_passes_the_tenant_backend_to_the_webapp(main_bicep: str) -> None:
    start = main_bicep.index("module webapp 'modules/webapp.bicep'")
    block = main_bicep[start : main_bicep.index("\n}\n", start)]
    assert "tenantCosmosEndpoint: tenantCosmosEndpoint" in block
    assert "acsEmailSender: effectiveAcsEmailSender" in block
    assert "userAssignedIdentityClientId: identity.outputs.clientId" in block


# --- MU-17: the operator email comes from Key Vault (the repo is public) ---------------------


def test_webapp_reads_the_operator_email_from_key_vault_when_the_param_is_empty(
    webapp_bicep: str,
) -> None:
    """The operator's address must not sit in a checked-in (public) param file: with
    ``operatorEmail`` empty the web reads ``OPERATOR-NOTIFY-EMAIL``, the secret the tenant jobs
    already use."""
    assert "var operatorEmailFromVault = empty(operatorEmail)" in webapp_bicep
    assert "secrets/OPERATOR-NOTIFY-EMAIL" in webapp_bicep
    assert "secretRef: 'operator-notify-email'" in webapp_bicep


def test_no_param_file_carries_an_email_address() -> None:
    for params in (DEV_PARAMS, PROD_PARAMS):
        text = params.read_text()
        assert "param operatorEmail = ''" in text, params.name
        assert "@gmail.com" not in text, params.name
