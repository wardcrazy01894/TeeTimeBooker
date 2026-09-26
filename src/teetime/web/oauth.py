"""OAuth sign-in via authlib (MULTIUSER_PLAN §8.3) — the ONLY module that touches authlib.

authlib ships no type information (see the per-module override in pyproject), so this shim
narrows every untyped result to a typed ``ProviderIdentity`` at the boundary. Identity is the
immutable ``(provider, subject)`` pair; the emails it reports are ONLY the ones the provider
itself has verified — GitHub via ``GET /user/emails`` entries with ``verified: true`` (never
the profile's public ``email`` field), Google via the ``email_verified`` claim. Everything else
(invite matching, sessions) lives in ``web.auth`` / ``web.app`` and is framework-typed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger(__name__)

PROVIDERS: tuple[str, ...] = ("github", "google")

_GITHUB = {
    "authorize_url": "https://github.com/login/oauth/authorize",
    "access_token_url": "https://github.com/login/oauth/access_token",
    "api_base_url": "https://api.github.com/",
    # `user:email` unlocks GET /user/emails (the verified list); PKCE is harmless where a
    # server ignores it (RFC 6749 §3.1: unrecognised params are ignored).
    "client_kwargs": {"scope": "user:email", "code_challenge_method": "S256"},
}
_GOOGLE = {
    # OIDC discovery gives the authorize/token/userinfo/jwks endpoints and lets authlib
    # validate an id_token when one is returned.
    "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
    "client_kwargs": {"scope": "openid email profile", "code_challenge_method": "S256"},
}


@dataclass(frozen=True, slots=True)
class OAuthProviderSettings:
    """One provider's client credentials. A provider is ENABLED iff its settings exist."""

    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """What a completed sign-in proves: the immutable subject plus provider-VERIFIED emails."""

    provider: str
    subject: str
    verified_emails: tuple[str, ...]
    display_name: str


class OAuthFlowError(RuntimeError):
    """The round-trip did not complete (bad state, provider error, malformed profile). The
    caller answers 400; nothing about the user is known."""


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OAuthFlowError("provider returned a non-object payload")
    return {str(k): v for k, v in value.items()}


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise OAuthFlowError("provider returned a non-list payload")
    return list(value)


def _display_name(profile: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        v = profile.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


class OAuthProviders:
    """The enabled providers, registered once per app."""

    def __init__(self, providers: Mapping[str, OAuthProviderSettings]) -> None:
        unknown = set(providers) - set(PROVIDERS)
        if unknown:
            raise ValueError(f"unknown OAuth provider(s): {sorted(unknown)}")
        self._oauth = OAuth()
        self._names: tuple[str, ...] = tuple(p for p in PROVIDERS if p in providers)
        for name in self._names:
            cfg = providers[name]
            extra = _GITHUB if name == "github" else _GOOGLE
            self._oauth.register(
                name=name, client_id=cfg.client_id, client_secret=cfg.client_secret, **extra
            )

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def is_enabled(self, provider: str) -> bool:
        return provider in self._names

    def _client(self, provider: str) -> object:
        if not self.is_enabled(provider):
            raise OAuthFlowError(f"provider {provider!r} is not enabled")
        return self._oauth.create_client(provider)

    async def start(self, request: Request, provider: str, *, redirect_uri: str) -> Response:
        """Store state (+ PKCE verifier) in the session and redirect to the provider."""
        client = self._client(provider)
        resp = await client.authorize_redirect(request, redirect_uri)  # type: ignore[attr-defined]
        if not isinstance(resp, Response):
            raise OAuthFlowError("authlib did not return a redirect response")
        return resp

    async def finish(self, request: Request, provider: str) -> ProviderIdentity:
        """Exchange the code (validating ``state``) and fetch the verified identity."""
        client = self._client(provider)
        try:
            token = await client.authorize_access_token(request)  # type: ignore[attr-defined]
        except Exception as e:
            # Any failure here (state mismatch, provider error, transport) means no identity
            # was established; the reason is logged, never shown.
            log.warning("oauth %s: token exchange failed: %s", provider, type(e).__name__)
            raise OAuthFlowError("token exchange failed") from e
        if provider == "github":
            return await _github_identity(client, token)
        return await _google_identity(client, token)


async def _github_identity(client: object, token: object) -> ProviderIdentity:
    profile = _as_dict(await _get_json(client, "user", token))
    subject = profile.get("id")
    if not isinstance(subject, int):
        raise OAuthFlowError("github profile has no numeric id")
    verified: list[str] = []
    for entry in _as_list(await _get_json(client, "user/emails", token)):
        e = _as_dict(entry)
        addr = e.get("email")
        if e.get("verified") is True and isinstance(addr, str) and addr:
            verified.append(addr)
    return ProviderIdentity(
        provider="github",
        subject=str(subject),
        verified_emails=tuple(verified),
        display_name=_display_name(profile, "name", "login"),
    )


async def _google_identity(client: object, token: object) -> ProviderIdentity:
    claims: object = token.get("userinfo") if isinstance(token, dict) else None
    if claims is None:
        claims = await client.userinfo(token=token)  # type: ignore[attr-defined]
    info = _as_dict(dict(claims) if isinstance(claims, Mapping) else claims)
    subject = info.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OAuthFlowError("google userinfo has no sub")
    email = info.get("email")
    verified = (
        (email,) if info.get("email_verified") is True and isinstance(email, str) and email else ()
    )
    return ProviderIdentity(
        provider="google",
        subject=subject,
        verified_emails=verified,
        display_name=_display_name(info, "name"),
    )


async def _get_json(client: object, path: str, token: object) -> object:
    resp = await client.get(path, token=token)  # type: ignore[attr-defined]
    status = getattr(resp, "status_code", None)
    if not isinstance(status, int) or status >= 400:  # noqa: PLR2004 — HTTP error class
        raise OAuthFlowError(f"provider GET {path} returned {status}")
    data: object = resp.json()
    return data
