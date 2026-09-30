"""Web security primitives (MULTIUSER_PLAN §8.3, §8.4, §9.1). Framework-free and unit-testable.

CSRF, session-cookie policy and the security headers (MU-12). The login-probe limiter lives in
``web/services.py`` (``ProbeLimits``, ``probe_username_hash``, MU-14).
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

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
    """A 32-byte URL-safe random token, stored in the session and rendered into every form as the
    hidden ``csrf_token`` field."""
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
