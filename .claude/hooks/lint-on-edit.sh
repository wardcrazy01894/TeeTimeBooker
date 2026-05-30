#!/usr/bin/env bash
# PostToolUse hook: auto-format + lint Python files right after Claude edits them.
#
# Wired in .claude/settings.json on Edit|Write. Reads the tool-call JSON from
# stdin, pulls out the edited file path, and — for .py files under the repo —
# runs `ruff format` then `ruff check --fix`. If lint errors remain after the
# autofix, it exits 2 so the agent sees them and self-corrects in the same loop
# (this is what kills the trailing "style: ruff format" fixup commits).
#
# Fail-open: anything unexpected (missing uv, non-Python file, parse error)
# exits 0 so the hook never blocks normal editing.
set -uo pipefail

root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
input="$(cat)"

file="$(printf '%s' "$input" | python3 -c \
  'import json,sys
try:
    print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))
except Exception:
    print("")' 2>/dev/null)"

# Only act on Python files that actually exist.
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

command -v uv >/dev/null 2>&1 || exit 0
cd "$root" || exit 0

uv run ruff format "$file" >/dev/null 2>&1 || true

if ! out="$(uv run ruff check --fix "$file" 2>&1)"; then
  {
    echo "ruff still reports issues in $file after autofix:"
    echo "$out"
    echo "Fix these before continuing (do not commit lint failures)."
  } >&2
  exit 2
fi
exit 0
