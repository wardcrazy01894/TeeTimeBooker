"""Report a bug / Request a course (operator request 2026-09-29).

A "Report a bug" link in the footer of every signed-in page and a "Request a course" link where a
course is chosen both open ``GET /feedback?kind=…&from=<page>``: one small form that emails the
operator (``WebSettings.operator_email``) through the same ``EmailSender`` as invitations. Sending
is best-effort and bounded; the user is thanked either way, and told if it could not be sent.
The email carries the user's name and address (so the operator can reply), the page, and the
message; the subject is stripped of line breaks so a display name cannot inject a header.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..tenant.models import User
from ..tenant.notify import SITE_NAME, EmailMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .app import _Ctx
    from .pages import _Pages

log = logging.getLogger(__name__)

# kind -> (page heading, prompt, subject prefix)
KINDS: dict[str, tuple[str, str, str]] = {
    "bug": (
        "Report a bug",
        "What went wrong? What were you trying to do, and what happened instead?",
        "Bug report",
    ),
    "course": (
        "Request a course",
        "Which course would you like added? Its name and city help.",
        "Course request",
    ),
}
MESSAGE_MAX_LEN = 4000
FROM_PATH_MAX_LEN = 200
FEEDBACK_EMAIL_TIMEOUT_S = 20.0


def local_path(value: str | None) -> str | None:
    """``value`` iff it is a plain same-site path ("/dates"), else None: it is only ever shown and
    mailed, never redirected to, but a URL to another site has no business in either."""
    if not value or len(value) > FROM_PATH_MAX_LEN:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    if any(c in value for c in "\\\r\n\t "):
        return None
    return value


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_feedback(kind: str, *, user: User, page: str | None, message: str) -> EmailMessage:
    """The operator's copy. ``to`` is filled in by the caller."""
    _, _, prefix = KINDS[kind]
    name = _one_line(user.display_name) or user.email
    body = "\n".join(
        [
            f"{prefix} from the site.",
            "",
            f"From: {name} <{user.email}>",
            f"Page: {page or '(unknown)'}",
            "",
            message,
        ]
    )
    return EmailMessage(to="", subject=f"[{SITE_NAME}] {prefix} from {name}", body=body)


def register_feedback_routes(
    app: FastAPI, pages: "_Pages", *, current_user: "Callable[..., Awaitable[Any]]"
) -> None:
    # No `from __future__ import annotations` in this module: FastAPI must resolve the local
    # `CurrentUser` alias at runtime, or `user` silently becomes a query parameter (422).
    ctx = pages.ctx
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias

    @app.get("/feedback", response_class=HTMLResponse)
    async def feedback_page(request: Request, user: CurrentUser) -> Response:
        kind = request.query_params.get("kind", "bug")
        if kind not in KINDS:
            kind = "bug"
        heading, prompt, _ = KINDS[kind]
        context = pages.base_context(request, user)
        context |= {
            "kind": kind,
            "heading": heading,
            "prompt": prompt,
            "from_page": local_path(request.query_params.get("from")),
            "max_len": MESSAGE_MAX_LEN,
        }
        return ctx.page(request, "feedback.html", context)

    @app.post("/feedback")
    async def send_feedback(request: Request, user: CurrentUser) -> Response:
        form = await request.form()
        kind = str(form.get("kind", ""))
        message = str(form.get("message", "")).strip()
        if kind not in KINDS:
            raise HTTPException(status_code=400, detail="unknown feedback kind")
        if not message or len(message) > MESSAGE_MAX_LEN:
            raise HTTPException(
                status_code=400, detail=f"write a message (at most {MESSAGE_MAX_LEN} characters)"
            )
        mail = render_feedback(
            kind, user=user, page=local_path(str(form.get("from", ""))), message=message
        )
        sent = await _send(ctx, mail)
        await ctx.store.append_audit(
            user_id=user.id,
            action="feedback",
            row_id=None,
            detail={"kind": kind, "length": len(message), "emailed": sent},
            at=ctx.clock.now_utc(),
        )
        notice = "feedback_sent" if sent else "feedback_not_sent"
        return RedirectResponse(f"/?notice={notice}", status_code=303)


async def _send(ctx: "_Ctx", mail: EmailMessage) -> bool:
    to = ctx.settings.operator_email
    if ctx.email_sender is None or not to:
        log.warning("feedback not emailed: email or the operator address is not configured")
        return False
    message = EmailMessage(to=to, subject=mail.subject, body=mail.body)
    try:
        result = await asyncio.wait_for(
            ctx.email_sender.send(message), timeout=FEEDBACK_EMAIL_TIMEOUT_S
        )
    except Exception:  # TimeoutError included
        log.warning("feedback email failed", exc_info=True)
        return False
    return bool(result.ok)
