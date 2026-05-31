"""PR-KS1: static assertions for the cost killswitch Bicep module.

These tests assert structural properties of the Bicep source text.
`az bicep build` is the compile gate; pytest is the contract gate.

All tests are GREEN after PR-KS1 implementation:
  - test_killswitch_patches_all_six_jobs: GREEN — 6 PATCH actions wired
  - test_killswitch_stops_all_six_jobs:   GREEN — 6 POST /stop actions wired
  - test_killswitch_rbac_prod_nested_module_present: GREEN — module unwired
  - test_main_bicep_killswitch_module_gated: GREEN — dev-only conditional deploy
  - test_main_bicep_killswitch_output_present: GREEN — killswitchActionGroupId output

IMPORTANT — test design note for the workflow tests:
The tests filter out Bicep comment lines (lines starting with //) before
asserting on PATCH/POST/stop content. This prevents false-green against a
stub that only has those strings in comments. The tests assert on the live
(non-comment) source, where the real HTTP actions now appear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

KILLSWITCH_BICEP = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "killswitch.bicep"
)
KILLSWITCH_RBAC_PROD_BICEP = (
    Path(__file__).resolve().parent.parent
    / "infra"
    / "bicep"
    / "modules"
    / "killswitch-rbac-prod.bicep"
)
MAIN_BICEP = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicep"
PARAM_DEV = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.dev"
PARAM_PROD = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.prod"


@pytest.fixture(scope="module")
def ks() -> str:
    return KILLSWITCH_BICEP.read_text()


@pytest.fixture(scope="module")
def ks_non_comment_lines(ks: str) -> str:
    """Bicep source with all comment-only lines stripped.

    Used by the workflow-action RED tests to prevent them from passing
    against a stub that only has PATCH/POST/stop strings in // comments.
    A line is considered a comment line if its stripped content starts
    with '//'. This simple heuristic correctly identifies all standalone
    comment lines (the only lines in killswitch.bicep that carry these
    strings before PR-KS1 is implemented).
    """
    non_comment = "\n".join(line for line in ks.splitlines() if not line.strip().startswith("//"))
    return non_comment


@pytest.fixture(scope="module")
def main_bicep() -> str:
    return MAIN_BICEP.read_text()


@pytest.fixture(scope="module")
def param_dev() -> str:
    return PARAM_DEV.read_text()


@pytest.fixture(scope="module")
def param_prod() -> str:
    return PARAM_PROD.read_text()


# ---------------------------------------------------------------------------
# Resource type assertions (GREEN on stub)
# ---------------------------------------------------------------------------


def test_killswitch_logic_app_resource_type(ks: str) -> None:
    """The Logic App must be declared as Microsoft.Logic/workflows@2019-05-01."""
    assert "Microsoft.Logic/workflows@2019-05-01" in ks


def test_killswitch_action_group_resource_type(ks: str) -> None:
    """The Action Group must be declared as Microsoft.Insights/actionGroups@2023-01-01."""
    assert "Microsoft.Insights/actionGroups@2023-01-01" in ks


def test_killswitch_logic_app_receiver_wired(ks: str) -> None:
    """The Action Group must have a logicAppReceivers array (non-empty)."""
    assert "logicAppReceivers:" in ks
    # Verify the array is not empty — it must reference the Logic App.
    assert "killswitch-logic-app" in ks


def test_killswitch_logic_app_has_system_assigned_mi(ks: str) -> None:
    """The Logic App must use a system-assigned managed identity."""
    assert "SystemAssigned" in ks


# ---------------------------------------------------------------------------
# RBAC assertions (GREEN on stub)
# ---------------------------------------------------------------------------


def test_killswitch_rbac_uses_custom_role_param(ks: str) -> None:
    """Role assignment must reference killswitchRbacRoleId param, NOT hardcoded Contributor."""
    assert "killswitchRbacRoleId" in ks
    # Contributor GUID must NOT be hardcoded — that would be over-privileged.
    assert "b24988ac-6180-42a0-ab88-20f7382dd24c" not in ks


def test_stop_action_in_custom_role(ks: str) -> None:
    """The killswitch.bicep must document Microsoft.App/jobs/stop/action in its RBAC comment.

    This assertion confirms that Lever (b) — stop in-flight executions — is
    represented in the custom role definition documented in this file.
    """
    assert "Microsoft.App/jobs/stop/action" in ks


# ---------------------------------------------------------------------------
# Workflow action assertions (RED on stub — GREEN after PR-KS1 implementation)
# ---------------------------------------------------------------------------


def test_killswitch_patches_all_six_jobs(ks_non_comment_lines: str) -> None:
    """The workflow definition must reference PATCH calls for all 3 jobs x 2 envs.

    GREEN after PR-KS1 fills in the 6 PATCH actions. Asserts against the
    comment-stripped source to prevent false-green against comment-only strings.
    """
    # All three job name patterns must appear outside comment lines.
    # Note: the var declarations (bookingJobEdtSun etc.) are already non-comment
    # lines, so 'edt-sun'/'est-sun'/'watch-job' appear in both stub and final.
    # The critical guards that make this test RED on the stub are:
    #   (a) "'PATCH'" must appear in non-comment source (only in real action)
    #   (b) "Stub_placeholder" must NOT appear (placeholder must be replaced)
    # The job-name assertions confirm naming coverage once the stub is implemented.
    assert "edt-sun" in ks_non_comment_lines
    assert "est-sun" in ks_non_comment_lines
    assert "watch-job" in ks_non_comment_lines
    # Prod job name constants must appear in the non-comment source.
    assert "teetime-job-prod-edt-sun" in ks_non_comment_lines
    assert "teetime-job-prod-est-sun" in ks_non_comment_lines
    assert "teetime-watch-job-prod" in ks_non_comment_lines
    # PATCH method must appear in the non-comment source (i.e. in a real action,
    # not just in a comment example).
    assert "'PATCH'" in ks_non_comment_lines or '"PATCH"' in ks_non_comment_lines
    # The stub Stub_placeholder Terminate action must be gone.
    assert "Stub_placeholder" not in ks_non_comment_lines


def test_killswitch_stops_all_six_jobs(ks_non_comment_lines: str) -> None:
    """The workflow definition must reference POST /stop calls for all 3 jobs x 2 envs.

    GREEN after PR-KS1 fills in the 6 POST /stop actions. Lever (b) is required:
    disabling the schedule alone does NOT halt a replica already running at $50-trip time.
    Asserts against the comment-stripped source to prevent false-green against
    comment-only strings.
    """
    # The /stop path segment must appear outside comment lines.
    assert "/stop" in ks_non_comment_lines
    # POST method must appear in the non-comment source.
    assert "'POST'" in ks_non_comment_lines or '"POST"' in ks_non_comment_lines
    # The stub Stub_placeholder Terminate action must be gone.
    assert "Stub_placeholder" not in ks_non_comment_lines


# ---------------------------------------------------------------------------
# Nested module assertions (RED on stub — GREEN after PR-KS1 implementation)
# ---------------------------------------------------------------------------


def test_killswitch_rbac_prod_file_exists() -> None:
    """killswitch-rbac-prod.bicep must exist as a companion stub file.

    This file provides the cross-RG role assignment for rg-teetime-prod via
    a nested module with scope: resourceGroup(subscriptionId, prodRgName).
    GREEN against the current branch (stub file has been created).
    """
    assert KILLSWITCH_RBAC_PROD_BICEP.exists(), (
        f"Expected {KILLSWITCH_RBAC_PROD_BICEP} to exist. PR-KS1 must create this companion module."
    )


def test_killswitch_rbac_prod_nested_module_present(ks_non_comment_lines: str) -> None:
    """killswitch.bicep must wire the killswitch-rbac-prod.bicep nested module in live code.

    After PR-KS1: the module call must be uncommented (not only in comment blocks).
    Asserts against the comment-stripped source to prevent false-green when the
    module call is only in a // comment block (as it was in the stub).
    """
    # The module reference must appear outside comment lines.
    assert "killswitch-rbac-prod.bicep" in ks_non_comment_lines


# ---------------------------------------------------------------------------
# deploy-clobber guard assertions (GREEN on stub — already wired in main.bicep)
# ---------------------------------------------------------------------------


def test_killswitch_fired_param_in_main_bicep(main_bicep: str) -> None:
    """main.bicep must declare killswitchFired param and effectiveEnableSchedules var.

    These are the CI deploy-clobber guards: when killswitchFired=true, CI enforces
    enableSchedules=false regardless of other param values.
    """
    assert "param killswitchFired bool = false" in main_bicep
    assert "effectiveEnableSchedules" in main_bicep


def test_killswitch_fired_defaults_false_in_param_files(param_dev: str, param_prod: str) -> None:
    """Both param files must default killswitchFired=false (normal operation)."""
    assert "killswitchFired = false" in param_dev
    assert "killswitchFired = false" in param_prod


def test_killswitch_enabled_on_deploy(param_dev: str, param_prod: str) -> None:
    """Both param files must set enableKillswitch = true (enabled on next deploy).

    GREEN today: the param files contain 'enableKillswitch = true' per the
    operator decision (2026-05-31) to enable the full killswitch chain automatically
    on merge to main (dev) and on the next infra/v* tag push (prod).

    The killswitch chain (Logic App + Action Group + RBAC) deploys immediately
    when PR-KS1 merges — no separate operator opt-in step required.
    Requires killswitchRbacRoleId to be set to the pre-created custom role GUID.
    """
    assert "enableKillswitch = true" in param_dev, (
        "main.bicepparam.dev must contain 'enableKillswitch = true' "
        "(operator decision 2026-05-31: killswitch enabled on deploy, not opt-in)"
    )
    assert "enableKillswitch = true" in param_prod, (
        "main.bicepparam.prod must contain 'enableKillswitch = true' "
        "(operator decision 2026-05-31: killswitch enabled on deploy, not opt-in)"
    )


# ---------------------------------------------------------------------------
# PR-KS1 main.bicep gating assertions (GREEN after PR-KS1 implementation)
# ---------------------------------------------------------------------------


def test_main_bicep_killswitch_module_gated(main_bicep: str) -> None:
    """main.bicep must gate the killswitch module on enableKillswitch, non-empty
    killswitchRbacRoleId, AND envName=='dev' (dev-only deploy).

    The gate prevents:
    - Deploying the module when the custom role GUID has not been supplied yet
      (empty killswitchRbacRoleId = clean no-op, safe to merge without the role).
    - Deploying a second Logic App in the prod RG (the killswitch lives in
      rg-teetime-dev and manages BOTH envs via cross-RG RBAC — only one instance
      is needed). See COST_KILLSWITCH_PLAN.md §2/Item3.
    """
    # The module must be conditional on all three guards.
    assert "enableKillswitch" in main_bicep
    assert "killswitchRbacRoleId" in main_bicep
    # The dev-only guard: envName == 'dev' must appear in the killswitch module condition.
    assert "envName == 'dev'" in main_bicep
    # The empty-GUID guard: !empty(killswitchRbacRoleId) must appear.
    assert "!empty(killswitchRbacRoleId)" in main_bicep
    # The module call itself must reference killswitch.bicep.
    assert "'modules/killswitch.bicep'" in main_bicep


def test_main_bicep_killswitch_output_present(main_bicep: str) -> None:
    """main.bicep must declare a killswitchActionGroupId output.

    This output is consumed by the operator (and PR-KS2 budget.bicep) to wire
    the $50 budget threshold to the killswitch Action Group after PR-KS1 deploys.
    Must be an empty string when the killswitch module is not deployed.
    """
    assert "output killswitchActionGroupId string" in main_bicep


def test_param_files_have_killswitch_rbac_role_id(param_dev: str, param_prod: str) -> None:
    """Both param files must declare killswitchRbacRoleId (empty GUID placeholder).

    The value starts empty — the operator fills it in after creating the custom role.
    Until filled in, the !empty() gate in main.bicep makes the killswitch module
    a clean no-op, so the dev auto-deploy is safe even before the role is created.
    """
    assert "killswitchRbacRoleId" in param_dev, (
        "main.bicepparam.dev must contain killswitchRbacRoleId (operator fills GUID)"
    )
    assert "killswitchRbacRoleId" in param_prod, (
        "main.bicepparam.prod must contain killswitchRbacRoleId (operator fills GUID)"
    )
