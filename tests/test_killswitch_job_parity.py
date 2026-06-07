"""MULTIDAY PR5: the killswitch must target exactly the jobs compute.bicep creates.

The cost killswitch (killswitch.bicep) PATCHes + stops each ACA Job by name. If
compute.bicep renames a booking job (e.g. dropping `-sun`), the killswitch's job-name
references MUST change in the same PR or it would silently fail to stop a renamed job.
This static test couples the two so a future rename can't de-sync them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MODULES = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules"
COMPUTE = _MODULES / "compute.bicep"
KILLSWITCH = _MODULES / "killswitch.bicep"


@pytest.fixture(scope="module")
def compute() -> str:
    return COMPUTE.read_text()


@pytest.fixture(scope="module")
def killswitch() -> str:
    return KILLSWITCH.read_text()


def test_compute_uses_edt_est_job_names(compute: str) -> None:
    # The post-rename job-name suffixes (no -sun).
    assert "'${jobName}-edt'" in compute
    assert "'${jobName}-est'" in compute


def test_killswitch_targets_match_compute_job_suffixes(killswitch: str) -> None:
    # The killswitch references the booking jobs by their full names; after the rename it
    # must use -edt / -est (dev + prod) and NOT the stale -sun suffix.
    assert "teetime-job-${envName}-edt'" in killswitch
    assert "teetime-job-${envName}-est'" in killswitch
    assert "teetime-job-prod-edt'" in killswitch
    assert "teetime-job-prod-est'" in killswitch
    # The watch job name is unchanged.
    assert "teetime-watch-job-${envName}'" in killswitch
    # No stale -sun job references remain.
    assert "-edt-sun" not in killswitch
    assert "-est-sun" not in killswitch
