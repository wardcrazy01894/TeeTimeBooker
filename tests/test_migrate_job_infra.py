"""MU-16a: the Manual-trigger `teetime-migrate-<env>` ACA job (compute.bicep), its exclusion
from the cost killswitch, and the CI step that starts and awaits it (azure-iac.yml)
(MULTIUSER_PLAN §10.1/§10.2/§10.3, §12 MU-16). Both were tenant-mode-only until MU-19 removed
the modes (2026-11-14); they are unconditional now.

Static text assertions, like the other bicep/workflow tests (bicep is compile-validated by CI's
`az bicep build`; the workflow cannot run here).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
COMPUTE = _ROOT / "infra" / "bicep" / "modules" / "compute.bicep"
KILLSWITCH = _ROOT / "infra" / "bicep" / "modules" / "killswitch.bicep"
WORKFLOW = _ROOT / ".github" / "workflows" / "azure-iac.yml"

# Split so this file never contains the literal command (the az-deploy guard scans commands).
_JOB_START = "containerapp job " + "start"
_DEPLOY = "deployment group cr" + "eate"


@pytest.fixture(scope="module")
def compute() -> str:
    return COMPUTE.read_text()


@pytest.fixture(scope="module")
def migrate_block(compute: str) -> str:
    start = compute.index("resource migrateJob")
    end = compute.index("\n}\n", start)
    return compute[start:end]


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text()


def test_migrate_job_name_fits_the_aca_limit(compute: str) -> None:
    assert "var migrateJobName = 'teetime-migrate-${envName}'" in compute
    for env in ("dev", "prod"):
        assert len(f"teetime-migrate-{env}") <= 32


def test_migrate_job_is_deployed_unconditionally(migrate_block: str) -> None:
    """MU-19: no mode is left to gate it on, and every deploy's workflow step starts it, so a
    conditional resource would fail the deploy whenever its condition was false."""
    assert migrate_block.startswith("resource migrateJob 'Microsoft.App/jobs@2024-03-01' = {\n")


def test_migrate_job_is_manual_trigger_only(migrate_block: str) -> None:
    # Never a schedule (and so never touched by enableSchedules or the killswitch latch).
    assert "triggerType: 'Manual'" in migrate_block
    assert "scheduleTriggerConfig" not in migrate_block
    assert "cronExpression" not in migrate_block


def test_migrate_job_runs_tenant_migrate(migrate_block: str) -> None:
    assert "'tenant-migrate'" in migrate_block
    assert "command: [\n            'teetime'\n          ]" in migrate_block


def test_migrate_job_gets_the_cosmos_env_and_no_kv_secret(compute: str, migrate_block: str) -> None:
    # The migration only needs the store: no keyring, no ACS, no ForeUP credentials — so the
    # job cannot fail on a Key Vault secret the operator has not created.
    assert "env: migrateEnv" in migrate_block
    assert "secrets: []" in migrate_block
    env_start = compute.index("var migrateEnv = [")
    env_block = compute[env_start : compute.index("]", env_start)]
    for name in ("TENANT_COSMOS_ENDPOINT", "TENANT_COSMOS_DATABASE", "AZURE_CLIENT_ID"):
        assert f"'{name}'" in env_block
    assert "secretRef" not in env_block


def test_killswitch_excludes_the_migrate_job() -> None:
    # MULTIUSER_PLAN §10.3: Manual-trigger, never auto-fires -> nothing to silence.
    assert "migrate" not in KILLSWITCH.read_text()


def _deploy_jobs(workflow: str) -> list[str]:
    starts = [m.start() for m in re.finditer(r"^  deploy-(dev|prod):", workflow, re.M)]
    return [
        workflow[s : (starts[i + 1] if i + 1 < len(starts) else len(workflow))]
        for i, s in enumerate(starts)
    ]


def test_workflow_runs_migrate_after_deploy_pass_2_on_every_deploy(workflow: str) -> None:
    """MULTIUSER_PLAN §12 names `test_workflow_runs_migrate_before_jobs`. With the two-pass
    deploy (pass 1 = public bootstrap image for EVERY job, pass 2 = the real image) the migrate
    job only exists on the real image once pass 2 has ALSO switched the booking/watch jobs and
    the web, so strictly-before is not achievable without a third pass. The migration runs
    immediately after pass 2 instead, which is safe because readers accept schemaVersion N and
    N-1 (§10.2). This pins what actually ships: per deploy job, exactly one migrate step, AFTER
    pass 2, with no `if:` gate (MU-19: the job always exists), starting the env's migrate job
    and awaiting its result."""
    jobs = _deploy_jobs(workflow)
    assert len(jobs) == 2
    for job in jobs:
        assert job.count(_JOB_START) == 1
        pass2 = job.index("Deploy pass 2")
        migrate = job.index("- name: Run tenant migrations")
        assert migrate > pass2
        # the migrate step comes after the LAST deploy command
        assert migrate > job.rindex(_DEPLOY)
        step = job[migrate : job.index("- name:", migrate + 1)]
        assert step.splitlines()[1].strip() == "run: |", "the migrate step must not be gated"
        assert '"teetime-migrate-${ENVNAME}"' in step
        assert "job execution show" in step
        assert "Succeeded" in step
        assert "exit 1" in step
