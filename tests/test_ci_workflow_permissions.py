"""ci.yml declares least-privilege token permissions in code (full-repo scan 2026-09-30).

It was safe only because the REPO default is read-only; a settings change would have silently
given every PR job a write token."""

from __future__ import annotations

import re
from pathlib import Path

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_token_is_read_only() -> None:
    text = CI.read_text()
    m = re.search(r"^permissions:\n((?:  .+\n)+)", text, flags=re.M)
    assert m, "ci.yml needs a top-level permissions block"
    assert m.group(1).strip() == "contents: read"
