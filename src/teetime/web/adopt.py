"""MU-16b (MULTIUSER_PLAN §11 steps 6-7): the operator-only "Adopt existing bookings" action.

At the prod cutover the TOML bot already holds tee times the tenant rows now cover. The operator
clicks "Refresh from course" (a live login that persists a TRUSTED snapshot), reviews the preview
on the accounts page, ticks the confirm box and adopts: each matching reservation is recorded as
an OWNED booking (``adopted_owned``), so the watcher may upgrade it (``tenant.seed``).

Deviation from the plan's ``teetime tenant-seed --adopt`` CLI: the web app already holds the
managed identity, the keyring and the adapters, so running it here needs no local secrets and no
temporary write role on the prod database. The preview and the write both re-plan server-side
from the persisted snapshot; nothing from the form except the confirm box is trusted. The
snapshot must be trusted and at most ``MAX_SNAPSHOT_AGE`` old (§7.5), because the TOML watcher can
upgrade in between.
"""

# NO `from __future__ import annotations` (see web/pages.py): FastAPI must resolve the
# closure-bound `Depends` in the handler annotation eagerly.

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request, Response

from ..core.clock import Clock
from ..tenant.models import CourseAccount, CourseAccountId, User, UserId
from ..tenant.seed import Adoption, SeedReport, apply_adoptions, plan_adoptions
from ..tenant.store import TenantStore
from . import auth
from .services import ActionRefusedError, InvalidInputError, WebNotFoundError, parse_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .app import _Ctx
    from .pages import _Pages

MAX_SNAPSHOT_AGE = timedelta(minutes=15)
_HORIZON = timedelta(days=30)
STALE_MESSAGE = (
    'Click "Refresh from course" first: adoption uses a reservation list from the last 15 minutes.'
)


@dataclass(frozen=True, slots=True)
class AdoptionLine:
    """One planned adoption, for display (course-local date and time)."""

    target_date: date
    tee_time: str
    raw_id: str
    kind: str


async def _plans(
    store: TenantStore, *, user_id: UserId, account: CourseAccount, clock: Clock
) -> list[Adoption] | None:
    """The plan from a fresh trusted snapshot, or None when there is none."""
    now = clock.now_utc()
    snapshot = await store.get_snapshot(account.id)
    if snapshot is None or not snapshot.trusted or now - snapshot.observed_at > MAX_SNAPSHOT_AGE:
        return None
    today = now.date()
    rows = [
        r
        for r in await store.list_rows_for_user(
            user_id, from_date=today - timedelta(days=1), to_date=today + _HORIZON
        )
        if r.course_account_id == account.id
    ]
    owned = []
    for day in sorted({r.target_date for r in rows}):
        owned.extend(await store.list_owned_bookings(account.id, target_date=day))
    return plan_adoptions(rows, snapshot, owned=owned)


async def preview(
    store: TenantStore, *, user_id: UserId, account: CourseAccount, clock: Clock
) -> list[AdoptionLine] | None:
    """What adopting would record now; None = no fresh trusted snapshot (refresh first)."""
    plans = await _plans(store, user_id=user_id, account=account, clock=clock)
    if plans is None:
        return None
    return [
        AdoptionLine(
            target_date=p.row.target_date,
            tee_time=p.tee_time.astimezone(ZoneInfo(p.row.timezone)).strftime("%H:%M"),
            raw_id=p.raw_id,
            kind=p.kind.value,
        )
        for p in plans
    ]


async def adopt(
    store: TenantStore,
    *,
    user_id: UserId,
    account_id: CourseAccountId,
    clock: Clock,
    confirmed: bool,
) -> SeedReport:
    if not confirmed:
        raise InvalidInputError("tick the box to confirm these reservations were made by the bot")
    account = await store.get_account(account_id, user_id=user_id)
    if account is None:
        raise WebNotFoundError
    plans = await _plans(store, user_id=user_id, account=account, clock=clock)
    if plans is None:
        raise ActionRefusedError(STALE_MESSAGE)
    report = await apply_adoptions(store, plans, clock=clock)
    if report.failed:
        raise ActionRefusedError(
            f"Adopted {len(report.adopted)} of {len(plans)}; the rest changed while you were "
            "looking (booking in progress?). Refresh from course and try again."
        )
    return report


def register_adopt_routes(
    app: FastAPI, pages: _Pages, *, current_user: Callable[..., Awaitable[Any]]
) -> None:
    ctx: _Ctx = pages.ctx
    CurrentUser = Annotated[User, Depends(current_user)]  # noqa: N806 — type alias

    @app.post("/accounts/{id}/adopt")
    async def adopt_bookings(request: Request, user: CurrentUser, id: str) -> Response:
        if not auth.is_operator(user, operator_email=ctx.settings.operator_email):
            raise auth.ForbiddenError("operator only")
        aid = CourseAccountId(parse_id(id))

        async def action(form: dict[str, str]) -> str:
            await adopt(
                ctx.store,
                user_id=user.id,
                account_id=aid,
                clock=ctx.clock,
                confirmed=form.get("confirm") == "on",
            )
            return "/accounts?notice=adopted"

        return await pages.act(request, user, action, on_error=pages.accounts)
