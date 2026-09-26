"""MU-13 pages: dashboard, rules, dates (MULTIUSER_PLAN §8.2, §3.4, §7.4, §7.7).

Server-rendered, no script at all (the CSP forbids inline script; every action is a plain form
POST carrying the session's CSRF token, which the app-level guard verifies BEFORE any handler
runs). Handlers are thin: they parse the form, call ``web.services`` (which scopes every read and
write to the signed-in user) and Post/Redirect/Get to a fixed ``?notice=`` key, so nothing
user-supplied is ever reflected from the query string.

Response mapping (§8.2): ``WebNotFoundError`` -> ONE fixed 404 page for a missing, malformed or
another user's id (IDOR, §9.1); ``InvalidInputError`` -> 400 and ``ActionRefusedError`` -> 409,
both re-rendering the page the form came from with the message (and, for
``RuleNoLongerCoversError``, the prefilled "add it as a one-off" form). Cancel of a BOOKED row is
MU-14: the button is rendered disabled.
"""

# NO `from __future__ import annotations` (see web/app.py): the closure-bound `Depends` in the
# handler annotations must resolve eagerly.

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData

from ..tenant.models import CourseAccountId, RowId, RuleId, User
from . import auth, services
from .services import (
    RULE_EDIT_HINT,
    WEEKDAY_NAMES,
    ActionRefusedError,
    InvalidInputError,
    OneOffPrefill,
    WebNotFoundError,
)

if TYPE_CHECKING:
    from .app import _Ctx

__all__ = ["register_page_routes"]

_Dependency = Callable[..., Awaitable[Any]]
_FormAction = Callable[[dict[str, str]], Awaitable[str]]  # returns the PRG location
_RowService = Callable[..., Awaitable[object]]

# Fixed strings keyed by the `notice` query param: nothing user-supplied is ever reflected.
_NOTICES = {
    "rule_created": "Rule saved and its dates added.",
    "rule_updated": "Rule updated.",
    "rule_deactivated": "Rule deactivated. Its pending dates were withdrawn; booked ones are kept.",
    "rule_activated": "Rule reactivated and its dates added back.",
    "one_off_added": "Date added.",
    "skipped": "Date skipped: the bot will not book it.",
    "unskipped": "Date back on: the bot will try to book it.",
    "withdrawn": "Date withdrawn.",
}
_STATUS_LABELS = {
    ("cancelled", "external"): "cancelled at the course",
    ("cancelled", "user"): "cancelled by you",
    ("cancelled", "already_gone"): "cancelled (already gone at the course)",
}


def _form_dict(form: FormData) -> dict[str, str]:
    return {k: v for k, v in form.items() if isinstance(v, str)}


class _Pages:
    """Page renderers + the form-action runner shared by the handlers."""

    def __init__(self, ctx: "_Ctx") -> None:
        self.ctx = ctx

    def base_context(self, request: Request, user: User) -> dict[str, object]:
        return {
            "user": user,
            "is_operator": auth.is_operator(user, operator_email=self.ctx.settings.operator_email),
            "dry_run": self.ctx.settings.dry_run,
            "notice": _NOTICES.get(request.query_params.get("notice", "")),
            "weekdays": WEEKDAY_NAMES,
            "status_labels": _STATUS_LABELS,
        }

    async def dashboard(self, request: Request, user: User) -> Response:
        context = self.base_context(request, user)
        ctx = self.ctx
        context["rows"] = await services.dashboard(ctx.store, user_id=user.id, clock=ctx.clock)
        context["accounts"] = await services.list_accounts(ctx.store, user_id=user.id)
        return ctx.page(request, "dashboard.html", context)

    async def dates(
        self,
        request: Request,
        user: User,
        *,
        status_code: int = 200,
        error: str | None = None,
        one_off: OneOffPrefill | None = None,
    ) -> Response:
        ctx = self.ctx
        context = self.base_context(request, user)
        context["rows"] = await services.dashboard(ctx.store, user_id=user.id, clock=ctx.clock)
        context["accounts"] = await services.list_accounts(ctx.store, user_id=user.id)
        context["error"], context["one_off"] = error, one_off
        return ctx.page(request, "dates.html", context, status_code=status_code)

    async def rules(
        self,
        request: Request,
        user: User,
        *,
        status_code: int = 200,
        error: str | None = None,
        one_off: OneOffPrefill | None = None,
    ) -> Response:
        ctx = self.ctx
        context = self.base_context(request, user)
        context["rules"] = await services.list_rules(ctx.store, user_id=user.id)
        context["accounts"] = await services.list_accounts(ctx.store, user_id=user.id)
        context["rule_edit_hint"] = RULE_EDIT_HINT
        context["error"], context["one_off"] = error, one_off
        return ctx.page(request, "rules.html", context, status_code=status_code)

    async def act(
        self,
        request: Request,
        user: User,
        action: _FormAction,
        *,
        on_error: Callable[..., Awaitable[Response]],
    ) -> Response:
        """Run one form action: PRG on success, re-render the source page on a refusal."""
        form = _form_dict(await request.form())
        try:
            location = await action(form)
        except InvalidInputError as e:
            return await on_error(request, user, status_code=400, error=str(e))
        except ActionRefusedError as e:
            return await on_error(
                request, user, status_code=409, error=e.message, one_off=e.one_off
            )
        return RedirectResponse(location, status_code=303)


def register_page_routes(app: FastAPI, ctx: "_Ctx", *, current_user: _Dependency) -> None:
    pages = _Pages(ctx)

    async def on_not_found(request: Request, exc: Exception) -> Response:
        # No user, no id, no request data: a foreign row and a missing one render byte-identical.
        return ctx.page(request, "not_found.html", {}, status_code=404)

    app.add_exception_handler(WebNotFoundError, on_not_found)
    _register_read_routes(app, pages, current_user=current_user)
    _register_rule_routes(app, pages, current_user=current_user)
    _register_row_routes(app, pages, current_user=current_user)


def _register_read_routes(app: FastAPI, pages: _Pages, *, current_user: _Dependency) -> None:
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, user: CurrentUser) -> Response:
        return await pages.dashboard(request, user)

    @app.get("/dates", response_class=HTMLResponse)
    async def list_dates(request: Request, user: CurrentUser) -> Response:
        return await pages.dates(request, user)

    @app.get("/rules", response_class=HTMLResponse)
    async def list_rules(request: Request, user: CurrentUser) -> Response:
        return await pages.rules(request, user)


def _register_rule_routes(app: FastAPI, pages: _Pages, *, current_user: _Dependency) -> None:
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias
    ctx = pages.ctx

    @app.post("/rules")
    async def create_rule(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            rule_input = services.parse_rule_form(form)
            await services.create_rule(
                ctx.store,
                user_id=user.id,
                account_id=CourseAccountId(services.parse_id(form.get("account_id", ""))),
                rule_input=rule_input,
                policies=ctx.policies,
                cutoff=ctx.cutoff,
                clock=ctx.clock,
            )
            return "/rules?notice=rule_created"

        return await pages.act(request, user, action, on_error=pages.rules)

    @app.post("/rules/{id}")
    async def upsert_rule(request: Request, user: CurrentUser, id: str) -> Response:
        rid = RuleId(services.parse_id(id))

        async def action(form: dict[str, str]) -> str:
            verb = form.get("action", "save")
            if verb not in ("save", "deactivate", "activate"):
                raise InvalidInputError("unknown action")
            version = services.parse_version(form)
            if verb == "save":
                await services.edit_rule(
                    ctx.store,
                    user_id=user.id,
                    rule_id=rid,
                    rule_input=services.parse_rule_form(form),
                    version=version,
                    policies=ctx.policies,
                    cutoff=ctx.cutoff,
                    clock=ctx.clock,
                )
                return "/rules?notice=rule_updated"
            await services.set_rule_active(
                ctx.store,
                user_id=user.id,
                rule_id=rid,
                active=verb == "activate",
                version=version,
                policies=ctx.policies,
                cutoff=ctx.cutoff,
                clock=ctx.clock,
            )
            return f"/rules?notice=rule_{verb}d"

        return await pages.act(request, user, action, on_error=pages.rules)


def _register_row_routes(app: FastAPI, pages: _Pages, *, current_user: _Dependency) -> None:
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias
    ctx = pages.ctx

    @app.post("/rows")
    async def create_explicit_row(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            one_off = services.parse_one_off_form(form)
            await services.create_one_off(
                ctx.store, user_id=user.id, one_off=one_off, clock=ctx.clock
            )
            return "/dates?notice=one_off_added"

        return await pages.act(request, user, action, on_error=pages.dates)

    def row_route(path: str, service: _RowService, notice: str) -> None:
        async def handler(request: Request, user: CurrentUser, id: str) -> Response:
            rid = RowId(services.parse_id(id))

            async def action(form: dict[str, str]) -> str:
                await service(ctx.store, user_id=user.id, row_id=rid, clock=ctx.clock)
                return f"/dates?notice={notice}"

            return await pages.act(request, user, action, on_error=pages.dates)

        handler.__name__ = f"{notice}_row"
        app.post(path)(handler)

    row_route("/rows/{id}/skip", services.skip_row, "skipped")
    row_route("/rows/{id}/unskip", services.unskip_row, "unskipped")
    row_route("/rows/{id}/withdraw", services.withdraw_row, "withdrawn")
