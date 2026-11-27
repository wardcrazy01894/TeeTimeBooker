"""The operator's used-tee-times report, ``/admin/bookings`` (operator request 2026-11-16: "I
might start charging people for the tee times they use").

Every signed-in person's tee times that were still held when they started (a BOOKED row whose
tee time has passed, today's included), in a chosen window, newest first, with a count per
person. A booked-then-cancelled date is not "used": it is counted per person but listed only
when asked for. "Still held" is ``HistoryRow.outcome``, so a reservation the course dropped
from its upcoming list at its tee time (check-in; the watcher once wrote those cancelled) is
used, not cancelled. A reservation the bot never owned (``services.HOW_NOT_OURS``: one the
person made themselves, found on the date) is listed, flagged and NOT counted, since it is not
the site's work to charge for.

Built on ``services.history`` per person (the read each person's own ``/history`` makes, plus
today's started tee times), so the report and the person's page cannot disagree about a past
date. Read-only; never logs in to a course.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from ..core.clock import Clock
from ..tenant.models import RowStatus, User
from ..tenant.store import TenantStore
from . import services
from .services import HistoryRow

# key -> label, in the order the page offers them. A day window ends TODAY: a tee time counts
# from its tee time on (``services.history(started_today=True)``), not from the next midnight.
WINDOWS: dict[str, str] = {
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "365d": "Last 365 days",
    "this-month": "This month",
    "last-month": "Last month",
}
DEFAULT_WINDOW = "30d"
# "Today" and calendar months are the operator's (every hosted course so far is Eastern).
_ET = ZoneInfo("America/New_York")


def window_dates(window: str, *, today: date) -> tuple[date, date]:
    """The inclusive (first, last) dates of a ``WINDOWS`` key as seen on ``today``."""
    if window == "this-month":
        return today.replace(day=1), today
    if window == "last-month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    return today - timedelta(days=int(window.removesuffix("d"))), today


@dataclass(frozen=True, slots=True)
class UsedBooking:
    user: User
    entry: HistoryRow
    # A tee time the bot booked that was still held when its date passed: what a charge is for.
    billable: bool
    # A second booking of ONE ranked request (same group and date) that was still held: the
    # group collapse did not cancel it in time. Listed and flagged, never billable.
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class PersonTotal:
    user: User
    used: int  # billable tee times
    players: int  # their party sizes, summed
    cancelled: int  # booked, then cancelled (by the person or at the course)
    not_ours: int  # held on the date but never booked by the bot
    duplicates: int = 0  # extra bookings of one ranked request (see ``UsedBooking.duplicate``)


@dataclass(frozen=True, slots=True)
class UsageReport:
    window: str  # a WINDOWS key
    first: date
    last: date
    include_cancelled: bool
    bookings: tuple[UsedBooking, ...]  # newest first, then by email
    # Most used first, then by email; people with nothing in the window are left out.
    totals: tuple[PersonTotal, ...]

    @property
    def label(self) -> str:
        return WINDOWS[self.window]

    @property
    def used(self) -> int:
        return sum(t.used for t in self.totals)


def _one_per_request(entries: list[HistoryRow]) -> list[HistoryRow]:
    """``entries`` with at most one per ranked request: of several held rows sharing a
    ``group_id`` on one date (the group invariant is one, ``tenant/groups.py``; two means a
    collapse cancel failed until the date passed), keep the best-ranked. Ungrouped rows all
    stay."""
    best: dict[tuple[object, date], HistoryRow] = {}
    for e in entries:
        if e.row.group_id is None:
            continue
        key = (e.row.group_id, e.row.target_date)
        if key not in best or _rank(e) < _rank(best[key]):
            best[key] = e
    return [
        e for e in entries if e.row.group_id is None or best[e.row.group_id, e.row.target_date] is e
    ]


def _rank(entry: HistoryRow) -> tuple[int, int, str]:
    unranked = 1_000_000
    row = entry.row
    return (entry.booked_rank or unranked, row.group_rank or unranked, str(row.id))


async def usage_report(
    store: TenantStore, *, clock: Clock, window: str, include_cancelled: bool
) -> UsageReport:
    """The report for ``window`` (an unknown key is the default: the value comes from a query
    string). One ``services.history`` read per signed-in person."""
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    first, last = window_dates(window, today=clock.now_utc().astimezone(_ET).date())
    bookings: list[UsedBooking] = []
    totals: list[PersonTotal] = []
    for user in await store.list_users():
        if user.oauth_subject is None:  # never signed in: no accounts, no rows
            continue
        entries = await services.history(
            store, user_id=user.id, clock=clock, first=first, last=last, started_today=True
        )
        held = [e for e in entries if e.outcome is RowStatus.BOOKED]
        cancelled = [e for e in entries if e.outcome is RowStatus.CANCELLED]
        ours = [e for e in held if e.how != services.HOW_NOT_OURS]
        used = _one_per_request(ours)
        if not held and not cancelled:
            continue
        totals.append(
            PersonTotal(
                user=user,
                used=len(used),
                players=sum(e.row.party_size for e in used),
                cancelled=len(cancelled),
                not_ours=len(held) - len(ours),
                duplicates=len(ours) - len(used),
            )
        )
        listed = held + cancelled if include_cancelled else held
        bookings += [
            UsedBooking(
                user=user, entry=e, billable=e in used, duplicate=e in ours and e not in used
            )
            for e in listed
        ]
    bookings.sort(key=lambda b: b.user.email.casefold())
    bookings.sort(key=lambda b: b.entry.row.target_date, reverse=True)
    totals.sort(key=lambda t: (-t.used, t.user.email.casefold()))
    return UsageReport(
        window=window,
        first=first,
        last=last,
        include_cancelled=include_cancelled,
        bookings=tuple(bookings),
        totals=tuple(totals),
    )
