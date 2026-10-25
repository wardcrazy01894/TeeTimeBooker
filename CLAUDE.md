# CLAUDE.md

Operator and agent notes for this repo: current state, commands, the load-bearing invariants
that are not obvious from filenames, and the rules for keeping docs in sync. The engine design
is [PLAN.md](./PLAN.md); read it first if you are new.

Nested `CLAUDE.md` files load automatically when you work in their subtree (and NOT when you only
touch `tests/`, so read the relevant one first): [`src/teetime/core/CLAUDE.md`](./src/teetime/core/CLAUDE.md)
(engine invariants: time gates, stores + double booking, search/book errors, the T0 race, the
blind-POST burst, watcher + upgrade), [`src/teetime/tenant/CLAUDE.md`](./src/teetime/tenant/CLAUDE.md)
(multi-user path), [`src/teetime/web/CLAUDE.md`](./src/teetime/web/CLAUDE.md) (web app),
[`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md) (per-course IDs and quirks,
adding a course, email OTP) and [`infra/CLAUDE.md`](./infra/CLAUDE.md) (Azure infra and deploy safety).

<!-- toc -->
## Contents

- [What this is](#what-this-is)
- [Current status](#current-status)
  - [Production (tenant path since MU-18)](#production-tenant-path-since-mu-18)
  - [Dev (tenant path, MU-17)](#dev-tenant-path-mu-17)
  - [Multi-user milestones](#multi-user-milestones)
  - [Cut from scope](#cut-from-scope)
- [Where the docs live](#where-the-docs-live)
- [Package layout](#package-layout)
- [Common commands](#common-commands)
- [Architectural invariants](#architectural-invariants)
  - [Design principles](#design-principles)
  - [Where the rest live (nested files, loaded per subtree)](#where-the-rest-live-nested-files-loaded-per-subtree)
  - [Security and redaction](#security-and-redaction)
- [Per-course specifics](#per-course-specifics)
- [Red-green TDD (mandatory)](#red-green-tdd-mandatory)
- [Documentation standard](#documentation-standard)
  - [Change→docs map](#changedocs-map)
- [Required CI checks](#required-ci-checks)
- [When in doubt](#when-in-doubt)
- [Azure infra and the deploy safety rule](#azure-infra-and-the-deploy-safety-rule)

<!-- /toc -->

## What this is

A Python 3.14+ bot that books golf tee times on the ForeUP and TeeItUp platforms. The primary
target is **Mangrove Bay Golf Course** (St. Petersburg, FL; ForeUP), whose online window opens at
06:00 America/New_York, 7 days ahead; the bot books Saturday and Sunday mornings. TeeItUp courses
(e.g. Sydney R. Marovitz, Chicago Park District) are also supported. It runs as Azure Container
Apps (ACA) Jobs; GitHub Actions is CI and deploy only.

Two paths exist side by side:

- **Tenant path** (`teetime tenant-run` / `tenant-watch` / `web`): the invite-only multi-user site
  from [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md). It runs the UNMODIFIED `Orchestrator` once per
  account. This is what **prod** runs since MU-18 stage B (`infra/v3.0.0`) and **dev** since MU-17.
- **TOML path** (`teetime run` / `teetime watch`): single-user, configured by a TOML file. Kept for
  local course testing and as prod's rollback (flip `bookingMode`/`watchMode` back to `toml`).

## Current status

### Production (tenant path since MU-18)

| | |
|---|---|
| Latest infra tag | latest infra tag `infra/v3.16.0` (2026-10-24: web dependency sweep, fastapi 0.151.0 + authlib 1.9.1, filelock 4.2.4 for three same-day CVEs in a dev-only dependency, no booking-behaviour change); history in [docs/RELEASES.md](./docs/RELEASES.md) |
| Mode | `dryRun=false`, `killswitchFired=false`, `enableSchedules=true` |
| Booking jobs | `teetime-job-prod-edt` `50 9 * * *` and `teetime-job-prod-est` `50 10 * * *` (05:50 ET, one per DST half), 1200 s timeout; `tenant-run --event mb0600et --wait` since MU-18 stage B (was `run --wait`) |
| Watch job | `teetime-watch-job-prod` `*/10 * * * *`, 300 s timeout; `tenant-watch` since MU-18 stage B |
| Books | Sat + Sun, one reservation per day, 7 days ahead, nearest the window midpoint |

What is live: multi-day Sat+Sun booking with per-day windows, the 16:00-day-before booking cutoff,
Portal-editable skip-days, within-window upgrade, the race pre-warm bundle (login pre-warm,
multi-token CAPTCHA pool, search-sleep trim), the Mangrove Bay blind-POST burst (2 POSTs staggered
`-369/0` ms across T0 since 2026-10-04, was `-400/0` until 2026-10-04, `-400/-250/0` since 2026-09-30 and `-500/-250/0` before; keep best, cancel extras, re-guard then fresh-search fallback), the
watcher's duplicate-reservation crash-net, blind-POST rejection reason tagging, log redaction on
every handler, and email-OTP challenge detection.

Known benign quirk: a watch-cron fire that lands mid-deploy can lose one 10-minute cycle (a
transient ACR 401, or the placeholder image on a NEW environment's first deploy: deploy pass 1 is
skipped once the env's AcrPull grant exists); it self-heals on the next fire. Verification and
cutover runbook: AZURE_PLAN §10.4/§10.5. The `enableSchedules` Bicep param silences an env.

### Dev (tenant path, MU-17)

Dev auto-deploys from `main` in permanent `dryRun = true`. Since MU-17 its booker and watcher run
`tenant-run --event mb0600et` / `tenant-watch` (watcher hourly, `0 * * * *`) over the shared Cosmos
`dev` database, and the `teetime-web-dev` Container App (Google sign-in) and ACS email are
deployed. Runbook: AZURE_PLAN §10.7 (prod followed at MU-18, §10.8).

The cost killswitch ($50 actual spend → Logic App disables and stops every ACA Job and stops the
web apps) is armed in dev and manages both envs; the $20 email budget is the early-warning tier.
See `infra/AZURE_PLAN.md §9.2`.

### Multi-user milestones

Plan: [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md) §12 (ratified 2026-09-25) holds the per-milestone
table; how each was actually built, deviations included: [docs/MULTIUSER_AS_BUILT.md](./docs/MULTIUSER_AS_BUILT.md).
Every milestone MU-1 … MU-18 and MU-R1 … R3 is done (prod cutover `infra/v3.0.0`, 2026-09-27);
**MU-19** (retire the TOML job wiring after 4 clean weekends) is open; **MU-20** (remove the
TOML CLI) was dropped: the operator keeps it (MULTIUSER_PLAN §13 Q6).

### Cut from scope

- **M2.T3** (a synchronous in-run UNCERTAIN → RECONCILING path): an UNCERTAIN book (timeout/5xx)
  raises out loudly and the watcher reconciles it on its next poll. The watcher's uptime is
  therefore load-bearing. PLAN.md §9.1.
- **M3** (SQLite) and **M4** (email) for the TOML path: `InMemoryStore` + `ConsoleNotifier` are
  the final wiring there, not stubs. The tenant path has its own durable store and email.

## Where the docs live

| Doc | What it is |
|-----|-----------|
| [README.md](./README.md) | Newcomer overview, quick start, config, running, deploy |
| [PLAN.md](./PLAN.md) | Engine design: state machine, DST math, etiquette, milestones |
| [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md) | Multi-user site design and MU milestone table |
| [infra/AZURE_PLAN.md](./infra/AZURE_PLAN.md) | Azure hosting design, secrets inventory, runbooks |
| [BACKLOG.md](./BACKLOG.md) | Future wants and deferred items |
| [docs/RELEASES.md](./docs/RELEASES.md) | Every prod infra tag, newest first |
| [docs/MULTIUSER_AS_BUILT.md](./docs/MULTIUSER_AS_BUILT.md) | Per-milestone build notes for the tenant path |
| [docs/plans/](./docs/plans/) | Shipped (historical) design plans, each with a status banner |
| [`src/teetime/core/CLAUDE.md`](./src/teetime/core/CLAUDE.md) | Engine invariants (loads under `core/`): time gates, stores, search/book errors, the T0 race, the blind-POST burst, watcher + upgrade |
| [`src/teetime/tenant/CLAUDE.md`](./src/teetime/tenant/CLAUDE.md) | Multi-user path invariants (loads under `tenant/`) |
| [`src/teetime/web/CLAUDE.md`](./src/teetime/web/CLAUDE.md) | Web app invariants (loads under `web/`) |
| [`.claude/skills/docs-map/SKILL.md`](./.claude/skills/docs-map/SKILL.md) | The change→docs map (`/docs-map`): every doc site each kind of change must update |

## Package layout

`src/teetime/` is one package per concern: `core`, `persistence`, `notifications`,
`courses/{foreup,teeitup,chronogolf}`, `tenant`, `web`, `dev`; `config/`, `infra/bicep/`, `docs/`,
`.github/workflows/` and `tests/` are what `ls` shows.

The orchestrator is the only thing that knows all four engine subsystems. Persistence,
notifications and adapters see each other only through Protocols in `core/`; that is the cut line
for parallel work. Nothing in `core/`, `courses/` or `persistence/` imports `tenant`.

## Common commands

The standard toolchain (`uv sync`, `uv run pytest [-m "not integration"]`, `uv run mypy`,
`uv run ruff check|format .`) is what `pyproject.toml` and `.githooks/pre-push` define; the
project's own commands:

| Command | Purpose |
|---------|---------|
| `uv run teetime run --config config/local.toml --dry-run true` | One-shot booking attempt, no final POST |
| `uv run teetime watch --config config/local.toml --dry-run true` | One watcher pass |
| `uv run teetime show-config --config config/local.toml` | Resolved AppConfig, secrets redacted |
| `uv run teetime web --port 8000` | Multi-user web app (MU-12). Cosmos when `TENANT_COSMOS_ENDPOINT` is set, else in memory; `TENANT_CREDS_KEYRING` enables connect/refresh/cancel. Env vars in README |
| `uv run teetime tenant-watch --dry-run true` | One tenant-watcher run (MU-10b). Needs `TENANT_CREDS_KEYRING`, plus `TWOCAPTCHA_API_KEY` unless dry-run |
| `uv run teetime tenant-run --event mb0600et --dry-run true --no-wait` | One tenant booking run for a release event (MU-9b). Same env as `tenant-watch`. Non-zero exit only for systemic causes (MULTIUSER_PLAN §4.5) |
| `uv run teetime tenant-plan --event mb0600et` | Print an event's pending rows + blind-slot allocation; no ForeUP call |
| `uv run teetime tenant-migrate` | Tenant data migrations (MU-16a). Requires `TENANT_COSMOS_ENDPOINT` + `TENANT_COSMOS_DATABASE`; what the Manual `teetime-migrate-<env>` job runs |

## Architectural invariants

Each bullet is a rule that code, tests or incidents have made load-bearing. Change one only with
a test that proves the new behaviour, and update this section in the same PR.

### Design principles

- **Stubs raise `NotImplementedError`** with a milestone reference. Implementing one means making
  its tests pass in the same PR.
- **Protocols over ABCs.** Contracts are `runtime_checkable` `Protocol`s. Subclassing for shared
  state is fine, but tests must not require it: structural typing is the contract.
- **Clock is injectable everywhere.** Anything touching wall-clock time takes a `Clock`, never
  `datetime.now`. Tests use `FakeClock` (single account) or `VirtualClock` (many concurrent
  sleepers); the 06:00 race is otherwise untestable.
- **No secrets in TOML.** Configs name env vars; the loader resolves them and a missing one raises
  a clear error.
- **Each run is independent.** No state is shared between the watch job and the booking job; the
  live `list_reservations()` call is the cross-run source of truth. ACA Job concurrency serializes
  runs; in-process advisory locks serialize writes within a run.
- **`WatchOrchestrator` and `UpgradeOrchestrator` live in `core/`** and use the same
  collaborator-injection pattern as `Orchestrator`. Neither is long-running: each is one check per
  ACA Job execution.

### Where the rest live (nested files, loaded per subtree)

The remaining invariants moved on 2026-10-25 to the directory they govern. They are as
load-bearing as the ones above; read the file BEFORE editing that tree or its tests.
What must never be forgotten even without them:

- **[`src/teetime/core/CLAUDE.md`](./src/teetime/core/CLAUDE.md)**: configuration, dates and
  time (the DST-half gate, per-weekday windows, the `(RequestId, resolved_date)` key, the hard
  booking cutoff and fail-open skip-days); double booking, idempotency and stores; search and
  book error handling; the 06:00 race (race path = `--wait` only); the blind-POST burst
  (ladder `-369/0`, the rank-0 POST at `stagger[0]`, NOTHING ever scheduled earlier, the per-POST
  measured log line stays); watcher and upgrade. `book()`'s POST is never retried.
- **[`src/teetime/tenant/CLAUDE.md`](./src/teetime/tenant/CLAUDE.md)**: the tenant path runs the
  UNMODIFIED `Orchestrator`/`WatchOrchestrator` per account and engine code never imports
  `tenant`; ownership gates every cancel and upgrade; dry-run environments never mutate a
  reservation and send no email except invitations; no store call inside the race window;
  exit codes are non-zero only for systemic causes; `TenantStore`'s contract is
  `tests/tenant/conformance.py`; the Cosmos index policy must equal `QUERIED_PATHS`.
- **[`src/teetime/web/CLAUDE.md`](./src/teetime/web/CLAUDE.md)**: the site works with JavaScript
  off, CSP allows no inline script or style, every form is a CSRF-guarded POST, every id is a
  user-scoped read, static assets go through `static_url`, one canonical host.
- **[`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md)**: per-course quirks and the
  Mangrove Bay email OTP (detection wired, not on the booking critical path).

### Security and redaction

- **Card data is platform-specific.** ForeUP keeps a card on file and the ForeUP path never POSTs
  PAN/CVV. TeeItUp has no wallet, so its adapter POSTs PAN + CVV + expiry + billing to
  `tr.gnsvc.com` on every booking (from `*_env` vars, `follow_redirects=False`). That brings PCI
  scope (PLAN.md §7): `BookingStore.append_attempt` applies `core.redaction.redact_payload` at the
  store boundary on every `attempt_log` write (PLAN.md §10.1), so no caller can leak card data.
- **Player PII is redacted before any `attempt_log` write** (SHA-256 prefix, PLAN.md §10.1). The
  log lives in `InMemoryStore` only.
- **Log redaction is a HANDLER filter, not call-site discipline.** Third-party loggers bypass
  `redact_text`: httpx logs every request at INFO and the 2captcha result-poll URL carries
  `res.php?key=<API_KEY>` (71 such lines in one prod run, 2026-08-01).
  `core.redaction.RedactingLogFilter` + `install_log_redaction()` fix this; every entrypoint
  calls `install_log_redaction()` immediately AFTER `logging.basicConfig(...)`.
  - **Placement is load-bearing:** the filter goes on the ROOT LOGGER'S HANDLERS. A filter on a
    logger sees only that logger's own records, not those propagating from `httpx`/`httpcore`.
  - **Ordering is load-bearing:** `basicConfig` creates the handler, so installing first attaches
    to nothing. `tests/test_log_redaction.py` pins the order by source position (under pytest
    the root logger already has handlers, so a functional test cannot) and pins through a real
    CLI entrypoint that the wiring exists.
  - The filter resolves `%`-args eagerly (the secret is usually in an arg), clears `record.args`,
    scrubs `exc_info` (via `record.exc_text`) and `stack_info`, is idempotent across handler
    fan-out, and never drops a record or raises `Exception` (a raise from `filter()` reaches the
    `log.…()` call site and at T0 would kill the run; a `BaseException` from a pathological
    `msg.__str__` still escapes, deliberately).
  - **Exact-literal secrets (E7, MULTIUSER_PLAN §9.4).** Keyring keys and decrypted passwords
    have no shape a pattern can catch. `core.redaction.register_secret_literals(values)` adds them
    to a process-wide registry that `redact_text` applies FIRST (masked whole as
    `<redacted-secret>`), so the filter and call-site `redact_text` both cover them. Additive and
    idempotent; longest literal wins on overlap; values shorter than
    `SECRET_LITERAL_MIN_LEN = 8` are ignored (a <8-char password is NOT masked), as are values
    inside a redaction marker; refused values are counted in one DEBUG line, and the call returns
    how many distinct values are masked. A bare `str` raises `TypeError`. The (set, pattern) state
    swaps as one tuple under a lock. An empty registry is a strict no-op (the TOML path registers
    nothing).
  - **Known gap (accepted):** a traceback printed by Python's default excepthook never passes
    through logging (`_run` logs `exc_info=True` then re-raises). Keeping credentials out of
    exception messages stays the primary defence.
  - **Testing note:** `install_log_redaction()` attaches to every root handler, including
    pytest's `LogCaptureHandler`, so `tests/conftest.py` restores handler filters AND the secret
    registry around every test. Without that, `caplog` stays redacted for the session and an
    `assert secret not in caplog.text` passes vacuously.

## Per-course specifics

Course IDs, URLs and quirks (Mangrove Bay / ForeUP, Sydney R. Marovitz / TeeItUp) and the
step-by-step for adding a course live in
[`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md).

## Red-green TDD (mandatory)

Every behaviour change lands test-first:

1. **Red.** Write the smallest test for the desired behaviour; confirm it fails for the right
   reason, not an import error or fixture typo.
2. **Green.** Write the minimum implementation that passes. No extra fields, no untested branches.
3. **Refactor.** Clean up with the tests green; re-run them.
4. **Commit boundary.** One meaningful red → green → refactor unit per commit.

Rules:

- A stub's `NotImplementedError` is the red phase already on disk: write the test next, then the
  body.
- Protocol implementations get a structural test (`isinstance(impl, Protocol)`) and at least one
  behavioural path (reference: `tests/test_adapter_stub.py`).
- State-machine work (PLAN §9.1, MULTIUSER_PLAN §3.4): one failing test per transition before its
  branch. The state machine is too subtle to backfill.
- `pytest -k <name>` for the inner loop; the full suite before commit.
- A bug in merged code gets its reproducing test FIRST.

Anti-patterns we reject: tests written after the code to describe it (they encode bugs as
features); mocking the type under test (mock collaborators only); skipping red; tests that pass
without calling the code path.

## Documentation standard

Every PR leaves the docs in sync with the code. Not every PR touches every doc, but every PR checks
the rows below that apply. A new CLI flag, env var or milestone with no doc update is incomplete.

| Doc | Update when |
|-----|-------------|
| `README.md` | Status or roadmap changes; new prerequisites, commands, env vars; architecture changes |
| `CLAUDE.md` | New invariants or agent rules; command changes; new subsystems or Protocols; status changes |
| `PLAN.md` | Engine milestone done or re-scoped; open questions resolved; new spikes |
| `MULTIUSER_PLAN.md` | MU milestone done (§12 row) or re-scoped; tenant design changes |
| `docs/MULTIUSER_AS_BUILT.md` | A tenant milestone lands or deviates from the plan |
| `docs/RELEASES.md` | Every prod infra tag |
| `infra/AZURE_PLAN.md` | Azure questions resolved; new Key Vault secrets; IaC module changes; OIDC/RBAC changes; runbooks |
| `BACKLOG.md` | A deferred item is added or retired |

Style: every doc over ~150 lines carries a `<!-- toc -->` block (CI checks that its links resolve);
prefer tables for facts, short paragraphs, one `> **Status:**` callout at the top of a plan.
Shipped plans move to `docs/plans/` with a status banner and keep their bodies.

### Change→docs map

The map (one row per kind of change, every doc site that claim lives in) is the project skill
[`.claude/skills/docs-map/SKILL.md`](./.claude/skills/docs-map/SKILL.md). **Invoke `/docs-map` before
opening or finishing every PR** and update ALL the sites its row lists; the recurring
full-repo-scan finding is a claim updated in most but not all of its homes. When a sweep fixes a
stale claim, ask whether a cheap test can pin it; add sibling checks to
`tests/test_docs_consistency.py` when a claim class recurs.

## Required CI checks

A NEW validation job in `ci.yml` (one that runs on PRs and should gate merge) **must be added to
`main`'s branch-protection required checks in the same PR**. Validation checks are required by
default; do not add an advisory-only merge gate. Deploy jobs are not required checks.

```bash
gh api -X PATCH repos/<owner>/<repo>/branches/main/protection/required_status_checks \
  -F strict=true \
  -f 'contexts[]=<job-name-1>' \
  -f 'contexts[]=<job-name-2>'
  # include the FULL current list every time (it replaces, not appends)
```

**Current required checks:** `test / lint / typecheck`, `docker build`, `docker smoke`,
`bicep lint`, `secret scan`.

**Local pre-push gate (`.githooks/pre-push`).** Runs every command of CI's `test / lint /
typecheck` job (`uv lock --locked`, `ruff check .`, `ruff format --check .`, `mypy`,
`pytest -m "not integration"`, `pip-audit`; the audit joined on 2026-09-30 after a same-day
urllib3 CVE failed a PR that had passed locally) with `set -euo pipefail` and blocks the
push on the first failure. Enable once per clone (worktrees share it):
`git config core.hooksPath .githooks`. It exists because checks read through an output filter
(`rtk pipe`, `| tail -1`) hid non-zero exits and PRs opened with failing lint. Agents: never
`git push --no-verify` for normal work, and judge every gate by its EXIT CODE, never by filtered
output. `tests/test_prepush_hook.py` fails CI if the hook drifts from `ci.yml`.

## When in doubt

- New engine milestone task: PLAN.md §16 has inputs, outputs and dependencies.
- New tenant task: MULTIUSER_PLAN.md §12, then the milestone's section in
  `docs/MULTIUSER_AS_BUILT.md`.
- Adding a course: [`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md).
- Touching the orchestrator: FakeAdapter + FakeClock + InMemoryStore tests must still cover it
  (collaborators are built inline, see `tests/test_orchestrator.py::_build`); the race-window test
  is the canary.
- Anti-bot etiquette: re-read PLAN.md §12 first. ToS posture is not ours to negotiate around.

## Azure infra and the deploy safety rule

Bicep layout, the `az login` runbook and the agent deploy safety rules are in
[`infra/CLAUDE.md`](./infra/CLAUDE.md); the design is `infra/AZURE_PLAN.md`. Both load when you
work under `infra/`.

- **`azure-iac.yml` deploys with INLINE parameters,** so every value a `.bicepparam` file sets must
  also be parsed and passed by the workflow (a missed one kept dev's watcher at `*/10`).
  `tests/test_azure_iac_killswitch_latch.py::test_every_param_file_value_reaches_every_ci_deploy`
  enforces it.

**Safety rule that always applies (enforced by `.claude/hooks/az-deploy-guard.sh`, regression-tested
in `tests/test_az_deploy_guard.py`):** an agent MUST NOT run `az deployment … create`,
`az containerapp job start`, `az keyvault secret set/delete`, vault-level
`az keyvault purge/delete`, or `az group delete` without explicit user approval. Read-only `az`
(list/show/validate/what-if) and `az bicep build` are fine.
