"""Route contract table (MULTIUSER_PLAN §8.2). Handlers are thin and delegate to ``services``.

``ROUTES`` is DATA: the single place the path/auth/CSRF policy is declared. MU-12/13 bind it to
the framework, and a test asserts every non-GET route has ``csrf=True`` and every ``user`` route
has an IDOR test (``test_route_rejects_other_users_row``).

The MU-12 rows are BOUND (``web/app.py``; ``tests/web/test_web_app.py`` pins that every MU-12
row exists in the app with its method and that ``auth=NONE`` rows are exactly the public
allowlist). The MU-13/MU-14 rows are still contract-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AuthLevel(StrEnum):
    NONE = "none"
    USER = "user"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class RouteSpec:
    method: str
    path: str
    handler: str  # name of the web.services function it delegates to
    auth: AuthLevel
    csrf: bool
    milestone: str


ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec("GET", "/healthz", "healthz", AuthLevel.NONE, False, "MU-12"),
    RouteSpec("GET", "/login", "login_page", AuthLevel.NONE, False, "MU-12"),
    RouteSpec("GET", "/login/{provider}", "login_start", AuthLevel.NONE, False, "MU-12"),
    RouteSpec("GET", "/auth/{provider}/callback", "login_callback", AuthLevel.NONE, False, "MU-12"),
    RouteSpec("POST", "/logout", "logout", AuthLevel.USER, True, "MU-12"),
    # MU-12 serves a placeholder page here; MU-13 replaces it with the real dashboard.
    RouteSpec("GET", "/", "dashboard", AuthLevel.USER, False, "MU-12"),
    RouteSpec("GET", "/accounts", "list_accounts", AuthLevel.USER, False, "MU-14"),
    RouteSpec("POST", "/accounts/connect", "connect_account", AuthLevel.USER, True, "MU-14"),
    RouteSpec("POST", "/accounts/{id}/reverify", "reverify_account", AuthLevel.USER, True, "MU-14"),
    RouteSpec("POST", "/accounts/{id}/refresh", "refresh_account", AuthLevel.USER, True, "MU-14"),
    RouteSpec("GET", "/rules", "list_rules", AuthLevel.USER, False, "MU-13"),
    RouteSpec("POST", "/rules", "upsert_rule", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rules/{id}", "upsert_rule", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows", "create_explicit_row", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/skip", "skip_row", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/unskip", "unskip_row", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/withdraw", "withdraw_row", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/cancel", "cancel_row", AuthLevel.USER, True, "MU-14"),
    # MU-12: invite by email + disable/enable by (provider, subject). MU-13 adds the listing.
    RouteSpec("GET", "/admin/users", "admin_users", AuthLevel.OPERATOR, False, "MU-12"),
    RouteSpec("POST", "/admin/users", "admin_users_action", AuthLevel.OPERATOR, True, "MU-12"),
)
