"""The `teetime web` entrypoint (MULTIUSER_PLAN §8.1, §9.4): logging order, fail-closed config,
secret literals registered, uvicorn handed the built app."""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from teetime import __main__ as entry
from teetime.core.redaction import redact_text

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

    def fake_serve(app: FastAPI, *, host: str, port: int) -> None:
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
    spoof its scheme/host. `_serve_web` must hand uvicorn an explicit, non-wildcard value."""
    captured: dict[str, Any] = {}

    def fake_run(app: FastAPI, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    entry._serve_web(FastAPI(), host="0.0.0.0", port=8000)
    assert "forwarded_allow_ips" in captured
    assert captured["forwarded_allow_ips"] != "*"
    assert captured["forwarded_allow_ips"] == "127.0.0.1"
    assert captured["proxy_headers"] is True


def test_serve_web_forwarded_allow_ips_overridable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator can widen the trusted range (e.g. a future VNet-integrated ACA revision)
    without a code change, via WEB_FORWARDED_ALLOW_IPS — but the DEFAULT stays localhost-only."""
    captured: dict[str, Any] = {}

    def fake_run(app: FastAPI, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    monkeypatch.setenv("WEB_FORWARDED_ALLOW_IPS", "10.0.0.0/16")
    entry._serve_web(FastAPI(), host="0.0.0.0", port=8000)
    assert captured["forwarded_allow_ips"] == "10.0.0.0/16"
