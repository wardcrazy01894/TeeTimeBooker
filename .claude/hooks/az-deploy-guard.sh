#!/usr/bin/env bash
# PreToolUse hook: hard-block Azure commands that create/modify/delete live
# resources, turning the CLAUDE.md "agents MUST NOT deploy without approval"
# rule into actual enforcement (CLAUDE.md is guidance; a hook is a gate).
#
# Wired in .claude/settings.json on the Bash matcher. Reads the tool-call JSON
# from stdin, inspects the command, and exits 2 (blocking the call) with an
# explanation if it matches a destructive `az` operation. Everything else — incl.
# `az ... what-if`, `validate`, `list`, `show`, `bicep build` — is allowed.
set -uo pipefail

input="$(cat)"
cmd="$(printf '%s' "$input" | python3 -c \
  'import json,sys
try:
    print(json.load(sys.stdin).get("tool_input",{}).get("command",""))
except Exception:
    print("")' 2>/dev/null)"

# Normalize: strip quote chars (so a quoted binary `"az" deployment …` can't
# slip past the leading `az ` anchor) and squeeze runs of whitespace (so
# "az   deployment" still matches). Quote-stripping closes the quoted-binary
# bypass — but it is NOT a general monotonic guarantee: quotes used as token
# SEPARATORS (e.g. `az"keyvault" purge` → `azkeyvault purge`) still evade, just
# as they would unstripped. More broadly, this guard inspects the literal
# command string only — it cannot follow shell indirection (env vars, xargs,
# eval, base64|sh), so it is defense-in-depth, not a sandbox. A path-prefixed
# binary (`/usr/bin/az …`) is caught by the substring match.
norm="$(printf '%s' "$cmd" | tr -d "\"'" | tr -s "[:space:]" " ")"

# keyvault alternation notes: the bare `purge`/`delete` cover the vault-level
# `az keyvault purge` / `az keyvault delete`. `delete` (no boundary) also matches
# `az keyvault delete-policy` — that is INTENTIONAL: removing an access policy is a
# privileged mutation, gated alongside `set-policy`. It does NOT match the read-only
# `az keyvault list-deleted` / `show-deleted` (those start with list-/show-, not
# delete-/purge-, immediately after "keyvault "). `update` and `network-rule
# (add|remove)` are the vault-ACL mutations (a `--default-action Deny` or a rule
# change can lock the vault and DoS the jobs); `network-rule list` stays read-only
# (the alternative requires add/remove). All behaviours are pinned in
# tests/test_az_deploy_guard.py.
if printf '%s' "$norm" | grep -Eiq \
  'az (deployment (group|sub|mg|tenant) (create|delete)|containerapp job (start|stop|create|update|delete)|role (assignment|definition) (create|delete)|keyvault (purge|delete|update|network-rule (add|remove)|secret (set|delete|purge)|set-policy)|group delete|resource delete)'; then
  {
    echo "BLOCKED by az-deploy-guard: this command creates, modifies, or deletes"
    echo "live Azure resources, which CLAUDE.md forbids without explicit user"
    echo "approval. Command:"
    echo "  $cmd"
    echo "Ask the user to run it manually, or use 'what-if' / 'validate' instead."
  } >&2
  exit 2
fi
exit 0
