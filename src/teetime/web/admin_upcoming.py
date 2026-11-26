"""The operator's upcoming-tee-times page, ``/admin/upcoming`` (operator request 2026-11-26: "a
page I can go to to see all upcoming bookings across all accounts, similar to how I can see all
historical ones").

Every signed-in person's dates from today on, on ONE page: the tee times already booked,
soonest first, and the dates the bot is still looking for. The forward-looking sibling of
``admin_bookings`` (the used-tee-times report, which covers past dates).

Built on ``services.dashboard`` per person (the same read each person's own dashboard makes,
so its 21 days ahead in each course's local date), so this page and the person's page can never
disagree. Skipped, cancelled and lost dates are left out: they hold no tee time and the bot is
not looking for one. Read-only; never logs in to a course.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.clock import Clock
from ..tenant.models import RowStatus, User
from ..tenant.store import TenantStore
from . import services
from .services import DashboardRow


@dataclass(frozen=True, slots=True)
class UpcomingBooking:
    user: User
    entry: DashboardRow


@dataclass(frozen=True, slots=True)
class UpcomingReport:
    # Soonest first: by date, then tee time, then email.
    booked: tuple[UpcomingBooking, ...]
    # Dates the bot is still looking for (PENDING), soonest first, then by email.
    pending: tuple[UpcomingBooking, ...]

    @property
    def players(self) -> int:
        """The booked tee times' party sizes, summed."""
        return sum(b.entry.row.party_size for b in self.booked)


def _order(b: UpcomingBooking) -> tuple[object, ...]:
    row = b.entry.row
    # A BOOKED row with no tee time recorded sorts after the day's timed ones.
    return (
        row.target_date,
        b.entry.booked_tee_time_local or "99:99",
        b.user.email.casefold(),
        str(row.id),
    )


async def upcoming_report(store: TenantStore, *, clock: Clock) -> UpcomingReport:
    """One ``services.dashboard`` read per signed-in person."""
    booked: list[UpcomingBooking] = []
    pending: list[UpcomingBooking] = []
    for user in await store.list_users():
        if user.oauth_subject is None:  # never signed in: no accounts, no rows
            continue
        for entry in await services.dashboard(store, user_id=user.id, clock=clock):
            if entry.row.status is RowStatus.BOOKED:
                booked.append(UpcomingBooking(user=user, entry=entry))
            elif entry.row.status is RowStatus.PENDING:
                pending.append(UpcomingBooking(user=user, entry=entry))
    return UpcomingReport(
        booked=tuple(sorted(booked, key=_order)), pending=tuple(sorted(pending, key=_order))
    )
