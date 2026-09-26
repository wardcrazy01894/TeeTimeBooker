"""Session payload + sign-in identity policy (MULTIUSER_PLAN §8.3). Framework-free.

The signed cookie (Starlette ``SessionMiddleware`` over itsdangerous) carries ONE dict under
``SESSION_KEY``: ``user_id``, the immutable ``(provider, subject)`` and ``issued_at``. The
absolute lifetime is checked SERVER-SIDE against the injected clock, and a signed cookie can
never be revoked, so ``web.app`` re-reads ``users.status`` on every request (SF10).
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from ..tenant.models import User, UserId, UserRole
from ..tenant.store import TenantStore
from .oauth import ProviderIdentity
from .security import issue_csrf_token

SESSION_KEY = "ttb"
CSRF_KEY = "csrf"

_MU12 = "MULTIUSER_PLAN.md MU-12"


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    user_id: UserId
    provider: str
    subject: str
    issued_at: datetime


def read_session(session: MutableMapping[str, Any]) -> SessionIdentity | None:
    """Parse the signed session's identity; anything malformed reads as signed-out."""
    raw = session.get(SESSION_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        user_id = UserId(UUID(str(raw["user_id"])))
        provider, subject = str(raw["provider"]), str(raw["subject"])
        issued_at = datetime.fromisoformat(str(raw["issued_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    if issued_at.tzinfo is None or not provider or not subject:
        return None
    return SessionIdentity(user_id=user_id, provider=provider, subject=subject, issued_at=issued_at)


def establish_session(
    session: MutableMapping[str, Any], *, user: User, identity: ProviderIdentity, now: datetime
) -> None:
    """Rotate on login: drop everything (incl. the OAuth state) and mint a fresh CSRF token."""
    session.clear()
    session[SESSION_KEY] = {
        "user_id": str(user.id),
        "provider": identity.provider,
        "subject": identity.subject,
        "issued_at": now.isoformat(),
    }
    session[CSRF_KEY] = issue_csrf_token()


def is_expired(identity: SessionIdentity, *, now: datetime, max_age_s: int) -> bool:
    """Absolute lifetime from ``issued_at`` — never refreshed by activity (§8.3)."""
    return now - identity.issued_at >= timedelta(seconds=max_age_s)


def is_operator(user: User, *, operator_email: str | None) -> bool:
    """Operator = the ``users.role`` says so, OR the (invite-bound, provider-verified) email
    equals the configured operator email. The setting is what bootstraps the first operator;
    the role is what ``/admin/users`` can hand to another user later."""
    if user.role is UserRole.OPERATOR:
        return True
    return bool(operator_email) and user.email.casefold() == str(operator_email).casefold()


async def resolve_identity(
    store: TenantStore, *, identity: ProviderIdentity, operator_email: str | None, now: datetime
) -> User | None:
    """Map a completed sign-in to a ``users`` row, or None when the subject is not invited.

    Order: the bound subject wins (a provider-side email change never rebinds); else an
    INVITED row matched ONLY against a provider-VERIFIED email; else the implicit operator
    invite (``operator_email``); else None.
    """
    raise NotImplementedError(_MU12)
