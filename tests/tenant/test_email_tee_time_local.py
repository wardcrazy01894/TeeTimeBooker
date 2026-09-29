"""Every user email shows the COURSE wall clock (2026-09-28 bug: a cancel email said "12:30 PM" for
an 8:30 AM EDT booking). ``RequestRow.booked_tee_time`` comes back from Cosmos as a UTC instant,
so a ``UserEvent`` must never take it raw: convert with ``tenant.models.row_local_tee_time``."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from teetime.core.adapter import CancelError
from teetime.tenant.models import RankedWindow, RequestRow, row_local_tee_time
from teetime.tenant.notify import UserEventKind
from teetime.tenant.runner import run_release_event

from .runner_builders import (
    EVENT,
    KEYRING,
    POLICIES,
    TZ,
    WINDOW,
    NullUserNotifier,
    ScriptedFactory,
    SpyStore,
    blind_adapter,
    new_store,
    race_clock,
    scheduler,
    seed_account,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "teetime"


# ``tee_time`` assigned or passed from any ``….booked_tee_time`` (spaces, attribute chains,
# a parenthesised multi-line value), searched over whole files so line breaks cannot hide it.
_RAW_STORED = re.compile(r"\btee_time\s*=\s*\(?\s*[\w.]*\.booked_tee_time\b")


def test_no_user_event_takes_a_stored_tee_time_raw() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{text.count(chr(10), 0, m.start()) + 1}"
        for path in SRC.rglob("*.py")
        for text in [path.read_text()]
        for m in _RAW_STORED.finditer(text)
    ]
    assert offenders == [], f"use row_local_tee_time(row): {offenders}"


def test_the_guard_catches_every_spelling() -> None:
    for bad in (
        "tee_time=row.booked_tee_time,",
        "tee_time = row.booked_tee_time",
        "tee_time=event_row.row.booked_tee_time",
        "tee_time=self.row.booked_tee_time",
        "tee_time=(\n        row.booked_tee_time\n    )",
    ):
        assert _RAW_STORED.search(bad), bad
    assert not _RAW_STORED.search("tee_time=row_local_tee_time(row),")
    assert not _RAW_STORED.search("booked_tee_time=tee,")


class _UtcRowsStore(SpyStore):
    """What Cosmos does: stored tee times come back as UTC instants."""

    async def rows_in_groups(self, keys: Any) -> list[RequestRow]:
        rows = await self._inner.rows_in_groups(keys)
        return [
            replace(r, booked_tee_time=r.booked_tee_time.astimezone(UTC))
            if r.booked_tee_time is not None
            else r
            for r in rows
        ]


async def test_booker_double_held_email_shows_the_course_wall_clock() -> None:
    """The booker's same-drop group collapse: the worse booking's cancel fails, so the user is
    told they hold two tee times. Its time must read in the course zone even though the store
    handed the row back in UTC."""
    inner = new_store()
    group = uuid4()
    worse = await seed_account(inner, n=1, group_id=group, options=(RankedWindow(3, *WINDOW),))
    await seed_account(inner, n=2, group_id=group, options=(RankedWindow(1, *WINDOW),))
    clock = race_clock()
    stuck = blind_adapter()
    stuck.set_cancel_to_raise(CancelError("course said no"))
    notifier = NullUserNotifier()

    await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=_UtcRowsStore(inner, clock),
        clock=clock,
        scheduler=scheduler(),
        keyring=KEYRING,
        adapter_factory=ScriptedFactory(adapters={worse.account.id: stuck}),
        notifier=notifier,
        dry_run=False,
        wait=True,
    )

    (event,) = [e for e in notifier.events if e.kind is UserEventKind.DOUBLE_HELD]
    held = (await inner.get_row(worse.row.id, user_id=worse.user.id)).booked_tee_time
    assert held is not None and event.tee_time == held
    assert event.tee_time.tzinfo == ZoneInfo(TZ)
    assert event.tee_time.hour == held.astimezone(ZoneInfo(TZ)).hour


async def test_row_local_tee_time_converts_a_utc_instant_to_the_course_zone() -> None:
    store = new_store()
    s = await seed_account(store, n=1)
    utc = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)  # 8:30 AM EDT
    local = row_local_tee_time(replace(s.row, booked_tee_time=utc))
    assert local == utc
    assert local is not None and local.tzinfo == ZoneInfo(s.row.timezone)
    assert (local.hour, local.minute) == (8, 30)
    assert row_local_tee_time(s.row) is None  # a row with no booking
