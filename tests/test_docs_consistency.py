"""Docs-freshness guards — mechanical checks for the doc claims that keep going stale.

The 2026-07-09 full-repo-scan found the SAME class of staleness the 2026-06-29 sweep
fixed elsewhere: a "latest infra tag" claim left behind in one doc (PLAN.md said
`infra/v2.7.0` while README.md/CLAUDE.md said `infra/v2.8.0`). Every prod deploy bumps
that claim in THREE docs; whichever one the bump misses is a silent lie until the next
scan. This test makes the disagreement a CI failure instead.

Deliberately checks AGREEMENT, not correctness — CI cannot know which tag is truly
deployed, but it CAN know the three docs must name the SAME one. See the
"Documentation standard" section of CLAUDE.md for the full change→docs mapping.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Docs that carry a "latest [prod] infra tag `infra/vX.Y.Z`" current-state claim.
# If a doc legitimately stops carrying the claim, remove it here in the same PR.
_TAG_CLAIM_DOCS = ("README.md", "CLAUDE.md", "PLAN.md")
_TAG_CLAIM_RE = re.compile(r"latest (?:prod )?infra tag\s*`?(infra/v\d+\.\d+\.\d+)`?")


def test_latest_infra_tag_claims_agree_across_docs() -> None:
    """Every 'latest infra tag' claim in README/CLAUDE/PLAN must name the SAME version."""
    versions_by_doc: dict[str, set[str]] = {}
    for doc in _TAG_CLAIM_DOCS:
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        found = set(_TAG_CLAIM_RE.findall(text))
        # A doc with ZERO claims means the phrasing drifted and this guard went vacuous —
        # fail loudly so the regex (or the doc list) is updated in the same PR.
        assert found, (
            f"{doc}: no 'latest infra tag' claim found — update _TAG_CLAIM_RE or _TAG_CLAIM_DOCS"
        )
        versions_by_doc[doc] = found

    distinct = set().union(*versions_by_doc.values())
    assert len(distinct) == 1, (
        "The 'latest infra tag' claims disagree across docs — a prod-tag bump missed one. "
        f"Per-doc claims: {versions_by_doc}. Fix the stale doc(s) so all name the same tag."
    )
