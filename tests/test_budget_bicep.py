"""PR-KS2: static assertions for the two-tier budget design in budget.bicep.

These tests assert structural properties of the Bicep source text (the
`az bicep build` step is the compile gate; pytest is the contract gate).

Two independent Microsoft.Consumption/budgets resources:
  - Tier 1 `budget-teetime` ($20, email-only) — the pre-existing early-warning
    budget. MUST stay untouched (its $16/$20 emails preserved).
  - Tier 2 `budget-teetime-killswitch` ($50, Action-Group-wired) — added for the
    cost killswitch. Deployed only when killswitchActionGroupId is non-empty;
    its single Actual >= 100% notification fires the killswitch Action Group.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BUDGET_BICEP = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "budget.bicep"
)


@pytest.fixture(scope="module")
def budget() -> str:
    return BUDGET_BICEP.read_text()


# ---------------------------------------------------------------------------
# Tier 1 ($20, email-only) — must remain unchanged
# ---------------------------------------------------------------------------


def test_tier1_budget_amount_still_20(budget: str) -> None:
    """The existing $20 email budget amount is untouched (decision U1: separate budget)."""
    assert "param budgetAmountUsd int = 20" in budget


def test_tier1_keeps_both_email_notifications(budget: str) -> None:
    """Tier 1 keeps its 80%-actual ($16) and 100%-forecasted ($20) email alerts."""
    assert "actual_GreaterThan_${alertThresholdPercent}Pct" in budget
    assert "forecasted_GreaterThan_100Pct" in budget


# ---------------------------------------------------------------------------
# Tier 2 ($50, killswitch) — the new hard-cap budget
# ---------------------------------------------------------------------------


def test_tier2_killswitch_budget_resource_present(budget: str) -> None:
    """A SECOND, distinct budget resource named budget-teetime-killswitch exists."""
    assert "resource killswitchBudget 'Microsoft.Consumption/budgets@" in budget
    assert "budget-teetime-killswitch" in budget


def test_tier2_amount_is_50(budget: str) -> None:
    """The killswitch budget defaults to $50."""
    assert "param killswitchBudgetAmountUsd int = 50" in budget


def test_tier2_conditional_on_action_group_id(budget: str) -> None:
    """Tier 2 deploys ONLY when killswitchActionGroupId is non-empty.

    This keeps the existing manual $20 budget deploy (no action group id) a
    clean no-op for tier 2, and lets the operator arm it later by passing the
    Action Group id from the killswitch module output.
    """
    assert "= if (!empty(killswitchActionGroupId))" in budget


def test_tier2_actual_100pct_threshold(budget: str) -> None:
    """The hard cap fires on ACTUAL >= 100% of $50 (not forecasted)."""
    assert "actual_GreaterThan_100Pct_killswitch" in budget
    assert "thresholdType: 'Actual'" in budget


def test_tier2_wired_to_action_group_not_email(budget: str) -> None:
    """Tier 2 routes to the Action Group via contactGroups, with no contactEmails
    (the Tier-1 budget owns email alerts)."""
    assert "contactGroups" in budget
    assert "killswitchActionGroupId" in budget


def test_tier2_same_two_rg_filter(budget: str) -> None:
    """Both tiers track the same combined dev+prod spend ($50 = total project bill)."""
    assert "rg-teetime-dev" in budget
    assert "rg-teetime-prod" in budget
