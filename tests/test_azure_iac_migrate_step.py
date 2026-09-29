"""The deploy's "Run tenant migrations" step survives a transient Azure API blip.

2026-09-29: a dev deploy failed only because the call that STARTS the migrate job got
`ConnectionResetError: Connection reset by peer`. The step now retries the start (3 attempts) and
tolerates a failed status poll (it consumes one poll, not the step). A re-start is safe: the
migrations are an ordered, idempotent list (MULTIUSER_PLAN §10.2), so a start that landed despite
the reset only runs them twice. A real Failed / Stopped / Degraded execution still fails the step.

The step's real script is extracted from the workflow and run under Actions' `bash -eo pipefail`
against a fake `az` (both deploy jobs carry the same step).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from .test_azure_iac_bootstrap_skip import _run_script, _step_blocks

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "azure-iac.yml"
STEP = "- name: Run tenant migrations"

# FAKE_AZ_START: how many `job start` calls fail before one succeeds ("never" = all fail).
# FAKE_AZ_POLLS: space-separated results of successive `execution show` calls ("ERR" = az fails).
FAKE_AZ = """#!/usr/bin/env bash
state="$FAKE_AZ_STATE"
case "$*" in
  *"job start"*)
    n=$(cat "$state/starts" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$state/starts"
    if [ "$FAKE_AZ_START" = never ] || [ "$n" -le "$FAKE_AZ_START" ]; then
      echo "ConnectionResetError: Connection reset by peer" >&2; exit 1
    fi
    echo "exec-123" ;;
  *"execution list"*)
    # FAKE_AZ_LANDED=1: the failed start actually landed, so a Running execution exists.
    n=$(cat "$state/starts" 2>/dev/null || echo 0)
    if [ "${FAKE_AZ_LANDED:-0}" = 1 ] && [ "$n" -ge 1 ]; then echo "exec-landed"; fi ;;
  *"execution show"*)
    i=$(cat "$state/polls" 2>/dev/null || echo 0); i=$((i + 1)); echo "$i" > "$state/polls"
    r=$(echo "$FAKE_AZ_POLLS" | cut -d' ' -f"$i")
    if [ "$r" = ERR ]; then echo "transient" >&2; exit 1; fi
    echo "${r:-Running}" ;;
esac
"""


def _run(
    tmp_path: Path, *, start_failures: str, polls: str, landed: bool = False
) -> tuple[int, str, int]:
    az = tmp_path / "az"
    az.write_text(FAKE_AZ)
    az.chmod(az.stat().st_mode | stat.S_IEXEC)
    blocks = _step_blocks(WORKFLOW.read_text(), STEP)
    assert len(blocks) == 2  # dev + prod, never vacuous
    results = []
    for block in blocks:
        state = tmp_path / f"state-{len(results)}"
        state.mkdir()
        env = {
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_AZ_STATE": str(state),
            "FAKE_AZ_START": start_failures,
            "FAKE_AZ_POLLS": polls,
            "FAKE_AZ_LANDED": "1" if landed else "0",
            "ENVNAME": "dev",
            "RESOURCE_GROUP": "rg-teetime-dev",
            "MIGRATE_RETRY_SLEEP_S": "0",
            "MIGRATE_POLL_SLEEP_S": "0",
        }
        r = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _run_script(block)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        starts = int((state / "starts").read_text())
        results.append((r.returncode, r.stdout + r.stderr, starts))
    assert results[0][0] == results[1][0]  # both jobs behave the same
    return results[0]


def test_a_transient_start_failure_is_retried(tmp_path: Path) -> None:
    code, out, starts = _run(tmp_path, start_failures="1", polls="Running Succeeded")
    assert code == 0, out
    assert starts == 2
    assert "Succeeded" in out


def test_a_failed_status_poll_only_costs_one_poll(tmp_path: Path) -> None:
    code, out, _ = _run(tmp_path, start_failures="0", polls="ERR Running Succeeded")
    assert code == 0, out


@pytest.mark.parametrize("final", ["Failed", "Stopped", "Degraded"])
def test_a_real_failure_still_fails_the_step(tmp_path: Path, final: str) -> None:
    code, out, _ = _run(tmp_path, start_failures="0", polls=f"Running {final}")
    assert code == 1
    assert f": {final}" in out


def test_a_start_that_never_works_gives_up_after_three_tries(tmp_path: Path) -> None:
    code, out, starts = _run(tmp_path, start_failures="never", polls="")
    assert code == 1
    assert starts == 3
    assert "after 3 attempts" in out


def test_a_start_that_landed_despite_the_error_is_followed_not_repeated(tmp_path: Path) -> None:
    """A reset AFTER the start POST landed leaves a Running execution: follow it rather than
    start a second, concurrent run."""
    code, out, starts = _run(tmp_path, start_failures="1", polls="Succeeded", landed=True)
    assert code == 0, out
    assert starts == 1
    assert "exec-landed" in out
