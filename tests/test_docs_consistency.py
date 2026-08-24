"""Docs-freshness guards — mechanical checks for the doc claims that keep going stale.

The 2026-07-09 full-repo-scan found the SAME class of staleness the 2026-06-29 sweep
fixed elsewhere: a "latest infra tag" claim left behind in one doc (PLAN.md said
`infra/v2.7.0` while README.md/CLAUDE.md said `infra/v2.8.0`). Every prod deploy bumps
that claim in THREE docs; whichever one the bump misses is a silent lie until the next
scan. This test makes the disagreement a CI failure instead.

The infra-tag guard deliberately checks AGREEMENT, not correctness — CI cannot know which
tag is truly deployed, but it CAN know the three docs must name the SAME one.

The pyproject guards below extend that idea to CODE comments, which drift exactly like docs
do: a dependency comment quoting the version it sits above is stale the next time Dependabot
bumps that floor (observed twice on the `idna` pin — cleaned in #106, re-drifted by #204).
`test_idna_comment_names_no_tracking_version` is that comment guard. Its neighbour
`test_idna_floor_never_drops_below_cve_boundary` is NOT a comment guard but a SECURITY one
on the requirement itself — it lives here because it pins the invariant that comment
describes, and it can check CORRECTNESS rather than agreement since the CVE boundary is a
fixed fact CI knows. See the "Documentation standard" section of CLAUDE.md for the full
change→docs mapping.
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
# that was false the moment it merged. Dependabot bumps this floor on every idna release
# (weekly-grouped), so any version literal repeated in that comment is stale by
# construction. The durable fix is to state no version there at all; this guard keeps it
# that way.
#
# The CVE boundary (3.15) is the ONE version legitimately named: a fixed historical fact,
# not a tracking datum. Anything else is flagged.

_IDNA_CVE_BOUNDARY = "3.15"
# Versions allowed to appear in the idna comment. If a future edit legitimately needs
# another (a python version, say), add it HERE rather than loosening the regex — the guard
# is deliberately biased toward a loud false positive over a silent miss.
_IDNA_COMMENT_ALLOWED_VERSIONS = frozenset({_IDNA_CVE_BOUNDARY})

# Matches multi-part floors ("3.19", "3.19.1"), optional extras and spacing, and does NOT
# require a closing quote — so `"idna>=3.19.1"` and `"idna >= 3.19,<4"` still match. The
# two-part-only original went VACUOUS-ish on a three-part floor (cryptic StopIteration),
# and three-part floors are this file's norm: see httpx/pydantic above.
_IDNA_REQ_RE = re.compile(r'^\s*"idna(?:\[[^\]]*\])?\s*>=\s*(?P<floor>\d+(?:\.\d+)*)', re.MULTILINE)
# Lookbehind excludes only [\d.], NOT \w: `\b` and `(?<![\w.])` BOTH fail between "v" and
# "3" (v is a word char), so `v3.18` — the obvious re-drift wording — slips past them
# unflagged. Excluding just digits/dots catches it.
#
# The trailing (?:\.\d+)+ captures the WHOLE dotted version rather than its first two
# parts, so a tracking literal that merely EXTENDS the boundary ("3.15.2") surfaces as
# "3.15.2" and is flagged, instead of truncating to an allow-listed "3.15" and passing.
_VERSION_LITERAL_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)+")


def _version_tuple(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.split("."))


def _idna_requirement_line_index(lines: list[str]) -> int:
    idx = next((i for i, line in enumerate(lines) if _IDNA_REQ_RE.match(line)), None)
    assert idx is not None, (
        "no `idna>=X.Y` requirement line in pyproject.toml — the pin format changed or the "
        "dependency moved; update _IDNA_REQ_RE in the same PR so this guard stays live."
    )
    return idx


def _idna_comment_block() -> str:
    """Every comment attached to the `idna` requirement: the lines above it AND its inline `#`."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lines = text.splitlines()
    idx = _idna_requirement_line_index(lines)
    block: list[str] = []
    for line in reversed(lines[:idx]):
        if not line.strip().startswith("#"):
            break
        block.append(line)
    block.reverse()
    # House style puts a TRAILING `#` comment on the requirement line itself — every other
    # dep in this list does (httpx, pydantic, click, ntplib). Scanning only the lines above
    # left that channel unchecked, so `"idna>=3.20",  # tracks the lock (3.20)` passed.
    inline = lines[idx].partition("#")[2]
    if inline.strip():
        block.append(inline)
    return "\n".join(block)


def test_idna_comment_names_no_tracking_version() -> None:
    """The idna comment must not repeat a version Dependabot will bump out from under it."""
    block = _idna_comment_block()
    assert block, "no comment found on or above the idna requirement — did the pin move?"
    stale = set(_VERSION_LITERAL_RE.findall(block)) - _IDNA_COMMENT_ALLOWED_VERSIONS
    assert not stale, (
        f"pyproject.toml: the `idna` comment names version literal(s) {sorted(stale)} that "
        f"Dependabot bumps on every release, so they go stale on the next bump (this is "
        f"exactly how '(3.18)' survived the 3.19 bump). Describe the floor without a "
        f"version, or — if the literal is a fixed fact like the CVE boundary — add it to "
        f"_IDNA_COMMENT_ALLOWED_VERSIONS."
    )


def test_idna_floor_never_drops_below_cve_boundary() -> None:
    """The floor's whole purpose: the resolver can never pick an idna with CVE-2026-45409."""
    lines = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    # Reuse the SAME locator the comment guard uses. Locating by `.search` over the whole
    # file instead let the two tests pick different lines under exotic line endings; going
    # through one helper makes that divergence impossible by construction.
    line = lines[_idna_requirement_line_index(lines)]
    match = _IDNA_REQ_RE.match(line)
    assert match is not None, "no `idna>=` requirement in pyproject.toml — the CVE floor is gone"
    floor = _version_tuple(match.group("floor"))
    boundary = _version_tuple(_IDNA_CVE_BOUNDARY)
    # Zero-pad the shorter so a multi-part floor compares correctly: (3,15) must NOT count
    # as >= (3,15,1). Coupled to _IDNA_REQ_RE now accepting three-part floors.
    width = max(len(floor), len(boundary))
    floor += (0,) * (width - len(floor))
    boundary += (0,) * (width - len(boundary))
    assert floor >= boundary, (
        f"idna floor {match.group('floor')} is below the CVE-2026-45409 boundary "
        f"{_IDNA_CVE_BOUNDARY} — the resolver could pick a vulnerable idna."
    )
