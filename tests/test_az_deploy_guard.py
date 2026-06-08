"""Regression tests for .claude/hooks/az-deploy-guard.sh — the PreToolUse gate that
hard-blocks destructive `az` commands (CLAUDE.md "agents MUST NOT deploy without
approval"). The hook reads the tool-call JSON on stdin and exits 2 to block.

These pin the block-list so a future regex edit can't silently re-open a hole (the
full-repo-scan security review found vault-level `keyvault purge`/`delete` were NOT
blocked). Driven the same way the harness drives it: pipe {"tool_input":{"command":...}}.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "az-deploy-guard.sh"


def _run_hook(command: str) -> int:
    """Invoke the hook with a Bash tool-call payload; return its exit code (2 = blocked)."""
    payload = json.dumps({"tool_input": {"command": command}})
    proc = subprocess.run(
        ["bash", str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode


_BLOCKED = [
    # vault-level destructive ops (the gap the security review found)
    "az keyvault purge --name kv-teetime-dev",
    "az keyvault delete --name kv-teetime-dev",
    # access-policy removal is a privileged mutation — intentionally gated like set-policy
    "az keyvault delete-policy --name kv --object-id o",
    # secret-level ops (already covered — guard against regression)
    "az keyvault secret set --vault-name kv --name s --value x",
    "az keyvault secret delete --vault-name kv --name s",
    "az keyvault secret purge --vault-name kv --name s",
    "az keyvault set-policy --name kv --object-id o --secret-permissions get",
    # deployments / jobs / RBAC / resource teardown
    "az deployment group create -g rg -f main.bicep",
    "az deployment sub create -l eastus -f main.bicep",
    "az containerapp job start -n j -g rg",
    "az containerapp job update -n j -g rg --image x",
    "az containerapp job delete -n j -g rg",
    "az role assignment create --assignee a --role r --scope s",
    "az role assignment delete --assignee a --role r",
    "az group delete -n rg",
    "az resource delete --ids /subscriptions/x",
    # normalization: extra whitespace must still match
    "az   keyvault    purge  --name kv",
    # full-repo-scan security review: verbs the guard previously MISSED
    "az containerapp job stop -n j -g rg",  # the exact verb the cost killswitch uses
    "az containerapp job create -n j -g rg --image x --trigger-type Schedule",
    "az deployment group delete -n d -g rg",
    "az deployment sub delete -n d",
    "az role definition create --role-definition role.json",  # custom-role escalation
    "az role definition delete --name custom-role",
    "az keyvault update --name kv --default-action Deny",  # network-ACL lockout (DoS)
    # network-rule add/remove perform the SAME vault-ACL mutation as `keyvault update`
    "az keyvault network-rule add --name kv --ip-address 1.2.3.4",
    "az keyvault network-rule remove --name kv --ip-address 1.2.3.4",
    # quoted-binary evasion: quoting the az token must NOT bypass the gate
    '"az" deployment group create -g rg -f main.bicep',
    "/usr/bin/az keyvault purge --name kv",  # path-prefixed binary still caught
]

_ALLOWED = [
    "az keyvault secret list --vault-name kv",
    "az keyvault show --name kv",
    "az keyvault list-deleted",  # must NOT be caught by the new 'delete' alternative
    "az keyvault show-deleted --name kv",  # read-only; starts with show-, not delete-
    "az deployment group what-if -g rg -f main.bicep",
    "az deployment group validate -g rg -f main.bicep",
    "az containerapp job show -n j -g rg",
    "az containerapp job list -g rg",  # read-only; not stop/create/update/delete
    "az containerapp job execution list -n j -g rg",  # read-only history
    "az role definition list",  # read-only; must NOT be caught by 'definition create|delete'
    "az deployment group list -g rg",  # read-only; not create|delete
    "az keyvault network-rule list --name kv",  # read-only; not add/remove
    "az bicep build --file main.bicep",
    "echo hello",
    "git status",
]


@pytest.mark.parametrize("command", _BLOCKED)
def test_destructive_commands_are_blocked(command: str) -> None:
    assert _run_hook(command) == 2, f"expected BLOCKED (exit 2): {command}"


@pytest.mark.parametrize("command", _ALLOWED)
def test_readonly_commands_are_allowed(command: str) -> None:
    assert _run_hook(command) == 0, f"expected ALLOWED (exit 0): {command}"
