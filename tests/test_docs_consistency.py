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
# Sibling guard (2026-07-18 review of the v2.11.0 bump): the M6 roadmap row used a
# DIFFERENT phrasing — "current prod `infra/vX.Y.Z`" — that _TAG_CLAIM_RE missed, so it
# silently drifted 3 tags behind (said v2.8.0 while prod was v2.11.0). Any "current [prod]
# [infra tag] `infra/vX`" claim names the deployed tag too, so it must AGREE with the
# "latest infra tag" claims. This regex need not match anything (it's a forward tripwire for
# reintroduced phrasing); when it DOES match, the version is folded into the agreement check.
_CURRENT_TAG_RE = re.compile(r"current (?:prod )?(?:infra tag\s*)?`?(infra/v\d+\.\d+\.\d+)`?")


def test_latest_infra_tag_claims_agree_across_docs() -> None:
    """Every 'latest'/'current' prod infra-tag claim in README/CLAUDE/PLAN names the SAME version."""
    versions_by_doc: dict[str, set[str]] = {}
    for doc in _TAG_CLAIM_DOCS:
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        # "latest infra tag" claims must exist (a doc that lost them means the phrasing drifted);
        # "current prod `infra/vX`" claims are optional but, when present, must agree too.
        latest = set(_TAG_CLAIM_RE.findall(text))
        current = set(_CURRENT_TAG_RE.findall(text))
        # A doc with ZERO 'latest' claims means the phrasing drifted and this guard went vacuous —
        # fail loudly so the regex (or the doc list) is updated in the same PR.
        assert latest, (
            f"{doc}: no 'latest infra tag' claim found — update _TAG_CLAIM_RE or _TAG_CLAIM_DOCS"
        )
        versions_by_doc[doc] = latest | current

    distinct = set().union(*versions_by_doc.values())
    assert len(distinct) == 1, (
        "The 'latest infra tag' claims disagree across docs — a prod-tag bump missed one. "
        f"Per-doc claims: {versions_by_doc}. Fix the stale doc(s) so all name the same tag."
    )


# --- pyproject dependency-comment drift -------------------------------------------------
#
# 2026-08-24: Dependabot #204 bumped the `idna` floor 3.18 -> 3.19 but left the comment
# above it reading "Floor now tracks the locked version (3.18)" — a present-tense claim
# that was false the moment it merged. Dependabot bumps this floor on EVERY idna release,
# so any version literal repeated in that comment is stale by construction. The durable
# fix is to state no version there at all; this guard keeps it that way.
#
# The CVE boundary (3.15) is the ONE version legitimately named: it is a fixed historical
# fact, not a tracking datum, so it is allowed by _CVE_BOUNDARY below.

_IDNA_CVE_BOUNDARY = "3.15"
_IDNA_REQ_RE = re.compile(r'^\s*"idna>=(?P<floor>\d+\.\d+)"', re.MULTILINE)
_VERSION_LITERAL_RE = re.compile(r"\b\d+\.\d+\b")


def _idna_comment_block() -> str:
    """The contiguous `#` comment lines immediately preceding the `idna>=` requirement."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lines = text.splitlines()
    idx = next(i for i, line in enumerate(lines) if _IDNA_REQ_RE.match(line))
    block: list[str] = []
    for line in reversed(lines[:idx]):
        if not line.strip().startswith("#"):
            break
        block.append(line)
    return "\n".join(reversed(block))


def test_idna_comment_names_no_tracking_version() -> None:
    """The idna comment must not repeat a version Dependabot will bump out from under it."""
    block = _idna_comment_block()
    assert block, "no comment block found above the idna requirement — did the pin move?"
    stale = {v for v in _VERSION_LITERAL_RE.findall(block) if v != _IDNA_CVE_BOUNDARY}
    assert not stale, (
        f"pyproject.toml: the `idna` comment names version literal(s) {sorted(stale)} that "
        f"Dependabot bumps on every release, so they go stale on the next bump (this is "
        f"exactly how '(3.18)' survived the 3.19 bump). Only the CVE boundary "
        f"{_IDNA_CVE_BOUNDARY} may be named — describe the floor without a version."
    )


def test_idna_floor_never_drops_below_cve_boundary() -> None:
    """The floor's whole purpose: the resolver can never pick an idna with CVE-2026-45409."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = _IDNA_REQ_RE.search(text)
    assert match is not None, "no `idna>=` requirement in pyproject.toml — the CVE floor is gone"
    floor = tuple(int(p) for p in match.group("floor").split("."))
    boundary = tuple(int(p) for p in _IDNA_CVE_BOUNDARY.split("."))
    assert floor >= boundary, (
        f"idna floor {match.group('floor')} is below the CVE-2026-45409 boundary "
        f"{_IDNA_CVE_BOUNDARY} — the resolver could pick a vulnerable idna."
    )
