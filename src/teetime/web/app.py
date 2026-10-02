"""App factory + settings (MULTIUSER_PLAN §8.1, §8.3, §10.1).

One scale-to-zero Azure Container App (``teetime web``: uvicorn, max 1 replica) serves the pages
and the HTMX endpoints. ``create_app`` returns the ASGI app: session middleware (``Secure``,
``HttpOnly``, ``SameSite=Lax``), CSRF verification on every non-GET, security headers on every
response, the OAuth routes with invite-only binding, the operator's ``/admin/users``, the MU-13
dashboard / rules / dates pages and the MU-14 accounts page + cancel (``web/pages.py``). No DB
besides the injected ``TenantStore``; ``/healthz`` never touches it.

Logging is configured by the ``teetime web`` entrypoint (``basicConfig`` THEN
``install_log_redaction()``), never here: a factory must not reconfigure the process logger.
"""

# NO `from __future__ import annotations` here, deliberately: FastAPI resolves string
# annotations against the handler's module GLOBALS, so an `Annotated[User, Depends(closure)]`
# built inside `create_app` (the dependencies close over the injected store/clock) would fail
# to resolve and silently degrade to a query parameter (a 422 on every page). Eager
# annotations keep the closure-bound dependencies working; PEP 604 unions need no future
# import on 3.12+.

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..core.clock import Clock
from ..core.config import BookingCutoffConfig
from ..core.release_policy import ReleasePolicy
from ..courses.names import (
    COURSE_DISPLAY_NAMES,
    COURSE_SIGNUP_URLS,
    COURSE_TEE_SHEET_HOURS,
    TeeSheetHours,
    course_display_name,
    tee_sheet_hours,
)
from ..tenant.crypto import Keyring
from ..tenant.models import User, UserId, UserRole, UserStatus
from ..tenant.notify import EmailMessage, EmailSender, UserNotifier, render_invitation
from ..tenant.runner import AdapterFactory
from ..tenant.store import TenantStore
from . import admin_users as admin_users_view
from . import auth
from .auth import ForbiddenError
from .background import BackgroundJobs
from .course_info import ReleaseCycle, release_cycle
from .github_issues import GitHubIssues
from .oauth import (
    PROVIDERS,
    OAuthFlowError,
    OAuthProviders,
    OAuthProviderSettings,
    ProviderIdentity,
)
from .pages import STATUS_LABELS, register_page_routes
from .ranking_explainer import cutoff_text, ranking_example
from .security import CookiePolicy, security_headers, verify_csrf_token
from .services import ProbeLimits, RefreshCache
from .time_options import check_window, time_label, time_options, union_hours, with_values

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
STATIC_DIR = _HERE / "static"


def static_asset_versions(directory: Path) -> dict[str, str]:
    """Path relative to ``directory`` (``img/logo.png`` for a nested file) -> a 12-hex SHA-256
    prefix of its bytes. Templates link assets as ``static_url(name)`` =
    ``/static/<name>?v=<hash>``, so a deploy that changes a file changes its URL and no browser
    can keep using a stale copy (dev showed pre-#268 CSS for hours)."""
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _static_url_for(versions: Mapping[str, str]) -> Callable[[str], str]:
    def static_url(name: str) -> str:
        # Raises at render: a typo'd asset link fails loudly, never a silent 404.
        if name not in versions:
            raise KeyError(f"unknown static asset {name!r} (not in web/static)")
        return f"/static/{name}?v={versions[name]}"

    return static_url


_PROVIDER_NAMES = {"github": "GitHub", "google": "Google"}


def provider_display_name(provider: object) -> str:
    """A sign-in provider as people write it ("GitHub", not "Github"); unknown ones capitalized."""
    key = str(provider)
    return _PROVIDER_NAMES.get(key, key.capitalize())


class _RevalidatingStaticFiles(StaticFiles):
    """``Cache-Control: no-cache`` on every static response: a browser may keep the file but must
    revalidate it (a cheap 304 via the ETag). Without it, browsers cached heuristically from
    ``Last-Modified`` and kept serving old files after a deploy."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MIN_SESSION_SECRET_LEN = 32
_SESSION_COOKIE = "teetime_session"

__all__ = [
    "WEB_ENV_VARS",
    "ForbiddenError",
    "LoginRequiredError",
    "OAuthProviderSettings",
    "WebConfigError",
    "WebSettings",
    "create_app",
    "load_web_settings",
]


class WebConfigError(ValueError):
    """The site must not start half-configured (no session secret, no provider, …)."""


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Resolved from env at startup. Secrets arrive as env vars via ACA KV secretRefs (the
    env-var NAMES are the contract; ``test_every_web_env_var_is_wired_in_webapp_bicep`` pins them).
    Every secret-bearing field is ``repr=False``; the entrypoint registers them with the log
    filter (E7). ``public_base_url`` is the ONE setting the OAuth redirect URIs derive from
    (the free Container Apps hostname today; a custom domain later is a one-value change)."""

    public_base_url: str
    session_secret: str = field(repr=False)
    github: OAuthProviderSettings | None = None
    google: OAuthProviderSettings | None = None
    # The operator's sign-in email (operator decision: a settings value, never hard-coded).
    # It is IMPLICITLY invited so the first operator can sign in to an empty site.
    operator_email: str | None = None
    session_max_age_s: int = 12 * 60 * 60  # absolute lifetime, checked server-side
    refresh_ttl_s: int = 120
    max_refreshes_per_account_per_hour: int = 6
    max_probes_per_user_per_hour: int = 5
    max_probes_per_username_per_hour: int = 3
    max_probes_site_per_hour: int = 30
    max_accounts_per_course: int = 8
    # True in any env deployed with dryRun=true: the web refuses cancel (§7.8, round-1 SF2).
    dry_run: bool = True
    # Redirect any request to another host (www., the old *.azurecontainerapps.io name) to the
    # same path on ``public_base_url``. The OAuth state lives in the session cookie of the host
    # sign-in started on, so a second host breaks sign-in. On only where a custom domain is set.
    canonical_host_redirect: bool = False
    # For bug-report diagnostics (2026-09-29): TEETIME_ENV ("dev"/"prod") and TEETIME_BUILD (the
    # image tag, i.e. the git sha CI built). None when unset (local runs).
    environment: str | None = None
    build: str | None = None
    # Site reports -> anonymized issues in this repo (2026-09-29). Both or neither.
    github_issues_repo: str | None = None
    github_issues_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.github is None and self.google is None:
            raise WebConfigError(
                "at least one OAuth provider must be configured "
                "(OAUTH_GITHUB_CLIENT_ID/SECRET or OAUTH_GOOGLE_CLIENT_ID/SECRET)"
            )
        url = self.public_base_url.strip().rstrip("/")
        if not url.startswith("https://") or len(url) <= len("https://"):
            raise WebConfigError("TEETIME_PUBLIC_BASE_URL must be an https:// origin")
        object.__setattr__(self, "public_base_url", url)
        if len(self.session_secret) < _MIN_SESSION_SECRET_LEN:
            raise WebConfigError(
                f"WEB_SESSION_SECRET must be at least {_MIN_SESSION_SECRET_LEN} characters"
            )

    @property
    def enabled_providers(self) -> tuple[str, ...]:
        return tuple(p for p in PROVIDERS if self.provider(p) is not None)

    def provider(self, name: str) -> OAuthProviderSettings | None:
        if name == "github":
            return self.github
        if name == "google":
            return self.google
        return None

    def redirect_uri(self, provider: str) -> str:
        return f"{self.public_base_url}/auth/{provider}/callback"


# Env-var NAMES read by ``load_web_settings`` (never literal values in config). The tenant
# store / keyring / ACS names (``TENANT_COSMOS_*``, ``AZURE_CLIENT_ID``,
# ``TENANT_CREDS_KEYRING``, ``ACS_EMAIL_CONNECTION``) belong to THEIR loaders (MU-8b / MU-11 /
# MU-15a), which the ``teetime web`` entrypoint composes with these.
WEB_ENV_VARS: tuple[str, ...] = (
    "TEETIME_PUBLIC_BASE_URL",
    "WEB_SESSION_SECRET",
    "OAUTH_GITHUB_CLIENT_ID",
    "OAUTH_GITHUB_CLIENT_SECRET",
    "OAUTH_GOOGLE_CLIENT_ID",
    "OAUTH_GOOGLE_CLIENT_SECRET",
    "TEETIME_OPERATOR_EMAIL",
    "TEETIME_WEB_DRY_RUN",  # "true" (default) | "false"
    "TEETIME_CANONICAL_HOST_REDIRECT",  # "false" (default) | "true"
    "TEETIME_ENV",  # optional, bug-report diagnostics ("dev" | "prod")
    "TEETIME_BUILD",  # optional, bug-report diagnostics (the image tag = git sha)
    "GITHUB_ISSUES_REPO",  # optional, "owner/repo": site reports become issues there
    "GITHUB_ISSUES_TOKEN",  # optional secret, fine-grained, Issues read+write on that repo
)


def load_web_settings(env: Mapping[str, str] | None = None) -> WebSettings:
    """Read ``WEB_ENV_VARS``; FAIL-CLOSED on any missing secret (the site must not start
    half-configured, e.g. with no session secret). ``env`` defaults to ``os.environ``."""
    source: Mapping[str, str] = os.environ if env is None else env

    def value(name: str) -> str:
        return source.get(name, "").strip()

    def required(name: str) -> str:
        v = value(name)
        if not v:
            raise WebConfigError(f"{name} is required (unset or empty)")
        return v

    providers: dict[str, OAuthProviderSettings | None] = {}
    for name in PROVIDERS:
        id_key = f"OAUTH_{name.upper()}_CLIENT_ID"
        secret_key = f"OAUTH_{name.upper()}_CLIENT_SECRET"
        client_id, client_secret = value(id_key), value(secret_key)
        if bool(client_id) != bool(client_secret):
            missing = secret_key if client_id else id_key
            raise WebConfigError(
                f"{id_key} and {secret_key} must be set together ({missing} missing)"
            )
        providers[name] = (
            OAuthProviderSettings(client_id=client_id, client_secret=client_secret)
            if client_id
            else None
        )
    dry_run_raw = value("TEETIME_WEB_DRY_RUN").lower() or "true"
    if dry_run_raw not in ("true", "false"):
        raise WebConfigError("TEETIME_WEB_DRY_RUN must be 'true' or 'false'")
    canonical_raw = value("TEETIME_CANONICAL_HOST_REDIRECT").lower() or "false"
    if canonical_raw not in ("true", "false"):
        raise WebConfigError("TEETIME_CANONICAL_HOST_REDIRECT must be 'true' or 'false'")
    # Both or neither: a half-configured pair leaves filing off rather than failing.
    gh_issues = bool(value("GITHUB_ISSUES_REPO")) and bool(value("GITHUB_ISSUES_TOKEN"))
    return WebSettings(
        public_base_url=required("TEETIME_PUBLIC_BASE_URL"),
        session_secret=required("WEB_SESSION_SECRET"),
        github=providers["github"],
        google=providers["google"],
        operator_email=value("TEETIME_OPERATOR_EMAIL") or None,
        dry_run=dry_run_raw == "true",
        canonical_host_redirect=canonical_raw == "true",
        environment=value("TEETIME_ENV") or None,
        build=value("TEETIME_BUILD") or None,
        github_issues_repo=(value("GITHUB_ISSUES_REPO") or None) if gh_issues else None,
        github_issues_token=(value("GITHUB_ISSUES_TOKEN") or None) if gh_issues else None,
    )


class LoginRequiredError(Exception):
    """No (valid, unexpired) session: the handler redirects to ``/login``."""


class _SecurityHeadersMiddleware:
    """Adds ``security_headers()`` to EVERY http response (redirects, 403s, 404s, static)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in security_headers().items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ASCII control characters (C0 + DEL): never echoed into a Location header.
_CONTROL_CHARS = frozenset(map(chr, [*range(0x20), 0x7F]))


class _CanonicalHostMiddleware:
    """Redirects every http request whose ``Host`` is not ``origin``'s host to the same path and
    query on ``origin``: 301 for GET/HEAD, 308 otherwise (keeps the method and body). The target
    is always the configured origin, so a spoofed ``Host`` cannot steer it. ``/healthz`` is
    exempt so a platform probe addressed to any host keeps working."""

    def __init__(self, app: ASGIApp, *, origin: str) -> None:
        self.app = app
        self._origin = origin
        self._host = (urlsplit(origin).hostname or "").lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz" or self._on_host(scope):
            await self.app(scope, receive, send)
            return
        raw_path = scope.get("raw_path") or scope["path"].encode()
        target = self._origin + raw_path.decode("latin-1")
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        if any(c in _CONTROL_CHARS for c in target):
            # Defence in depth (upstream parsers reject these): never echo a control byte.
            target = self._origin + "/"
        status = 301 if scope["method"] in ("GET", "HEAD") else 308
        await RedirectResponse(target, status_code=status)(scope, receive, send)

    def _on_host(self, scope: Scope) -> bool:
        for name, value in scope["headers"]:
            if name == b"host":
                host: str = value.decode("latin-1").lower().split(":", 1)[0]
                return host == self._host
        return False


# Anyone with a Google or GitHub account can script the OAuth round trip, and each audit doc
# lives 400 days. The rejected_signin record counts every attempt; the audit trail (SF10) needs
# a subject only once per cooldown. In-process (per replica, reset on restart): it bounds growth,
# it is not a security boundary.
REJECTED_AUDIT_COOLDOWN = timedelta(hours=1)


class RejectionAuditThrottle:
    """At most one rejected-sign-in audit doc per (provider, subject) per cooldown.

    ``due`` asks; ``record`` is called only after the audit write succeeded, so a failed write
    never silences a subject. At ``MAX_TRACKED`` live subjects (a flood of distinct identities
    inside one cooldown) a NEW subject is not audited: memory stays bounded, and the
    rejected_signin records still count every attempt."""

    MAX_TRACKED = 1000

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], datetime] = {}

    def due(self, key: tuple[str, str], *, now: datetime) -> bool:
        last = self._last.get(key)
        if last is not None:
            return now - last >= REJECTED_AUDIT_COOLDOWN
        if len(self._last) >= self.MAX_TRACKED:
            self._last = {k: t for k, t in self._last.items() if now - t < REJECTED_AUDIT_COOLDOWN}
        return len(self._last) < self.MAX_TRACKED

    def record(self, key: tuple[str, str], *, now: datetime) -> None:
        self._last[key] = now


@dataclass(frozen=True, slots=True)
class _Ctx:
    """Everything the routes and dependencies share; built once per ``create_app``."""

    settings: WebSettings
    store: TenantStore
    clock: Clock
    oauth: OAuthProviders
    templates: Jinja2Templates
    # MU-13: the rule materializer's inputs, keyed by ``str(course_id)`` (as ``materialize_tick``).
    policies: Mapping[str, ReleasePolicy] = field(default_factory=dict)
    cutoff: BookingCutoffConfig = field(default_factory=BookingCutoffConfig)
    # MU-14: connect / refresh / cancel. ``None`` keyring or factory = those actions are
    # refused with a message (an app wired only for MU-12/13).
    keyring: Keyring | None = None
    adapter_factory: AdapterFactory | None = None
    notifier: UserNotifier | None = None
    email_sender: EmailSender | None = None  # invitations; None = not configured
    github_issues: GitHubIssues | None = None  # site reports -> public issues; None = off
    refresh_cache: RefreshCache = field(default_factory=lambda: RefreshCache(ttl_s=120))
    probe_limits: ProbeLimits = field(default_factory=ProbeLimits)
    # ``str(course_id)`` -> the name a person reads (default ``courses.names``); never a raw id.
    course_names: Mapping[str, str] = field(default_factory=dict)
    # ``str(course_id)`` -> the course's own booking site (default ``courses.names``), linked
    # from Connect a course as where to create the login the bot will use.
    course_signup_urls: Mapping[str, str] = field(default_factory=dict)
    # ``str(course_id)`` -> the course's tee-sheet span (default ``courses.names``): the time
    # pickers list only those hours and ``check_window`` refuses a window outside them.
    tee_sheet_hours: Mapping[str, TeeSheetHours] = field(default_factory=dict)
    # Sends that run AFTER the response (invite, resend, feedback); drained on shutdown.
    jobs: BackgroundJobs = field(default_factory=BackgroundJobs)
    rejection_audits: RejectionAuditThrottle = field(default_factory=RejectionAuditThrottle)

    def course_name(self, course_id: object) -> str:
        return course_display_name(str(course_id), self.course_names)

    def hours_of(self, course_id: object) -> TeeSheetHours:
        return tee_sheet_hours(str(course_id), self.tee_sheet_hours)

    def time_choices(self, course_ids: Sequence[object], values: Sequence[time] = ()) -> list[time]:
        """The picker's choices for a form over ``course_ids`` (their union: the ranked form has
        a course dropdown per row, and with script off the list cannot follow it), plus
        ``values`` already saved (an off-grid window stays selectable on its edit form)."""
        hours = union_hours(self.hours_of(c) for c in course_ids)
        return with_values(time_options(hours), *values)

    def check_window(self, course_id: object, earliest: time, latest: time) -> None:
        check_window(self.course_name(course_id), self.hours_of(course_id), earliest, latest)

    def release_cycle(self, course_id: object) -> ReleaseCycle | None:
        """The course's release cycle in words (``web/course_info.py``), None for a course with
        no hosted policy (nothing is shown rather than a guess)."""
        policy = self.policies.get(str(course_id))
        if policy is None:
            return None
        return release_cycle(
            policy, cutoff_text=cutoff_text(self.cutoff.days_before, self.cutoff.time_of_day)
        )

    def page(
        self, request: Request, name: str, context: dict[str, Any], *, status_code: int = 200
    ) -> Response:
        context.setdefault("csrf_token", request.session.get(auth.CSRF_KEY))
        return self.templates.TemplateResponse(request, name, context, status_code=status_code)

    def require_provider(self, provider: str) -> None:
        if not self.oauth.is_enabled(provider):
            raise HTTPException(status_code=404, detail="unknown sign-in provider")


_Dependency = Callable[..., Awaitable[Any]]


def _csrf_guard(ctx: _Ctx) -> Callable[[Request], Awaitable[None]]:
    async def csrf_guard(request: Request) -> None:
        """Double-submit check on every non-safe request (app-level dependency)."""
        if request.method in _SAFE_METHODS:
            return
        submitted: str | None = request.headers.get("x-csrf-token")
        if submitted is None:
            form = await request.form()
            candidate = form.get("csrf_token")
            submitted = candidate if isinstance(candidate, str) else None
        if not verify_csrf_token(request.session.get(auth.CSRF_KEY), submitted):
            raise ForbiddenError("missing or invalid CSRF token")

    return csrf_guard


def _current_user(ctx: _Ctx) -> Callable[[Request], Awaitable[User]]:
    async def current_user(request: Request) -> User:
        """Per-request: parse the session, enforce the absolute lifetime, RE-READ the user
        from the store so a disable takes effect on the very next request (SF10)."""
        ident = auth.read_session(request.session)
        if ident is None:
            raise LoginRequiredError
        now = ctx.clock.now_utc()
        if auth.is_expired(ident, now=now, max_age_s=ctx.settings.session_max_age_s):
            request.session.clear()
            raise LoginRequiredError
        user = await ctx.store.get_user_by_subject(ident.provider, ident.subject)
        if user is None or user.id != ident.user_id or user.status is not UserStatus.ACTIVE:
            request.session.clear()
            raise ForbiddenError("this account is disabled")
        return user

    return current_user


def _require_operator(ctx: _Ctx, current_user: _Dependency) -> _Dependency:
    async def require_operator(user: Annotated[User, Depends(current_user)]) -> User:
        if not auth.is_operator(user, operator_email=ctx.settings.operator_email):
            raise ForbiddenError("operator only")
        return user

    return require_operator


def _install_error_pages(app: FastAPI, ctx: _Ctx) -> None:
    async def on_login_required(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/login", status_code=303)

    async def on_forbidden(request: Request, exc: Exception) -> Response:
        reason = exc.reason if isinstance(exc, ForbiddenError) else "forbidden"
        return ctx.page(request, "forbidden.html", {"reason": reason}, status_code=403)

    app.add_exception_handler(LoginRequiredError, on_login_required)
    app.add_exception_handler(ForbiddenError, on_forbidden)


def _register_public_routes(app: FastAPI, ctx: _Ctx) -> None:
    @app.get("/healthz", response_class=JSONResponse)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if auth.read_session(request.session) is not None:
            return RedirectResponse("/", status_code=303)
        return ctx.page(request, "login.html", {"providers": ctx.oauth.names})

    @app.get("/login/{provider}")
    async def login_start(request: Request, provider: str) -> Response:
        ctx.require_provider(provider)
        redirect_uri = ctx.settings.redirect_uri(provider)
        return await ctx.oauth.start(request, provider, redirect_uri=redirect_uri)

    @app.get("/auth/{provider}/callback")
    async def login_callback(request: Request, provider: str) -> Response:
        ctx.require_provider(provider)
        try:
            identity = await ctx.oauth.finish(request, provider)
        except OAuthFlowError as e:
            # The reason is a path + status or a malformed-profile note: no PII.
            log.warning("sign-in failed provider=%s: %s", provider, e)
            raise HTTPException(status_code=400, detail="sign-in could not be completed") from e
        return await _complete_signin(ctx, request, identity)


async def _complete_signin(ctx: _Ctx, request: Request, identity: ProviderIdentity) -> Response:
    """Invite-only binding (§8.3): a non-invited or disabled identity gets the 403 page, an
    ``audit`` doc, and NO session; an active user gets a rotated session."""
    now = ctx.clock.now_utc()
    user = await auth.resolve_identity(
        ctx.store, identity=identity, operator_email=ctx.settings.operator_email, now=now
    )
    detail = {
        "provider": identity.provider,
        "subject": identity.subject,
        "verified_email_count": len(identity.verified_emails),
    }
    if user is None or user.status is not UserStatus.ACTIVE:
        reason = "not_invited" if user is None else "disabled"
        log.info("signin rejected provider=%s reason=%s", identity.provider, reason)
        if user is None:
            await _remember_rejected_signin(ctx, identity, now=now)
        key = (identity.provider, identity.subject)
        if ctx.rejection_audits.due(key, now=now) and await _best_effort_store_write(
            ctx.store.append_audit(
                user_id=None if user is None else user.id,
                action=f"signin_rejected_{reason}",
                row_id=None,
                detail=detail,
                at=now,
            ),
            what="a rejected sign-in's audit entry",
        ):
            ctx.rejection_audits.record(key, now=now)
        request.session.clear()
        raise ForbiddenError("not invited" if user is None else "this account is disabled")
    auth.establish_session(request.session, user=user, identity=identity, now=now)
    log.info("signin ok provider=%s user_id=%s", identity.provider, user.id)
    return RedirectResponse("/", status_code=303)


# The uninvited-sign-in record is written on an UNAUTHENTICATED path, so what a provider profile
# can put into it is capped, and the write is bounded in time.
REJECTED_NAME_MAX_LEN = 200
REJECTED_EMAILS_MAX = 5
REJECTED_SIGNIN_WRITE_TIMEOUT_S = 5.0


async def _best_effort_store_write(write: Awaitable[object], *, what: str) -> bool:
    """A write on the UNAUTHENTICATED 403 path: bounded in time, and a failure or a hung store
    is logged and never changes the 403. True iff it succeeded."""
    try:
        await asyncio.wait_for(write, timeout=REJECTED_SIGNIN_WRITE_TIMEOUT_S)
    except Exception:  # TimeoutError included
        log.warning("could not record %s", what, exc_info=True)
        return False
    return True


async def _remember_rejected_signin(
    ctx: _Ctx, identity: ProviderIdentity, *, now: datetime
) -> None:
    """Best-effort: an uninvited identity (verified emails only, capped) for the operator's
    ``/admin/users`` list."""
    await _best_effort_store_write(
        ctx.store.record_rejected_signin(
            provider=identity.provider,
            subject=identity.subject,
            emails=tuple(identity.verified_emails[:REJECTED_EMAILS_MAX]),
            display_name=identity.display_name[:REJECTED_NAME_MAX_LEN],
            at=now,
        ),
        what="an uninvited sign-in",
    )


def _register_user_routes(app: FastAPI, ctx: _Ctx, *, current_user: _Dependency) -> None:
    require_operator = _require_operator(ctx, current_user)
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias
    Operator = Annotated[User, Depends(require_operator)]  # noqa: N806 — type alias

    @app.post("/logout")
    async def logout(request: Request, user: CurrentUser) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request, operator: Operator) -> Response:
        notice = _ADMIN_NOTICES.get(request.query_params.get("notice", ""))
        users = await admin_users_view.user_overviews(ctx.store, clock=ctx.clock)
        attempts = await admin_users_view.uninvited_attempts(
            ctx.store, users=[o.user for o in users], clock=ctx.clock
        )
        return ctx.page(
            request,
            "admin_users.html",
            {
                "user": operator,
                "is_operator": True,
                "notice": notice,
                "status_labels": STATUS_LABELS,
                "users": users,
                "attempts": attempts,
            },
        )

    @app.get("/admin/users/{id}", response_class=HTMLResponse)
    async def admin_user(request: Request, operator: Operator, id: str) -> Response:
        try:
            user_id = UserId(UUID(id))
        except ValueError as e:
            raise HTTPException(status_code=404) from e
        detail = await admin_users_view.user_detail(
            ctx.store, user_id=user_id, clock=ctx.clock, policies=ctx.policies
        )
        if detail is None:
            raise HTTPException(status_code=404)
        return ctx.page(
            request,
            "admin_user.html",
            {
                "user": operator,
                "is_operator": True,
                "person": detail,
                "status_labels": STATUS_LABELS,
            },
        )

    @app.post("/admin/users")
    async def admin_users_action(request: Request, operator: Operator) -> Response:
        form = await request.form()
        action = _form_str(form, "action")
        now = ctx.clock.now_utc()
        if action == "invite":
            return await _admin_invite(ctx, operator, form, now=now)
        if action == "resend":
            return await _admin_resend_invite(ctx, operator, form, now=now)
        if action == "uninvite":
            return await _admin_uninvite(ctx, operator, form, now=now)
        if action in ("disable", "enable"):
            return await _admin_set_status(ctx, operator, form, enable=action == "enable", now=now)
        raise HTTPException(status_code=400, detail="unknown action")


# Fixed strings keyed by the `notice` query param: nothing user-supplied is ever reflected.
# The invitation email is sent after the response (``BackgroundJobs``), so the notice cannot
# know the outcome; the audit entry records it.
_ADMIN_NOTICES = {
    "invited": (
        "Invite created; the invitation email is on its way. It binds on their first sign-in "
        "with Google. If it doesn't arrive within a few minutes, use Resend invite."
    ),
    "uninvited": "Invite removed: that address can no longer sign in.",
    "resent": (
        "Invitation email is on its way. If it still doesn't arrive, tell them to sign in with "
        "Google using that address."
    ),
    "disabled": "User disabled. Their session is rejected on their next request.",
    "enabled": "User enabled.",
    "already_invited": (
        "That address is already invited, so no second invite was made. Use Resend invite to "
        "email it again."
    ),
    "already_member": (
        "That address already has an account, so no invite was made. If it is disabled, use Enable."
    ),
}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


async def _admin_invite(ctx: _Ctx, operator: User, form: FormData, *, now: datetime) -> Response:
    """Create an INVITED row. The subject binds on first sign-in, matched only against a
    provider-verified email (auth.resolve_identity); the store never sees a self-signup."""
    email = _form_str(form, "email").casefold()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="invalid email")
    try:
        role = UserRole(_form_str(form, "role") or UserRole.MEMBER.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid role") from e
    # One row per address: a second INVITED row would outlive an Uninvite of the first (the
    # earliest-sorting row binds). Two concurrent submits can still both pass this check.
    same = [u for u in await ctx.store.list_users() if u.email.casefold() == email]
    if same:
        notice = (
            "already_invited"
            if all(u.status is UserStatus.INVITED for u in same)
            else "already_member"
        )
        return RedirectResponse(f"/admin/users?notice={notice}", status_code=303)
    invited = User(
        id=UserId(uuid4()),
        oauth_provider="",  # unknown until the invitee signs in; bind_invited_user sets it
        oauth_subject=None,
        email=email,
        display_name=email.split("@", 1)[0],
        role=role,
        status=UserStatus.INVITED,
    )
    await ctx.store.upsert_user(invited)  # BEFORE responding: the invite itself is never lost

    async def email_then_audit() -> None:
        emailed = await _send_invitation(ctx, email)
        await ctx.store.append_audit(
            user_id=operator.id,
            action="admin_invite",
            row_id=None,
            detail={"invited_user_id": str(invited.id), "role": role.value, "sent": emailed},
            at=now,
        )

    ctx.jobs.spawn(email_then_audit(), name="admin_invite")
    return RedirectResponse("/admin/users?notice=invited", status_code=303)


# Bounds the background send. ACS may already have ACCEPTED the message when this fires (the
# client then polls delivery status), so a timeout reads "couldn't be confirmed", not "failed".
INVITE_EMAIL_TIMEOUT_S = 20.0


async def _send_invitation(ctx: _Ctx, email: str) -> bool:
    """Best-effort: True iff the invitation was accepted for delivery. The sender returns a
    result rather than raising; a raise or a hang is logged, never a failed invite."""
    if ctx.email_sender is None:
        log.warning("invitation not emailed: email is not configured")
        return False
    rendered = render_invitation(email, site_url=ctx.settings.public_base_url)
    message = EmailMessage(to=email, subject=rendered.subject, body=rendered.body)
    try:
        result = await asyncio.wait_for(
            ctx.email_sender.send(message), timeout=INVITE_EMAIL_TIMEOUT_S
        )
    except Exception:  # TimeoutError included
        log.warning("invitation email failed", exc_info=True)
        return False
    if not result.ok:
        log.warning(
            "invitation email not delivered (status=%s error=%s)", result.status, result.error
        )
    return result.ok


async def _admin_uninvite(ctx: _Ctx, operator: User, form: FormData, *, now: datetime) -> Response:
    """Delete a still-INVITED (never signed in) user, so the address can no longer sign in. A
    signed-in user has an account: that is Disable, not uninvite (400). A first sign-in racing
    this wins (the store's delete is conditional)."""
    try:
        user_id = UserId(UUID(_form_str(form, "user_id")))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid user id") from e
    target = await ctx.store.get_user_unscoped(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="no such user")
    if target.status is not UserStatus.INVITED or not await ctx.store.delete_invited_user(user_id):
        raise HTTPException(
            status_code=400, detail="that person has already signed in; use Disable instead"
        )
    await ctx.store.append_audit(
        user_id=operator.id,
        action="admin_uninvite",
        row_id=None,
        detail={"uninvited_user_id": str(user_id)},
        at=now,
    )
    return RedirectResponse("/admin/users?notice=uninvited", status_code=303)


async def _admin_resend_invite(
    ctx: _Ctx, operator: User, form: FormData, *, now: datetime
) -> Response:
    """Email the invitation again to a user who has not signed in yet (status INVITED)."""
    try:
        user_id = UserId(UUID(_form_str(form, "user_id")))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid user id") from e
    target = await ctx.store.get_user_unscoped(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="no such user")
    if target.status is not UserStatus.INVITED:
        raise HTTPException(status_code=400, detail="that person has already signed in")

    async def email_then_audit() -> None:
        emailed = await _send_invitation(ctx, target.email)
        await ctx.store.append_audit(
            user_id=operator.id,
            action="admin_resend_invite",
            row_id=None,
            detail={"invited_user_id": str(target.id), "sent": emailed},
            at=now,
        )

    ctx.jobs.spawn(email_then_audit(), name="admin_resend_invite")
    return RedirectResponse("/admin/users?notice=resent", status_code=303)


async def _admin_set_status(
    ctx: _Ctx, operator: User, form: FormData, *, enable: bool, now: datetime
) -> Response:
    """Disable/enable by the bound (provider, subject): the identity is what the audit log, the
    user's own dashboard and the People table's per-row buttons post."""
    provider, subject = _form_str(form, "provider"), _form_str(form, "subject")
    if not provider or not subject:
        raise HTTPException(status_code=400, detail="provider and subject are required")
    target = await ctx.store.get_user_by_subject(provider, subject)
    if target is None:
        raise HTTPException(status_code=404, detail="no user is bound to that identity")
    if not enable and target.id == operator.id:
        raise HTTPException(status_code=400, detail="you cannot disable your own account")
    status = UserStatus.ACTIVE if enable else UserStatus.DISABLED
    await ctx.store.upsert_user(replace(target, status=status))
    await ctx.store.append_audit(
        user_id=operator.id,
        action="admin_enable" if enable else "admin_disable",
        row_id=None,
        detail={"provider": provider, "subject": subject, "target_user_id": str(target.id)},
        at=now,
    )
    notice = "enabled" if enable else "disabled"
    return RedirectResponse(f"/admin/users?notice={notice}", status_code=303)


# ACA sends SIGTERM and waits 30 s by default before SIGKILL; stay inside it.
SHUTDOWN_DRAIN_TIMEOUT_S = 20.0


def create_app(
    settings: WebSettings,
    *,
    store: TenantStore,
    clock: Clock,
    keyring: Keyring | None = None,
    notifier: UserNotifier | None = None,
    email_sender: EmailSender | None = None,
    github_issues: GitHubIssues | None = None,
    policies: Mapping[str, ReleasePolicy] | None = None,
    cutoff: BookingCutoffConfig | None = None,
    adapter_factory: AdapterFactory | None = None,
    course_names: Mapping[str, str] | None = None,
    course_signup_urls: Mapping[str, str] | None = None,
    tee_sheet_hours: Mapping[str, TeeSheetHours] | None = None,
) -> FastAPI:
    """Build the ASGI app. MU-14 (connect, refresh, cancel) needs ``keyring`` (decrypt /
    encrypt the course passwords) and ``adapter_factory`` (one throwaway ForeUP adapter per
    live login); without either those actions are refused with a message. ``notifier`` emails
    the user after a cancel (best-effort); ``email_sender`` sends invitations (best-effort; the
    invite is created either way). ``policies`` (``str(course_id)`` ->
    ``ReleasePolicy``) and ``cutoff`` feed the synchronous rule materialize (MU-13, §7.7): a
    course with no policy cannot take a standing rule (one-off dates still work), and the
    policies' courses are the ones an account can be connected to (MU-14). ``course_names``
    (``str(course_id)`` -> display name, default ``courses.names.COURSE_DISPLAY_NAMES``) is what
    every page shows instead of a course id; an unknown id falls back to itself.
    ``course_signup_urls`` (default ``courses.names.COURSE_SIGNUP_URLS``) is where Connect a
    course sends a person to create the course login; a course without one gets no link.
    ``tee_sheet_hours`` (default ``courses.names.COURSE_TEE_SHEET_HOURS``) bounds the booking
    forms' time pickers per course and the server's window check; a course without an entry
    gets the whole day."""
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    ctx = _Ctx(
        settings=settings,
        store=store,
        clock=clock,
        oauth=OAuthProviders({p: s for p in PROVIDERS if (s := settings.provider(p)) is not None}),
        templates=templates,
        policies=dict(policies or {}),
        cutoff=cutoff if cutoff is not None else BookingCutoffConfig(),
        keyring=keyring,
        adapter_factory=adapter_factory,
        notifier=notifier,
        email_sender=email_sender,
        github_issues=github_issues,
        refresh_cache=RefreshCache(ttl_s=settings.refresh_ttl_s),
        probe_limits=ProbeLimits(
            per_user_per_hour=settings.max_probes_per_user_per_hour,
            per_username_per_hour=settings.max_probes_per_username_per_hour,
            site_per_hour=settings.max_probes_site_per_hour,
            refreshes_per_account_per_hour=settings.max_refreshes_per_account_per_hour,
        ),
        course_names=(
            {str(cid): name for cid, name in COURSE_DISPLAY_NAMES.items()}
            if course_names is None
            else dict(course_names)
        ),
        course_signup_urls=(
            {str(cid): url for cid, url in COURSE_SIGNUP_URLS.items()}
            if course_signup_urls is None
            else dict(course_signup_urls)
        ),
        tee_sheet_hours=(
            {str(cid): h for cid, h in COURSE_TEE_SHEET_HOURS.items()}
            if tee_sheet_hours is None
            else dict(tee_sheet_hours)
        ),
    )
    templates.env.filters["course_name"] = ctx.course_name
    templates.env.filters["course_signup_url"] = lambda cid: ctx.course_signup_urls.get(str(cid))
    templates.env.filters["release_cycle"] = ctx.release_cycle
    templates.env.filters["provider_name"] = provider_display_name
    templates.env.filters["time_label"] = time_label
    templates.env.globals["time_choices"] = ctx.time_choices
    templates.env.globals["tee_hours"] = ctx.hours_of
    templates.env.globals["static_url"] = _static_url_for(static_asset_versions(STATIC_DIR))
    templates.env.globals["ranking_example"] = ranking_example()
    templates.env.globals["booking_cutoff_text"] = cutoff_text(
        ctx.cutoff.days_before, ctx.cutoff.time_of_day
    )
    cookie = CookiePolicy()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # Let in-flight sends (invites, reports) finish, bounded, before the process exits.
        await ctx.jobs.drain(timeout_s=SHUTDOWN_DRAIN_TIMEOUT_S)

    app = FastAPI(
        title="TeeTimeBooker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(_csrf_guard(ctx))],
        lifespan=lifespan,
    )
    app.state.background_jobs = ctx.jobs
    if settings.canonical_host_redirect:
        # Added before the security headers so the redirect carries them too.
        app.add_middleware(_CanonicalHostMiddleware, origin=settings.public_base_url)
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=_SESSION_COOKIE,
        max_age=settings.session_max_age_s,
        path=cookie.path,
        same_site=cookie.same_site,
        https_only=cookie.secure,
    )
    app.mount("/static", _RevalidatingStaticFiles(directory=str(STATIC_DIR)), name="static")
    _install_error_pages(app, ctx)
    _register_public_routes(app, ctx)
    current_user = _current_user(ctx)
    _register_user_routes(app, ctx, current_user=current_user)
    register_page_routes(app, ctx, current_user=current_user)
    return app
