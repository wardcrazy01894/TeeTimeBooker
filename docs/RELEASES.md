# Release history

> **Status:** living log of every prod infra tag, newest first. The CURRENT prod tag is stated in
> README.md, CLAUDE.md and PLAN.md (CI checks they agree: `tests/test_docs_consistency.py`); add a
> section here on every prod deploy.

Moved out of the root CLAUDE.md Status section on 2026-09-26, where each release was repeated two
or three times. Facts are unchanged; only the repetition is gone. Dev auto-deploys from `main` on
every merge and is not tagged.

<!-- toc -->
## Contents

- [Summary](#summary)
- [infra/v2.16.0: 2026-08-24 (`main`@`4462f56`)](#infrav2160-2026-08-24-main4462f56)
- [infra/v2.15.0: 2026-08-16 (`main`@`8342b67`)](#infrav2150-2026-08-16-main8342b67)
- [infra/v2.14.0: 2026-08-15 (`main`@`e6a8abb`)](#infrav2140-2026-08-15-maine6a8abb)
- [infra/v2.13.0: 2026-08-09 (`main`@`de90316`)](#infrav2130-2026-08-09-mainde90316)
- [infra/v2.12.0: 2026-08-02 (`main`@`a7a1a6c`)](#infrav2120-2026-08-02-maina7a1a6c)
- [infra/v2.11.0: 2026-07-18 (`main`@`cdf5618`)](#infrav2110-2026-07-18-maincdf5618)
- [infra/v2.10.0: 2026-07-15 (`main`@`fb2a133`)](#infrav2100-2026-07-15-mainfb2a133)
- [infra/v2.9.0: 2026-07-10](#infrav290-2026-07-10)
- [infra/v2.8.0: 2026-06-29 (`main`@`21b24cf`)](#infrav280-2026-06-29-main21b24cf)
- [infra/v2.7.0: 2026-06-23 (`main`@`b2c0051`)](#infrav270-2026-06-23-mainb2c0051)
- [infra/v2.6.0: 2026-06-23](#infrav260-2026-06-23)
- [infra/v2.5.0: 2026-06-22 (`main`@`ddf0112`)](#infrav250-2026-06-22-mainddf0112)
- [infra/v2.4.0](#infrav240)
- [infra/v2.2.0](#infrav220)
- [infra/v2.1.0: 2026-06-10](#infrav210-2026-06-10)
- [Before v2.1.0](#before-v210)

<!-- /toc -->

## Summary

| Tag | Deployed | `main` | Booking behaviour | Headline |
|-----|----------|--------|-------------------|----------|
| `infra/v2.16.0` | 2026-08-24 | `4462f56` | unchanged | Dependency refresh + dep-comment drift guard (#203–#206) |
| `infra/v2.15.0` | 2026-08-16 | `8342b67` | unchanged | Blind-POST rejection reason tagging (#201) |
| `infra/v2.14.0` | 2026-08-15 | `e6a8abb` | **changed** | T0 blind-POST stagger (#199) |
| `infra/v2.13.0` | 2026-08-09 | `de90316` | unchanged | Dependency + toolchain refresh |
| `infra/v2.12.0` | 2026-08-02 | `a7a1a6c` | unchanged | Log-redaction filter + 0-match search diagnostics |
| `infra/v2.11.0` | 2026-07-18 | `cdf5618` | **changed** | Burst reverted to 3 + server-`Date` logging (#181, #182) |
| `infra/v2.10.0` | 2026-07-15 | `fb2a133` | **changed** | Email-OTP response batch, burst-of-one (#177–#179) |
| `infra/v2.9.0` | 2026-07-10 | `a67eccd` | **changed** | 2026-07-09 scan fix batch + python 3.14 base |
| `infra/v2.8.0` | 2026-06-29 | `21b24cf` | **changed** | Blind-POST fallback rework + scan hardening (#157–#164) |
| `infra/v2.7.0` | 2026-06-23 | `b2c0051` | unchanged | Observability + redaction hardening (#153, #154) |
| `infra/v2.6.0` | 2026-06-23 | | unchanged | Shared-ACR consolidation (infra only) |
| `infra/v2.5.0` | 2026-06-22 | `ddf0112` | **changed** | Blind-POST race feature (#125–#130, #131) |
| `infra/v2.4.0` | | | **changed** | Race pre-warm bundle |
| `infra/v2.2.0` | | | **changed** | Within-window upgrade |
| `infra/v2.1.0` | 2026-06-10 | | **changed** | Multi-day Sat+Sun, cutoff + skip-days live |

## infra/v2.16.0: 2026-08-24 (`main`@`4462f56`)

The **dependency refresh + dep-comment drift guard** (#203/#204/#205/#206). **NO booking-behavior
change** — nothing touches slot selection, burst size, stagger offsets, timing, or any T0 decision
path (same risk class as `infra/v2.13.0`, the previous dependency refresh).

**Runtime dep IN the image:** `idna` 3.18→3.19 (#204) — transitive via httpx/anyio, never imported
directly, and NOT security-driven: the CVE-2026-45409 boundary is **3.15**, which prod cleared two
releases ago. **NOT in the image** (`uv sync --no-dev` excludes the PEP 735 dev group; setup-uv is a
CI runner action only): `astral-sh/setup-uv` 9.0.0→10.0.1 (#203 — a MAJOR bump; v10 disables the
cache on sensitive events, and it is used only in `ci.yml`), `ruff` 0.16.2→0.16.3 and `mypy`
2.3.0→2.3.1 (#205).

**Docs/tests only (#206):** the `idna` comment in `pyproject.toml` asserted "Floor now tracks the
locked version (3.18)" while sitting above `idna>=3.19` — false the moment it merged, and the
**second** drift of that same claim (cleaned in #106, re-drifted by #197/#204). Dependabot bumps
that floor on every idna release, so ANY version literal in the comment is stale by construction;
the comment now names **no** tracking version and two guards in `tests/test_docs_consistency.py` pin
it — the comment may name no version but the CVE boundary, and the floor may never drop below that
boundary. CLAUDE.md's change→docs map gained a row for the class. Known scope limit: the guard is
**idna-only**, so a NEW tracking comment on another dep would not be caught (no other dep carries
one today — the `ruff`/`mypy` comments state facts that survive bumps).

**Why deployed at all:** only `idna` reaches the image and it is not security-driven, so this was
initially recommended for batching — but with no further changes expected for a while there is no
next tag to batch into, and prod would otherwise sit indefinitely behind. Operator decision
2026-08-24.

Verification: 761 tests, ruff + ruff format + mypy strict clean, `uv lock --locked` ok, two
adversarial review rounds on #206 (APPROVE, APPROVE), dev auto-deploy green on this commit. Prod
param latch verified pre-tag (`dryRun=false`, `killswitchFired=false`, `enableSchedules=true`). All
three prod jobs verified live on `teetime:4462f561b771c8f1dfaaf0f856edb5b39dac9fd8` with crons +
timeouts + `dryRun=false` UNCHANGED from the v2.15.0 baseline: `teetime-job-prod-edt` `50 9 * * *` /
1200 s, `teetime-job-prod-est` `50 10 * * *` / 1200 s, `teetime-watch-job-prod` `*/10 * * * *` / 300
s; post-deploy watch cycle at 16:30 UTC ran clean (authenticated, saw both held reservations,
checked both target dates). First exercise: the Sat 2026-08-29 05:50 ET drop (books 9/5).

## infra/v2.15.0: 2026-08-16 (`main`@`8342b67`)

Blind-POST rejection **reason tagging** (#201). **NO booking-behavior change** — nothing touches
slot selection, burst size, timing, or any T0 decision path (same risk class as
`infra/v2.7.0`/`v2.12.0`). ForeUP returns HTTP 400 for two rejections carrying OPPOSITE evidential
weight, with no machine-readable discriminator — only the `msg` prose differs: `"Time not
available."` (the slot was not bookable — claimed first, OR our POST beat the release flip; the ONLY
one bearing on the pre-open-vs-race question) versus `"...1 online reservation per day."` (ForeUP
bouncing the SURPLUS POSTs of a burst we ALREADY WON). The burst collapsed both into `gone`, and the
aggregate line asserted "claimed pre-book" for every 4xx — so the 2026-08-16 drop (1 booked / 2
`daily_limit`) reported two lost races that never happened, polluting exactly the offset→outcome
signal `v2.14.0`'s stagger exists to produce, immediately before the Sat 8/22 reading.
`SlotGoneError` now carries `reason` (`unavailable` | `daily_limit` | `conflict` | `unknown`),
tagged by `ForeUpAdapter._classify_book_rejection`; `_blind_outcome_label` logs `gone[<reason>]` and
the aggregate becomes `N of M slot(s) rejected (<reason>=<count>, …)`.

**Three deliberate design points:** (1) an ALL-`daily_limit` sweep with nothing booked is NOT
reported as a race wipeout — it means a reservation for that date ALREADY EXISTED which this burst
did not make, and `_reguard_before_fallback` then short-circuits to `ALREADY_BOOKED` without the
fallback search the old text promised (it stays WARNING; a reservation the pre-T0 layer-2 guard
could not see warrants attention); (2) markers cover only wordings OBSERVED live — a 35-day prod Log
Analytics sweep found exactly TWO distinct book-rejection bodies, so the speculative "no longer
available" marker was dropped and anything unmatched surfaces as `gone[unknown]` rather than being
misfiled as race evidence; (3) `reason` is DIAGNOSTIC ONLY — every value routes identically
(`SlotGoneError` → try-next-slot), verified against every `except SlotGoneError` site.

**Evidential caution (do not lose this before Sat 8/22):** the 1-booked/2-`daily_limit` shape is NOT
established as a stagger effect. It was the first non-uniform outcome in the LOG RETENTION WINDOW,
but the pre-stagger 2026-07-11 drop produced the same shape from a SIMULTANEOUS burst (see the
`infra/v2.9.0` paragraph), so a simultaneous burst can also serialize behind ForeUP's 1/day counter.
Treat the shape as uninformative about timing until more drops land.

**Known open question (BACKLOG):** whether ForeUP's 1/day rule is scoped to the PLAY date or the
CALENDAR day the booking is made. The multi-day design implies play-date and nothing observed
contradicts it, but it is unpinned — if it were booking-day scoped, an all-`daily_limit` Sunday
burst could be caused by Saturday's reservation and the "we already hold a reservation for this
date" wording would be wrong.

Verification: 759 tests, ruff + ruff format + mypy strict clean, TWO adversarial review rounds
(APPROVE, APPROVE — round 2 caught the causal over-claim above). All three prod jobs verified live
on `teetime:8342b67f2113657b45786191b72a859df25ac4c5` with crons + timeouts + `dryRun=false`
UNCHANGED from the v2.14.0 baseline: `teetime-job-prod-edt` `50 9 * * *` / 1200 s,
`teetime-job-prod-est` `50 10 * * *` / 1200 s, `teetime-watch-job-prod` `*/10 * * * *` / 300 s.
First exercise: the Sat 2026-08-22 05:50 ET drop (books 8/29) — which is also the first real
diagnostic reading of the stagger itself.

## infra/v2.14.0: 2026-08-15 (`main`@`e6a8abb`)

The **T0 blind-POST stagger** (docs/plans/STAGGER_PLAN.md, #199). **BOOKING-BEHAVIOR CHANGE**, race
path only (`--wait` + blind-capable primary) — the first since `infra/v2.11.0`. The T0 blind burst
no longer fires all N POSTs at one instant: each sleeps to its own offset from
`scheduler.blind_post_stagger_ms` (default `(-500, -250, 0)` ms relative to T0), paired positionally
with the RANK-ordered slots, so the burst SPANS ForeUP's release boundary instead of point-sampling
it.

**Why:** every drop in the Log Analytics retention window came back 3/3 or 0/3, NEVER mixed — a
shape a genuine slot race cannot produce, since our POSTs land within ~100 ms of the open and nobody
books three specific tee times in 100 ms. A burst arriving before the release flip gets the SAME
`400 {"success":false,"msg":"Time not available."}` a claimed slot returns, and the server `Date`
header's 1-second resolution (added in `infra/v2.11.0` for exactly this question) cannot separate
them. Staggering is both a HEDGE (one POST is always SENT no earlier than T0, so a wipeout can't
take the burst as a unit) and a DIAGNOSTIC (outcomes become ordered by offset). Motivated by the
2026-08-15 miss (target Sat 8/22) and the still-unexplained 2026-07-18 miss. **Non-regression is the
binding constraint and is pinned mechanically:** `stagger[0] == -early_arrival_ms`, so the rank-0
(nearest-midpoint) slot fires at EXACTLY its pre-stagger instant and every drop already won is
unchanged — only the surplus POSTs move, and those are already 400'd by the 1/day rule when rank-0
wins. `tests/test_container_config_parity.py` asserts both `stagger[0] == -early_arrival_ms` and
`min(stagger) == -early_arrival_ms` (operator directive 2026-08-15: nothing may be scheduled EARLIER
than today's fire instant). The tail offset is `0` — SENT at 06:00:00.000, carried past the open by
network latency on ARRIVAL — chosen over `+250` to give up the least ground in a genuine race.

**Three supporting changes:** (1) the burst RE-RANKS with `rank_slots_for_request` before pairing
offsets — ranked order had been only an adapter convention the simultaneous burst never depended on,
and is now a safety property, since a worse slot POSTing first would let the 1/day rule reject the
better one; (2) a `field_validator` rejects a DESCENDING offset list (`(-500, 0, -250)` passes every
parity assertion while doing exactly that); (3) the per-POST diagnostic reports the **MEASURED**
send offset, not the planned one — on a run starting past T0 every delay is non-positive and all
POSTs go out simultaneously, so logging the planned ladder would show instants that never happened
and an operator reading a 0/N would wrongly conclude "unordered ⇒ not the boundary". Known
limitation (accepted, BACKLOG.md): offsets correlate with slot rank, so the offset→outcome signal is
confounded — a control POST (same slot at two offsets) and CAPTCHA-token recycling are deferred.

Verification: 749 tests, ruff + ruff format + mypy strict clean, TWO adversarial review rounds
(BLOCK → APPROVE), dev deploy green on this commit. All three prod jobs verified live on
`teetime:e6a8abbe72d4846099864e5d032720ad3017470a` with crons + timeouts + `dryRun=false` UNCHANGED
from the v2.13.0 baseline: `teetime-job-prod-edt` `50 9 * * *` / 1200 s, `teetime-job-prod-est` `50
10 * * *` / 1200 s, `teetime-watch-job-prod` `*/10 * * * *` / 300 s. First exercise: the Sun
2026-08-16 05:50 ET drop (books 8/23) — a non-regression check only, since both 0/3 misses (one
explained, one not) fell on SATURDAYS and every Sunday in the retention window booked cleanly. First
real diagnostic reading: the Sat 2026-08-22 drop.

## infra/v2.13.0: 2026-08-09 (`main`@`de90316`)

The dependency + toolchain refresh. **No booking-behavior change** — nothing touches slot selection,
burst size, timing, or any T0 decision path (same risk class as `infra/v2.7.0`/`v2.12.0`). Runtime
deps IN the image: `click` 8.3.3→8.4.2, `idna` 3.17→3.18 (`httpx` 0.28.1 and `pydantic` 2.13.4 were
already current). Dev toolchain — NOT in the image, `uv sync --no-dev` excludes it: `ruff`
0.15.15→0.16.2, `mypy` 2.1.0→2.3.0, `pytest` 9.0.3→9.1.1, `pytest-asyncio` 1.3.0→1.4.0, `pip-audit`
2.10.0→2.10.1. Floors in `pyproject.toml` were raised to match; they had drifted badly (`ruff>=0.5`
against a locked 0.15.15), advertising support for untested versions.

**One application-code change, behavior-preserving (#197):** `UpgradeOrchestrator. _persist_upgrade`
and `._cancel_and_book_slot` are now KEYWORD-ONLY past their leading args. `_persist_upgrade` takes
`current_booking` AND `new_result`, **both `BookingResult`** — a positional transposition
type-checked clean and would have silently persisted the OLD booking as the upgrade result. mypy
cannot catch same-typed adjacent params; the `*` can. Surfaced by ruff 0.16 promoting `PLR0917` to
stable. Verified argument-by-argument at both call sites and covered by
`test_upgrade_deletes_then_reinserts_terminal_under_lock`, which fails on a swap. `PLR0917` is
deliberately NOT ignored globally — only per-file for the three orchestrator collaborator-injection
ctors (all-distinct types, so mypy catches a swap) and `tests/**` date-builders; it stays live
everywhere else, verified non-vacuous.

**Two ruff-0.16 config consequences:** (1) `PLR0917` as above; (2) ruff 0.16 began formatting python
code blocks INSIDE Markdown, and our plan docs quote FRAGMENTS (bare ctor params) which it parses as
standalone statements and rewrites WRONGLY — turning `x: T | None = None,` into `x: T | None =
(None,)`, a TUPLE. Unnoticed, `ruff format .` would have silently corrupted 8 ratified design docs
into describing something other than what shipped. Markdown is now excluded (`extend-exclude =
["*.md"]` in `[tool.ruff]`).

**Supporting CI/config changes, no image effect:** Dependabot now manages Python via the `uv`
ecosystem (#192) with a WORKING prod/dev split — the first attempt silently collapsed because dev
deps were a `[project.optional-dependencies]` extra, which Dependabot classifies as production, so
#193 put pytest/ruff/mypy in the "python-runtime" group; #195 moved them to a PEP 735
`[dependency-groups]`, which ALSO made the Dockerfile's `uv sync --no-dev` load-bearing (it excludes
GROUPS, not extras — previously the image stayed lean only because extras are opt-in). `pip`
26.1.1→26.1.2 (#191) let the `PYSEC-2026-196` pip-audit suppression be retired (#194), so the CVE
gate runs unsuppressed. Dependabot alerts + security updates are ENABLED.

Verification: ruff clean, ruff format clean, mypy strict clean, 731 tests, pip-audit clean
unsuppressed, `uv lock --locked` passes, docker build + smoke green, dev deploy green on this
commit, three adversarial review rounds all APPROVE. Deployed while holding Sat 8/15 09:22 and Sun
8/16 09:22; first exercise is the Sat 2026-08-15 05:50 ET drop (books 8/22).

## infra/v2.12.0: 2026-08-02 (`main`@`a7a1a6c`)

The log-redaction filter + 0-match search diagnostics. **No booking-behavior change** — nothing
touches slot selection, burst size, timing, or any T0 decision path (same risk class as
`infra/v2.7.0`). Security (#187): the 2captcha API key no longer reaches stdout / Log Analytics.
httpx logs every request at INFO and the 2captcha result-poll URL carries the key as a query param —
71 such lines in the 2026-08-01 prod run. `core.redaction.RedactingLogFilter` +
`install_log_redaction()` attach to the root logger's HANDLERS (a logger-level filter does NOT see
records propagating up from `httpx`) and scrub the rendered message, `%`-args, `exc_info` tracebacks
and `stack_info`. The leaked key was **rotated** the same day (2026-08-02); both Key Vaults hold the
new value and the local `.env` was updated. Known gap (accepted): a traceback printed by Python's
default excepthook bypasses logging entirely. Observability (#188): a search that returns inventory
but matches NOTHING now logs a per-filter rejection tally + the span of tee times actually on offer,
against the requested window — which distinguishes a course-level block from a lost slot race. That
ambiguity cost real diagnosis time after the 2026-08-01 miss, whose cause turned out to be Mangrove
Bay's 8 AM shotgun Anniversary Tournament on the 8/8 TARGET date (no public tee time before ~16:07).
INFO when purely out-of-window (the routine sell-out), WARNING when any other leg fires. Also in
this tag: Dependabot action bumps (setup-python v7, setup-uv v9, checkout 7.0.1), SHA-pins verified
against upstream tags. All three prod jobs verified on the `teetime:a7a1a6c…` image with
crons/timeouts/`dryRun=false` unchanged. First exercise: the Sat 2026-08-08 05:50 ET drop (books
8/15).

## infra/v2.11.0: 2026-07-18 (`main`@`cdf5618`)

The T0-hedge restore + early-arrival diagnostic. Booking-behavior: the blind burst is reverted to
**3** (`blind_post_max_count=3`, default + all shipped configs) — burst-of-one (v2.10.0) bet the
whole drop on the single nearest-midpoint slot and lost that race with nothing else in flight (the
2026-07-18 Sat 7/25 miss, terminal `no_inventory`); the top-3 concurrent POSTs restore the slot-race
hedge, and ForeUP's 1/day rule 400-rejects the surplus once the first lands (`_cancel_extras` keeps
the best, proven live 2026-07-11 which booked the 3rd-ranked sibling). Observability:
`ForeUpAdapter.book()` logs ForeUP's server `Date` response header on every book POST — the server
clock at processing time disambiguates a **pre-open rejection** (the 500 ms `early_arrival_ms` fire
landing before the 06:00 ET open → 400 stamped 05:59:59) from a **genuine slot-race loss**
(06:00:00), which are byte-identical by body (the 2026-07-18 miss couldn't tell them apart). All
three prod jobs verified on the `teetime:cdf5618` image. First exercise: the Sun 2026-07-19 05:50 ET
drop (books 7/26) — the first burst-3 + Date-log drop.

## infra/v2.10.0: 2026-07-15 (`main`@`fb2a133`)

The email-OTP response batch (#177/#178/#179), shipped the same day MB's email-OTP gate went live.
Booking-behavior: the blind burst is **burst-of-one** (`blind_post_max_count=1`, default + all
shipped configs — ForeUP's "1 online reservation per day" rule 400-rejects surplus POSTs, so a wider
burst made the winner first-processed rather than best-ranked; a miss falls to the sequential
center-out fallback with the 2 pooled reserve tokens; **superseded 2026-07-18 by `infra/v2.11.0` —
burst reverted to 3** after burst-of-one's single-slot race loss caused the 2026-07-18 Sat 7/25
miss, see the v2.11.0 paragraph above) — and a cancel-DELETE 400 "We can't find that teetime" is
treated as already-cancelled (ForeUP uses it, not 404, for a missing/expired reservation — observed
live). OTP posture: the 2026-07-15 live recon showed the email-OTP gate is **UI-only** (the bot's
direct API book POST books unchallenged, HTTP 200 + instant confirmation), so the OtpSource stays
off the critical path; `_guard_otp_challenge` → `OtpChallengeError` (CaptchaError subclass) is the
loud observation signal if ForeUP ever extends enforcement to the API. All three prod jobs verified
on the `teetime:fb2a133` image. First drop on this image: Sat 2026-07-18 05:50 ET (books 7/25) — the
first OTP-era drop.

## infra/v2.9.0: 2026-07-10

The 2026-07-09 full-repo-scan fix batch + the python 3.14 base. Booking-behavior: a SURPLUS-cancel
failure (429/captcha/transport blip while cancelling a blind-POST extra) can no longer discard the
kept booking — `_cancel_extras` catches `Exception` broadly, and the watcher reconcile got the same
broadening with the watch-contract errors re-raised (#166). Security: the 2captcha result-poll no
longer leaks the API key into Log Analytics on a non-2xx (sanitized RuntimeError; `redact_text` also
masks credential-named URL query params) (#167); the container runs as a non-root user on a
digest-pinned `python:3.14-slim` base, all workflow actions are SHA-pinned, and Dependabot keeps the
pins fresh (#168, #174 — dev venvs already ran 3.14, the image was the lagging environment).
Observability: the blind-burst captured-`BaseException` branch now logs, the reguard-reauth-fail
WARNING + reconcile CRITICAL are test-pinned, and lock-defer logs are visible at INFO (#173).
Config/docs: `blind_post_max_count` code default aligned to **3**, the `tests/
test_docs_consistency.py` tag-agreement CI guard + the CLAUDE.md change→docs map exist (#172). The
prod jobs were rebuilt + redeployed on the `infra/v2.9.0` image (`teetime:a67eccd`, verified live on
all three jobs); its first real booking exercise (and CPython 3.14's) was the 2026-07-11 drop —
booked Sat 7/18 09:30 (3rd-ranked; the two blind-burst siblings were 400-rejected by the
1-reservation/day rule, the observation that motivated v2.10.0's burst-of-one).

## infra/v2.8.0: 2026-06-29 (`main`@`21b24cf`)

The blind-POST fallback rework + scan hardening (a booking-behaviour change): the booking jobs run
`blind_post_max_count=3` + `blind_post_fallback_token_reserve=2`, the concurrent hedge search is
dropped, and the 0-booked path fires a FRESH search strictly after the re-guard
([RESEARCH_FALLBACK_PLAN.md](./plans/RESEARCH_FALLBACK_PLAN.md), PRs #157–#160), plus the
robustness fixes #161–#164 (book() 429 → `RateLimitError`, re-guard skip-on-reauth-fail, a
blind-burst `BaseException` secures a booked sibling).

## infra/v2.7.0: 2026-06-23 (`main`@`b2c0051`)

Observability + redaction hardening, no booking-behaviour change: the booking job's `_run` logs a
traceback before exit on a failed run (#154), and `redact_text` Luhn-masks PANs in free-text logs
(#153).

## infra/v2.6.0: 2026-06-23

Infra-only shared-ACR consolidation: the ACR moved to a dedicated `rg-teetime-shared` with both envs
as non-owners (AZURE_PLAN §2.1/§10.6).

## infra/v2.5.0: 2026-06-22 (`main`@`ddf0112`)

The runtime feature set, `dryRun=false`: the **blind-POST race feature**
([BLIND_POST_PLAN.md](./plans/BLIND_POST_PLAN.md), PRs #125–#130: Mangrove-Bay-only concurrent
blind book POSTs at T0 for the top-N synthesized in-window slots, keep-best + cancel-extras in-run,
re-guard before the search fallback, watcher >1-reservation reconcile crash-net) and the #131
soft-login-skip fix (record the post-T0 re-auth skip only on a session-established login). Also
active from earlier tags: multi-day Sat+Sun booking, the 4 PM-day-before booking cutoff, the
Portal-editable skip-days (`TEETIME-SKIP-DATES` Key Vault secret, present in both vaults), the
watcher today+7 horizon (#119) and CAPTCHA `TimeoutError` recovery (`book()` / `prepare_book()` →
`CaptchaError`, lead 120 s; #120).

## infra/v2.4.0

The **race pre-warm bundle** ([RACE_PREWARM_PLAN.md](./plans/RACE_PREWARM_PLAN.md)): pre-T0
ForeUP login pre-warm + layer-2 guard, a multi-token concurrent CAPTCHA pool, and the race-path
leading-search-sleep trim.

## infra/v2.2.0

Within-window upgrade: a strictly-closer-to-midpoint slot in the same tier triggers
cancel-before-book.

## infra/v2.1.0: 2026-06-10

Multi-day re-architecture live ([MULTIDAY_PLAN.md](./plans/MULTIDAY_PLAN.md),
[PERDAY_WINDOWS_PLAN.md](./plans/PERDAY_WINDOWS_PLAN.md),
[LEADTIME_SKIP_PLAN.md](./plans/LEADTIME_SKIP_PLAN.md)): daily booking crons + booking-day gate,
Saturday and Sunday booked (one reservation per day), per-day windows, the booking cutoff and
skip-days. The renamed `-edt`/`-est` jobs are deployed in both envs and the old
`-edt-sun`/`-est-sun` orphans were deleted per the AZURE_PLAN §10.2 runbook.

## Before v2.1.0

M6 took prod live (`dryRun=false`; [M6_PLAN.md](./plans/M6_PLAN.md)). A real booking race ran
2026-06-07 and lost on CAPTCHA latency, which led to the race-path CAPTCHA pre-fetch (#68) and the
book-POST 4xx → next-slot fallback (#67).
