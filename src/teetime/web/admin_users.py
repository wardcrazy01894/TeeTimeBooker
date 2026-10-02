"""The operator's user list on ``/admin/users`` (operator request 2026-09-29).

One row per user in any status: whether the invite has been used (INVITED = never signed in),
the sign-in provider, the connected courses, active weekly bookings and the next 21 days' dated
rows by status, plus (operator request 2026-10-02) the dates themselves under a disclosure: what
was asked for, the status and the booked tee time, from the same ``services.dashboard`` read the
person's own dashboard uses. Read-only and NEVER logs in to a course. The route is
operator-gated; this is the only place the web lists users at all (``TenantStore.list_users``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..core.clock import Clock
from ..core.models import CourseId
from ..tenant.materialize import MIN_HORIZON_DAYS
from ..tenant.models import RejectedSignin, RowStatus, User
from ..tenant.store import TenantStore
from . import services
from .services import DashboardRow

# The statuses counted in the "next 21 days" column, in display order.
_COUNTED = (RowStatus.BOOKED, RowStatus.PENDING)


@dataclass(frozen=True, slots=True)
class UserOverview:
    user: User
    course_ids: tuple[CourseId, ...]
    weekly_rules: int  # active standing rules
    upcoming: tuple[tuple[str, int], ...]  # ("booked", 2), ("pending", 1): non-zero only
    # The next 21 days' rows as the person's own dashboard shows them (date, options, status,
    # booked tee time); empty for someone who never signed in.
    dates: tuple[DashboardRow, ...] = ()

    @property
    def signed_in(self) -> bool:
        return self.user.oauth_subject is not None


async def user_overviews(store: TenantStore, *, clock: Clock) -> list[UserOverview]:
    """Every user (``list_users`` order: by email) with what they have set up. A user who never
    signed in has no accounts, so their reads are skipped."""
    today = clock.now_utc().date()
    out: list[UserOverview] = []
    for user in await store.list_users():
        if user.oauth_subject is None:
            out.append(UserOverview(user=user, course_ids=(), weekly_rules=0, upcoming=()))
            continue
        accounts = await store.list_accounts_for_user(user.id)
        rules = await store.list_rules_for_user(user.id)
        rows = await store.list_rows_for_user(
            user.id, from_date=today, to_date=today + timedelta(days=MIN_HORIZON_DAYS)
        )
        counts = Counter(r.status for r in rows)
        out.append(
            UserOverview(
                user=user,
                course_ids=tuple(a.course_id for a in accounts),
                weekly_rules=sum(1 for r in rules if r.active),
                upcoming=tuple((s.value, counts[s]) for s in _COUNTED if counts[s]),
                dates=tuple(await services.dashboard(store, user_id=user.id, clock=clock)),
            )
        )
    return out


# The operator reads attempt times in Eastern time (every hosted course so far is ET).
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class UninvitedAttempt:
    record: RejectedSignin
    emails: tuple[str, ...]  # verified, and not already a user's (invited or bound)

    @property
    def first_local(self) -> datetime:
        return self.record.first_at.astimezone(_ET)

    @property
    def last_local(self) -> datetime:
        return self.record.last_at.astimezone(_ET)


async def uninvited_attempts(
    store: TenantStore, *, users: list[User], clock: Clock
) -> list[UninvitedAttempt]:
    """Uninvited sign-ins within the retention, newest first. An email that is now a user's
    (e.g. just invited from this list) is dropped, and so is an attempt with none left; an
    attempt that had no verified email at all is kept (it still shows someone tried)."""
    known = {u.email.casefold() for u in users}
    out: list[UninvitedAttempt] = []
    for record in await store.list_rejected_signins(now=clock.now_utc()):
        emails = tuple(e for e in record.emails if e.casefold() not in known)
        if record.emails and not emails:
            continue
        out.append(UninvitedAttempt(record=record, emails=emails))
    return out
