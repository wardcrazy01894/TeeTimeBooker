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

import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

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
from ..tenant.crypto import Keyring
from ..tenant.models import User, UserId, UserRole, UserStatus
from ..tenant.notify import UserNotifier
from ..tenant.runner import AdapterFactory
from ..tenant.store import TenantStore
from . import auth
from .oauth import (
    PROVIDERS,
    OAuthFlowError,
    OAuthProviders,
    OAuthProviderSettings,
    ProviderIdentity,
)
from .pages import register_page_routes
from .security import CookiePolicy, security_headers, verify_csrf_token
from .services import ProbeLimits, RefreshCache

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
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
    env-var NAMES are the contract; ``test_tenant_env_refs_wired_in_compute_bicep`` pins them).
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
    return WebSettings(
        public_base_url=required("TEETIME_PUBLIC_BASE_URL"),
        session_secret=required("WEB_SESSION_SECRET"),
        github=providers["github"],
        google=providers["google"],
        operator_email=value("TEETIME_OPERATOR_EMAIL") or None,
        dry_run=dry_run_raw == "true",
    )


class LoginRequiredError(Exception):
    """No (valid, unexpired) session: the handler redirects to ``/login``."""


class ForbiddenError(Exception):
    """Signed in (or just signed in) but not allowed: rendered as the 403 page."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    refresh_cache: RefreshCache = field(default_factory=lambda: RefreshCache(ttl_s=120))
    probe_limits: ProbeLimits = field(default_factory=ProbeLimits)

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
        action = "signin_rejected_not_invited" if user is None else "signin_rejected_disabled"
        await ctx.store.append_audit(
            user_id=None if user is None else user.id,
            action=action,
            row_id=None,
            detail=detail,
            at=now,
        )
        request.session.clear()
        raise ForbiddenError("not invited" if user is None else "this account is disabled")
    auth.establish_session(request.session, user=user, identity=identity, now=now)
    log.info("signin ok provider=%s user_id=%s", identity.provider, user.id)
    return RedirectResponse("/", status_code=303)


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
        return ctx.page(request, "admin_users.html", {"user": operator, "notice": notice})

    @app.post("/admin/users")
    async def admin_users_action(request: Request, operator: Operator) -> Response:
        form = await request.form()
        action = _form_str(form, "action")
        now = ctx.clock.now_utc()
        if action == "invite":
            return await _admin_invite(ctx, operator, form, now=now)
        if action in ("disable", "enable"):
            return await _admin_set_status(ctx, operator, form, enable=action == "enable", now=now)
        raise HTTPException(status_code=400, detail="unknown action")


# Fixed strings keyed by the `notice` query param: nothing user-supplied is ever reflected.
_ADMIN_NOTICES = {
    "invited": "Invite created. It binds on that person's first sign-in.",
    "disabled": "User disabled. Their session is rejected on their next request.",
    "enabled": "User enabled.",
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
    invited = User(
        id=UserId(uuid4()),
        oauth_provider="",  # unknown until the invitee signs in; bind_invited_user sets it
        oauth_subject=None,
        email=email,
        display_name=email.split("@", 1)[0],
        role=role,
        status=UserStatus.INVITED,
    )
    await ctx.store.upsert_user(invited)
    await ctx.store.append_audit(
        user_id=operator.id,
        action="admin_invite",
        row_id=None,
        detail={"invited_user_id": str(invited.id), "role": role.value},
        at=now,
    )
    return RedirectResponse("/admin/users?notice=invited", status_code=303)


async def _admin_set_status(
    ctx: _Ctx, operator: User, form: FormData, *, enable: bool, now: datetime
) -> Response:
    """Disable/enable by the bound (provider, subject) — the TenantStore Protocol has no user
    listing or email lookup (MU-13's `list_users` owns that), and the identity is what the
    audit log and the user's own dashboard show."""
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
    return RedirectResponse(f"/admin/users?notice={status.value}", status_code=303)


def create_app(
    settings: WebSettings,
    *,
    store: TenantStore,
    clock: Clock,
    keyring: Keyring | None = None,
    notifier: UserNotifier | None = None,
    policies: Mapping[str, ReleasePolicy] | None = None,
    cutoff: BookingCutoffConfig | None = None,
    adapter_factory: AdapterFactory | None = None,
) -> FastAPI:
    """Build the ASGI app. MU-14 (connect, refresh, cancel) needs ``keyring`` (decrypt /
    encrypt the course passwords) and ``adapter_factory`` (one throwaway ForeUP adapter per
    live login); without either those actions are refused with a message. ``notifier`` emails
    the user after a cancel (best-effort). ``policies`` (``str(course_id)`` ->
    ``ReleasePolicy``) and ``cutoff`` feed the synchronous rule materialize (MU-13, §7.7): a
    course with no policy cannot take a standing rule (one-off dates still work), and the
    policies' courses are the ones an account can be connected to (MU-14)."""
    ctx = _Ctx(
        settings=settings,
        store=store,
        clock=clock,
        oauth=OAuthProviders({p: s for p in PROVIDERS if (s := settings.provider(p)) is not None}),
        templates=Jinja2Templates(directory=str(_HERE / "templates")),
        policies=dict(policies or {}),
        cutoff=cutoff if cutoff is not None else BookingCutoffConfig(),
        keyring=keyring,
        adapter_factory=adapter_factory,
        notifier=notifier,
        refresh_cache=RefreshCache(ttl_s=settings.refresh_ttl_s),
        probe_limits=ProbeLimits(
            per_user_per_hour=settings.max_probes_per_user_per_hour,
            per_username_per_hour=settings.max_probes_per_username_per_hour,
            site_per_hour=settings.max_probes_site_per_hour,
            refreshes_per_account_per_hour=settings.max_refreshes_per_account_per_hour,
        ),
    )
    cookie = CookiePolicy()
    app = FastAPI(
        title="TeeTimeBooker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(_csrf_guard(ctx))],
    )
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
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    _install_error_pages(app, ctx)
    _register_public_routes(app, ctx)
    current_user = _current_user(ctx)
    _register_user_routes(app, ctx, current_user=current_user)
    register_page_routes(app, ctx, current_user=current_user)
    return app
