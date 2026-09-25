"""MU-5: ``InMemoryTenantStore`` runs the TenantStore conformance suite (MULTIUSER_PLAN §3.7).

The suite in ``tests/tenant/conformance.py`` is the contract; ``CosmosTenantStore`` (MU-8b) runs
the same class ``integration``-marked. Only in-memory specifics live below it.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from teetime.core.models import CourseId
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import CourseAccountId, RowId, RowStatus, RuleId, UserId

from .conformance import (
    COURSE_TIMEZONES,
    CUTOFF,
    FROZEN_NOW,
    MAX_ACCOUNTS_PER_COURSE,
    NOW,
    TARGET,
    StoreHarness,
    TenantStoreConformance,
    _rule_row,
    _tenant,
)


def _store() -> InMemoryTenantStore:
    return InMemoryTenantStore(
        course_timezones=COURSE_TIMEZONES,
        cutoff=CUTOFF,
        max_accounts_per_course=MAX_ACCOUNTS_PER_COURSE,
    )


class TestInMemoryTenantStore(TenantStoreConformance):
    @pytest.fixture
    def harness(self) -> StoreHarness:
        store = _store()

        async def slot_pointer(account: CourseAccountId, day: date) -> RowId | None:
            return store.slot_pointer(account, day)

        async def ruleday_pointer(account: CourseAccountId, weekday: int) -> RuleId | None:
            return store.ruleday_pointer(account, weekday)

        return StoreHarness(store=store, slot_pointer=slot_pointer, ruleday_pointer=ruleday_pointer)


async def test_append_audit_redacts_detail() -> None:
    """``detail`` passes through ``core.redaction.redact_payload`` at the store boundary."""
    store = _store()
    await store.append_audit(
        user_id=UserId(uuid4()),
        action="account.connect",
        row_id=None,
        detail={"card_number": "4111111111111111", "note": "ok"},
        at=NOW,
    )
    (entry,) = store.audit_log
    assert "4111111111111111" not in repr(entry)
    assert entry.detail["note"] == "ok"


async def test_unknown_course_timezone_is_a_loud_error() -> None:
    store = InMemoryTenantStore(course_timezones={}, cutoff=CUTOFF)
    with pytest.raises(KeyError):
        store.course_timezone(CourseId("nowhere:1"))


async def test_finalize_withdraws_row_of_deleted_rule_as_rule_deleted() -> None:
    """Nit (round 4): a row whose rule no longer exists is withdrawn ``rule_deleted``, not
    ``rule_deactivated``. In-memory only: the Protocol has no rule delete yet, so the missing
    rule is simulated by removing it from the store's dict."""
    store = _store()
    t = await _tenant(store)
    rule, _row = await _rule_row(store, t)
    del store._rules[rule.id]
    assert await store.finalize_lost(now=FROZEN_NOW) == []
    (after,) = await store.rows_for_account_date(t.account.id, TARGET)
    assert (after.status, after.status_reason) == (RowStatus.WITHDRAWN, "rule_deleted")
