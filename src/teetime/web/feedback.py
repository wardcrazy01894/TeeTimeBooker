"""Report a bug / Request a course (operator request 2026-09-29).

A "Report a bug" button in the top bar of every signed-in page and a "Request a course" link where
a course is chosen both open ``GET /feedback?kind=…&from=<page>``: one small form that emails the
operator (``WebSettings.operator_email``) through the same ``EmailSender`` as invitations. Sending
is best-effort and bounded; the user is thanked either way, and told if it could not be sent.
The email carries the user's name and address (so the operator can reply), the page, and the
message; the subject is stripped of line breaks so a display name cannot inject a header.
"""

import asyncio
import hashlib
import logging
import re
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..tenant.materialize import MIN_HORIZON_DAYS
from ..tenant.models import User, UserId
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
NAME_MAX_LEN = 100  # in the subject
# Abuse bound: each report is an email to the operator. Per user, per web replica (in-process;
# prod runs one warm replica), which is plenty for a handful of invited users.
MAX_REPORTS_PER_HOUR = 5
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
    name = _one_line(user.display_name)[:NAME_MAX_LEN] or user.email
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
    recent: dict[UserId, deque[datetime]] = defaultdict(deque)

    def within_limit(user_id: UserId) -> bool:
        now = ctx.clock.now_utc()
        sent = recent[user_id]
        while sent and now - sent[0] >= timedelta(hours=1):
            sent.popleft()
        if len(sent) >= MAX_REPORTS_PER_HOUR:
            return False
        sent.append(now)
        return True

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
            "files_issue": ctx.github_issues is not None,
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
        if not within_limit(user.id):
            raise HTTPException(
                status_code=429, detail="That's a lot of reports for one hour. Try again later."
            )
        page = local_path(str(form.get("from", "")))
        mail = render_feedback(kind, user=user, page=page, message=message)
        # The public issue is filed concurrently with the private diagnostics (both bounded), so
        # a report never waits on them one after the other.
        issue_task = asyncio.create_task(
            _file_issue(ctx, kind=kind, user=user, page=page, message=message)
        )
        private_diag = (
            await bug_diagnostics(ctx, user=user, user_agent=request.headers.get("user-agent", ""))
            if kind == "bug"
            else None
        )
        issue_url = await issue_task
        if ctx.github_issues is not None:
            link = issue_url or "(not filed)"
            mail = EmailMessage(
                to=mail.to, subject=mail.subject, body=f"{mail.body}\n\nGitHub issue: {link}"
            )
        if private_diag is not None:
            mail = EmailMessage(
                to=mail.to, subject=mail.subject, body=f"{mail.body}\n\n{private_diag}"
            )
        sent = await _send(ctx, mail)
        await ctx.store.append_audit(
            user_id=user.id,
            action="feedback",
            row_id=None,
            detail={
                "kind": kind,
                "length": len(message),
                "emailed": sent,
                "issue": issue_url is not None,
            },
            at=ctx.clock.now_utc(),
        )
        notice = "feedback_sent" if sent else "feedback_not_sent"
        return RedirectResponse(f"/?notice={notice}", status_code=303)


TITLE_MAX_LEN = 80
# The only pages a PUBLIC issue may name: `from` is user-controlled (it could carry a backtick +
# @mention, HTML or an id), so anything else is "(other page)" and the query string is dropped.
PUBLIC_PAGES = frozenset({"/", "/dates", "/rules", "/accounts", "/feedback", "/admin/users"})


def public_page(page: str | None) -> str:
    path = (page or "").split("?", 1)[0]
    return f"`{path}`" if path in PUBLIC_PAGES else "(other page)"


def anonymous_reporter(user: User) -> str:
    """A stable tag that groups one person's reports without naming them (``r-`` + 8 hex)."""
    return "r-" + hashlib.sha256(str(user.id).encode()).hexdigest()[:8]


def issue_title(kind: str, message: str) -> str:
    """``[Bug report] <first line of the message>``: one line, capped, no @-mentions."""
    _, _, prefix = KINDS[kind]
    first = _one_line(message).replace("@", "")[:TITLE_MAX_LEN]
    return f"[{prefix}] {first}"


def _fenced(text: str) -> str:
    """A ``~~~~`` fence longer than any tilde run in ``text``: the reporter's own fences cannot
    close it, so their text renders literally (no @-mentions, links or HTML)."""
    longest = max((len(run) for run in re.findall(r"~+", text)), default=0)
    fence = "~" * max(4, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


async def _file_issue(
    ctx: "_Ctx", *, kind: str, user: User, page: str | None, message: str
) -> str | None:
    """Best-effort anonymized issue in the public repo; None when not configured or on failure."""
    if ctx.github_issues is None:
        return None
    _, _, prefix = KINDS[kind]
    lines = [
        f"{prefix} from the site, reporter `{anonymous_reporter(user)}` (anonymous).",
        "",
        f"Page: {public_page(page)}",
        "",
        _fenced(message),
    ]
    try:
        if kind == "bug":
            lines += ["", await public_diagnostics(ctx, user=user)]
        return await asyncio.wait_for(
            ctx.github_issues.create(title=issue_title(kind, message), body="\n".join(lines)),
            timeout=ISSUE_TIMEOUT_S,
        )
    except Exception:  # TimeoutError included; create() itself never raises
        log.warning("GitHub issue not filed", exc_info=True)
        return None


ISSUE_TIMEOUT_S = 15.0


async def public_diagnostics(ctx: "_Ctx", *, user: User) -> str:
    """The anonymized half of the diagnostics, fit for a PUBLIC issue: the build, each course's
    name and status, and counts of the next 21 days' dates by status. No names, emails or ids."""
    s = ctx.settings
    out = [
        "<details><summary>Diagnostics (anonymized)</summary>",
        "",
        f"- Environment: {s.environment or '(unset)'} · build {s.build or '(unset)'}",
        # The date only: a minute-level time could be matched to a person.
        f"- Reported: {ctx.clock.now_utc():%Y-%m-%d}",
    ]
    try:
        out += await asyncio.wait_for(
            _public_store_lines(ctx, user=user), timeout=DIAGNOSTICS_TIMEOUT_S
        )
    except Exception as exc:  # TimeoutError included
        out.append(f"- (diagnostics unavailable: {type(exc).__name__})")
    return "\n".join([*out, "", "</details>"])


async def _public_store_lines(ctx: "_Ctx", *, user: User) -> list[str]:
    accounts = await ctx.store.list_accounts_for_user(user.id)
    out = [
        f"- {ctx.course_name(a.course_id)}: {a.status.value}"
        + (
            f", {a.consecutive_soft_auth_failures} login failures in a row"
            if a.consecutive_soft_auth_failures
            else ""
        )
        for a in accounts
    ] or ["- No course connected"]
    today = ctx.clock.now_utc().date()
    rows = await ctx.store.list_rows_for_user(
        user.id, from_date=today, to_date=today + timedelta(days=MIN_HORIZON_DAYS)
    )
    counts = Counter(r.status.value for r in rows)
    reconcile = sum(1 for r in rows if r.needs_reconcile)
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "none"
    out.append(
        f"- Next 21 days: {summary}" + (f", {reconcile} need reconcile" if reconcile else "")
    )
    return out


DIAGNOSTIC_ACTIONS = 10
# Diagnostics are a handful of store reads; bounded so a slow store (the Cosmos SDK retries a 429
# for up to 30 s) can never stall the report.
DIAGNOSTICS_TIMEOUT_S = 10.0
_ET = ZoneInfo("America/New_York")


def _et(t: datetime) -> str:
    return t.astimezone(_ET).strftime("%a %b %-d %-I:%M %p ET")


async def bug_diagnostics(ctx: "_Ctx", *, user: User, user_agent: str) -> str:
    """What the operator needs to reproduce a bug, gathered best-effort from the store (never a
    course login, never a password): the build, the reporter's courses, next 21 days' dates and
    last actions. A failure yields a one-line note instead: it never blocks the report."""
    now = ctx.clock.now_utc()
    s = ctx.settings
    head = [
        "--- Diagnostics ---",
        f"Environment: {s.environment or '(unset)'} · build {s.build or '(unset)'}",
        f"Reported: {_et(now)} ({now:%Y-%m-%d %H:%M} UTC)",
        f"Browser: {_one_line(user_agent)[:300] or '(unknown)'}",
        f"User id: {user.id} · role {user.role.value} · status {user.status.value}",
    ]
    try:
        body = await asyncio.wait_for(
            _store_diagnostics(ctx, user=user, now=now), timeout=DIAGNOSTICS_TIMEOUT_S
        )
    except Exception as exc:  # TimeoutError included
        log.warning("bug-report diagnostics failed", exc_info=True)
        body = [f"(diagnostics unavailable: {type(exc).__name__})"]
    return "\n".join(head + body)


async def _store_diagnostics(ctx: "_Ctx", *, user: User, now: datetime) -> list[str]:
    out = ["", "Courses:"]
    accounts = await ctx.store.list_accounts_for_user(user.id)
    for a in accounts:
        snap = await ctx.store.get_snapshot(a.id)
        checked = (
            f"last checked {_et(snap.observed_at)}{'' if snap.trusted else ' (untrusted)'}"
            if snap is not None
            else "never checked"
        )
        out.append(
            f"  - {ctx.course_name(a.course_id)}: {a.status.value} · {checked} · "
            f"login failures in a row {a.consecutive_soft_auth_failures} · account {a.id}"
        )
    if not accounts:
        out.append("  (none connected)")
    today = now.date()
    rows = await ctx.store.list_rows_for_user(
        user.id, from_date=today, to_date=today + timedelta(days=MIN_HORIZON_DAYS)
    )
    out += ["", "Next 21 days:"]
    for r in rows:
        tee = (
            f" {r.booked_tee_time.astimezone(ZoneInfo(r.timezone)):%-I:%M %p}"
            if r.booked_tee_time
            else ""
        )
        flags = " · NEEDS RECONCILE" if r.needs_reconcile else ""
        out.append(
            f"  - {r.target_date:%a %b %-d} {ctx.course_name(r.course_id)}: "
            f"{r.status.value}{tee} · last outcome {r.last_outcome or '-'}{flags} · row {r.id}"
        )
    if not rows:
        out.append("  (no dates)")
    out += ["", f"Last {DIAGNOSTIC_ACTIONS} actions (newest first):"]
    actions = await ctx.store.recent_audit(user.id, limit=DIAGNOSTIC_ACTIONS)
    out += [
        f"  - {_et(a.at)} {a.action}" + (f" (row {a.row_id})" if a.row_id else "") for a in actions
    ]
    if not actions:
        out.append("  (none)")
    return out


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
