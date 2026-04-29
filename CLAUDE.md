# CLAUDE.md

Operator/agent notes for working in this repo. The authoritative design doc is
[PLAN.md](./PLAN.md) — read it first if you're new. This file gives the local
shape, common commands, and architectural notes that aren't obvious from
filenames.

## What this is

Python 3.12+ bot that books a tee time at **Mangrove Bay Golf Course** (St.
Petersburg, FL — ForeUP-backed) at 06:00 America/New_York, 7 days in advance.
v0 is single-user, GitHub Actions-driven. No frontend.

## Status

M1 + M2 + M5 complete. ForeUP adapter is fully implemented and a live dry-run
(`outcome=dry_run`) has been confirmed against Mangrove Bay. M3 (SQLite), M4
(email notifications), and M6 (first production run) are the remaining v0 tasks.

## Package layout

```
src/teetime/
  core/             # models, adapter Protocol, orchestrator, config, clock
  persistence/      # BookingStore Protocol + SqliteStore
  notifications/    # Notifier Protocol + EmailNotifier
  courses/foreup/   # Shared ForeUP HTTP base + per-course IDs
  courses/chronogolf/  # placeholder; not used in v0
config/             # example.toml; secrets via env-var refs only
.github/workflows/  # GH Actions cron (2 entries for DST)
tests/              # pytest; vcrpy cassettes go in tests/cassettes/
```

The orchestrator is the only thing that knows about all four subsystems.
Persistence, notifications, and adapters all see each other via Protocols
in `core/` — never directly. This is the cut line for parallel work.

## Common commands

| Command                                  | Purpose                                  |
|------------------------------------------|------------------------------------------|
| `uv sync`                                | Install deps + dev deps from pyproject.  |
| `uv run pytest`                          | Run the test suite.                      |
| `uv run pytest -m "not integration"`     | Skip live-network tests (default in CI). |
| `uv run mypy`                            | `strict` type-check (must pass to merge).|
| `uv run ruff check .`                    | Lint.                                    |
| `uv run ruff format .`                   | Format.                                  |
| `uv run teetime run --config config/local.toml --dry-run true` | One-shot booking attempt, no final POST. |
| `uv run teetime show-config --config config/local.toml` | Print resolved AppConfig (with secrets redacted). |
| `gh workflow run book-tee-time -f dry_run=true` | Trigger the workflow on demand.   |

## Architectural notes (non-obvious)

- **Stubs raise `NotImplementedError`** with a PLAN.md milestone reference. If
  you implement one, also make its tests pass before merging.
- **Protocols over ABCs.** Most contracts are `Protocol` (`runtime_checkable`).
  Subclassing the Protocol is fine for shared state, but tests should not
  require subclassing — structural typing is the contract.
- **Clock is injectable everywhere.** Anything that touches wall-clock time
  takes a `Clock`, not `datetime.now`. Tests use `FakeClock`. The 6:00 AM race
  is otherwise untestable.
- **No secrets in TOML.** Config files reference env vars by name. Loader
  resolves them; missing env raises a clear error.
- **No credit-card data, ever.** ForeUP keeps card-on-file; we never POST PAN
  or CVV. If a course requires it, that's a fatal error, not a feature.
- **Double-booking defense is layered.** Idempotency check, pre-book remote
  list, single-attempt-per-slot rule, post-mortem reconciliation, advisory
  lock, GH Actions concurrency group. PLAN.md §9 has the full flow; §9.1 has
  the explicit state machine that M2.T1 implements. `list_reservations` is
  on the `CourseAdapter` Protocol from M0 — it is NOT optional.
- **DST handled by `zoneinfo`** + two GH Actions crons + a "DST-half" gate
  in the workflow that ACTUALLY checks ET wall-clock hour (book.yml `dst`
  step). Math in PLAN.md §6.3.
- **Cross-run state via `actions/cache`** (key `teetime-state-v1`). SQLite
  file in `state/teetime.db` survives between runs; cache loss is caught by
  `list_reservations` pre-book check, never produces phantom bookings.
  See PLAN.md §9.2.
- **Idempotency key is `(RequestId, resolved_date)`**, NOT just `RequestId`.
  This lets `target_offsets = [7]` produce one stable RequestId across the
  daily cron while still booking a fresh date each day. See PLAN.md §13.1.
- **Player PII redacted before write** to `attempt_log` (SHA-256 prefix).
  See PLAN.md §10.1. The store is a workflow artifact — assume contents
  are visible to anyone with repo read access.

## Mangrove Bay specifics

- Booking URL: `https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes`
- `course_pk = 19671`, `booking_class_id = 2149` (teesheet/URL ID), `schedule_id = 2149`
- `public_booking_class_id = 12239` — the "Public" booking class from the page's `SCHEDULES` JSON; used in the login POST and is distinct from the teesheet URL ID
- Login uses `api_key=""` (empty); search uses `api_key="no_limits"` — confirmed by browser capture
- 7-day window opens 06:00 America/New_York exactly; minimum 2 players required

## How we write code in this repo: red-green TDD

**Mandatory.** Every behavior change lands as test-first. The exact loop:

1. **Red.** Write the smallest test that captures the desired behavior. Run it
   and confirm it fails for the right reason (missing impl, wrong return, etc.)
   — not for an unrelated import error or fixture typo.
2. **Green.** Write the minimum implementation that makes the test pass. No
   extra fields, no future-proofing, no untested branches.
3. **Refactor.** With the safety net of green tests, clean up names,
   duplication, and structure. Re-run tests; they must stay green.
4. **Commit boundary.** A meaningful unit of red→green→refactor is a fine
   commit. Don't bundle ten unrelated cycles.

Per-milestone rules:

- A stub's `NotImplementedError` is the test's red phase already on disk —
  the next thing you write is the test that exercises the contract, then
  the body. Don't implement the body before the test exists.
- For Protocol implementations (Clock, BookingStore, Notifier, CourseAdapter):
  the test should verify the structural contract (`isinstance(impl, Protocol)`)
  and at least one behavioral path. See `tests/test_adapter_stub.py` for the
  reference pattern.
- For the §9.1 state-machine work (M2.T1, M2.T3): each transition listed in
  the diagram needs its own failing test before the orchestrator branch that
  implements it. The state machine is too subtle to backfill tests onto.
- `pytest -k <name>` for fast inner-loop iteration; full suite before commit.
- If you find a bug in already-merged code, write the failing test that
  reproduces it FIRST, then fix. The test is the regression guard.

Anti-patterns we don't accept:
- Writing implementation, then tests that "describe" what the code does
  (tests written this way encode bugs as features).
- Mocking the type under test. Mock collaborators, never the SUT.
- Skipping red — "obviously this passes" is how silent regressions ship.
- Tests that pass on `pytest` but only because they don't actually call
  the code path. Always verify the test fails before you write the impl.

## When in doubt

- Implementing a new milestone task? Read PLAN.md §16 for inputs/outputs/deps.
- Adding a new course? Drop a sibling next to `mangrove_bay.py` if ForeUP-backed,
  or stand up `chronogolf/base.py` if Chronogolf-backed (Spike S2 first).
- Touching the orchestrator? Make sure FakeAdapter + FakeClock + InMemoryStore
  tests still cover your change. Fixtures live in `tests/conftest.py`. The
  race-window test is the canary.
- Modifying anti-bot etiquette? Re-read PLAN.md §12 first. ToS posture is not
  ours to negotiate around.

## v1 Azure infra

The Azure serverless hosting design lives in `infra/AZURE_PLAN.md`. Read it
before touching anything under `infra/`. The v0 files (`src/`, `tests/`,
`.github/workflows/book.yml`) are v0 territory — do not modify them as part
of Azure infra work.

### Bicep location

```
infra/
  AZURE_PLAN.md              # authoritative Azure design doc
  bicep/
    main.bicep               # entry point (RG-scoped)
    main.bicepparam.dev      # dev parameter values
    main.bicepparam.prod     # prod parameter values
    modules/
      identity.bicep         # optional user-assigned MI stub
      registry.bicep         # ACR Basic
      storage.bicep          # Blob Storage + teetime-state container
      keyvault.bicep         # Key Vault Standard
      logs.bicep             # Log Analytics + App Insights
      compute.bicep          # ACA Environment + 2x ACA Jobs (DST crons)
      budget.bicep           # Cost Management budget (subscription-scoped)
  ci/
    azure-iac.yml            # IaC validation + deploy workflow
```

### Logging in for local Azure CLI work

```bash
az login                                      # browser-based login
az account set --subscription <SUBSCRIPTION_ID>
az account show                               # confirm correct subscription
```

For CI, authentication uses OIDC federated credentials (no client secret).
See AZURE_PLAN.md §8.2 for the one-time federated credential setup steps.

### Agent rules for Azure deployments

**CRITICAL: An agent MUST NOT run `az deployment group create` or
`az deployment sub create` without explicit user approval.** These commands
create or modify live Azure resources.

What agents CAN run autonomously:
- `az bicep build` (lint only; no network calls)
- `az deployment group validate` (validates template; no resource changes)
- `az deployment group what-if` (read-only; shows planned changes)
- `az keyvault secret list` (read-only; lists secret names, not values)
- `az containerapp job list` / `az containerapp job show` (read-only)

What agents MUST NOT run without explicit user instruction:
- `az deployment group create` / `az deployment sub create`
- `az containerapp job start` (triggers live job execution)
- `az keyvault secret set` / `az keyvault secret delete`
- `az group delete`
- Any `az` command that modifies, creates, or deletes Azure resources

### Pointer to open questions

Before first deploy, answer the 10 questions in AZURE_PLAN.md §12. Key
blockers: Azure AD tenant ID, subscription ID, ACR/KV/storage naming
uniqueness, and whether a Dockerfile exists (AZURE_PLAN.md §12 Q6).
