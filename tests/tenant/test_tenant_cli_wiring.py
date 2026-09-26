"""MU-16a: the tenant commands run on REAL collaborators — the one store builder (Cosmos when
configured), the hosted-course policies, the real adapter factory and the ACS user notifier —
and ``teetime tenant-migrate`` exists (MULTIUSER_PLAN §10.1/§10.2, §12 MU-16)."""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from click.testing import CliRunner

from teetime import __main__ as entry
from teetime.core.config import BookingCutoffConfig
from teetime.courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID, MangroveBayAdapter
from teetime.tenant import wiring
from teetime.tenant.booking_job import HostedAdapterFactory, StoreUserNotifier
from teetime.tenant.crypto import KEYRING_ENV_VAR
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.runner import WatchReport
from teetime.tenant.wiring import LoggingUserNotifier

GOOD_KEYRING = json.dumps(
    {"active": "k1", "keys": {"k1": base64.b64encode(os.urandom(32)).decode()}}
)
COSMOS_ENV = {
    "TENANT_COSMOS_ENDPOINT": "https://cosmos-teetime-shared.documents.azure.com:443/",
    "TENANT_COSMOS_DATABASE": "dev",
}
ACS_ENV = {
    "ACS_EMAIL_CONNECTION": "endpoint=https://acs.example.test/;accesskey="
    + base64.b64encode(b"k" * 32).decode(),
    "ACS_EMAIL_SENDER": "DoNotReply@x.azurecomm.net",
}
_TENANT_ENV = (
    "TENANT_COSMOS_ENDPOINT",
    "TENANT_COSMOS_DATABASE",
    "TENANT_COSMOS_CONTAINER_SUFFIX",
    "ACS_EMAIL_CONNECTION",
    "ACS_EMAIL_SENDER",
    "OPERATOR_NOTIFY_EMAIL",
    "TWOCAPTCHA_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TENANT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(KEYRING_ENV_VAR, GOOD_KEYRING)


class FakeCosmos:
    """Stands in for ``cosmos_tenant_store`` (the SDK-backed opener)."""

    def __init__(self) -> None:
        self.store = _ProbeStore()
        self.opened = 0
        self.closed = 0

    @asynccontextmanager
    async def __call__(self, settings: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.opened += 1
        try:
            yield self.store
        finally:
            self.closed += 1


class _ProbeStore(InMemoryTenantStore):
    def __init__(self) -> None:
        super().__init__(course_timezones={}, cutoff=BookingCutoffConfig())
        self.initialized = 0

    async def initialize(self) -> None:
        self.initialized += 1


@pytest.fixture
def cosmos(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCosmos]:
    fake = FakeCosmos()
    monkeypatch.setattr(wiring, "cosmos_tenant_store", fake)
    for name, value in COSMOS_ENV.items():
        monkeypatch.setenv(name, value)
    yield fake


def _empty_watch_report() -> WatchReport:
    return WatchReport(
        rows_loaded=0,
        searches=0,
        logins=0,
        booked=(),
        upgraded=(),
        lost=(),
        rate_limited=False,
        systemic_error=None,
    )


@pytest.fixture
def watch_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_run(**kwargs: Any) -> WatchReport:
        calls.append(kwargs)
        return _empty_watch_report()

    monkeypatch.setattr(entry, "run_tenant_watch", fake_run)
    return calls


# --- tenant-watch ----------------------------------------------------------------------------


def test_tenant_watch_wires_hosted_policies_real_adapters_and_logging_notifier(
    watch_calls: list[dict[str, Any]],
) -> None:
    result = CliRunner().invoke(entry.cli, ["tenant-watch", "--dry-run", "true"])
    assert result.exit_code == 0, result.output
    (call,) = watch_calls
    assert call["policies"] == {MANGROVE_BAY_COURSE_ID: MangroveBayAdapter.release_policy}
    assert isinstance(call["adapter_factory"], HostedAdapterFactory)
    assert isinstance(call["notifier"], LoggingUserNotifier)
    assert isinstance(call["store"], InMemoryTenantStore)


def test_tenant_watch_uses_cosmos_and_acs_when_configured(
    watch_calls: list[dict[str, Any]],
    cosmos: FakeCosmos,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for name, value in ACS_ENV.items():
        monkeypatch.setenv(name, value)
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(entry.cli, ["tenant-watch", "--dry-run", "true"])
    assert result.exit_code == 0, result.output
    (call,) = watch_calls
    assert call["store"] is cosmos.store
    assert isinstance(call["notifier"], StoreUserNotifier)
    assert (cosmos.opened, cosmos.closed) == (1, 1)
    assert "IN-MEMORY" not in caplog.text


def test_tenant_watch_live_requires_the_2captcha_key(watch_calls: list[dict[str, Any]]) -> None:
    # A live watcher books and upgrades; without a solver every book would fail at the course.
    result = CliRunner().invoke(entry.cli, ["tenant-watch", "--dry-run", "false"])
    assert result.exit_code != 0
    assert "TWOCAPTCHA_API_KEY" in result.output
    assert watch_calls == []


def test_tenant_watch_half_configured_store_fails_closed(
    watch_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TENANT_COSMOS_DATABASE", "prod")
    result = CliRunner().invoke(entry.cli, ["tenant-watch", "--dry-run", "true"])
    assert result.exit_code != 0
    assert "TENANT_COSMOS_ENDPOINT" in result.output
    assert watch_calls == []


# --- tenant-run / tenant-plan ----------------------------------------------------------------


def test_tenant_run_uses_the_cosmos_store_when_configured(
    cosmos: FakeCosmos, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen: dict[str, Any] = {}

    async def fake_job(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(entry, "run_booking_job", fake_job)
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            entry.cli, ["tenant-run", "--event", "mb0600et", "--dry-run", "true", "--no-wait"]
        )
    assert result.exit_code == 0, result.output
    assert seen["store"] is cosmos.store
    assert (cosmos.opened, cosmos.closed) == (1, 1)
    assert "IN-MEMORY" not in caplog.text


def test_tenant_run_half_configured_store_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_COSMOS_ENDPOINT", COSMOS_ENV["TENANT_COSMOS_ENDPOINT"])
    result = CliRunner().invoke(
        entry.cli, ["tenant-run", "--event", "mb0600et", "--dry-run", "true", "--no-wait"]
    )
    assert result.exit_code != 0
    assert "TENANT_COSMOS_DATABASE" in result.output


def test_tenant_plan_reads_the_cosmos_store_when_configured(cosmos: FakeCosmos) -> None:
    result = CliRunner().invoke(entry.cli, ["tenant-plan", "--event", "mb0600et"])
    assert result.exit_code == 0, result.output
    assert (cosmos.opened, cosmos.closed) == (1, 1)


# --- tenant-migrate --------------------------------------------------------------------------


def test_tenant_migrate_help_exits_zero() -> None:
    result = CliRunner().invoke(entry.cli, ["tenant-migrate", "--help"])
    assert result.exit_code == 0, result.output


def test_tenant_migrate_connects_runs_the_empty_list_and_exits_zero(
    cosmos: FakeCosmos, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    result = CliRunner().invoke(entry.cli, ["tenant-migrate"])
    assert result.exit_code == 0, result.output
    assert cosmos.store.initialized == 1
    assert (cosmos.opened, cosmos.closed) == (1, 1)
    assert "0 migration(s) run" in caplog.text


def test_tenant_migrate_refuses_the_in_memory_store() -> None:
    result = CliRunner().invoke(entry.cli, ["tenant-migrate"])
    assert result.exit_code != 0
    assert "TENANT_COSMOS_ENDPOINT" in result.output


def test_tenant_migrate_failure_exits_nonzero(
    cosmos: FakeCosmos, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable() -> None:
        raise ConnectionError("cosmos down")

    monkeypatch.setattr(cosmos.store, "initialize", unreachable)
    result = CliRunner().invoke(entry.cli, ["tenant-migrate"])
    assert result.exit_code != 0
    assert "ConnectionError" in result.output
