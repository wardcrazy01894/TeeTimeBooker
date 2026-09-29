"""Deploy pass 1 (the public bootstrap image) runs only when an environment needs it.

Pass 1 exists so a NEW environment's managed identity gets AcrPull on the shared ACR before
anything pulls from it. It used to run on EVERY deploy: it doubled the Bicep time (~4 of ~11
minutes after MU-17) and put every job AND the web app on the placeholder image for the length of
a pass (the "watch fire mid-deploy loses a cycle" quirk; the always-warm prod web served the
placeholder too). The "Detect bootstrap need" step skips it once the grant exists, and is
FAIL-SAFE: anything it cannot confirm (identity missing, an `az` error, no assignment) means
"run pass 1", i.e. the old behaviour.

The step's real shell script is extracted from the workflow and run against a fake `az`.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = _ROOT / ".github" / "workflows" / "azure-iac.yml"
DETECT = "- name: Detect bootstrap need"
PASS1 = "- name: Deploy pass 1"


def _step_blocks(text: str, marker: str) -> list[str]:
    """Every step whose `- name:` line starts with ``marker``, as raw text up to the next step."""
    lines = text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        if line.strip().startswith(marker):
            indent = len(line) - len(line.lstrip())
            body = [line]
            for nxt in lines[i + 1 :]:
                stripped = nxt.lstrip()
                nxt_indent = len(nxt) - len(stripped)
                if stripped and nxt_indent <= indent and not stripped.startswith("#"):
                    break
                body.append(nxt)
            blocks.append("\n".join(body))
    return blocks


def _run_script(block: str) -> str:
    lines = block.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "run: |")
    body = [ln for ln in lines[start + 1 :]]
    indent = min(len(ln) - len(ln.lstrip()) for ln in body if ln.strip())
    return "\n".join(ln[indent:] for ln in body)


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text()


def test_every_deploy_job_detects_before_pass_1_and_gates_it(workflow: str) -> None:
    detects, passes = _step_blocks(workflow, DETECT), _step_blocks(workflow, PASS1)
    assert len(detects) == len(passes) == 2  # dev + prod
    for detect, pass1 in zip(detects, passes, strict=True):
        assert "id: bootstrap" in detect
        assert "if: steps.bootstrap.outputs.needed == 'true'" in pass1
        assert workflow.index(detect) < workflow.index(pass1)


FAKE_AZ = """#!/usr/bin/env bash
# args joined; behaviour chosen by FAKE_AZ_MODE
args="$*"
case "$FAKE_AZ_MODE:$args" in
  error:*) echo "boom" >&2; exit 1 ;;
  no_identity:identity\\ show*) echo "ResourceNotFound" >&2; exit 3 ;;
  *:identity\\ show*) echo "pid-123" ;;
  *:acr\\ show*) echo "/subscriptions/s/resourceGroups/rg-teetime-shared/acr/x" ;;
  granted:role\\ assignment\\ list*)
    [[ "$args" == *"--assignee-object-id pid-123"* && "$args" == *"--role AcrPull"* ]] \\
      && echo 1 || echo 0 ;;
  garbage:role\\ assignment\\ list*) echo None ;;
  *:role\\ assignment\\ list*) echo 0 ;;
esac
"""


@pytest.mark.parametrize(
    ("mode", "needed"),
    [
        ("granted", "false"),  # the steady state: skip pass 1
        ("not_granted", "true"),  # a new env: identity exists, grant not yet
        ("no_identity", "true"),  # a brand-new env
        ("error", "true"),  # anything unconfirmed falls back to the old behaviour
        ("garbage", "true"),  # a non-numeric count, under Actions' `bash -e`, is not a grant
    ],
)
def test_detect_script_is_fail_safe(workflow: str, tmp_path: Path, mode: str, needed: str) -> None:
    az = tmp_path / "az"
    az.write_text(FAKE_AZ)
    az.chmod(az.stat().st_mode | stat.S_IEXEC)
    blocks = _step_blocks(workflow, DETECT)
    assert len(blocks) == 2  # never vacuous
    for block in blocks:
        out = tmp_path / "out"
        out.write_text("")
        env = {
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_AZ_MODE": mode,
            "GITHUB_OUTPUT": str(out),
            "RESOURCE_GROUP": "rg-teetime-dev",
            "ENVNAME": "dev",
            "ACR_NAME": "acrshared",
            "SHARED_ACR_RG": "rg-teetime-shared",
        }
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _run_script(block)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert re.findall(r"^needed=(\w+)$", out.read_text(), re.M) == [needed]
