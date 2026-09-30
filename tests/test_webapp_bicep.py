"""MU-15a: `webapp.bicep` (the `teetime-web-<env>` Container App) + its gating in `main.bicep`.

Static text assertions (bicep is compile-validated by CI's `az bicep build`, not
pytest-importable — see the other `test_*_bicep.py` files).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from teetime.web.app import WEB_ENV_VARS

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


def test_webapp_min_replicas_is_a_param_that_defaults_to_scale_to_zero(webapp_bicep: str) -> None:
    assert "param minReplicas int = 0" in webapp_bicep


def test_webapp_min_replicas_is_forced_to_zero_by_the_killswitch(webapp_bicep: str) -> None:
    """SF8: an always-warm replica must still be impossible once the killswitch fires."""
    assert "minReplicas: enableIngress ? minReplicas : 0" in webapp_bicep


def test_main_passes_web_min_replicas_bounded_to_one(main_bicep: str) -> None:
    assert "@minValue(0)\n@maxValue(1)\nparam webMinReplicas int = 0" in main_bicep
    webapp_block_start = main_bicep.index("module webapp 'modules/webapp.bicep'")
    webapp_block = main_bicep[webapp_block_start : main_bicep.index("\n}\n", webapp_block_start)]
    assert "minReplicas: webMinReplicas" in webapp_block


def test_prod_web_is_always_warm_and_dev_scales_to_zero() -> None:
    """Operator decision 2026-09-28: prod keeps one replica (~$6/mo idle) so users never wait
    out a ~30 s cold start; dev stays scale-to-zero."""
    assert "param webMinReplicas = 1" in PROD_PARAMS.read_text()
    assert "param webMinReplicas = 0" in DEV_PARAMS.read_text()


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
    # GitHub OAuth is deliberately not wired (operator decision: Google only). The GitHub ISSUES
    # token (site reports, 2026-09-29) is a different thing and is allowed.
    assert "OAUTH-GITHUB" not in webapp_bicep
    assert "OAUTH_GITHUB" not in webapp_bicep


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
        "secrets: concat(webSecrets, operatorEmailSecrets, githubIssuesSecrets, "
        "tenantBackend ? webTenantSecrets : [])" in webapp_bicep
    )
    assert (
        "env: concat(tenantBackend ? concat(webEnv, webTenantEnv) : webEnv, githubIssuesEnv)"
        in webapp_bicep
    )


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


# --- custom domain (prod: spicyteetimebooker.com, 2026-09-28) -----------------------------------


def test_webapp_custom_domain_param_defaults_to_none(webapp_bicep: str) -> None:
    assert "param customDomain string = ''" in webapp_bicep


def test_webapp_binds_apex_and_www_with_managed_certificates(webapp_bicep: str) -> None:
    """Both hostnames bind SNI with a managed certificate whose NAME is derived from the host,
    so the one-time bootstrap (AZURE_PLAN runbook) and every later deploy agree on it."""
    assert (
        "var customHostnames = empty(customDomain) ? [] : [customDomain, 'www.${customDomain}']"
        in (webapp_bicep)
    )
    assert "var customDomainBindings = [for host in customHostnames: {" in webapp_bicep
    assert "customDomains: customDomainBindings" in webapp_bicep
    assert "bindingType: 'SniEnabled'" in webapp_bicep
    assert (
        "certificateId: '${acaEnvironmentId}/managedCertificates/mc-${replace(host, '.', '-')}'"
        in webapp_bicep
    )


def test_webapp_redirects_to_the_canonical_host_only_with_a_custom_domain(
    webapp_bicep: str,
) -> None:
    assert (
        "{ name: 'TEETIME_CANONICAL_HOST_REDIRECT', value: empty(customDomain) ? 'false' : 'true' }"
        in webapp_bicep
    )


def test_main_passes_the_custom_domain(main_bicep: str) -> None:
    assert "param webCustomDomain string = ''" in main_bicep
    start = main_bicep.index("module webapp 'modules/webapp.bicep'")
    block = main_bicep[start : main_bicep.index("\n}\n", start)]
    assert "customDomain: webCustomDomain" in block


def test_prod_uses_its_custom_domain_and_dev_does_not() -> None:
    prod, dev = PROD_PARAMS.read_text(), DEV_PARAMS.read_text()
    assert "param webCustomDomain = 'spicyteetimebooker.com'" in prod
    assert "param webPublicBaseUrl = 'https://spicyteetimebooker.com'" in prod
    assert "param webCustomDomain = ''" in dev
    assert "param webPublicBaseUrl = 'https://teetime-web-dev." in dev


def test_web_app_knows_its_build_for_bug_reports(webapp_bicep: str) -> None:
    """The image tag (the git sha CI builds) reaches the app as TEETIME_BUILD (2026-09-29)."""
    assert "{ name: 'TEETIME_BUILD'" in webapp_bicep
    # The tag of the LAST path segment only (a registry host:port has a ':' too), or 'untagged'.
    assert "var imageName = last(split(containerImage, '/'))" in webapp_bicep
    assert "contains(imageName, ':') ? last(split(imageName, ':')) : 'untagged'" in webapp_bicep


def test_github_issues_token_is_wired_only_where_a_repo_is_set(webapp_bicep: str) -> None:
    """Site reports -> public GitHub issues (2026-09-29): the GITHUB-ISSUES-TOKEN secret exists
    only in prod's vault, so the secretRef must be gated on githubIssuesRepo."""
    assert "param githubIssuesRepo string = ''" in webapp_bicep
    assert "secrets/GITHUB-ISSUES-TOKEN'" in webapp_bicep
    assert "var githubIssues = !empty(githubIssuesRepo)" in webapp_bicep
    assert "{ name: 'GITHUB_ISSUES_TOKEN', secretRef: 'github-issues-token' }" in webapp_bicep
    assert "{ name: 'GITHUB_ISSUES_REPO', value: githubIssuesRepo }" in webapp_bicep


def test_prod_files_issues_in_the_public_repo_dev_does_not() -> None:
    assert "githubIssuesRepo: githubIssuesRepo" in MAIN_BICEP.read_text()
    params = MAIN_BICEP.parent
    assert (
        "param githubIssuesRepo = 'wardcrazy01894/TeeTimeBooker'"
        in (params / "main.bicepparam.prod").read_text()
    )
    assert "param githubIssuesRepo = ''" in (params / "main.bicepparam.dev").read_text()


# The web reads these; they are deliberately NOT deployed (GitHub sign-in is not offered).
_WEB_ENV_NOT_DEPLOYED = {"OAUTH_GITHUB_CLIENT_ID", "OAUTH_GITHUB_CLIENT_SECRET"}


def test_every_web_env_var_is_wired_in_webapp_bicep(webapp_bicep: str) -> None:
    """Scan 2026-09-30: most web env vars are optional, so a rename in either place degrades
    quietly (no build in bug reports, no canonical redirect, no issues filed). Every name the web
    reads is set in webapp.bicep, except the allowlisted undeployed ones."""
    wired = set(re.findall(r"name: '([A-Z0-9_]+)'", webapp_bicep))
    missing = sorted(set(WEB_ENV_VARS) - _WEB_ENV_NOT_DEPLOYED - wired)
    assert not missing, f"read by the web but not set in webapp.bicep: {missing}"
    assert not (_WEB_ENV_NOT_DEPLOYED & wired), "an allowlisted var is deployed: drop it here"
