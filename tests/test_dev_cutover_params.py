"""MU-17 (MULTIUSER_PLAN §11/§12): the dev cutover is a params-only change to
``main.bicepparam.dev`` — tenant booking + watch, the web app and ACS on, the shared Cosmos
endpoint — while dev stays dry-run and prod stays exactly as it is.

Static text assertions, like the other ``test_*_bicep.py`` files.
"""

from __future__ import annotations

from pathlib import Path

BICEP = Path(__file__).resolve().parent.parent / "infra" / "bicep"
DEV = (BICEP / "main.bicepparam.dev").read_text()
PROD = (BICEP / "main.bicepparam.prod").read_text()
MAIN = (BICEP / "main.bicep").read_text()

COSMOS = "https://cosmos-teetime-shared.documents.azure.com:443/"
DEV_WEB = "https://teetime-web-dev.kindwave-5d7c992b.eastus2.azurecontainerapps.io"


def test_dev_runs_the_tenant_booker_and_watcher() -> None:
    assert "param bookingMode = 'tenant'" in DEV
    assert "param watchMode = 'tenant'" in DEV
    assert f"param tenantCosmosEndpoint = '{COSMOS}'" in DEV


def test_dev_turns_on_the_web_app_and_acs_email() -> None:
    assert "param deployWebApp = true" in DEV
    assert "param deployAcsEmail = true" in DEV
    assert f"param webPublicBaseUrl = '{DEV_WEB}'" in DEV


def test_dev_stays_dry_run() -> None:
    assert "param dryRun = true" in DEV


def test_prod_jobs_stay_on_the_toml_path_until_stage_b() -> None:
    """MU-18 stage A turns on prod's web app (tests/test_prod_cutover_params.py); its booking and
    watch jobs stay on the TOML path until stage B."""
    for line in ("param bookingMode = 'toml'", "param watchMode = 'toml'", "param dryRun = false"):
        assert line in PROD, line


def test_acs_sender_is_derived_from_the_email_module_when_not_set() -> None:
    """The Azure-managed sender domain is generated at deploy time, so an explicit
    ``acsEmailSender`` would need a second PR. main.bicep derives ``DoNotReply@<domain>`` from
    the email module's output when the param is empty, and hands the SAME value to the jobs and
    the web (which also orders them after the module that writes ACS-EMAIL-CONNECTION)."""
    assert "var effectiveAcsEmailSender = " in MAIN
    assert (
        "mailFromSenderDomain" in MAIN.split("var effectiveAcsEmailSender = ", 1)[1].split("\n")[0]
    )
    assert "acsEmailSender: effectiveAcsEmailSender" in MAIN
    assert "acsEmailSender: acsEmailSender" not in MAIN
    assert "param acsEmailSender = ''" in DEV
