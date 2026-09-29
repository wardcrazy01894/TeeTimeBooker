"""The `teetime web` entrypoint (MULTIUSER_PLAN §8.1, §9.4): logging order, fail-closed config,
secret literals registered, uvicorn handed the built app."""

from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from teetime import __main__ as entry
from teetime.core.config import BookingCutoffConfig
from teetime.core.redaction import redact_text
from teetime.courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID, MangroveBayAdapter
from teetime.tenant import wiring
from teetime.tenant.acs_email import AcsEmailClient
from teetime.tenant.booking_job import HostedAdapterFactory, StoreUserNotifier
from teetime.tenant.crypto import KEYRING_ENV_VAR
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.wiring import LoggingUserNotifier

GOOD_ENV = {
    "TEETIME_PUBLIC_BASE_URL": "https://teetime-web-dev.example.azurecontainerapps.io",
    "WEB_SESSION_SECRET": "session-secret-value-0123456789abcdef",
    "OAUTH_GITHUB_CLIENT_ID": "gh-id",
    "OAUTH_GITHUB_CLIENT_SECRET": "github-client-secret-0123456789",
    "TEETIME_OPERATOR_EMAIL": "operator@example.test",
}


def test_web_entrypoint_installs_redaction_after_basicconfig() -> None:
    """Source-position pin, like the CLI test in tests/test_log_redaction.py: the web command
    must call logging.basicConfig( and THEN install_log_redaction() — installing first attaches
    the filter to no handler and leaves the httpx/authlib request lines unredacted."""
    # `web_cmd` is a click.Command; the decorated function body lives on `.callback`.
    callback = entry.web_cmd.callback
    assert callback is not None
    src = inspect.getsource(callback)
    config = src.index("logging.basicConfig(")
    install = src.index("install_log_redaction()")
    assert config < install
    # and nothing between them configures logging a second time
    assert len(re.findall(r"logging\.basicConfig\(", src)) == 1


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture what the command would hand to uvicorn instead of binding a socket."""
    captured: dict[str, Any] = {}

    async def fake_serve(app: FastAPI, *, host: str, port: int) -> None:
        captured.update(app=app, host=host, port=port)

    monkeypatch.setattr(entry, "_serve_web", fake_serve)
    return captured


def test_web_help_exits_zero() -> None:
    result = CliRunner().invoke(entry.cli, ["web", "--help"])
    assert result.exit_code == 0, result.output
    assert "--port" in result.output


def test_web_command_fails_closed_on_missing_env(
    served: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != "WEB_SESSION_SECRET"}
    result = CliRunner().invoke(entry.cli, ["web"], env=env)
    assert result.exit_code != 0
    assert "WEB_SESSION_SECRET" in result.output
    assert not served  # uvicorn never started


def test_web_command_builds_app_registers_secrets_and_serves(
    served: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in GOOD_ENV:
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(entry.cli, ["web", "--port", "9100"], env=GOOD_ENV)
    assert result.exit_code == 0, result.output
    assert isinstance(served["app"], FastAPI)
    assert (served["host"], served["port"]) == ("0.0.0.0", 9100)
    # E7: the session secret and OAuth client secret are exact-literal masked in logs.
    line = f"{GOOD_ENV['WEB_SESSION_SECRET']} {GOOD_ENV['OAUTH_GITHUB_CLIENT_SECRET']}"
    masked = redact_text(line)
    assert GOOD_ENV["WEB_SESSION_SECRET"] not in masked
    assert GOOD_ENV["OAUTH_GITHUB_CLIENT_SECRET"] not in masked


def test_web_port_falls_back_to_env_then_8000(served: dict[str, Any]) -> None:
    result = CliRunner().invoke(entry.cli, ["web"], env={**GOOD_ENV, "PORT": "8123"})
    assert result.exit_code == 0, result.output
    assert served["port"] == 8123
    served.clear()
    result = CliRunner().invoke(entry.cli, ["web"], env=GOOD_ENV)
    assert result.exit_code == 0, result.output
    assert served["port"] == 8000


def test_serve_web_scopes_forwarded_allow_ips_to_localhost_not_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BACKLOG "scope forwarded_allow_ips": ACA's ingress sidecar talks to the container over
    localhost within the same pod, so trusting X-Forwarded-* from "*" (any peer) would let a
    request that reached the container by some OTHER path (e.g. a future VNet integration)
    spoof its scheme/host. The uvicorn config must carry an explicit, non-wildcard value."""
    monkeypatch.delenv("WEB_FORWARDED_ALLOW_IPS", raising=False)
    config = entry._uvicorn_config(FastAPI(), host="0.0.0.0", port=8000)
    assert config.forwarded_allow_ips != "*"
    assert config.forwarded_allow_ips == "127.0.0.1"
    assert config.proxy_headers is True
    # basicConfig + the redaction filter stay in charge (uvicorn must not install handlers).
    assert config.log_config is None


def test_serve_web_forwarded_allow_ips_overridable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator can widen the trusted range (e.g. a future VNet-integrated ACA revision)
    without a code change, via WEB_FORWARDED_ALLOW_IPS — but the DEFAULT stays localhost-only."""
    monkeypatch.setenv("WEB_FORWARDED_ALLOW_IPS", "10.0.0.0/16")
    config = entry._uvicorn_config(FastAPI(), host="0.0.0.0", port=8000)
    assert config.forwarded_allow_ips == "10.0.0.0/16"


# --- MU-16a: the real collaborators ----------------------------------------------------------

KEY_B64 = base64.b64encode(os.urandom(32)).decode()
GOOD_KEYRING = json.dumps({"active": "k1", "keys": {"k1": KEY_B64}})
_TENANT_ENV = (
    KEYRING_ENV_VAR,
    "TENANT_COSMOS_ENDPOINT",
    "TENANT_COSMOS_DATABASE",
    "TENANT_COSMOS_CONTAINER_SUFFIX",
    "ACS_EMAIL_CONNECTION",
    "ACS_EMAIL_SENDER",
)


@pytest.fixture
def app_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture what ``web`` hands to ``create_app`` (the real factory still builds the app)."""
    for name in (*GOOD_ENV, *_TENANT_ENV):
        monkeypatch.delenv(name, raising=False)
    captured: dict[str, Any] = {}
    real = entry.create_app

    def spy(settings: Any, **kwargs: Any) -> FastAPI:
        captured.update(kwargs)
        return real(settings, **kwargs)

    monkeypatch.setattr(entry, "create_app", spy)
    return captured


def test_web_wires_keyring_adapters_policies_and_notifier(
    served: dict[str, Any], app_kwargs: dict[str, Any]
) -> None:
    result = CliRunner().invoke(entry.cli, ["web"], env={**GOOD_ENV, KEYRING_ENV_VAR: GOOD_KEYRING})
    assert result.exit_code == 0, result.output
    assert app_kwargs["keyring"] is not None
    assert isinstance(app_kwargs["adapter_factory"], HostedAdapterFactory)
    assert app_kwargs["policies"] == {
        str(MANGROVE_BAY_COURSE_ID): MangroveBayAdapter.release_policy
    }
    assert isinstance(app_kwargs["notifier"], LoggingUserNotifier)
    assert app_kwargs["email_sender"] is None  # no ACS settings: invites are not emailed
    assert isinstance(app_kwargs["store"], InMemoryTenantStore)
    # E7: the keyring's key material is masked from now on.
    assert KEY_B64 not in redact_text(f"key={KEY_B64}")


def test_web_without_a_keyring_serves_with_connect_disabled(
    served: dict[str, Any], app_kwargs: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(entry.cli, ["web"], env=GOOD_ENV)
    assert result.exit_code == 0, result.output
    assert app_kwargs["keyring"] is None
    assert KEYRING_ENV_VAR in caplog.text


def test_web_malformed_keyring_fails_closed(
    served: dict[str, Any], app_kwargs: dict[str, Any]
) -> None:
    result = CliRunner().invoke(entry.cli, ["web"], env={**GOOD_ENV, KEYRING_ENV_VAR: "{nope"})
    assert result.exit_code != 0
    assert not served


def test_web_uses_the_cosmos_store_and_acs_when_configured(
    served: dict[str, Any], app_kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = InMemoryTenantStore(course_timezones={}, cutoff=BookingCutoffConfig())
    opened: list[Any] = []

    @asynccontextmanager
    async def fake_cosmos(settings: Any, **kwargs: Any) -> AsyncIterator[Any]:
        opened.append(settings)
        yield sentinel

    monkeypatch.setattr(wiring, "cosmos_tenant_store", fake_cosmos)
    env = {
        **GOOD_ENV,
        KEYRING_ENV_VAR: GOOD_KEYRING,
        "TENANT_COSMOS_ENDPOINT": "https://cosmos-teetime-shared.documents.azure.com:443/",
        "TENANT_COSMOS_DATABASE": "dev",
        "ACS_EMAIL_CONNECTION": "endpoint=https://acs.example.test/;accesskey="
        + base64.b64encode(b"k" * 32).decode(),
        "ACS_EMAIL_SENDER": "DoNotReply@x.azurecomm.net",
    }
    result = CliRunner().invoke(entry.cli, ["web"], env=env)
    assert result.exit_code == 0, result.output
    assert app_kwargs["store"] is sentinel
    assert len(opened) == 1
    assert isinstance(app_kwargs["notifier"], StoreUserNotifier)
    assert isinstance(app_kwargs["email_sender"], AcsEmailClient)  # invitations
