"""MU-18 (MULTIUSER_PLAN §11 steps 5-7, AZURE_PLAN §10.8): the prod cutover, in two stages.

Stage A: prod gets the web app, ACS email and the shared Cosmos endpoint
while its booking and watch jobs STAY on the TOML path, so the operator can connect, create the
rule and adopt the live reservations before anything books through the tenant path. Stage B flipped
the booking and watch jobs to the tenant path; MU-19 (2026-11-14) then removed the mode params, so
the jobs can run nothing else. Static text assertions, like the other ``test_*_bicep.py`` files.
"""

from __future__ import annotations

from pathlib import Path

PROD = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.prod"
).read_text()

COSMOS = "https://cosmos-teetime-shared.documents.azure.com:443/"
# The prod site moved to its custom domain on 2026-09-28 (the old host redirects there).
PROD_WEB = "https://spicyteetimebooker.com"


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


def test_prod_sets_no_job_mode() -> None:
    """Stage B set `bookingMode`/`watchMode = 'tenant'`; MU-19 removed both params (the jobs
    run `tenant-run --event mb0600et` and `tenant-watch` unconditionally, pinned in
    tests/test_compute_bicep_tenant_mode.py). A param file that still set one would fail the
    deploy: main.bicep no longer declares it."""
    assert "param bookingMode" not in PROD
    assert "param watchMode" not in PROD
