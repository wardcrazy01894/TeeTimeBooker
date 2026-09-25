"""Standing-rule materializer (MULTIUSER_PLAN §7.7, §3.4).

Turns each active ``StandingRule`` into dated PENDING rows 2-3 weeks ahead, idempotent on
UNIQUE(rule_id, target_date). Owners: the web (synchronously on rule create/edit/reactivate) and
the watcher (a cheap tick every run). The booking runner NEVER materializes, so it stays read +
claim only.

Resurrection is decided by the (account, date) HISTORY, never by a document-id collision (round-2
M1): see ``classify_date_history``. A user-terminal row (``models.USER_TERMINAL``) blocks the date
for every rule, including a new rule_id created after an external cancel. A system-withdrawn row
(``models.SYSTEM_WITHDRAW_REASONS``) is reactivated when a rule applies to the date again (weekday
flipped back, rule reactivated).

Rules: no row for a date already frozen (``core.booking_cutoff.frozen_reason`` in the COURSE
timezone) or in the past. A collision with an active explicit row is inserted SUPERSEDED. A
window/party edit rewrites only PENDING, unleased, not-frozen rows (never BOOKED/SKIPPED/
SUPERSEDED). Deactivation, deletion and a weekday change WITHDRAW the rule's PENDING **and
SUPERSEDED** rows (system reason; round-4 D1); BOOKED rows are never touched. Order (§7.7):
``reset_materialized_through`` FIRST, then withdraw the unleased rows, then reset AGAIN (a
concurrent tick may have re-advanced it), then write the rule inactive, so a crash part-way
leaves the tick work to do. Reactivation and a weekday change need no separate reset:
``upsert_rule`` clears the watermark itself in those writes (round-5). Rows skipped because they
were leased are swept later by the tick via ``rows_no_longer_covered`` (missing, inactive or
weekday-moved rules); until then ``load_event_rows`` /
``load_watch_rows`` never offer them and ``finalize_lost`` withdraws them instead of LOST.
Reactivation goes through ``TenantStore.reactivate_rule_row`` only, which restores the row's
pre-supersede status (``superseded_from``: SKIPPED stays SKIPPED, round-5) else PENDING. The
materializer NEVER writes superseded -> pending (only the web's one-off withdraw restores a
superseded row, round-4 D2).

STUB — implemented in MULTIUSER_PLAN MU-6.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from ..core.config import BookingCutoffConfig
from ..core.release_policy import ReleasePolicy
from .models import RequestRow, RowId, StandingRule
from .store import TenantStore

_MU6 = "MULTIUSER_PLAN.md MU-6"

# Floor on the materialization horizon (days ahead of course-local today). The effective horizon
# is max(this, advance_days + 7), which gives >= 1 week of slack before a drop finds no row if the
# watcher (the tick owner) is down.
MIN_HORIZON_DAYS = 21


class RuleConflictError(ValueError):
    """Raised at rule create/edit when the account already has an ACTIVE rule for that weekday.

    v1 allows ONE active rule per (account, weekday) (round-3 SF1, §3.4): a second rule's rows
    would be created ``superseded`` and nothing re-classifies them when the first rule is later
    removed — a silent missed drop. Window-preference lists per weekday are a follow-up.
    """


class DateAction(StrEnum):
    SKIP_USER_TERMINAL = "skip_user_terminal"
    SKIP_FROZEN = "skip_frozen"
    # own row already pending/booked/skipped, or superseded (then the slot is held by the
    # explicit row, and D1 guarantees its rule is active; the materializer never un-supersedes)
    NOTHING = "nothing"
    # own row system-withdrawn and the slot is free -> ``reactivate_rule_row`` (restores
    # ``superseded_from`` or PENDING). NEVER a superseded row (round-4 D1).
    REACTIVATE = "reactivate"
    CREATE = "create"  # no own row; the slot is free
    CREATE_SUPERSEDED = "create_superseded"  # no own row; another active row holds the slot


def classify_date_history(
    rule: StandingRule,
    history: Sequence[RequestRow],
    *,
    frozen: bool,
) -> DateAction:
    """Pure (round-2 M1, §7.7): decide what the materializer does for one date given every row the
    account has for it. Order: frozen -> user-terminal (any rule) -> own row -> slot state."""
    raise NotImplementedError(_MU6)


@dataclass(frozen=True, slots=True)
class MaterializeReport:
    rule_id: str
    inserted: tuple[RowId, ...]
    superseded_on_insert: tuple[RowId, ...]
    skipped_frozen: tuple[date, ...]
    skipped_user_terminal: tuple[date, ...]
    reactivated: tuple[RowId, ...]
    materialized_through: date


def horizon_days(policy: ReleasePolicy) -> int:
    """``max(MIN_HORIZON_DAYS, policy.advance_days + 7)``."""
    raise NotImplementedError(_MU6)


def dates_for_rule(rule: StandingRule, *, today: date, horizon: int) -> list[date]:
    """Pure: every date in ``[today, today + horizon]`` with ``weekday == rule.weekday``."""
    raise NotImplementedError(_MU6)


async def materialize_rule(
    rule: StandingRule,
    *,
    store: TenantStore,
    policy: ReleasePolicy,
    cutoff: BookingCutoffConfig,
    now: datetime,
) -> MaterializeReport:
    """Insert missing rows for ``rule`` up to the horizon, skipping frozen dates, then advance
    ``materialized_through``. Safe to call repeatedly (idempotent). Materialize the FULL horizon
    ``[today, today + horizon]``, never only the dates after the watermark: the daily tick is what
    repairs withdrawn rows left by an interrupted deactivation (§7.7)."""
    raise NotImplementedError(_MU6)


async def materialize_tick(
    *,
    store: TenantStore,
    policies: dict[str, ReleasePolicy],
    cutoff: BookingCutoffConfig,
    now: datetime,
) -> list[MaterializeReport]:
    """Watcher entry: one indexed query (rules with ``materialized_through`` short of the
    horizon), a no-op on most runs, plus the ``rows_no_longer_covered`` sweep that withdraws rows a
    deactivation had to skip while they were leased (§7.7). ``policies`` is keyed by CourseId
    string."""
    raise NotImplementedError(_MU6)


async def apply_rule_edit(
    old: StandingRule,
    new: StandingRule,
    *,
    store: TenantStore,
    policy: ReleasePolicy,
    cutoff: BookingCutoffConfig,
    now: datetime,
) -> MaterializeReport:
    """Window/party change: rewrite PENDING unleased not-frozen rule rows in place. Weekday
    change: withdraw old-weekday PENDING and SUPERSEDED rows (``rule_weekday_changed``), then
    materialize the new weekday. Deactivate (``new.active is False``): withdraw PENDING and
    SUPERSEDED rows (``rule_deactivated``) after ``reset_materialized_through`` and BEFORE
    writing the rule inactive (§7.7); BOOKED rows are untouched (round-4 D1)."""
    raise NotImplementedError(_MU6)
