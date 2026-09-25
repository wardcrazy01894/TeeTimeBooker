"""Route contract table (MULTIUSER_PLAN §8.2). Handlers are thin and delegate to ``services``.

``ROUTES`` is DATA: the single place the path/auth/CSRF policy is declared. MU-12/13 bind it to
the framework, and a test asserts every non-GET route has ``csrf=True`` and every ``user`` route
has an IDOR test (``test_route_rejects_other_users_row``).

STUB — bound in MULTIUSER_PLAN MU-12..MU-14.
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
    RouteSpec("GET", "/login", "login_start", AuthLevel.NONE, False, "MU-12"),
    RouteSpec("GET", "/auth/{provider}/callback", "login_callback", AuthLevel.NONE, False, "MU-12"),
    RouteSpec("POST", "/logout", "logout", AuthLevel.USER, True, "MU-12"),
    RouteSpec("GET", "/", "dashboard", AuthLevel.USER, False, "MU-13"),
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
    RouteSpec("GET", "/admin/users", "list_users", AuthLevel.OPERATOR, False, "MU-13"),
    RouteSpec("POST", "/admin/users", "invite_user", AuthLevel.OPERATOR, True, "MU-13"),
)
