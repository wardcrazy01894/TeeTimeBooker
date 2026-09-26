"""WebSettings + load_web_settings (MULTIUSER_PLAN §8.3, §10.1): fail-closed, OAuth providers
enabled by config with at least one required, one public base URL setting."""

from __future__ import annotations

import pytest

from teetime.web.app import (
    WEB_ENV_VARS,
    OAuthProviderSettings,
    WebConfigError,
    WebSettings,
    load_web_settings,
)

_GH = OAuthProviderSettings(client_id="gh", client_secret="gh-secret-0123456789")


def _env(**overrides: str | None) -> dict[str, str]:
    base: dict[str, str | None] = {
        "TEETIME_PUBLIC_BASE_URL": "https://teetime-web-dev.example.azurecontainerapps.io",
        "WEB_SESSION_SECRET": "x" * 48,
        "OAUTH_GITHUB_CLIENT_ID": "gh",
        "OAUTH_GITHUB_CLIENT_SECRET": "gh-secret-0123456789",
        "TEETIME_OPERATOR_EMAIL": "operator@example.test",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_at_least_one_provider_required() -> None:
    with pytest.raises(WebConfigError, match="provider"):
        WebSettings(public_base_url="https://x.test", session_secret="s" * 48)
    with pytest.raises(WebConfigError, match="provider"):
        load_web_settings(_env(OAUTH_GITHUB_CLIENT_ID=None, OAUTH_GITHUB_CLIENT_SECRET=None))


def test_either_provider_alone_is_enough() -> None:
    gh_only = load_web_settings(_env())
    assert gh_only.github is not None and gh_only.google is None
    gg_only = load_web_settings(
        _env(
            OAUTH_GITHUB_CLIENT_ID=None,
            OAUTH_GITHUB_CLIENT_SECRET=None,
            OAUTH_GOOGLE_CLIENT_ID="gg",
            OAUTH_GOOGLE_CLIENT_SECRET="gg-secret-0123456789",
        )
    )
    assert gg_only.google is not None and gg_only.github is None
    assert gg_only.enabled_providers == ("google",)
    assert gh_only.enabled_providers == ("github",)


def test_half_configured_provider_is_rejected() -> None:
    """An id without its secret (or vice versa) is a deployment mistake, not 'disabled'."""
    with pytest.raises(WebConfigError, match="OAUTH_GOOGLE_CLIENT_SECRET"):
        load_web_settings(_env(OAUTH_GOOGLE_CLIENT_ID="gg"))


def test_load_fails_closed_on_missing_session_secret() -> None:
    with pytest.raises(WebConfigError, match="WEB_SESSION_SECRET"):
        load_web_settings(_env(WEB_SESSION_SECRET=None))


def test_session_secret_too_short_is_rejected() -> None:
    with pytest.raises(WebConfigError, match="WEB_SESSION_SECRET"):
        load_web_settings(_env(WEB_SESSION_SECRET="short"))


def test_public_base_url_must_be_https_without_trailing_slash() -> None:
    with pytest.raises(WebConfigError, match="TEETIME_PUBLIC_BASE_URL"):
        load_web_settings(_env(TEETIME_PUBLIC_BASE_URL="http://insecure.test"))
    s = load_web_settings(_env(TEETIME_PUBLIC_BASE_URL="https://ok.test/"))
    assert s.public_base_url == "https://ok.test"
    assert s.redirect_uri("github") == "https://ok.test/auth/github/callback"


def test_dry_run_defaults_true_and_parses_env() -> None:
    assert WebSettings(
        public_base_url="https://x.test", session_secret="s" * 48, github=_GH
    ).dry_run
    assert load_web_settings(_env()).dry_run is True
    assert load_web_settings(_env(TEETIME_WEB_DRY_RUN="false")).dry_run is False
    with pytest.raises(WebConfigError, match="TEETIME_WEB_DRY_RUN"):
        load_web_settings(_env(TEETIME_WEB_DRY_RUN="maybe"))


def test_secrets_never_in_repr() -> None:
    s = WebSettings(public_base_url="https://x.test", session_secret="s" * 48, github=_GH)
    text = repr(s)
    assert "s" * 48 not in text
    assert "gh-secret-0123456789" not in text


def test_every_env_var_the_loader_reads_is_in_the_contract_tuple() -> None:
    for name in (
        "TEETIME_PUBLIC_BASE_URL",
        "WEB_SESSION_SECRET",
        "OAUTH_GITHUB_CLIENT_ID",
        "OAUTH_GITHUB_CLIENT_SECRET",
        "OAUTH_GOOGLE_CLIENT_ID",
        "OAUTH_GOOGLE_CLIENT_SECRET",
        "TEETIME_OPERATOR_EMAIL",
        "TEETIME_WEB_DRY_RUN",
    ):
        assert name in WEB_ENV_VARS
