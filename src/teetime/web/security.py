"""Web security primitives (MULTIUSER_PLAN §8.3, §8.4, §9.1). Framework-free and unit-testable.

STUB — implemented in MULTIUSER_PLAN MU-12 (CSRF, session policy, headers) and MU-14 (probe
limiter).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..core.models import CourseId
from ..tenant.models import UserId
from ..tenant.store import TenantStore

_MU12 = "MULTIUSER_PLAN.md MU-12"
_MU14 = "MULTIUSER_PLAN.md MU-14"


@dataclass(frozen=True, slots=True)
class CookiePolicy:
    """The session cookie flags. A test pins these exact values."""

    secure: bool = True
    http_only: bool = True
    same_site: str = "lax"
    path: str = "/"


def issue_csrf_token() -> str:
    """A 32-byte URL-safe random token, stored in the session and rendered into forms / sent by
    HTMX as ``X-CSRF-Token``."""
    raise NotImplementedError(_MU12)


def verify_csrf_token(session_token: str | None, submitted: str | None) -> bool:
    """Constant-time comparison; False if either side is missing."""
    raise NotImplementedError(_MU12)


def security_headers() -> dict[str, str]:
    """CSP (``default-src 'self'``; no inline script), HSTS, ``X-Frame-Options: DENY``,
    ``Referrer-Policy: same-origin``, ``X-Content-Type-Options: nosniff``."""
    raise NotImplementedError(_MU12)


def username_hash(username: str) -> str:
    """SHA-256 prefix of the normalized username, for ``probe`` docs (no raw PII stored)."""
    raise NotImplementedError(_MU14)


async def probe_allowed(
    store: TenantStore,
    *,
    user_id: UserId,
    course_id: CourseId,
    username: str,
    now: datetime,
) -> bool:
    """DB-backed login-probe limiter (§8.4): <= 5/user/h, <= 3/username/h, <= 30 site-wide/h,
    and a 15-min lockout after 2 consecutive failures for a username. Checked BEFORE any
    ForeUP call; a failed probe is never retried automatically (PLAN §8.1/§12)."""
    raise NotImplementedError(_MU14)
