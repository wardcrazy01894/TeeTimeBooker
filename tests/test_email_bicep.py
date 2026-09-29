"""MU-15a: `email.bicep` (ACS Communication Service + Email Service + Azure-managed domain)
+ its gating in `main.bicep`. Static text assertions (see other `test_*_bicep.py` files)."""

from __future__ import annotations

from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules"
MAIN_BICEP = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicep"
EMAIL_BICEP = MODULES / "email.bicep"
DEV_PARAMS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.dev"
PROD_PARAMS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.prod"


@pytest.fixture(scope="module")
def main_bicep() -> str:
    return MAIN_BICEP.read_text()


@pytest.fixture(scope="module")
def email_bicep() -> str:
    return EMAIL_BICEP.read_text()


def test_email_module_file_exists() -> None:
    assert EMAIL_BICEP.exists()


def test_email_module_gated_on_deploy_acs_email_param(main_bicep: str) -> None:
    assert "module email 'modules/email.bicep' = if (deployAcsEmail)" in main_bicep


def test_deploy_acs_email_param_defaults_false(main_bicep: str) -> None:
    assert "param deployAcsEmail bool = false" in main_bicep


def test_deploy_acs_email_is_on_in_both_envs() -> None:
    """MU-17 turned ACS on in dev; MU-18 stage A turns it on in prod."""
    assert "param deployAcsEmail = true" in DEV_PARAMS.read_text()
    assert "param deployAcsEmail = true" in PROD_PARAMS.read_text()


def test_email_creates_communication_service_and_managed_domain(email_bicep: str) -> None:
    assert "Microsoft.Communication/emailServices@" in email_bicep
    assert "Microsoft.Communication/emailServices/domains@" in email_bicep
    assert "Microsoft.Communication/communicationServices@" in email_bicep
    assert "domainManagement: 'AzureManaged'" in email_bicep


def test_email_writes_the_kv_secret_by_name(email_bicep: str) -> None:
    assert "name: 'ACS-EMAIL-CONNECTION'" in email_bicep
    assert "listKeys().primaryConnectionString" in email_bicep


# --- customer-managed sender domain (operator request 2026-09-29) --------------------------------
# Stage 1 creates the domain (its DNS records then go into Cloudflare and are verified); stage 2
# flips emailCustomDomainLinked, which links it and makes hello@<domain> the sender. Azure refuses
# to link an unverified domain, hence two stages.


def test_custom_domain_is_customer_managed_and_gated_on_its_param(email_bicep: str) -> None:
    assert "param customDomain string = ''" in email_bicep
    assert "param customDomainLinked bool = false" in email_bicep
    assert "= if (!empty(customDomain)) {" in email_bicep
    assert "domainManagement: 'CustomerManaged'" in email_bicep


def test_hello_sender_and_link_only_once_the_domain_is_linked(email_bicep: str) -> None:
    assert "= if (customDomainLinked && !empty(customDomain)) {" in email_bicep
    assert "username: 'hello'" in email_bicep
    assert "'hello@${customDomain}'" in email_bicep
    assert "displayName: 'Spicy\\'s Tee Time Booker'" in email_bicep
    assert "customDomainLinked && !empty(customDomain) ? [customDomainResource.id] : []" in (
        email_bicep
    )


def test_main_passes_the_params_and_derives_the_hello_sender(main_bicep: str) -> None:
    assert "param emailCustomDomain string = ''" in main_bicep
    assert "param emailCustomDomainLinked bool = false" in main_bicep
    assert "customDomain: emailCustomDomain" in main_bicep
    assert "customDomainLinked: emailCustomDomainLinked" in main_bicep
    # From the MODULE output, so compute + webapp stay ordered after the KV secret write.
    assert "email.?outputs.senderAddress" in main_bicep


def test_prod_gets_the_domain_unlinked_dev_gets_none() -> None:
    prod, dev = PROD_PARAMS.read_text(), DEV_PARAMS.read_text()
    assert "param emailCustomDomain = 'spicyteetimebooker.com'" in prod
    assert "param emailCustomDomainLinked = false" in prod  # stage 1: flip after verification
    assert "param emailCustomDomain = ''" in dev
    assert "param emailCustomDomainLinked = false" in dev
