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
import subprocess
from pathlib import Path

from teetime.core.config import SchedulerConfig
from teetime.web.routes import ROUTES

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


# --- markdown structure: links resolve, long docs carry a working TOC -------------------
#
# 2026-09-26 docs cleanup: the shipped plan docs moved to docs/plans/ and per-release history
# moved to docs/RELEASES.md. A moved or renamed doc silently breaks every relative link to it,
# and a heading rename silently breaks every TOC entry pointing at it. Both are cheap to check
# mechanically, so CI does: every relative markdown link must resolve to an existing file, and
# every doc over _TOC_MIN_LINES lines must carry a `<!-- toc -->` block whose `#anchor` links
# all name a heading in that doc (GitHub's slug rules). The TOC check does NOT require the TOC
# to list every heading — only that nothing it lists is dangling.

_TOC_MIN_LINES = 150
_TOC_BLOCK_RE = re.compile(r"<!-- toc -->(.*?)<!-- /toc -->", re.S)
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _markdown_docs() -> list[Path]:
    roots = [REPO_ROOT.glob("*.md")]
    for sub in ("docs", "infra", "src", ".claude/skills"):
        roots.append((REPO_ROOT / sub).rglob("*.md"))
    return sorted({p for gen in roots for p in gen})


def _strip_code(text: str) -> str:
    """Drop fenced code blocks and inline code spans, which may contain link-like text."""
    kept: list[str] = []
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            kept.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(kept)


def _github_slug(title: str) -> str:
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
    title = re.sub(r"[^\w\- ]", "", title.strip().lower())
    return title.replace(" ", "-")


def _heading_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in _strip_code_fences_only(text).splitlines():
        m = _HEADING_RE.match(line)
        if not m:
            continue
        slug = _github_slug(m.group(2))
        n = counts.get(slug, 0)
        counts[slug] = n + 1
        anchors.add(slug if n == 0 else f"{slug}-{n}")
    return anchors


def _strip_code_fences_only(text: str) -> str:
    kept: list[str] = []
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            kept.append(line)
    return "\n".join(kept)


def _broken_relative_links(doc: Path, text: str) -> list[str]:
    broken: list[str] = []
    for target in _MD_LINK_RE.findall(_strip_code(text)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue
        path = target.split("#", 1)[0]
        if not (doc.parent / path).exists():
            broken.append(target)
    return broken


def _dangling_toc_anchors(text: str) -> list[str] | None:
    """None when the doc has no TOC block; else the TOC anchors naming no heading."""
    block = _TOC_BLOCK_RE.search(text)
    if block is None:
        return None
    anchors = _heading_anchors(text)
    return [a for a in re.findall(r"\]\(#([^)\s]+)\)", block.group(1)) if a not in anchors]


def test_doc_guards_are_not_vacuous() -> None:
    """The helpers flag a broken link and a dangling TOC entry (else the guards pass on anything)."""
    fake = REPO_ROOT / "CLAUDE.md"
    assert _broken_relative_links(fake, "see [x](./no/such/file.md) and [y](./README.md)") == [
        "./no/such/file.md"
    ]
    toc = "<!-- toc -->\n- [A](#a-heading)\n- [B](#gone)\n<!-- /toc -->\n## A heading\n"
    assert _dangling_toc_anchors(toc) == ["gone"]
    assert _markdown_docs(), "no markdown docs found — the doc globs are wrong"


def test_relative_markdown_links_resolve() -> None:
    """Every relative link in every repo doc points at a file that exists."""
    failures = {
        str(doc.relative_to(REPO_ROOT)): bad
        for doc in _markdown_docs()
        if (bad := _broken_relative_links(doc, doc.read_text(encoding="utf-8")))
    }
    assert not failures, (
        f"Broken relative markdown links (a doc moved or was renamed?): {failures}. "
        "Fix the link targets in the same PR as the move."
    )


def test_long_docs_have_a_working_toc() -> None:
    """Docs over _TOC_MIN_LINES lines carry a `<!-- toc -->` block with no dangling anchors."""
    missing: list[str] = []
    dangling: dict[str, list[str]] = {}
    for doc in _markdown_docs():
        text = doc.read_text(encoding="utf-8")
        name = str(doc.relative_to(REPO_ROOT))
        result = _dangling_toc_anchors(text)
        if result is None:
            if len(text.splitlines()) > _TOC_MIN_LINES:
                missing.append(name)
        elif result:
            dangling[name] = result
    assert not missing, (
        f"Docs over {_TOC_MIN_LINES} lines without a `<!-- toc -->` ... `<!-- /toc -->` "
        f"block: {missing}. Add a '## Contents' list of the doc's sections."
    )
    assert not dangling, f"TOC entries that match no heading (a heading was renamed?): {dangling}"


# --- the prod mode claim (MU-18, #257) ---------------------------------------------------------
# While MU-18 landed, four review rounds each found another doc still saying prod runs the TOML
# path, in a new wording each time. The prod param file is the truth: when it runs the tenant path,
# no current-state doc may say otherwise. docs/plans/ and docs/RELEASES.md are history, exempt.

_STALE_PROD_TOML = re.compile(
    r"prod (still )?runs the (single-user )?TOML|prod(uction)? is (untouched|unchanged) until MU-18"
    r"|Open: the prod cutover|cutover \(MU-18\) open|not yet to prod|\| MU-18 \|[^|\n]*\| Open \|"
    r"|MULTIUSER_PLAN\.md\]\(\./MULTIUSER_PLAN\.md\) \(live in dev\)",
    re.IGNORECASE,
)


def test_no_doc_says_prod_runs_toml_once_prod_runs_the_tenant_path() -> None:
    prod = (REPO_ROOT / "infra" / "bicep" / "main.bicepparam.prod").read_text()
    if "param bookingMode = 'tenant'" not in prod:
        return  # prod is on the TOML path: the claim is true
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.md")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(("docs/plans/", ".venv/", ".claude/")) or rel == "docs/RELEASES.md":
            continue
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if _STALE_PROD_TOML.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert offenders == [], (
        "prod runs the tenant path (main.bicepparam.prod); stale claims:\n" + "\n".join(offenders)
    )


def test_stale_prod_claim_guard_is_not_vacuous() -> None:
    for stale in (
        "Prod runs the single-user TOML jobs",
        "Open: the prod cutover MU-18",
        "| MU-18 | Prod cutover | Open |",
        "[MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md) (live in dev).",
    ):
        assert _STALE_PROD_TOML.search(stale), stale
    assert not _STALE_PROD_TOML.search("prod was unchanged until MU-18 (below).")


# A merge-conflict marker must never be committed (2026-09-29: three `||||||| <sha>` zdiff3 base
# markers reached main in docs/MULTIUSER_AS_BUILT.md via hand-rolled conflict resolutions).
CONFLICT_MARKER = re.compile(r"^(<<<<<<<|\|\|\|\|\|\|\||=======|>>>>>>>)( |$)", re.MULTILINE)
_TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".yml",
    ".yaml",
    ".toml",
    ".bicep",
    ".bicepparam",
    ".html",
    ".css",
    ".js",
    ".json",
    ".txt",
    ".cfg",
    ".ini",
    ".sh",
}


def conflict_markers(text: str) -> list[str]:
    return [m.group(0) for m in CONFLICT_MARKER.finditer(text)]


def test_no_tracked_file_contains_a_merge_conflict_marker() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    offenders = []
    for rel in tracked:
        path = REPO_ROOT / rel
        if path.suffix not in _TEXT_SUFFIXES and path.name not in {"CLAUDE.md", "pre-push"}:
            continue
        if path.is_file() and conflict_markers(path.read_text(errors="replace")):
            offenders.append(rel)
    assert offenders == []


def test_every_web_route_is_in_the_plans_route_table() -> None:
    """MULTIUSER_PLAN §8.2 says ``web/routes.py::ROUTES`` is its contract table; the scan of
    2026-09-30 found six routes missing from it. Every route path must appear there, in
    backticks, so a new route cannot land without its row."""
    plan = (REPO_ROOT / "MULTIUSER_PLAN.md").read_text(encoding="utf-8")
    start = plan.index("### 8.2 Routes")
    section = plan[start : plan.index("\n### ", start + 1)]
    documented = set(re.findall(r"`(/[^`]*)`", section))
    missing = sorted({r.path for r in ROUTES} - documented)
    assert not missing, f"routes missing from MULTIUSER_PLAN §8.2: {missing}"


_LADDER_DOCS = ("README.md", "CLAUDE.md", "PLAN.md", "MULTIUSER_PLAN.md")


def _ladder_forms(ladder: tuple[int, ...]) -> set[str]:
    nums = [str(n) for n in ladder]
    return {
        "/".join(nums),
        " / ".join(nums),
        "(" + ", ".join(nums) + ")",
        "[" + ", ".join(nums) + "]",
        "(" + ",".join(nums) + ")",
    }


def test_every_current_state_doc_names_the_shipped_stagger_ladder() -> None:
    """2026-09-30: the ladder moved -500/-250/0 -> -400/-250/0 and a check found six docs
    still describing the old one as current. Each doc that describes the T0 burst must name the
    SHIPPED default (``SchedulerConfig``, which the tenant booker uses), and a "~N ms window"
    claim must match its span. Change the ladder -> the change->docs map row in CLAUDE.md."""
    shipped = SchedulerConfig().blind_post_stagger_ms
    forms = _ladder_forms(shipped)
    span = str(max(shipped) - min(shipped))
    for name in _LADDER_DOCS:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert any(f in text for f in forms), f"{name} never names the shipped ladder {shipped}"
        for claimed in re.findall(r"~(\d+) ms window", text):
            assert claimed == span, f"{name} says a ~{claimed} ms window; the ladder spans {span}"
    assert SchedulerConfig().early_arrival_ms == -shipped[0]


# Every current-state doc + shipped config that talks about the T0 burst (the map row's sites).
_LADDER_SWEEP = (
    "README.md",
    "CLAUDE.md",
    "PLAN.md",
    "MULTIUSER_PLAN.md",
    "BACKLOG.md",
    "infra/AZURE_PLAN.md",
    "src/teetime/courses/CLAUDE.md",
    "config/container.toml",
    "config/example.toml",
)
_RETIRED_LADDER = (-500, -250, 0)
_HISTORY_MARKERS = ("until 2026-09-30", "was ", "2026-09-29", "2026-09-30")


def test_no_current_state_doc_describes_the_retired_ladder_as_current() -> None:
    """Review of the 2026-09-30 sweep: one correct mention per doc is not enough; a stale
    "fired at T0-0.5" (with a Unicode minus) or "(-500, -250, 0)" elsewhere in the same doc still misleads. Any line
    naming the retired ladder must mark itself as history."""
    stale_forms = _ladder_forms(_RETIRED_LADDER) | {"T0\u22120.5", "T0-0.5", "~500 ms"}
    offenders = []
    for name in _LADDER_SWEEP:
        for n, line in enumerate((REPO_ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
            if any(f in line for f in stale_forms) and not any(m in line for m in _HISTORY_MARKERS):
                offenders.append(f"{name}:{n}: {line.strip()[:90]}")
    assert not offenders, "retired ladder described as current:\n" + "\n".join(offenders)
