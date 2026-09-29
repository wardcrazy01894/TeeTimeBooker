"""Every user email shows the COURSE wall clock (2026-09-28 bug: a cancel email said "12:30 PM" for
an 8:30 AM EDT booking). ``RequestRow.booked_tee_time`` comes back from Cosmos as a UTC instant,
so a ``UserEvent`` must never take it raw: convert with ``tenant.models.row_local_tee_time``."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from teetime.tenant.models import row_local_tee_time

from .runner_builders import new_store, seed_account

SRC = Path(__file__).resolve().parents[2] / "src" / "teetime"


def test_no_user_event_takes_a_stored_tee_time_raw() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{n}"
        for path in SRC.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"\btee_time=\w+\.booked_tee_time\b", line)
    ]
    assert offenders == [], f"use row_local_tee_time(row): {offenders}"


async def test_row_local_tee_time_converts_a_utc_instant_to_the_course_zone() -> None:
    store = new_store()
    s = await seed_account(store, n=1)
    utc = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)  # 8:30 AM EDT
    local = row_local_tee_time(replace(s.row, booked_tee_time=utc))
    assert local == utc
    assert local is not None and local.tzinfo == ZoneInfo(s.row.timezone)
    assert (local.hour, local.minute) == (8, 30)
    assert row_local_tee_time(s.row) is None  # a row with no booking
