"""Route contract table (MULTIUSER_PLAN §8.2). Handlers are thin and delegate to ``services``.

``ROUTES`` is DATA: the single place the path/auth/CSRF policy is declared. MU-12/13 bind it to
the framework, and a test asserts every non-GET route has ``csrf=True`` and every ``user`` route
has an IDOR test (``test_route_rejects_other_users_row``).

Every row is BOUND (``web/app.py``, ``web/pages.py``; ``tests/web/test_web_app.py`` pins that
every MU-12 row exists in the app with its method and that ``auth=NONE`` rows are exactly the
public allowlist; ``tests/web/test_web_pages.py`` and ``tests/web/test_web_accounts_pages.py``
that every MU-13 / MU-14 row is bound, user-auth, CSRF on POST, IDOR-tested, and names a real
``web.services`` function).
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
    RouteSpec("GET", "/", "dashboard", AuthLevel.USER, False, "MU-13"),
    RouteSpec("GET", "/accounts", "list_accounts", AuthLevel.USER, False, "MU-14"),
    RouteSpec("POST", "/accounts/connect", "connect_account", AuthLevel.USER, True, "MU-14"),
    RouteSpec("POST", "/accounts/{id}/reverify", "reverify_account", AuthLevel.USER, True, "MU-14"),
    RouteSpec("POST", "/accounts/{id}/refresh", "refresh_account", AuthLevel.USER, True, "MU-14"),
    RouteSpec("POST", "/accounts/{id}/price", "set_default_price", AuthLevel.USER, True, "MU-R3"),
    RouteSpec("POST", "/accounts/{id}/adopt", "adopt_bookings", AuthLevel.OPERATOR, True, "MU-16b"),
    RouteSpec("POST", "/bookings/date", "book_date", AuthLevel.USER, True, "MU-R3"),
    RouteSpec("POST", "/bookings/weekly", "book_weekly", AuthLevel.USER, True, "MU-R3"),
    RouteSpec("GET", "/rules", "list_rules", AuthLevel.USER, False, "MU-13"),
    RouteSpec("POST", "/rules", "create_rule", AuthLevel.USER, True, "MU-13"),
    # form `action`: save (edit_rule) | deactivate / activate (set_rule_active)
    RouteSpec("POST", "/rules/{id}", "edit_rule", AuthLevel.USER, True, "MU-13"),
    # the dated-row list with its actions; the same read model as the dashboard
    RouteSpec("GET", "/dates", "dashboard", AuthLevel.USER, False, "MU-13"),
    # a one-off date; also "Re-request this date" and "add it as a one-off instead"
    RouteSpec("POST", "/rows", "create_one_off", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/skip", "skip_row", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/unskip", "unskip_row", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/withdraw", "withdraw_row", AuthLevel.USER, True, "MU-13"),
    RouteSpec("POST", "/rows/{id}/cancel", "cancel_row", AuthLevel.USER, True, "MU-14"),
    # 2026-09-29: Report a bug / Request a course (web/feedback.py): emails the operator and, in
    # prod, files an anonymized GitHub issue.
    RouteSpec("POST", "/me/name", "set_display_name", AuthLevel.USER, True, "OPS"),
    RouteSpec("GET", "/feedback", "feedback_page", AuthLevel.USER, False, "OPS"),
    RouteSpec("POST", "/feedback", "send_feedback", AuthLevel.USER, True, "OPS"),
    # MU-12: invite by email + disable/enable by (provider, subject); 2026-09-29: the user list,
    # uninvited sign-in attempts, Resend invite and Uninvite.
    RouteSpec("GET", "/admin/users", "admin_users", AuthLevel.OPERATOR, False, "MU-12"),
    RouteSpec("POST", "/admin/users", "admin_users_action", AuthLevel.OPERATOR, True, "MU-12"),
)
