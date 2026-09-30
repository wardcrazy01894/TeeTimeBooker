"""Deploy hygiene in azure-iac.yml (full-repo scan 2026-09-30).

- A prod deploy runs only code that is on ``main``: a tag or a dispatch from a branch whose
  commit never passed main's required checks is refused before any Azure call. (The GitHub
  ``prod`` environment ALSO allows only ``main`` + ``infra/v*`` tags, a repo setting.)
- Every Azure job is bounded (``timeout-minutes``), not GitHub's 360-minute default.
- Deploys to one env are serialized per ENV, not per ref: a tag push and a dispatch to prod
  target the same resource group.
- The migrate step follows an execution that is Running OR still Processing / Pending.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "azure-iac.yml"
AZURE_JOBS = ("validate", "deploy-dev", "deploy-prod")


def _job(name: str) -> str:
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {name}:")
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if (
            line.startswith("  ")
            and not line.startswith("   ")
            and not line.lstrip().startswith("#")
        ):
            break
        body.append(line)
    return "\n".join(body)


def test_every_azure_job_has_a_timeout() -> None:
    for name in AZURE_JOBS:
        m = re.search(r"^    timeout-minutes: (\d+)$", _job(name), flags=re.M)
        assert m, f"{name} has no timeout-minutes"
        assert int(m.group(1)) <= 60


def test_deploys_are_serialized_per_environment() -> None:
    for name, env in (("deploy-dev", "dev"), ("deploy-prod", "prod")):
        job = _job(name)
        assert f"group: azure-iac-deploy-{env}" in job, name
        assert "cancel-in-progress: false" in job, name


def test_prod_refuses_a_commit_that_is_not_on_main_before_touching_azure() -> None:
    job = _job("deploy-prod")
    guard = job.index('git merge-base --is-ancestor "${GITHUB_SHA}" origin/main')
    assert guard < job.index("azure/login@"), "the guard must run before the Azure login"
    assert "fetch-depth: 0" in job[: job.index("azure/login@")]  # full history for the check


def test_the_migrate_step_follows_a_pending_or_processing_execution() -> None:
    text = WORKFLOW.read_text()
    queries = re.findall(r'--query "(\[\?[^"]*\]) \| \[0\]\.name"', text)
    assert len(queries) == 2  # dev + prod
    for q in queries:
        for status in ("Running", "Processing", "Pending"):
            assert f"'{status}'" in q, q
