"""MU-18 (MULTIUSER_PLAN §11 steps 5-7, AZURE_PLAN §10.8): the prod cutover, in two stages.

Stage A (this file's current pin): prod gets the web app, ACS email and the shared Cosmos endpoint
while its booking and watch jobs STAY on the TOML path, so the operator can connect, create the
rule and adopt the live reservations before anything books through the tenant path. Stage B flips
the modes. Static text assertions, like the other ``test_*_bicep.py`` files.
"""

from __future__ import annotations

from pathlib import Path

PROD = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.prod"
).read_text()

COSMOS = "https://cosmos-teetime-shared.documents.azure.com:443/"
PROD_WEB = "https://teetime-web-prod.wittydesert-02f9f0cd.eastus2.azurecontainerapps.io"


def test_prod_gets_the_web_app_acs_and_the_tenant_store() -> None:
    assert "param deployWebApp = true" in PROD
    assert "param deployAcsEmail = true" in PROD
    assert f"param tenantCosmosEndpoint = '{COSMOS}'" in PROD
    assert f"param webPublicBaseUrl = '{PROD_WEB}'" in PROD


def test_prod_stays_live_and_its_address_free() -> None:
    assert "param dryRun = false" in PROD
    assert "param operatorEmail = ''" in PROD  # read from OPERATOR-NOTIFY-EMAIL (public repo)
    assert "param acsEmailSender = ''" in PROD  # derived from the email module
    assert "param watchCron = '*/10 * * * *'" in PROD
