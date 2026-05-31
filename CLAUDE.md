# CLAUDE.md

Operator/agent notes for working in this repo. The authoritative design doc is
[PLAN.md](./PLAN.md) — read it first if you're new. This file gives the local
shape, common commands, and architectural notes that aren't obvious from
filenames.

Situational detail lives in nested `CLAUDE.md` files that load automatically when
you work in those subtrees: [`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md)
(per-course IDs/quirks + adding a course) and [`infra/CLAUDE.md`](./infra/CLAUDE.md)
(Azure infra + deploy safety rules).

## What this is

Python 3.12+ bot that books tee times at golf courses (ForeUP and TeeItUp
platforms). Primary target is **Mangrove Bay Golf Course** (St. Petersburg, FL —
ForeUP-backed) at 06:00 America/New_York, 7 days in advance. Also supports
**TeeItUp-backed courses** (e.g. Sydney R. Marovitz, Chicago Park District).
v0 is single-user, GitHub Actions-driven. No frontend.

## Status

M1 + M5 + M-feature-1 (watch job) + M-feature-3 (slot ranking) complete. M2 is
PARTIAL — the core orchestrator is done, but M2.T3 (post-mortem reconciliation,
the UNCERTAIN→RECONCILING→BOOKED/LOST path) is not yet implemented. ForeUP adapter
fully implemented; live dry-run confirmed against Mangrove Bay. TeeItUp adapter
fully implemented; live booking + cancel confirmed against Sydney Marovitz
(2026-05-29). M3 (SQLite) and M4 (email notifications) are CUT — `InMemoryStore`
+ `ConsoleNotifier` are the final production wiring, not stubs. Remaining v0 tasks:
M2.T3 (reconciliation) and M6 (first production cron run).

**Azure v1 IaC is implemented.** All Bicep modules are complete (`identity`,
`registry`, `keyvault`, `logs`, `compute`, `budget`). Dev auto-deploys on merge
to main via `.github/workflows/azure-iac.yml` with `dryRun = true` — no real
bookings fire in dev. State is in-process only (`InMemoryStore`); the bot makes
no authenticated Azure SDK calls at runtime.

## Package layout

```
src/teetime/
  core/             # models, adapter Protocol, orchestrator, config, clock
  persistence/      # BookingStore Protocol + InMemoryStore
  notifications/    # Notifier Protocol + ConsoleNotifier
  courses/foreup/   # Shared ForeUP HTTP base + per-course IDs
  courses/teeitup/  # Shared TeeItUp/Kenna HTTP base + per-course IDs
  courses/chronogolf/  # placeholder; not used in v0
config/             # example.toml; secrets via env-var refs only
.github/workflows/  # ci.yml (lint/test) + azure-iac.yml (Bicep deploy)
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
- **Credit-card data is platform-specific.** ForeUP keeps card-on-file; the
  ForeUP path never POSTs PAN/CVV. TeeItUp has no wallet, so the TeeItUp adapter
  DOES POST PAN + CVV + expiry + billing to `tr.gnsvc.com` on every booking
  (sourced from `*_env` vars, never committed). This is a deliberate scope
  expansion past the original "no card data, ever" rule — see PLAN.md §7.
  Consequence: handling raw PAN/CVV brings PCI scope; card fields MUST be dropped
  by `_redact_payload` before any `attempt_log` write (PLAN.md §10.1), and the
  card POST uses `follow_redirects=False`.
- **Double-booking defense is layered.** Live pre-book `list_reservations`
  check, single-attempt-per-slot rule, in-process advisory lock, ACA Job /
  GH Actions concurrency groups. There is no durable cross-run idempotency
  record; the live remote check is the primary cross-run guard. PLAN.md §9
  has the full flow; §9.1 has the explicit state machine that M2.T1
  implements. `list_reservations` is on the `CourseAdapter` Protocol from
  M0 — it is NOT optional.
- **DST handled by `zoneinfo`** (the bot computes T0 in `America/New_York`,
  which resolves the ambiguous/skipped-hour edge cases) + four ACA Job crons
  (two per day, one per DST half) in `infra/bicep/modules/compute.bicep`. Math
  in PLAN.md §6.3. The booking and watch schedules run as ACA Jobs;
  `book.yml`/`watch-tee-time.yml` have been removed. The precise T0 busy-wait is
  wired (M6 PR1): `teetime run --wait` uses the real `cfg.scheduler` (busy-waits to
  06:00:00 ET); `--no-wait` (default; `TEETIME_WAIT` env fallback) keeps immediate
  local-demo timing via `_local_demo_scheduler`. The ACA booking job will pass
  `--wait` in M6 PR3. The wall-clock **"DST-half" gate** (which suppresses the
  wrong-season cron so only one of a day's two crons books) now lives in
  `core/dst_gate.py` (`should_proceed`, M6 PR2) — re-homed from the deleted
  `book.yml` `dst` step. It is a pure function of `(clock, timezone, fire_time)`
  evaluated in `_run` ONLY on the `--wait` path, BEFORE the busy-wait: proceed iff
  the ET wall-clock hour == `fire_time.hour - 1` (i.e. 5 for a 06:00 drop). A
  wrong-season cron exits 0 without booking. `--no-wait` bypasses the gate (matching
  the old `workflow_dispatch` always-proceed). See PLAN.md §6.3.
- **`teetime run --fire-time HH:MM:SS`** is a DEV/TEST-ONLY override of the scheduler
  fire_time, hard-refused unless `--dry-run true`. It makes an on-demand `--wait`
  busy-wait reachable at any wall-clock hour (it cannot shift a real booking). See
  `_with_fire_time_override` and AZURE_PLAN §6.5.
- **Watcher is ENABLED in the v1 configs** (`config/container.toml` +
  `config/local.toml`, `watcher.enabled = true`, M6 PR4). Under `--dry-run true` (dev)
  it does ALL the looking/ranking/logging and suppresses ONLY the final POST
  (`WatchOrchestrator` returns `DRY_RUN` before the lock+POST). `one_booking_policy`
  (cancel+rebook upgrade) stays DISABLED in M6 (a separate, higher-blast-radius change).
  The watch cron stays daily: a Sunday booking is upgradeable all week, so polling every
  day catches a better slot.
- **Target date anchors to `target_weekday` (default Sunday), NOT a rolling `today+7`**
  (M6 PR7, `core/target_date.py`). `_build_request` computes `target = most-recent
  target_weekday(today) + offset`, so the DAILY watch job locks onto the upcoming target
  Sunday and holds it all week (instead of drifting — from a Wednesday `today+7` isn't even
  a Sunday). On the booking weekday the anchor is `today`, so the 6 AM Sunday booker's
  `today+7` is unchanged. A manual non-Sunday `teetime run` now targets the upcoming Sunday
  (not `today+7`) — intended. `--date` still overrides for the watch command.
- **Idempotency key is `(RequestId, resolved_date)`**, NOT just `RequestId`.
  This lets `target_offsets = [7]` produce one stable RequestId within a run
  while still targeting a fresh date each week. The key is held in-process
  only (InMemoryStore); there is no durable record across runs. See PLAN.md §13.1.
- **Player PII redacted before write** to `attempt_log` (SHA-256 prefix).
  See PLAN.md §10.1. The attempt_log lives in InMemoryStore (in-process only,
  not persisted to disk or any external store).
- **`cancel_reservation` is on the `CourseAdapter` Protocol** (breaking — all
  adapters must implement it). Raises `CancelError` on failure. Returns normally
  on 404 (already-cancelled is the desired post-condition). See `core/adapter.py`.
- **`delete_terminal` is on the `BookingStore` Protocol** (all stores must
  implement it; `InMemoryStore` does). Used only by `UpgradeOrchestrator` after
  a successful cancel+rebook to clear the old in-process idempotency record
  before inserting the new one. Must be called under the advisory lock.
  See `persistence/store.py`.
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
  cleared after use). Adapters with no pre-fetch cost (FakeAdapter, TeeItUpAdapter,
  future Chronogolf) implement it as a no-op. This shrinks the cancel-to-book
  no-booking window from ~60 s to ~1-2 s.
- **Each run is independent** — there is no shared state cache between the watch
  job and the main booking job. The live `list_reservations()` call is the source
  of truth across runs. Concurrent-run serialization is handled by ACA Job /
  GH Actions `concurrency:` groups. In-process advisory locks serialise writes
  within a single run.

## Per-course specifics → `src/teetime/courses/CLAUDE.md`

Course-specific IDs, URLs, and quirks (Mangrove Bay / ForeUP, Sydney R. Marovitz /
TeeItUp) and the step-by-step for adding a new course live in
[`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md) — a nested
guide that loads automatically when you work on an adapter.

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
- Adding a new course (ForeUP / TeeItUp / Chronogolf)? See the step-by-step and
  per-course IDs in [`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md).
- Touching the orchestrator? Make sure FakeAdapter + FakeClock + InMemoryStore
  tests still cover your change. Fixtures live in `tests/conftest.py`. The
  race-window test is the canary.
- Modifying anti-bot etiquette? Re-read PLAN.md §12 first. ToS posture is not
  ours to negotiate around.

## v1 Azure infra → `infra/CLAUDE.md`

The Azure hosting work (Bicep layout, `az login` runbook, the **agent deploy
safety rules**, and the open-questions pointer) lives in
[`infra/CLAUDE.md`](./infra/CLAUDE.md), with the authoritative design in
`infra/AZURE_PLAN.md`. Both load automatically when you work under `infra/`.

**Safety rule that always applies (also enforced by `.claude/hooks/az-deploy-guard.sh`):**
an agent MUST NOT run `az deployment … create`, `az containerapp job start`,
`az keyvault secret set/delete`, or `az group delete` without explicit user
approval. Read-only `az` (list/show/validate/what-if) and `az bicep build` are fine.
