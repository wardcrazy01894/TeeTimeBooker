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


def test_deploy_acs_email_defaults_false_in_both_param_files() -> None:
    assert "param deployAcsEmail = false" in DEV_PARAMS.read_text()
    assert "param deployAcsEmail = false" in PROD_PARAMS.read_text()


def test_email_creates_communication_service_and_managed_domain(email_bicep: str) -> None:
    assert "Microsoft.Communication/emailServices@" in email_bicep
    assert "Microsoft.Communication/emailServices/domains@" in email_bicep
    assert "Microsoft.Communication/communicationServices@" in email_bicep
    assert "domainManagement: 'AzureManaged'" in email_bicep


def test_email_writes_the_kv_secret_by_name(email_bicep: str) -> None:
    assert "name: 'ACS-EMAIL-CONNECTION'" in email_bicep
    assert "listKeys().primaryConnectionString" in email_bicep
