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

# Normalize runs of whitespace so "az   deployment" still matches.
norm="$(printf '%s' "$cmd" | tr -s "[:space:]" " ")"

if printf '%s' "$norm" | grep -Eiq \
  'az (deployment (group|sub|mg|tenant) create|containerapp job (start|update|delete)|role assignment (create|delete)|keyvault (purge|delete|secret (set|delete|purge)|set-policy)|group delete|resource delete)'; then
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
