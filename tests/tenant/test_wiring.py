"""MU-16a: the ONE tenant-store builder and the shared collaborators the tenant commands wire
(``teetime.tenant.wiring``). Cosmos when ``TENANT_COSMOS_ENDPOINT`` is set, the in-memory store
with a loud WARNING when it is not, and a clear refusal on a half-configured environment."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest

from teetime.courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID, MangroveBayAdapter
from teetime.tenant import wiring
from teetime.tenant.booking_job import StoreUserNotifier
from teetime.tenant.cosmos.store import CosmosSettings
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.wiring import (
    LoggingUserNotifier,
    TenantStoreConfigError,
    hosted_policies,
    open_tenant_store,
    tenant_store_settings_from_env,
    user_notifier_from_env,
)

ENDPOINT = "https://cosmos-teetime-shared.documents.azure.com:443/"
ACS_ENV = {
    "ACS_EMAIL_CONNECTION": "endpoint=https://acs.example.test/;accesskey="
    + base64.b64encode(b"k" * 32).decode(),
    "ACS_EMAIL_SENDER": "DoNotReply@x.azurecomm.net",
}


# --- settings --------------------------------------------------------------------------------


def test_no_endpoint_means_no_cosmos_settings() -> None:
    assert tenant_store_settings_from_env({}) is None
    # An empty value (ACA passes '' for an unset bicep param) is the same as unset.
    assert tenant_store_settings_from_env({"TENANT_COSMOS_ENDPOINT": ""}) is None


def test_endpoint_and_database_build_cosmos_settings() -> None:
    settings = tenant_store_settings_from_env(
        {
            "TENANT_COSMOS_ENDPOINT": ENDPOINT,
            "TENANT_COSMOS_DATABASE": "prod",
            "AZURE_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
        }
    )
    assert settings == CosmosSettings(
        endpoint=ENDPOINT,
        database="prod",
        container_suffix="",
        managed_identity_client_id="11111111-2222-3333-4444-555555555555",
    )


def test_endpoint_without_database_fails_closed_never_defaults_to_dev() -> None:
    # CosmosSettings.from_env would default the database to `dev`: a prod job with a lost
    # TENANT_COSMOS_DATABASE would then silently read and write the DEV data.
    with pytest.raises(TenantStoreConfigError, match="TENANT_COSMOS_DATABASE"):
        tenant_store_settings_from_env({"TENANT_COSMOS_ENDPOINT": ENDPOINT})


@pytest.mark.parametrize(
    "env",
    [
        {"TENANT_COSMOS_DATABASE": "prod"},
        {"TENANT_COSMOS_CONTAINER_SUFFIX": "-ci"},
    ],
)
def test_cosmos_settings_without_an_endpoint_fail_closed(env: Mapping[str, str]) -> None:
    # compute.bicep always sets TENANT_COSMOS_DATABASE in tenant mode, so a tenant-mode job
    # whose `tenantCosmosEndpoint` param was left empty refuses to run on an empty in-memory
    # store (and exit 0 having watched nothing).
    with pytest.raises(TenantStoreConfigError, match="TENANT_COSMOS_ENDPOINT"):
        tenant_store_settings_from_env(env)


def test_invalid_cosmos_settings_are_a_config_error() -> None:
    with pytest.raises(TenantStoreConfigError, match="suffix"):
        tenant_store_settings_from_env(
            {
                "TENANT_COSMOS_ENDPOINT": ENDPOINT,
                "TENANT_COSMOS_DATABASE": "prod",
                "TENANT_COSMOS_CONTAINER_SUFFIX": "-x",
            }
        )


# --- the store builder -----------------------------------------------------------------------


class _FakeOpener:
    """Stands in for ``cosmos_tenant_store`` (the SDK-backed opener, a collaborator)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.store = InMemoryTenantStore(course_timezones={}, cutoff=wiring.BookingCutoffConfig())

    @asynccontextmanager
    async def __call__(self, settings: CosmosSettings, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append({"settings": settings, **kwargs})
        try:
            yield self.store
        finally:
            self.closed = True


async def test_open_tenant_store_without_cosmos_is_in_memory_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        async with open_tenant_store({}, command="tenant-watch") as store:
            assert isinstance(store, InMemoryTenantStore)
            # The hosted courses' zones are known (the materializer / runner need them).
            assert store.course_timezone(MANGROVE_BAY_COURSE_ID) == "America/New_York"
    assert "IN-MEMORY" in caplog.text
    assert "tenant-watch" in caplog.text


async def test_open_tenant_store_opens_cosmos_when_configured_and_closes_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    opener = _FakeOpener()
    monkeypatch.setattr(wiring, "cosmos_tenant_store", opener)
    env = {"TENANT_COSMOS_ENDPOINT": ENDPOINT, "TENANT_COSMOS_DATABASE": "dev"}
    with caplog.at_level(logging.INFO):
        async with open_tenant_store(env, command="tenant-run") as store:
            assert store is opener.store
            assert not opener.closed
    assert opener.closed
    (call,) = opener.calls
    assert call["settings"].database == "dev"
    assert call["course_timezones"] == {MANGROVE_BAY_COURSE_ID: "America/New_York"}
    assert "IN-MEMORY" not in caplog.text
    # The line names where it connected (endpoint + database), never a credential.
    assert "dev" in caplog.text


async def test_open_tenant_store_require_durable_refuses_in_memory() -> None:
    with pytest.raises(TenantStoreConfigError, match="TENANT_COSMOS_ENDPOINT"):
        async with open_tenant_store({}, command="tenant-migrate", require_durable=True):
            pass


async def test_open_tenant_store_half_configured_raises_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _FakeOpener()
    monkeypatch.setattr(wiring, "cosmos_tenant_store", opener)
    with pytest.raises(TenantStoreConfigError):
        async with open_tenant_store({"TENANT_COSMOS_ENDPOINT": ENDPOINT}, command="web"):
            pass
    assert opener.calls == []


# --- shared collaborators --------------------------------------------------------------------


def test_hosted_policies_are_every_hosted_course() -> None:
    assert hosted_policies() == {MANGROVE_BAY_COURSE_ID: MangroveBayAdapter.release_policy}


def test_user_notifier_uses_acs_when_configured() -> None:
    store = InMemoryTenantStore(course_timezones={}, cutoff=wiring.BookingCutoffConfig())
    notifier = user_notifier_from_env(store, ACS_ENV, command="tenant-watch")
    assert isinstance(notifier, StoreUserNotifier)


def test_user_notifier_falls_back_to_logging_when_email_is_unconfigured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryTenantStore(course_timezones={}, cutoff=wiring.BookingCutoffConfig())
    with caplog.at_level(logging.WARNING):
        notifier = user_notifier_from_env(store, {}, command="tenant-watch")
    assert isinstance(notifier, LoggingUserNotifier)
    assert "ACS_EMAIL_CONNECTION" in caplog.text


async def test_open_tenant_store_quiets_the_azure_sdk_request_logging(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The Azure SDK logs every HTTP request's headers at INFO (seen flooding the dev web logs,
    2026-09-26). Opening the Cosmos store raises the ``azure`` logger to WARNING, even when the
    root logger runs at INFO as the jobs do."""
    azure = logging.getLogger("azure")
    monkeypatch.setattr(azure, "level", logging.NOTSET)
    monkeypatch.setattr(wiring, "cosmos_tenant_store", _FakeOpener())
    env = {"TENANT_COSMOS_ENDPOINT": ENDPOINT, "TENANT_COSMOS_DATABASE": "dev"}
    with caplog.at_level(logging.INFO):
        assert azure.getEffectiveLevel() == logging.INFO  # non-vacuity
        async with open_tenant_store(env, command="test"):
            pass
        assert logging.getLogger("azure").getEffectiveLevel() >= logging.WARNING
