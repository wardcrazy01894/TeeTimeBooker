"""Web security primitives (MULTIUSER_PLAN §8.3, §8.4, §9.1). Framework-free and unit-testable.

CSRF, session-cookie policy and the security headers are implemented here (MU-12); the
DB-backed login-probe limiter is MU-14 and still a stub.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..core.models import CourseId
from ..tenant.models import UserId
from ..tenant.store import TenantStore

_MU14 = "MULTIUSER_PLAN.md MU-14"

# One year, subdomains included: the site only ever lives on an HTTPS hostname (§8.1), so a
# browser that has seen us once never tries plaintext again.
_HSTS = "max-age=31536000; includeSubDomains"

# No inline script or style anywhere (§8.3 / §9.1 XSS row): every page loads only same-origin
# assets, forms only post to us, and nothing may frame us (`frame-ancestors` is the CSP twin of
# `X-Frame-Options: DENY`; both are sent because older UAs honour only one).
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)


@dataclass(frozen=True, slots=True)
class CookiePolicy:
    """The session cookie flags. A test pins these exact values."""

    secure: bool = True
    # Documents a Starlette-enforced invariant, NOT a knob: SessionMiddleware always sets
    # HttpOnly and takes no parameter for it, so flipping this to False changes nothing.
    http_only: bool = True
    same_site: Literal["lax", "strict", "none"] = "lax"
    path: str = "/"


def issue_csrf_token() -> str:
    """A 32-byte URL-safe random token, stored in the session and rendered into forms / sent by
    HTMX as ``X-CSRF-Token``."""
    return secrets.token_urlsafe(32)


def verify_csrf_token(session_token: str | None, submitted: str | None) -> bool:
    """Constant-time comparison; False if either side is missing."""
    if not session_token or not submitted:
        return False
    return hmac.compare_digest(session_token.encode(), submitted.encode())


def security_headers() -> dict[str, str]:
    """CSP (``default-src 'self'``; no inline script), HSTS, ``X-Frame-Options: DENY``,
    ``Referrer-Policy: same-origin``, ``X-Content-Type-Options: nosniff``."""
    return {
        "Content-Security-Policy": _CSP,
        "Strict-Transport-Security": _HSTS,
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
    }


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
