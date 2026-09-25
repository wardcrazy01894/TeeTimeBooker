"""App factory + settings (MULTIUSER_PLAN §8.1, §8.3).

One scale-to-zero Azure Container App (``teetime web``: uvicorn, max 1 replica) serves the pages
and the HTMX endpoints. The factory returns the ASGI app. It is typed ``object`` in this stub
because the framework dependency does not exist yet (MU-12 narrows it to ``fastapi.FastAPI``).

STUB — implemented in MULTIUSER_PLAN MU-12.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.clock import Clock
from ..tenant.crypto import Keyring
from ..tenant.notify import UserNotifier
from ..tenant.store import TenantStore

_MU12 = "MULTIUSER_PLAN.md MU-12"


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Resolved from env at startup. Secrets arrive as env vars via ACA KV secretRefs (the
    env-var NAMES are the contract; ``test_tenant_env_refs_wired_in_compute_bicep`` pins them).
    Every secret-bearing field is ``repr=False`` and registered with the log filter (E7)."""

    public_base_url: str
    oauth_provider: str  # "github" | "google" (§13 Q2)
    oauth_client_id: str
    oauth_client_secret: str = field(repr=False)
    session_secret: str = field(repr=False)
    session_max_age_s: int = 12 * 60 * 60  # absolute lifetime, checked server-side
    refresh_ttl_s: int = 120
    max_refreshes_per_account_per_hour: int = 6
    max_probes_per_user_per_hour: int = 5
    max_probes_per_username_per_hour: int = 3
    max_probes_site_per_hour: int = 30
    max_accounts_per_course: int = 8
    # True in any env deployed with dryRun=true: the web refuses cancel (§7.8, round-1 SF2).
    dry_run: bool = True


# Env-var NAMES read by ``load_web_settings`` (never literal values in config).
WEB_ENV_VARS: tuple[str, ...] = (
    "TEETIME_PUBLIC_BASE_URL",
    "TEETIME_OAUTH_PROVIDER",
    "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET",
    "WEB_SESSION_SECRET",
    "TENANT_COSMOS_ENDPOINT",  # plain value; Cosmos auth is MI + data-plane RBAC (no DB secret)
    "TENANT_COSMOS_DATABASE",  # "prod" | "dev"
    "AZURE_CLIENT_ID",  # the user-assigned MI client id
    "TENANT_CREDS_KEYRING",
    "ACS_EMAIL_CONNECTION",
)


def load_web_settings() -> WebSettings:
    """Read ``WEB_ENV_VARS``; FAIL-CLOSED on any missing secret (the site must not start
    half-configured, e.g. with no session secret)."""
    raise NotImplementedError(_MU12)


def create_app(
    settings: WebSettings,
    *,
    store: TenantStore,
    keyring: Keyring,
    notifier: UserNotifier,
    clock: Clock,
) -> object:
    """Build the ASGI app: session middleware (Secure, HttpOnly, SameSite=Lax), CSRF
    verification on every non-GET, security headers (CSP without inline script, HSTS, DENY
    framing), OAuth routes, and the ``routes.ROUTES`` table bound to ``services`` functions.
    ``install_log_redaction()`` runs right after logging is configured (root CLAUDE.md
    ordering rule)."""
    raise NotImplementedError(_MU12)
