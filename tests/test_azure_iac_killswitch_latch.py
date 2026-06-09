"""Full-repo-scan finding H1: the killswitchFired clobber-guard latch was inert in CI.

The cost-killswitch design (COST_KILLSWITCH_PLAN.md §2/Item2) is a checked-in safety
latch: when the $50 budget fires, the operator sets `param killswitchFired = true` in
the .bicepparam files and pushes, and NO subsequent CI deploy may re-arm the cron
schedules. But `.github/workflows/azure-iac.yml` passes every parameter INLINE and never
read the .bicepparam files, so `killswitchFired` always fell back to its bicep default
(`false`) on the auto-deploy path — the latch had no effect on the path that actually
runs. main.bicep:73 `effectiveEnableSchedules = enableSchedules && !killswitchFired`
collapsed to `enableSchedules` (also never passed → default true).

These static tests couple the workflow to the latch: CI MUST source `killswitchFired`
(and `enableSchedules`) from the checked-in .bicepparam file and pass them into every
`az deployment group create`, so the latch is authoritative on the auto-deploy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = _ROOT / ".github" / "workflows" / "azure-iac.yml"
PARAM_DEV = _ROOT / "infra" / "bicep" / "main.bicepparam.dev"
PARAM_PROD = _ROOT / "infra" / "bicep" / "main.bicepparam.prod"


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text()


def test_workflow_reads_param_file_for_the_latch(workflow: str) -> None:
    """CI must read the checked-in .bicepparam file (the latch lives there), not rely
    on inline defaults that ignore it."""
    assert "main.bicepparam.${ENVNAME}" in workflow


def test_workflow_threads_killswitch_fired_into_every_deploy(workflow: str) -> None:
    """The parsed latch value must be passed into each deploy. Two jobs (dev + prod)
    x two passes (bootstrap + real image) = 4 deploys, each consuming the env var the
    parse step exported."""
    n_deploys = workflow.count("deployment group cr" + "eate")
    assert n_deploys == 4  # guard against the count drifting if a pass is added/removed
    assert workflow.count('killswitchFired="${KILLSWITCH_FIRED}"') == n_deploys


def test_workflow_threads_enable_schedules_into_every_deploy(workflow: str) -> None:
    """`enableSchedules` (the other half of effectiveEnableSchedules) must likewise be
    CI-controllable from the param file rather than defaulting silently."""
    n_deploys = workflow.count("deployment group cr" + "eate")
    assert workflow.count('enableSchedules="${ENABLE_SCHEDULES}"') == n_deploys


@pytest.mark.parametrize("param_file", [PARAM_DEV, PARAM_PROD])
def test_param_files_declare_both_latch_params(param_file: Path) -> None:
    """Both latch params must be declared in each .bicepparam file so CI can parse them
    and the checked-in value is the single source of truth."""
    text = param_file.read_text()
    assert "param killswitchFired" in text
    assert "param enableSchedules" in text
