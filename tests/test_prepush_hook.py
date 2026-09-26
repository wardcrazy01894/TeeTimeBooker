"""The local pre-push hook must run every gate CI's `test / lint / typecheck` job runs.

Why: PRs were repeatedly opened with a failing lint check. Local checks had "passed" because
their output was read through a filter/`tail` that hid a non-zero exit code. The hook runs the
same commands with `set -euo pipefail` and blocks the push on the first failure, so a PR can no
longer be opened with a gate CI will fail. This test keeps the hook in lockstep with ci.yml.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-push"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# pip-audit needs the network and is a CVE gate on the dependency set, not on the diff; CI
# still runs it. Everything else in the job must also run locally before a push.
_CI_ONLY = {"uv run pip-audit", "uv sync"}


def _ci_gate_commands() -> list[str]:
    text = CI.read_text()
    job = text[text.index("test / lint / typecheck") :]
    job = job[: job.index("\n  # ---")]
    cmds = [m.strip() for m in re.findall(r"^\s+run:\s*(.+)$", job, flags=re.M)]
    return [c for c in cmds if c.startswith("uv ") and c not in _CI_ONLY]


def test_hook_exists_and_is_executable() -> None:
    assert HOOK.is_file()
    assert os.access(HOOK, os.X_OK)


def test_hook_fails_fast_on_any_error() -> None:
    body = HOOK.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in body


def test_hook_runs_every_ci_gate() -> None:
    body = HOOK.read_text()
    missing = [c for c in _ci_gate_commands() if c not in body]
    assert not missing, f"pre-push hook is missing CI gate(s): {missing}"


def test_ci_gate_list_is_not_empty() -> None:
    # Non-vacuity: if the ci.yml parse broke, the parity test would pass trivially.
    cmds = _ci_gate_commands()
    assert "uv run ruff check ." in cmds
    assert "uv run mypy" in cmds
