"""MU-13 pages: dashboard, rules, dates (MULTIUSER_PLAN §8.2, §3.4, §7.4, §7.7); MU-14: the
accounts page (connect / re-verify / refresh, §8.4, §8.6) and Cancel on a booked date (§8.5).

Server-rendered, no script at all (the CSP forbids inline script; every action is a plain form
POST carrying the session's CSRF token, which the app-level guard verifies BEFORE any handler
runs). Handlers are thin: they parse the form, call ``web.services`` (which scopes every read and
write to the signed-in user) and Post/Redirect/Get to a fixed ``?notice=`` key, so nothing
user-supplied is ever reflected from the query string.

Response mapping (§8.2): ``WebNotFoundError`` -> ONE fixed 404 page for a missing, malformed or
another user's id (IDOR, §9.1); ``InvalidInputError`` -> 400 and ``ActionRefusedError`` -> 409,
both re-rendering the page the form came from with the message (and, for
``RuleNoLongerCoversError``, the prefilled "add it as a one-off" form); ``RateLimitedError`` (a
login-probe or refresh limit) -> 429. A submitted password is never rendered back: the
password inputs have no ``value``.
"""

# NO `from __future__ import annotations` (see web/app.py): the closure-bound `Depends` in the
# handler annotations must resolve eagerly.

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData

from ..core.config import BookingCutoffConfig
from ..core.models import CourseId
from ..tenant.crypto import Keyring
from ..tenant.models import CourseAccountId, RowId, RuleId, User
from ..tenant.runner import AdapterFactory
from . import adopt, auth, feedback, group_services, services
from .booking_form import MAX_OPTIONS, RankedChoice, parse_ranked_form
from .ranking_explainer import cutoff_text
from .services import (
    RULE_EDIT_HINT,
    WEEKDAY_NAMES,
    ActionRefusedError,
    InvalidInputError,
    OneOffPrefill,
    RateLimitedError,
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
    "name_saved": "Name saved.",
    "cutoff_saved": "Booking cutoff saved. Your pending and booked dates now follow it.",
    "cutoff_saved_partly": (
        "Booking cutoff saved. A date the bot was checking at that moment keeps the old "
        "cutoff: save again in a few minutes to apply it there too."
    ),
    "skipped": "Date skipped: the bot will not book it.",
    "unskipped": "Date back on: the bot will try to book it.",
    "withdrawn": "Date withdrawn.",
    "account_connected": "Course connected: the course accepted the login.",
    "account_verified": "Course login re-verified: the bot will log in with it again.",
    "refreshed": "Reservations refreshed from the course.",
    "cancelled": "Tee time cancelled.",
    "adopted": "Existing reservations adopted: the bot now treats them as its own bookings.",
    "group_saved": "Saved. The bot books the highest-ranked option that is available.",
    "group_rule_saved": "Weekly booking saved and its dates added.",
    "price_saved": "Default price saved.",
    "feedback_sent": (
        "Thanks! Spicy Al will take a look. If you don't hear back, you can also email "
        "hello@spicyteetimebooker.com."
    ),
}
STATUS_LABELS = {
    ("cancelled", "external"): "cancelled at the course",
    ("cancelled", "user"): "cancelled by you",
    ("cancelled", "already_gone"): "cancelled (already gone at the course)",
}


def _form_dict(form: FormData) -> dict[str, str]:
    return {k: v for k, v in form.items() if isinstance(v, str)}


class _Pages:
    """Page renderers + the form-action runner shared by the handlers."""

    def __init__(self, ctx: _Ctx) -> None:
        self.ctx = ctx

    def cutoff_of(self, user: User) -> BookingCutoffConfig:
        """The cutoff the bot runs for ``user`` (their own, else the site default): what every
        rule write materializes under and what the pages word."""
        return services.effective_cutoff(user, self.ctx.cutoff)

    def base_context(self, request: Request, user: User) -> dict[str, object]:
        cutoff = self.cutoff_of(user)
        return {
            "user": user,
            # The person's OWN cutoff in words (2026-10-08), shadowing the site-default global.
            "booking_cutoff_text": cutoff_text(cutoff.days_before, cutoff.time_of_day),
            "is_operator": auth.is_operator(user, operator_email=self.ctx.settings.operator_email),
            "dry_run": self.ctx.settings.dry_run,
            "notice": _NOTICES.get(request.query_params.get("notice", "")),
            "weekdays": WEEKDAY_NAMES,
            "status_labels": STATUS_LABELS,
            "option_slots": range(1, MAX_OPTIONS + 1),
        }

    async def dashboard(
        self,
        request: Request,
        user: User,
        *,
        status_code: int = 200,
        error: str | None = None,
        one_off: OneOffPrefill | None = None,
    ) -> Response:
        context = self.base_context(request, user)
        ctx = self.ctx
        context["rows"] = await services.dashboard(ctx.store, user_id=user.id, clock=ctx.clock)
        context["accounts"] = await services.list_accounts(ctx.store, user_id=user.id)
        context["error"], context["one_off"] = error, one_off
        return ctx.page(request, "dashboard.html", context, status_code=status_code)

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
        accounts = await services.list_accounts(ctx.store, user_id=user.id)
        context["accounts"] = accounts
        context["min_date"] = services.earliest_bookable_date(
            accounts, ctx.policies, now=ctx.clock.now_utc()
        )
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

    async def account(
        self,
        request: Request,
        user: User,
        *,
        status_code: int = 200,
        error: str | None = None,
        one_off: OneOffPrefill | None = None,
    ) -> Response:
        """ "Your account" (2026-10-02): who you are signed in as, your own name and, since
        2026-10-08, your own booking cutoff."""
        context = self.base_context(request, user)
        cutoff = self.cutoff_of(user)
        context["cutoff_days"], context["cutoff_time"] = cutoff.days_before, cutoff.time_of_day
        context["error"], context["one_off"] = error, one_off
        return self.ctx.page(request, "me.html", context, status_code=status_code)

    async def accounts(
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
        views = await services.account_views(
            ctx.store, user_id=user.id, clock=ctx.clock, policies=ctx.policies
        )
        courses = sorted(ctx.policies)
        # A course already connected leaves the Connect form (2026-10-01: blank username and
        # password boxes under a connected course read as "do it again?").
        connected = {str(v.account.course_id) for v in views}
        context["views"] = views
        context["courses"] = courses
        context["connectable"] = [c for c in courses if c not in connected]
        context["adoptions"] = (
            {
                str(v.account.id): await adopt.preview(
                    ctx.store, user_id=user.id, account=v.account, clock=ctx.clock
                )
                for v in views
            }
            if context["is_operator"]
            else {}
        )
        context["error"] = error
        return ctx.page(request, "accounts.html", context, status_code=status_code)

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
        except RateLimitedError as e:
            return await on_error(request, user, status_code=429, error=e.message)
        except ActionRefusedError as e:
            return await on_error(
                request, user, status_code=409, error=e.message, one_off=e.one_off
            )
        return RedirectResponse(location, status_code=303)


def register_page_routes(app: FastAPI, ctx: _Ctx, *, current_user: _Dependency) -> None:
    pages = _Pages(ctx)

    async def on_not_found(request: Request, exc: Exception) -> Response:
        # No user, no id, no request data: a foreign row and a missing one render byte-identical.
        return ctx.page(request, "not_found.html", {}, status_code=404)

    app.add_exception_handler(WebNotFoundError, on_not_found)
    _register_read_routes(app, pages, current_user=current_user)
    _register_rule_routes(app, pages, current_user=current_user)
    _register_row_routes(app, pages, current_user=current_user)
    _register_account_routes(app, pages, current_user=current_user)
    _register_booking_routes(app, pages, current_user=current_user)
    adopt.register_adopt_routes(app, pages, current_user=current_user)
    feedback.register_feedback_routes(app, pages, current_user=current_user)


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

    @app.get("/me", response_class=HTMLResponse)
    async def account_page(request: Request, user: CurrentUser) -> Response:
        return await pages.account(request, user)

    @app.post("/me/name")
    async def set_display_name(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            await services.set_display_name(
                pages.ctx.store, user_id=user.id, raw=form.get("display_name", "")
            )
            return "/me?notice=name_saved"

        return await pages.act(request, user, action, on_error=pages.account)

    @app.post("/me/cutoff")
    async def set_booking_cutoff(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            change = await services.set_booking_cutoff(
                pages.ctx.store,
                user_id=user.id,
                cutoff=services.parse_cutoff_form(form),
                clock=pages.ctx.clock,
            )
            notice = "cutoff_saved_partly" if change.skipped_leased else "cutoff_saved"
            return f"/me?notice={notice}"

        return await pages.act(request, user, action, on_error=pages.account)


def _register_rule_routes(app: FastAPI, pages: _Pages, *, current_user: _Dependency) -> None:
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias
    ctx = pages.ctx

    @app.post("/rules")
    async def create_rule(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            rule_input = services.parse_rule_form(form)
            account_id = CourseAccountId(services.parse_id(form.get("account_id", "")))
            if (course := (await _courses_of(ctx, user)).get(account_id)) is not None:
                ctx.check_window(course, rule_input.window_earliest, rule_input.window_latest)
            await services.create_rule(
                ctx.store,
                user_id=user.id,
                account_id=account_id,
                rule_input=rule_input,
                policies=ctx.policies,
                cutoff=pages.cutoff_of(user),
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
                    cutoff=pages.cutoff_of(user),
                    clock=ctx.clock,
                    check_window=ctx.check_window,
                )
                return "/rules?notice=rule_updated"
            await services.set_rule_active(
                ctx.store,
                user_id=user.id,
                rule_id=rid,
                active=verb == "activate",
                version=version,
                policies=ctx.policies,
                cutoff=pages.cutoff_of(user),
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
            if (course := (await _courses_of(ctx, user)).get(one_off.account_id)) is not None:
                ctx.check_window(course, one_off.window_earliest, one_off.window_latest)
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


def _mu14_wiring(ctx: _Ctx) -> tuple[Keyring, AdapterFactory]:
    """The keyring + adapter factory the MU-14 actions need; refused (409) when the app was
    built without them, never a crash."""
    if ctx.keyring is None or ctx.adapter_factory is None:
        raise ActionRefusedError(
            "Connecting, refreshing and cancelling are not available on this site yet."
        )
    return ctx.keyring, ctx.adapter_factory


def _register_account_routes(app: FastAPI, pages: _Pages, *, current_user: _Dependency) -> None:
    """MU-14: /accounts (connect, re-verify, refresh). Every route is user-scoped: connect
    derives the account id from the SESSION user, and re-verify / refresh resolve the path id
    through ``get_account(..., user_id=...)`` (a foreign id is the uniform 404)."""
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias
    ctx = pages.ctx

    @app.get("/accounts", response_class=HTMLResponse)
    async def list_accounts(request: Request, user: CurrentUser) -> Response:
        return await pages.accounts(request, user)

    @app.post("/accounts/connect")
    async def connect_account(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            course = form.get("course_id", "").strip()
            if course not in ctx.policies:
                raise InvalidInputError("choose a course from the list")
            keyring, factory = _mu14_wiring(ctx)
            await services.connect_account(
                ctx.store,
                user_id=user.id,
                course_id=CourseId(course),
                username=form.get("username", ""),
                password=form.get("password", ""),
                keyring=keyring,
                adapter_factory=factory,
                clock=ctx.clock,
                limits=ctx.probe_limits,
            )
            return "/accounts?notice=account_connected"

        return await pages.act(request, user, action, on_error=pages.accounts)

    @app.post("/accounts/{id}/reverify")
    async def reverify_account(request: Request, user: CurrentUser, id: str) -> Response:
        aid = CourseAccountId(services.parse_id(id))

        async def action(form: dict[str, str]) -> str:
            keyring, factory = _mu14_wiring(ctx)
            await services.reverify_account(
                ctx.store,
                user_id=user.id,
                account_id=aid,
                password=form.get("password", ""),
                keyring=keyring,
                adapter_factory=factory,
                clock=ctx.clock,
                limits=ctx.probe_limits,
            )
            return "/accounts?notice=account_verified"

        return await pages.act(request, user, action, on_error=pages.accounts)

    @app.post("/accounts/{id}/refresh")
    async def refresh_account(request: Request, user: CurrentUser, id: str) -> Response:
        aid = CourseAccountId(services.parse_id(id))

        async def action(form: dict[str, str]) -> str:
            keyring, factory = _mu14_wiring(ctx)
            await services.refresh_account(
                ctx.store,
                user_id=user.id,
                account_id=aid,
                keyring=keyring,
                adapter_factory=factory,
                clock=ctx.clock,
                cache=ctx.refresh_cache,
                limits=ctx.probe_limits,
            )
            return "/accounts?notice=refreshed"

        return await pages.act(request, user, action, on_error=pages.accounts)

    @app.post("/rows/{id}/cancel")
    async def cancel_row(request: Request, user: CurrentUser, id: str) -> Response:
        rid = RowId(services.parse_id(id))
        # Cancel lives on both /dates and the dashboard; `from` picks where to land. Only the
        # one fixed value is honoured, so it can never become an open redirect.
        from_dashboard = (await request.form()).get("from") == "dashboard"
        back = "/" if from_dashboard else "/dates"

        async def action(form: dict[str, str]) -> str:
            keyring, factory = _mu14_wiring(ctx)
            await services.cancel_row(
                ctx.store,
                user_id=user.id,
                row_id=rid,
                confirm_unowned=form.get("confirm_unowned") == "on",
                keyring=keyring,
                adapter_factory=factory,
                clock=ctx.clock,
                dry_run=ctx.settings.dry_run,
                cache=ctx.refresh_cache,
                notifier=ctx.notifier,
                limits=ctx.probe_limits,
                jobs=ctx.jobs,
            )
            return f"{back}?notice=cancelled"

        on_error = pages.dashboard if from_dashboard else pages.dates
        return await pages.act(request, user, action, on_error=on_error)


async def _courses_of(ctx: _Ctx, user: User) -> dict[CourseAccountId, CourseId]:
    accounts = await services.list_accounts(ctx.store, user_id=user.id)
    return {a.id: a.course_id for a in accounts}


def _check_choice(
    ctx: _Ctx, choice: RankedChoice, courses: dict[CourseAccountId, CourseId]
) -> None:
    """Every option's window must lie inside its course's tee-sheet hours (the picker lists
    only those; this is the guarantee with script off, where the list is the union). Checked
    in rank order, so the first refusal is the person's best-ranked bad option."""
    ranked = sorted(
        ((w.rank, account_id, w) for account_id, ws in choice.per_account.items() for w in ws)
    )
    for _, account_id, w in ranked:
        ctx.check_window(courses[account_id], w.earliest, w.latest)


def _partial(
    failures: tuple[group_services.GroupFailure, ...],
    saved: int,
    course_of: dict[CourseAccountId, CourseId],
    course_name: Callable[[object], str],
) -> ActionRefusedError:
    """A group saved for SOME courses (§16.2: not one transaction) re-renders the page with what
    was not saved (by course NAME); the saved rows show in the list."""
    detail = "; ".join(
        f"{course_name(c) if (c := course_of.get(f.account_id)) else '?'}: {f.message}"
        for f in failures
    )
    return ActionRefusedError(
        f"Saved for {saved} of {saved + len(failures)} courses. Not saved: {detail}"
    )


def _register_booking_routes(app: FastAPI, pages: _Pages, *, current_user: _Dependency) -> None:
    """MU-R3 (§16.1): the ranked booking form (one date or every week) and the per-account
    default price. Account ids in the form are resolved against the SESSION user's accounts."""
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias
    ctx = pages.ctx

    @app.post("/bookings/date")
    async def book_date(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            courses = await _courses_of(ctx, user)
            choice = parse_ranked_form(form, own_accounts=courses)
            _check_choice(ctx, choice, courses)
            report = await group_services.create_group_one_off(
                ctx.store,
                user_id=user.id,
                target_date=services.parse_date(form),
                choice=choice,
                clock=ctx.clock,
            )
            if report.failures:
                raise _partial(report.failures, len(report.rows), courses, ctx.course_name)
            return "/dates?notice=group_saved"

        return await pages.act(request, user, action, on_error=pages.dates)

    @app.post("/bookings/weekly")
    async def book_weekly(request: Request, user: CurrentUser) -> Response:
        async def action(form: dict[str, str]) -> str:
            courses = await _courses_of(ctx, user)
            choice = parse_ranked_form(form, own_accounts=courses)
            _check_choice(ctx, choice, courses)
            report = await group_services.create_group_rule(
                ctx.store,
                user_id=user.id,
                weekday=services.parse_weekday(form),
                choice=choice,
                policies=ctx.policies,
                cutoff=pages.cutoff_of(user),
                clock=ctx.clock,
            )
            if report.failures:
                raise _partial(report.failures, len(report.rules), courses, ctx.course_name)
            return "/rules?notice=group_rule_saved"

        return await pages.act(request, user, action, on_error=pages.rules)

    @app.post("/accounts/{id}/price")
    async def set_default_price(request: Request, user: CurrentUser, id: str) -> Response:
        aid = CourseAccountId(services.parse_id(id))

        async def action(form: dict[str, str]) -> str:
            await group_services.set_default_price(
                ctx.store,
                user_id=user.id,
                account_id=aid,
                raw_price=form.get("default_max_price", ""),
            )
            return "/accounts?notice=price_saved"

        return await pages.act(request, user, action, on_error=pages.accounts)
