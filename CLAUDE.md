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
.github/workflows/  # GH Actions cron (4 entries: Sat+Sun × 2 DST seasons)
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
  weekend cron while still booking a fresh date each week. See PLAN.md §13.1.
- **Player PII redacted before write** to `attempt_log` (SHA-256 prefix).
  See PLAN.md §10.1. The store is a workflow artifact — assume contents
  are visible to anyone with repo read access.
- **`cancel_reservation` is on the `CourseAdapter` Protocol** (breaking — all
  adapters must implement it). Raises `CancelError` on failure. Returns normally
  on 404 (already-cancelled is the desired post-condition). See `core/adapter.py`.
- **`delete_terminal` is on the `BookingStore` Protocol** (breaking — all stores
  must implement it). Used only by `UpgradeOrchestrator` after a successful
  cancel+rebook to clear the old idempotency record before inserting the new
  one. Must be called under the advisory lock. See `persistence/store.py`.
- **`WatchOrchestrator` and `UpgradeOrchestrator` live in `core/`**. They follow
  the same collaborator-injection pattern as `Orchestrator`. Neither is
  long-running — each is a single-invocation check (one ACA Job execution).
- **`BookingResult.confirmation_code` stores `TTB:<raw_foreup_id>`** (not the
  raw ForeUP id) when booked by this system (Option A, MF-1). `ForeUpAdapter.
  cancel_reservation()` strips the prefix before calling ForeUP. `ExistingReservation.
  confirmation_code` (from `list_reservations`) stores the raw server id — no
  prefix — so `is_managed` returns False for server-sourced reservations, as
  expected. `FakeAdapter.book()` stamps `TTB:FAKE-<slot_id>` in `BookingResult`
  and stores the raw `FAKE-<slot_id>` in `_existing` to mirror this behaviour.
- **`ForeUpAdapter.list_reservations()` reads a login-response cache, NOT a live
  GET.** ForeUP's `GET /reservations` endpoint returns a ~6 MB user-profile
  with `"reservations": false` (a lazy-load flag). Actual reservations come from
  the `POST /login` response body. `authenticate()` caches the list; subsequent
  `list_reservations()` reads from that snapshot. Consequence: reservations made
  AFTER `authenticate()` completes (e.g. a manual booking during the bot's run
  window) are not visible to `list_reservations()` in the same run. For the
  pre-book layer-2 guard this is acceptable (seconds of staleness). The RECONCILING
  path (M2.T3, not yet implemented) must re-authenticate before calling
  `list_reservations()` to get a fresh snapshot. `list_reservations()` raises
  `RuntimeError` if `authenticate()` has never been called — preventing a silent
  empty-list from vacuously passing the pre-book guard in misconfigured deployments.
- **`WatchOrchestrator.check_once` does NOT acquire `request_lock`**. It is
  read-only. If it delegates to `UpgradeOrchestrator.maybe_upgrade`, THAT method
  acquires and releases the lock itself. Never call `maybe_upgrade` while already
  holding the lock — that deadlocks.
- **`WatchOrchestrator` upgrade wiring**: Gate 3 (store already has BOOKED terminal)
  and `_check_course()` (live reservation found, no store record) both delegate to
  `_try_upgrade()` when `one_booking_policy.enabled = true`. `_try_upgrade()` builds
  a fresh `UpgradeOrchestrator` and calls `maybe_upgrade()`. For the no-store-record
  path, `_synthesize_managed_booking()` constructs a TTB:-prefixed `BookingResult`
  from the live `ExistingReservation` so the managed-booking guard in
  `maybe_upgrade()` passes.
- **Cancel-before-book protocol** in `UpgradeOrchestrator`: ForeUP rejects a second
  book POST with HTTP 400 while an existing reservation is live. The orchestrator
  therefore cancels first, then books. This leaves a ~1-2 second no-booking window
  (two HTTP round-trips). If book() fails after cancel, the next watch invocation
  recovers by booking any available slot.
- **`prepare_book()` on `CourseAdapter` Protocol**: called by `UpgradeOrchestrator`
  BEFORE `cancel_reservation()` to pre-fetch expensive prerequisites (CAPTCHA token,
  ~15-60 s). `ForeUpAdapter.prepare_book()` calls the CAPTCHA provider and caches
  the resulting token in `self._captcha_token`; `book()` consumes it (single-use,
  cleared after use). Adapters with no pre-fetch cost (FakeAdapter, future
  Chronogolf) implement it as a no-op. This shrinks the cancel-to-book no-booking
  window from ~60 s to ~1-2 s.
- **Watch job shares `teetime-state-v1` cache key** with the main booking job.
  Single SQLite file = single source of truth. Advisory locks serialise concurrent
  writes. See PLAN.md §20.1 Q1 (resolved).

## Mangrove Bay specifics

- Booking URL: `https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes`
- `course_pk = 19671`, `booking_class_id = 2149` (teesheet/URL ID), `schedule_id = 2149`
- `public_booking_class_id = 12239` — the "Public" booking class from the page's `SCHEDULES` JSON; used in the login POST and is distinct from the teesheet URL ID
- Login uses `api_key=""` (empty); search uses `api_key="no_limits"` — confirmed by browser capture
- 7-day window opens 06:00 America/New_York exactly; minimum 2 players required
- **Party size is 4** (configured in `config/example.toml` + `config/local.toml` as 4 `[[request.players]]` entries). The idempotency layer-2 guard (`list_reservations`) matches on `party_size == len(request.players)` exactly — if you change party size between production runs an existing booking with the old party size will NOT block a new attempt. Cancel any conflicting reservation before deploying a party-size change.
- **Schedule is Saturday + Sunday only.** The cron runs at 6:00 AM ET on weekends; `target_offsets = [7]` books the same weekday 7 days out (Sat→Sat, Sun→Sun). For ad-hoc mid-week bookings use `workflow_dispatch` and adjust `target_offsets` in `config/local.toml` before triggering.
- **Time window is 09:00–10:30 ET** (single morning window). For mid-week or afternoon bookings, add a second `[[request.time_windows]]` entry in `config/local.toml` for that run only.

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

## Documentation standard

Every PR must leave the docs in sync with the code. Before opening a PR,
check each of these and update any that the PR makes stale:

| Doc | Update when… |
|-----|-------------|
| `README.md` | Milestone status changes; new prerequisites, commands, or env vars; architecture diagram changes; roadmap table |
| `CLAUDE.md` | New architectural invariants; changes to common commands; new subsystems, protocols, or agent rules |
| `PLAN.md` | Milestone marked done or scope changes; open questions resolved or added; new spikes |
| `infra/AZURE_PLAN.md` | Azure open questions resolved; new Key Vault secrets; IaC module changes; OIDC/RBAC changes |

A PR that introduces a new CLI flag, env var, or milestone task with no
corresponding doc update is incomplete. Not every PR touches every doc —
the rule is to check and update the ones that are now stale.

## When in doubt

- Implementing a new milestone task? Read PLAN.md §16 for inputs/outputs/deps.
- Adding a new ForeUP course? Three steps:
  1. Drop a sibling file next to `mangrove_bay.py` (e.g. `twin_brooks.py`).
     Set all four IDs (`course_pk`, `booking_class_id`, `schedule_id`,
     `public_booking_class_id`) and override `booking_page_url`.
  2. Import it in `__main__.py` and add one line to `_ADAPTER_REGISTRY`:
     `"foreup.twin_brooks": TwinBrooksAdapter,`
  3. Add a `[[courses]]` entry in your TOML config and add `"foreup:twin_brooks"`
     to `course_preferences` in the desired priority position.
  No other code needs to change. Adding a course to `[[courses]]` without
  adding it to `course_preferences` is safe — it won't change the RequestId
  or be tried by the orchestrator.
- Adding a Chronogolf course? Stand up `chronogolf/base.py` first (Spike S2).
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
