# CLAUDE.md

Operator and agent notes for this repo: current state, commands, the load-bearing invariants
that are not obvious from filenames, and the rules for keeping docs in sync. The engine design
is [PLAN.md](./PLAN.md); read it first if you are new.

Nested `CLAUDE.md` files load automatically when you work in their subtree:
[`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md) (per-course IDs and quirks,
adding a course) and [`infra/CLAUDE.md`](./infra/CLAUDE.md) (Azure infra and deploy safety).

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
  - [Configuration, dates and time](#configuration-dates-and-time)
  - [Security and redaction](#security-and-redaction)
  - [Double booking, idempotency and stores](#double-booking-idempotency-and-stores)
  - [Search and book error handling](#search-and-book-error-handling)
  - [The 06:00 booking race (race path only)](#the-0600-booking-race-race-path-only)
  - [Blind-POST burst (Mangrove Bay)](#blind-post-burst-mangrove-bay)
  - [Watcher and upgrade](#watcher-and-upgrade)
  - [Multi-user (tenant) path](#multi-user-tenant-path)
  - [Email OTP](#email-otp)
- [Per-course specifics](#per-course-specifics)
- [Red-green TDD (mandatory)](#red-green-tdd-mandatory)
- [Documentation standard](#documentation-standard)
  - [Change→docs map](#changedocs-map)
- [Required CI checks](#required-ci-checks)
- [When in doubt](#when-in-doubt)
- [Azure infra and the deploy safety rule](#azure-infra-and-the-deploy-safety-rule)

<!-- /toc -->

## What this is

A Python 3.12+ bot that books golf tee times on the ForeUP and TeeItUp platforms. The primary
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
| Latest infra tag | latest infra tag `infra/v3.4.0` (2026-09-29: mail from hello@spicyteetimebooker.com, Report a bug + Request a course (also filed as anonymized GitHub issues), migrate-start retry; prod on https://spicyteetimebooker.com since `infra/v3.1.0`; the tenant path since `infra/v3.0.0`, MU-18 stage B); history in [docs/RELEASES.md](./docs/RELEASES.md) |
| Mode | `dryRun=false`, `killswitchFired=false`, `enableSchedules=true` |
| Booking jobs | `teetime-job-prod-edt` `50 9 * * *` and `teetime-job-prod-est` `50 10 * * *` (05:50 ET, one per DST half), 1200 s timeout; `tenant-run --event mb0600et --wait` since MU-18 stage B (was `run --wait`) |
| Watch job | `teetime-watch-job-prod` `*/10 * * * *`, 300 s timeout; `tenant-watch` since MU-18 stage B |
| Books | Sat + Sun, one reservation per day, 7 days ahead, nearest the window midpoint |

What is live: multi-day Sat+Sun booking with per-day windows, the 16:00-day-before booking cutoff,
Portal-editable skip-days, within-window upgrade, the race pre-warm bundle (login pre-warm,
multi-token CAPTCHA pool, search-sleep trim), the Mangrove Bay blind-POST burst (3 POSTs staggered
`-500/-250/0` ms across T0, keep best, cancel extras, re-guard then fresh-search fallback), the
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

Plan: [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md) §12 (ratified 2026-09-25). How each milestone was
actually built, deviations included: [docs/MULTIUSER_AS_BUILT.md](./docs/MULTIUSER_AS_BUILT.md).

| Milestone | What | State |
|-----------|------|-------|
| MU-1 | `core/release_policy.py` (E4) | Done |
| MU-2 | `SharedCaptchaPool` (E1); backs ForeUP's private pool in prod unchanged | Done |
| MU-3 | MB grid 07:00–12:00 + blind allowlist (E2, E3) + `tenant/allocation.py` | Done |
| MU-4 | Engine hooks E5 reconcile eligibility, E6 snapshot trust, E7 secret literals | Done |
| MU-5 | Tenant models, `TenantStore`, `InMemoryTenantStore`, conformance suite | Done |
| MU-6 | Materializer | Done |
| MU-7 | `tenant/crypto.py` (AES-GCM passwords) | Done |
| MU-8a / MU-8b | Cosmos document mapping / `CosmosTenantStore` | Done |
| MU-9a0 / 9a / 9b / 9c | Virtual clock + recorder / runner core / exit contract + CLI / `LeasedBookingStore` | Done |
| MU-10a / MU-10b | Tenant watcher decisions / runner + `tenant-watch` | Done |
| MU-11 | Notifications (ACS Email REST) | Done |
| MU-12 / 13 / 14 | Web skeleton / dashboard + rules + dates / connect, refresh, cancel | Done |
| MU-15a / MU-15b | Infra without the DB / Cosmos account | Done |
| MU-16a | Tenant commands on real collaborators + migrate job | Done |
| MU-16b | Adopt the TOML bot's live reservations as owned: operator-only **Adopt existing bookings** on `/accounts` (`tenant/seed.py`, `web/adopt.py`), used at the prod cutover (AZURE_PLAN §10.8) | Done |
| MU-17 | Dev cutover | Done, dev dry-run |
| MU-18 | Prod cutover: stage A `infra/v2.17.0` (web app, ACS, tenant store), stage B `infra/v3.0.0` (jobs on the tenant path), 2026-09-27 | Done |
| MU-19 | Retire the TOML job wiring (after 4 clean weekends) | Open |
| MU-20 | Remove the TOML CLI | Dropped: the operator keeps it (MULTIUSER_PLAN §13 Q6) |
| MU-R1 / R2 / R3 | Ranked options + price / group floor + collapse / ranked form | Done |

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

## Package layout

```
src/teetime/
  core/              models, adapter Protocols, orchestrators, config, clock, redaction,
                     gates (dst, booking-day, cutoff), release_policy, otp
  persistence/       BookingStore Protocol + InMemoryStore (per-run engine memory)
  notifications/     Notifier Protocol + ConsoleNotifier
  courses/foreup/    ForeUP HTTP base, SharedCaptchaPool, Mangrove Bay
  courses/teeitup/   TeeItUp/Kenna HTTP base, Sydney Marovitz
  courses/chronogolf/  placeholder, unused
  tenant/            multi-user: models, TenantStore + in-memory + cosmos/, runner, watcher,
                     materializer, groups, crypto, notify, wiring, migrate
  web/               FastAPI app, OAuth, pages, services, templates/, static/
  dev/               FakeAdapter, BlindFakeAdapter, VirtualClock
config/              example.toml, container.toml (secrets via env-var names only)
infra/               AZURE_PLAN.md, bicep/ (main + modules/ + release_events.json)
docs/                RELEASES.md, MULTIUSER_AS_BUILT.md, plans/ (shipped plans)
.github/workflows/   ci.yml (lint, types, tests, docker, secret scan, bicep lint), azure-iac.yml (deploy)
tests/               pytest; respx for httpx mocking; tests/tenant/, tests/web/
```

The orchestrator is the only thing that knows all four engine subsystems. Persistence,
notifications and adapters see each other only through Protocols in `core/`; that is the cut line
for parallel work. Nothing in `core/`, `courses/` or `persistence/` imports `tenant`.

## Common commands

| Command | Purpose |
|---------|---------|
| `uv sync` | Install deps + dev deps |
| `uv run pytest` | Full test suite |
| `uv run pytest -m "not integration"` | Skip live-network tests (CI default) |
| `uv run mypy` | Strict type-check (must pass to merge) |
| `uv run ruff check .` / `uv run ruff format .` | Lint / format |
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

### Configuration, dates and time

- **DST is handled by `zoneinfo` plus two daily crons.** T0 is computed in `America/New_York`.
  Two booking crons (one per DST half) live in `infra/bicep/modules/compute.bicep`; math in
  PLAN.md §6.3. `teetime run --wait` busy-waits to 06:00:00 ET with the real `cfg.scheduler` and is
  what the ACA booking job passes; `--no-wait` (default; `TEETIME_WAIT` env fallback) keeps
  immediate local-demo timing via `_local_demo_scheduler`. The **DST-half gate**
  (`core/dst_gate.py::should_proceed`) runs in `_run` only on the `--wait` path, before the
  busy-wait: proceed iff the ET hour equals `fire_time.hour - 1` (5 for a 06:00 drop). The
  wrong-season cron exits 0 without booking; `--no-wait` bypasses the gate.
- **`teetime run --fire-time HH:MM:SS` is dev/test only**, hard-refused unless `--dry-run true`.
  It makes a `--wait` busy-wait reachable at any hour and cannot shift a real booking
  (`_with_fire_time_override`, AZURE_PLAN §6.5).
- **Time windows are bound to weekdays; wanted days are derived from them** (PERDAY_WINDOWS_PLAN).
  Each `[[request.time_windows]]` has a `weekday`; several may share a day (one reservation per
  day, best window wins, list order = preference). `RequestConfig.wanted_weekday_indices` comes
  from the windows (the old `target_weekdays`/`target_weekday` keys were removed; untagged config
  errors loudly). The domain `TimeWindow` stays weekday-free: `_build_booking_request` (booker)
  and `_scope_request_to_date` (each watcher target date) pin
  `time_windows=_windows_for_date(...)`. Scoping is per TARGET DATE, not per execution day. The
  RequestId fingerprint encodes the weekday (`<wd>:HH:MM-HH:MM`), so a Sat and a Sun window are
  distinct identities.
- **Target dates.** The booking job books ONE gated date (`today + offset`, gated to a wanted
  weekday by `core/booking_day_gate.py`). The watcher checks
  `core/target_date.next_occurrences_within_horizon` over the wanted days. `watch --date`
  overrides to a single date (and errors if that weekday has no window).
- **The idempotency key is `(RequestId, resolved_date)`**, not `RequestId` alone, so
  `target_offsets = [7]` keeps one RequestId while targeting a new date each week. It lives
  in-process only (`InMemoryStore`); there is no durable cross-run record (PLAN.md §13.1).
- **Hard booking cutoff** (LEADTIME_SKIP_PLAN F1). `request.booking_cutoff`
  (`{days_before, time_of_day}`, default 16:00 ET the day before) FREEZES a date: no new booking
  and no upgrade after it; held bookings are never auto-cancelled. Cutoff + skip-days are decided
  by ONE pure primitive, `core/booking_cutoff.py::frozen_reason(now, target_date, *, timezone,
  cutoff, skip_dates) -> "cutoff" | "skip" | None`, which both the watcher's
  `_should_stop_acting_on_date` (adds a deadline leg) and the booking-day gate
  `should_book_today` (adds a weekday leg) call, so they cannot diverge. `is_past_booking_cutoff`
  is a thin clock-reading wrapper. This is policy, not identity: it does NOT feed the RequestId.
- **Skip-days are FAIL-OPEN and resolved at load** (LEADTIME_SKIP_PLAN F2).
  `request.skip_dates_env` names an env var with a comma/space-separated ISO date list;
  `core/skip_dates.parse_skip_dates` resolves it into `request.skip_dates` in `load()`. Unlike
  credential `*_env` vars, unset/empty/malformed yields an empty set, so a fat-fingered Portal
  edit can never crash the 06:00 booker. In prod it comes from a Key Vault secret editable with
  no redeploy. It does NOT feed the RequestId.

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

### Double booking, idempotency and stores

- **Double-booking defense is layered:** a live pre-book `list_reservations` check, one attempt
  per slot, the in-process advisory lock, and ACA Job concurrency (`parallelism=1`). The live
  remote check is the primary cross-run guard. Flow in PLAN.md §9, state machine in §9.1.
  `list_reservations` is on the `CourseAdapter` Protocol and is NOT optional.
- **`cancel_reservation` is on the `CourseAdapter` Protocol.** It raises `CancelError` on failure
  and returns normally on 404 (already cancelled is the desired end state). For ForeUP it also
  returns normally on the 400 "We can't find that teetime" that the platform uses for a
  missing/expired reservation (observed 2026-07-15).
- **`delete_terminal` is on the `BookingStore` Protocol.** Only `UpgradeOrchestrator` uses it,
  after a successful cancel+rebook, to clear the old record before inserting the new one, and it
  must be called under the advisory lock (`persistence/store.py`).
- **`BookingResult.confirmation_code` stores `TTB:<raw_foreup_id>`** when this system booked it.
  `ForeUpAdapter.cancel_reservation()` strips the prefix. `ExistingReservation.confirmation_code`
  (from `list_reservations`) is the raw id, so `is_managed` is False for server-sourced
  reservations. `FakeAdapter.book()` mirrors this (`TTB:FAKE-<slot_id>` returned, raw id stored).
  **The `book()` id-extraction chain reads `TTID`/`teetime_id` last** (the same two flat-response
  fields `_parse_reservation` reads; keep the order in sync). Mangrove Bay returns a FLAT dict
  with the id only there; before BLIND_POST_PLAN PR0 `confirmation_code` was `None` on every live
  MB booking. That was cosmetic for upgrade/cancel (which take the id from `list_reservations`,
  see `WatchOrchestrator._synthesize_managed_booking`) but is **load-bearing for blind-POST
  cancel-extras**, which cancels surplus reservations by the id `book()` returned.
- **`ForeUpAdapter.search()` needs no login; `book()` does.** `search()` creates the HTTP client on
  first use when `authenticate()` has not run, which the tenant watcher's shared unauthenticated
  search relies on (before the fix every such search raised `RuntimeError`, live in prod and dev on
  2026-09-27, pinned by `test_search_needs_no_login_on_an_adapter_built_without_a_client`).
  `book()` raises `AuthError` without a login and `cancel_reservation()` keeps requiring the client.
- **`ForeUpAdapter.list_reservations()` reads a login-response cache, not a live GET.** ForeUP's
  `GET /reservations` returns a ~6 MB profile with `"reservations": false`; the real list comes in
  the `POST /login` body, which `authenticate()` caches. Reservations made after login are
  invisible to `list_reservations()` in the same run. That is fine for the pre-book guard; any
  path that must see a later booking (watcher reconciliation of an UNCERTAIN book, the blind-POST
  `_reguard_before_fallback`) must call `refresh_reservations` first. `list_reservations()`
  raises `RuntimeError` if `authenticate()` never ran, so a misconfigured deployment cannot pass
  the guard with a silent empty list.
  - **Snapshot trust (E6, MULTIUSER_PLAN §7.5; read only by tenant code).** Three quiet
    `authenticate()` degradations leave the cache empty or stale: (a) a soft login failure
    (400/401/rejected body), (b) a 200 whose body is not JSON, (c) a JSON success whose
    `reservations` is missing or not a list. `ForeUpAdapter.snapshot_trusted` (the opt-in
    `ReservationSnapshotHealth` capability) is True only when the latest login parsed a real list
    (an EMPTY list is trusted). It resets at the start of every real login attempt, before the
    warm-up GET; an idempotent short-circuited `authenticate()` keeps the last value. The cache is
    unchanged; the flag only says whether an ABSENCE may be believed. Tenant vanish inference and
    adoption must ignore an untrusted snapshot, or `[]` reads as an external cancel and re-books.
- **Forcing a fresh snapshot = `refresh_reservations`, never a second `authenticate()`.**
  `authenticate()` is idempotent (`if self._logged_in: return`, RACE_PREWARM_PLAN §3.1), so calling
  it again returns the stale cache. The `ReservationCacheRefreshable` Protocol exposes
  `refresh_reservations(creds)`; ForeUP implements it as "reset `_logged_in`, then
  `authenticate()`". **Load-bearing in `Orchestrator._reguard_before_fallback`**: it must see a
  landed-but-uncertain blind reservation, so it calls `refresh_reservations` when available (else
  `authenticate()` for live-GET stores). A plain re-auth there would let the fallback double-book.
- **Two stores, two jobs: `BookingStore` vs `TenantStore`** (MULTIUSER_PLAN §3.7, MU-5).
  `persistence.BookingStore` (`InMemoryStore`) is the engine's per-run memory (terminals, attempt
  log, in-process `request_lock`), unchanged and not durable. `tenant.store.TenantStore` is the
  multi-user sibling: durable intent + ownership (users, course accounts, standing rules, dated
  request rows, the ownership ledger, snapshots, row leases). Neither imports the other's
  implementation. `tenant.in_memory_store.InMemoryTenantStore` is the reference implementation
  and reproduces the Cosmos semantics: deterministic ids (`rule_row_id`, `derive_account_id`), the
  `slot|<date>` pointer that makes "one ACTIVE row per (account, date)" a uniqueness fact, the
  `ruleday|<weekday>` pointer (one active rule per weekday), IfMatch-style rule versions, and
  all-or-nothing batches. The §3.4 state machine is the pure
  `tenant.models.check_transition`/`check_create` (actor, frozen and reason guards); the store adds
  lease guards (web/materializer writes need the row unleased, M4; `record_outcomes` status
  changes need an UNEXPIRED lease held by the writer) and the `RowFingerprint` check on lease
  acquire (M5). The bridge is `tenant.store.LeasedBookingStore` (MU-9c): a `BookingStore` whose
  `request_lock` IS the fingerprinted row lease (refusal → `ConcurrentRunError`, the engine's
  existing defer); the booker never uses it (it holds its lease from `claim_rows`). **The
  contract is `tests/tenant/conformance.py`, not a docstring:** every `TenantStore` (including
  `CosmosTenantStore`, run in CI over a fake container) must pass `TenantStoreConformance`
  unchanged; subclass it with a `harness` fixture as `tests/tenant/test_in_memory_store.py` does.
- **Operator-only reads and the uninvited-sign-in record (2026-09-29).** `list_users` and
  `list_rejected_signins` are the only unscoped listings the web makes, both behind the operator
  gate on `/admin/users`. A `rejected_signin` (a `global` doc, one per `(provider, subject)`,
  partition `rejected_signin:all`) KEEPS the provider-verified emails, unlike the audit log, which
  redacts them; so it has a per-item TTL of 90 days re-set on every attempt (it relies on the
  `global` container's `defaultTtl: -1`, pinned by `tests/test_cosmos_bicep.py`), and both stores
  also apply the cut-off when listing. It is written on the UNAUTHENTICATED 403 path, so the write
  is best-effort, bounded (`REJECTED_SIGNIN_WRITE_TIMEOUT_S`), and capped (5 emails, 200-char
  name); a failure never changes the 403.

### Search and book error handling

- **A 0-match search on a non-empty teesheet logs WHY.** `search()` tallies each rejected slot by
  `_rejection_reason` (`out-of-window` / `wrong-holes` / `insufficient-spots` / `over-price`,
  first match wins, so counts partition the rejects), and `_log_zero_match_diagnostics` emits one
  PII-free line with the tally, the span of tee times on offer, and the requested
  window/holes/party. Motivation: the 2026-08-01 miss (an 8 AM shotgun tournament on the target
  date) needed hand-calls to the live API to diagnose. **Level is split on purpose:** INFO when
  every rejection is `out-of-window` (a sold-out window is routine; at WARNING it would bury the
  `dropped N/M unparseable slot(s)` schema-break canary), WARNING when any other leg fires (for
  ForeUP those should not happen; `insufficient-spots` is unreachable at MB, so it firing means
  the platform changed). **Gated on a non-empty parsed list:** an unpublished date legitimately
  returns `[]` on every watcher cycle.
- **A book-POST 4xx means try the next slot, and slot exhaustion is graceful.**
  `ForeUpAdapter.book()` maps `409` and `400` to `SlotGoneError` (ForeUP created NO reservation;
  prod 2026-06-07 was a 400 after the slot was taken between search and book), so `_run_course`
  falls through to the next-ranked slot. When EVERY candidate is gone, `_run_course` raises the
  internal `_CourseSkippedError`, `run()` moves to the next course, and if nothing books it
  records a `NO_INVENTORY` terminal and notifies instead of crashing. `TeeItUpAdapter.book()` has
  parity via `_raise_for_booking_step` (a non-409 4xx at cart-item, lock, create-order or
  order-teetime → `SlotGoneError`). In both adapters the §9 UNCERTAIN case (timeout/5xx) still
  propagates. `ForeUpAdapter.book()` logs the full status + body on any non-2xx before raising. A
  captcha-challenge 400 is classified as `CaptchaError` first (`_guard_captcha` runs before the
  400 → SlotGone mapping). Caveat: each fallback candidate re-solves a CAPTCHA (~75 s,
  single-use token) unless a pooled token is left, so at a competitive drop fallbacks are
  best-effort.
- **A `RateLimitError` (429) in a course's flow skips the course; it does not crash the booking
  job.** `run()`'s per-course loop catches it (from the search GET, ForeUP `book()`, the
  blind-POST fresh fallback search, or a TeeItUp pre-payment step; all propagate through
  `_run_course`), logs `retry_after_s`, and continues to the next course. A 429 is rejected before
  processing, so no reservation exists and skipping is safe. ForeUP `book()` maps 429 →
  `RateLimitError` for parity with `search()`/cancel (409/400 still map to `SlotGoneError` first),
  so a throttled POST cannot surface as a raw `HTTPStatusError` in `_book_from_candidates`. If
  nothing books, `run()` records `NO_INVENTORY` + notifies and the command exits non-zero via a
  `ClickException`. **`CaptchaError`/`AuthError` are deliberately NOT caught:** a broken CAPTCHA or
  credential pipeline must not hide behind a clean `NO_INVENTORY`.
- **Transient-failure retry is for IDEMPOTENT ForeUP calls only.**
  `ForeUpAdapter._send_with_retry` retries `httpx.TransportError` (timeouts, blips; a lone
  `ReadTimeout` once cost a whole watch cycle) around the warm-up GET, login POST, search GET and
  cancel DELETE. It does not retry HTTP status errors, and **`book()`'s POST is never wrapped**: a
  timed-out book is the UNCERTAIN case the watcher reconciles, not a safe re-fire. Tuned by
  `max_retries` (default 2) and `retry_backoff_s` (0.5 s, linear; tests pass 0). The watch job's
  `replicaTimeout` is 300 s to leave room for retries (AZURE_PLAN §5.4). **The web's connect /
  re-verify login probe switches it OFF** (`set_transport_retries(0)` in `_probe_login`): that
  probe is ONE attempt (§8.4), and the rate limits count exactly one.
- **Retries everywhere else follow one rule: transient errors only, idempotent calls only,
  bounded, on the injected clock** (retry audit 2026-09-27).
  - **What is already retried below us, so we do not repeat it inside one call:** the
    azure-cosmos SDK retries 429 (9x / 30 s, honouring `x-ms-retry-after-ms`), 503 and
    never-established connections for every op, and 408 / 5xx / lost responses for READS only
    (`retry_write` stays 0: a write whose response was lost may have landed). azure-core retries
    the managed-identity token fetch. `AcsEmailClient` retries 429 / 5xx / transport errors with
    one `repeatability-request-id` per send.
  - **`tenant/retry.py`** (`retry_transient`, `is_transient_store_error`) is the call-site layer
    for what the SDK gave up on: HTTP 408/429/449/5xx, `ServiceRequestError` /
    `ServiceResponseError`, the SDK client timeout, and an `ExceptionGroup` only if EVERY leaf is
    transient (`record_outcomes` wraps each failure in a group, so a blip used to read as a
    refusal and the booker's WRITE #2 retry never fired against Cosmos). The runner's
    `_store_call` budget `TimeoutError` is deliberately NOT transient: the bound stays the bound.
  - **Booker:** READ #1 and the claim (idempotent per owner) replay a transient error only while
    the replay's sleep ends before the race window (`_pre_t0_store_call`; none at all when the run
    started inside it). WRITE #2 replays an all-transient group within its 60 s window; that write
    and the watcher's are IfMatch'd on the row's etag, so a replay after an ambiguous success is
    REFUSED (reported, needs_reconcile), never double-applied.
  - **Watcher:** the idempotent reads + the history-based materializer tick (`_read`) and the
    outcome write replay transient errors; a group's shared search replays ONCE on a transport
    error / 408 / 5xx (a 429 still aborts the run). The soft-auth counter, leases and
    `finalize_lost` are NOT retried (not idempotent).
  - **2captcha:** the submit replays a transport error / 429 / 5xx / `ERROR_NO_SLOT_AVAILABLE`
    twice (a duplicate queued task only costs a solve); a failed result poll CONSUMES that poll
    and only 3 consecutive failures give up, so the solve budget (`max_polls x interval`) the
    120 s prefetch lead is sized for never grows. Errors stay sanitized (the poll URL has the key).
  - **CI deploy (`azure-iac.yml`, 2026-09-29):** the "Run tenant migrations" step retries
    STARTING the migrate job (3 attempts, 15 s apart) and treats a failed status poll as one used
    poll, after a dev deploy failed on a lone "Connection reset by peer". Before re-starting
    it follows an already-Running execution (a start that landed despite the error), so
    runs do not overlap. The migrations are an ordered idempotent list anyway, and a real
    Failed/Stopped/Degraded execution still fails the deploy. Pinned by `tests/test_azure_iac_migrate_step.py`.
  - **Never retried:** ForeUP `book()` (UNCERTAIN, §9), the login probe, any non-idempotent write
    without an IfMatch/etag or idempotency key, and `tenant-migrate`'s migration steps (the job is
    the retry unit; only its `initialize()` read is replayed). The web's own store calls rely on
    the SDK (the user can resubmit; its writes are IfMatch'd).

### The 06:00 booking race (race path only)

The race path is `Orchestrator(prefetch_book=True)`, set only by the `--wait` ACA booking job
(`__main__._run` passes `prefetch_book=wait`). The watcher and local demo never pre-warm or
pre-fetch.

- **`prepare_book(slot, request, *, count=1)` is on the `CourseAdapter` Protocol** and pre-fetches
  expensive prerequisites (CAPTCHA tokens, ~15–60 s each). `slot` is `TeeTimeSlot | None` (the
  CAPTCHA is page-level). Adapters with no pre-fetch cost (Fake, TeeItUp) implement it as a no-op
  that accepts `count`.
  - `ForeUpAdapter.prepare_book()` solves `count` tokens CONCURRENTLY (one
    `gather(return_exceptions=True)`) into a FIFO pool; `book()` pops the OLDEST (single use), so
    a late fallback keeps the freshest token, and inline-solves when the pool is dry
    (RACE_PREWARM_PLAN Change C).
  - **NI10 raise contract:** `count == 1` + total failure RE-RAISES (`TimeoutError` →
    `CaptchaError`) so the upgrade caller aborts; `count > 1` never raises (the pool ends with
    however many succeeded).
  - **MF1 stale-token recovery:** a POOLED token rejected as a captcha challenge triggers exactly
    ONE inline re-solve + re-POST of the same slot (`_is_captcha_challenge` is the non-raising
    sibling of `_guard_captcha`); a second challenge → `CaptchaError`. An inline-solved token gets
    no retry.
  - **Inline solves are semaphore-bounded** (`SharedCaptchaPool.solve_inline`; the private pool
    uses the adapter's `max_concurrent_captcha_solves`, default 6), so a blind burst cannot start
    an N-way herd of ~75 s solves at T0. Single-book paths never hit the bound; the pre-T0
    prefetch calls the provider directly and is unbounded by it.
  - **The pool is a `SharedCaptchaPool`** (`courses/foreup/token_pool.py`, MULTIUSER_PLAN §5,
    MU-2). DEFAULT (no `captcha_pool=`, every TOML caller): a PRIVATE uncoordinated pool whose one
    lease is `_captcha_tokens`, so behaviour is unchanged (`tests/test_captcha_pool.py` is the
    unmodified gate). INJECTED (`captcha_pool=` + `captcha_lease_key=`, tenant runner): adapters
    of one course share it; with demand `register`ed, the first `prepare_book` starts ONE fill
    (≤ `max_concurrent_solves` in flight, `count` ignored, returns on wave 1 or at T0−10 s, never
    raises on solve failures, always returns even if the fill is cancelled); leases are granted
    round-robin in draft order; late arrivals and `release`d leases go to a shared reserve;
    `book()` pops own lease → reserve → inline (both pooled for MF1); `captcha_pool_size()`
    counts the lease only; the inline bound and provider are the pool's, and a pool for another
    `course_id` is refused. Misconfiguration raises `RuntimeError` from `prepare_book` (logged;
    `book()` then inline-solves): a coordinated pool never `arm(t0=…)`ed, or a key never
    `register`ed (over-cap accounts must register with k=0).
  - Two callers: `UpgradeOrchestrator` (chosen slot, `count=1`, BEFORE `cancel_reservation()`,
    shrinking the no-booking window from ~60 s to ~1–2 s) and the race-path `Orchestrator`
    (`slot=None` during the pre-T0 busy-wait).
- **The race pre-fetches the CAPTCHA before T0.** Motivation: on 2026-06-07 the booker fired at
  T0 but solved the CAPTCHA (~78 s) afterwards, POSTed ~100 s late, and lost the slot. The
  busy-wait is TWO-PHASE: wait to `T0 − scheduler.captcha_prefetch_lead_s` (default 120 s),
  `_prefetch_captcha()` (first-preference adapter, best-effort, failures logged and swallowed),
  then wait to exactly T0. Lead 120 s = 24 polls × 5 s, so the solve usually finishes before T0
  and token age at T0 stays within the ~120 s reCAPTCHA window. `book()` and `prepare_book()`
  turn a solve `TimeoutError` into `CaptchaError`: swallowed on the prefetch path (inline solve
  follows), a clean non-zero exit on the inline `book()` or upgrade paths. If the run starts past
  `T0 − lead` (the DST gate admits all of hour 5), it logs `prefetch lead not fully honored` and
  pre-fetches immediately.
- **CAPTCHA prefetch count scales with the blind burst.** `_captcha_prefetch_count_for` returns
  `min(blind_post_max_count, len(synthesize_blind_slots(...))) +
  scheduler.blind_post_fallback_token_reserve` for a blind-capable primary; the reserve (default
  2) stays pooled so the 0-booked fresh-search fallback books without a ~75 s inline solve
  (RESEARCH_FALLBACK_PLAN §2 Q3). A 0-grid blind case adds no reserve and, like any non-capable
  primary, uses the fixed `scheduler.captcha_prefetch_count` (default 3).
- **The race pre-warms the ForeUP login before T0** (RACE_PREWARM_PLAN PR1). `_prewarm_primary()`
  runs `_prewarm_login()` concurrently with the CAPTCHA solve: `authenticate()` + the layer-2
  `list_reservations` already-booked guard, so only `search` + `book` remain after T0. Both legs
  are best-effort. **The post-T0 re-auth skip is ORCHESTRATOR-owned** (`_prewarmed_course_ids`,
  passed into `_run_course`), independent of any adapter idempotency guard. **The skip is recorded
  ONLY on a session-established login** (§3.1 SF#1): ForeUP soft-fails a 400/401/rejected login
  (returns without raising, `_logged_in` stays False), so `_prewarm_login` adds the course only if
  `_login_established(adapter)` (reads `is_authenticated` for an `AuthStateReportable` adapter).
  Otherwise a transient pre-T0 401 would skip the T0 re-auth and `book()` would raise `AuthError`.
  Only the PRIMARY adapter is pre-warmed. If the pre-T0 guard finds we are already booked, the run
  **short-circuits before T0**: logs `race: short-circuited pre-T0 …` (the SF6 verification
  surface), records `ALREADY_BOOKED`, notifies, returns without searching.
- **The race drops the leading search courtesy sleep** (RACE_PREWARM_PLAN PR3).
  `CourseAdapter.search()` takes keyword-only `skip_initial_spacing: bool = False`; the booking
  `Orchestrator` passes `skip_initial_spacing=self._prefetch_book` so only the race path skips the
  250 ms `_MIN_BETWEEN_S` sleep before the FIRST per-date GET. Later per-date GETs are always
  spaced. **The watcher never passes the flag:** that leading sleep is its only inter-date spacing
  (§5.1). `cancel_reservation`'s courtesy sleep is untouched.

### Blind-POST burst (Mangrove Bay)

- **Blind-POST is an ADAPTER CAPABILITY, never a config flag.** Every adapter exposes
  `capabilities: AdapterCapabilities` (frozen dataclass, `core/adapter.py`); the orchestrator gate
  is `adapter.capabilities.blind_post`. `BlindPostCapable` (`captcha_pool_size()`,
  `synthesize_blind_slots(request, target_date, *, max_count)`) is only a typing cast target used
  once the flag says the methods exist; it is NOT `isinstance`-checked. (It used to be, and every
  ForeUP adapter satisfied it because the base shipped the methods; a hidden boolean was the real
  guard.) The ForeUP base sets `blind_post=False` and its `synthesize_blind_slots` raises
  `NotImplementedError`; Mangrove Bay sets `blind_post=True`. TeeItUp and the default FakeAdapter
  are `False`. The other opt-in capabilities (`ReservationCacheRefreshable`,
  `AuthStateReportable`, `ReservationSnapshotHealth`) stay honest `runtime_checkable` presence
  checks, because for them having the method IS the capability.
- **The blind path fires at T0, race path only** (BLIND_POST_PLAN PR3). After the layer-2 guard
  and before the sequential search, `_should_blind_post` requires ALL of: `not request.dry_run`,
  `self._prefetch_book`, `scheduler.blind_post_max_count > 0`, `_is_blind_capable(adapter)`, and
  the course being the PRIMARY. Otherwise the normal search-book loop runs. `_blind_post_course`
  fires the top `N = min(len(blind_slots), captcha_pool_size())` ranked in-window slots,
  staggered across T0 (`create_task` + `gather(return_exceptions=True)`). There is **no
  concurrent hedge search** (RESEARCH_FALLBACK_PLAN §2 Q1).
  - **≥1 booked:** `_keep_best` re-ranks the booked slots with the same `rank_slots_for_request`
    the search path uses and keeps the winner; `_cancel_extras` cancels the rest by their `book()`
    `confirmation_code`. A `None` code or ANY cancel failure (`CancelError`, 429, `CaptchaError`,
    a transport blip) is `log.critical`, never a crash, and never loses the kept booking: the
    catch is deliberately `Exception`-broad. No search runs.
  - **0 booked:** `_reguard_before_fallback` force-refreshes the snapshot, then
    `list_reservations`; a match short-circuits to `ALREADY_BOOKED` (no book, no search).
    Otherwise it fires ONE fresh `_poll_for_slots` search strictly AFTER the re-guard re-auth
    (freshest snapshot, no shared-client cookie race; §2 Q1/Q2) and falls through to
    `_book_from_candidates`, raising `_CourseSkippedError` if that finds nothing.
  - A blind `SlotGoneError` is dropped; any other exception is logged and dropped (the re-guard
    covers the UNCERTAIN case). A `BaseException` captured in `gather`'s results (e.g. a child's
    `CancelledError`) is held: a booked sibling is secured first and it is re-raised only if
    nothing booked (#e1). This is defensive depth, not a shutdown handler: `KeyboardInterrupt` /
    `SystemExit` escape the `await`, SIGTERM kills the process, and the parent's own cancellation
    bypasses the results.
- **The burst is STAGGERED across the release boundary** (STAGGER_PLAN).
  `scheduler.blind_post_stagger_ms` (default `(-500, -250, 0)`) gives each POST its own offset
  from T0, paired positionally with the RANKED slots; `_fire_blind_post` sleeps to `T0 + offset`
  (a non-positive delay fires immediately, so a late cron never waits). Why: every drop in the log
  window came back 3/3 or 0/3, which a real slot race cannot produce; a simultaneous burst
  point-samples ForeUP's release flip, and a pre-open POST gets the same `400 "Time not
  available."` as a claimed slot (the server `Date` header, logged since `infra/v2.11.0`, has only
  1 s resolution). Staggering orders outcomes by offset (clean cutoff = pre-open rejection,
  unordered = real race) and guarantees one POST is SENT no earlier than T0 (tail offset `0`).
  - **`stagger[0] == -early_arrival_ms`**, so the rank-0 slot keeps its pre-stagger instant and
    drops we already win are unchanged; **nothing is ever scheduled earlier than `stagger[0]`**
    (operator directive 2026-08-15). Both pinned by `tests/test_container_config_parity.py`.
  - **The burst RE-RANKS with `rank_slots_for_request` before pairing offsets.** Offsets ascend
    with position, so the best slot must POST first or the 1-per-day rule could reject it in
    favour of a worse sibling. A `field_validator` rejects a descending offset list.
  - Offsets earlier than `-early_arrival_ms` are clamped and logged, not rejected. `()` means
    legacy simultaneous firing; `_local_demo_scheduler` uses it.
  - The per-POST INFO line `blind-POST sent %s (planned %+dms) slot %s → %s` reports the MEASURED
    send offset (a late run fires everything at once, and logging the plan would show a ladder
    that never happened). With `_blind_outcome_label` it is the whole point of the feature:
    **don't drop it when touching the burst loop.**
- **A blind-POST rejection is tagged with WHY (`SlotGoneError.reason`).** ForeUP returns HTTP 400
  for two rejections with opposite meaning and no machine-readable discriminator, so
  `ForeUpAdapter._classify_book_rejection` tags by the `msg` prose:
  - `unavailable` (`"Time not available."`): claimed first, OR our POST beat the release flip.
    The only reason that bears on the pre-open-vs-race question.
  - `daily_limit` (`"...1 online reservation per day."`): alongside a booked sibling, ForeUP is
    bouncing the surplus of a burst WE WON (no race information). With NOTHING booked it means a
    reservation for that date already existed; `_rejection_summary` then says "we already hold a
    reservation" instead of "TOTAL wipeout, falling back", because the re-guard usually
    short-circuits to `ALREADY_BOOKED` ("usually": it matches date AND party size, so a manual
    booking with another party size still falls through to the harmless fallback search).
  - `conflict` (409) and `unknown` (fail-soft default, so other adapters and unobserved wordings
    are never misfiled).
  It is **diagnostic only**: every reason routes identically (`SlotGoneError` → next slot). It
  surfaces as `gone[<reason>]` and `blind-POST N of M slot(s) rejected (<reason>=<count>, …)`.
  The markers (`_BOOK_DAILY_LIMIT_MARKERS` / `_BOOK_UNAVAILABLE_MARKERS`) match only wordings
  observed live, on the stable prose tail ("make" and "have 1 online reservation per day" both
  seen). Evidential caution: a 1-booked / 2-`daily_limit` shape is NOT evidence of a stagger
  effect; a simultaneous pre-stagger burst produced it on 2026-07-11.

### Watcher and upgrade

- **The watcher is enabled in the configs** (`watcher.enabled = true`). Under `--dry-run true` it
  does all the looking, ranking and logging and suppresses only the final POST
  (`WatchOrchestrator` returns `DRY_RUN` before the lock + POST). `one_booking_policy` (cancel +
  rebook upgrade) is enabled: a booked day is upgraded when a higher-priority tier opens, or the
  SAME tier strictly closer to that day's window midpoint (ties never upgrade). Safe because the
  watch request is scoped per target date. Prod watch cron: every 10 min, year-round.
- **The watcher polls on every run; there is no time-of-day gate** (the old
  `polling_start_hour`/`polling_end_hour` fields are removed; they blinded us at the 06:00 drop).
  The only skip is `_is_past_watch_deadline`. Rate limiting is the cron cadence plus the
  `poll_interval_s >= 300` floor. An early-morning run that finds the just-dropped window open
  BOOKS it (a recovery path), safe per date via the in-lock `get_terminal` re-check.
- **The watcher checks multiple dates per run:** the next occurrence of each wanted weekday
  (`_watch` loops `check_once`, no `break`). `_check_course` scopes the search to each
  `target_date` (`dc_replace`) AND filters ranked candidates to it, so a Saturday check can never
  book a Sunday slot; the per-date `(RequestId, date)` key keeps the days independent.
- **A 429 ABORTS the whole watch run.** `check_once` catches `RateLimitError` before the generic
  `except Exception`, logs `retry_after_s` and re-raises, so it neither tries the next course nor
  polls further dates. `_watch` catches it at the date loop and **exits 0** (the cron is the
  backoff; PLAN §12). Non-zero watch exit is reserved for `CaptchaError`/`AuthError`. The generic
  transient handler, by contrast, continues to the next course.
- **`WatchOrchestrator.check_once` does NOT take `request_lock`.** It is read-only; when it
  delegates to `UpgradeOrchestrator.maybe_upgrade`, that method takes and releases the lock.
  Never call `maybe_upgrade` while holding the lock: it deadlocks.
- **Upgrade wiring.** Gate 3 (store already has a BOOKED terminal) and `_check_course()` (live
  reservation, no store record) both call `_try_upgrade()` when `one_booking_policy.enabled`,
  which builds a fresh `UpgradeOrchestrator` and calls `maybe_upgrade()`. For the no-record path,
  `_synthesize_managed_booking()` builds a `TTB:`-prefixed `BookingResult` from the live
  reservation so the managed-booking guard passes.
- **Cancel-before-book** in `UpgradeOrchestrator`: ForeUP rejects a second book POST (400) while a
  reservation is live, so it cancels first, leaving a ~1–2 s no-booking window. If `book()` fails
  after the cancel, the next watch run books any available slot.
- **The watcher reconciles >1 live reservation per date: a CRASH-NET BACKSTOP** (BLIND_POST_PLAN
  PR4). When `_check_course` finds more than one reservation matching `(target_date, party_size)`
  and the policy is enabled, `_reconcile_duplicate_reservations` keeps the best-ranked (the same
  midpoint order; `_rank_reservations` appends out-of-window ones by `tee_time` so the order is
  total) and cancels the rest under `request_lock`, BEFORE the upgrade.
  - It also runs on the Gate-3 short-circuit (`_reconcile_booked_course`), or a duplicate left by
    a failed in-run `_cancel_extras` (which still records BOOKED) would persist forever. An
    `ALREADY_BOOKED` terminal does not short-circuit, so it gets the live reconcile and a
    recovery book. The Gate-3 pre-check swallows a transient blip (skips the cycle), and the whole
    Gate-3 policy block DEFERS on `ConcurrentRunError`.
  - Best-effort: `ConcurrentRunError` defers (returns `matching` unchanged); any non-contract
    cancel failure is logged CRITICAL and retried next run. The per-extra catch re-raises the
    watch-contract errors (`RateLimitError`, `CaptchaError`, `AuthError`). The in-run
    `_cancel_extras` is the PRIMARY mechanism; this recovers crashes on a fresh run.
  - **Documented residual (single-user, accepted):** a deliberate MANUAL second booking on the
    same date + party size would be cancelled too; server-sourced reservations are all
    `is_managed=False`, so they are indistinguishable. N=1 or a disabled policy leaves
    reservations alone.
  - **E5 eligibility hook** (MULTIUSER_PLAN §2.3/§7.6): keyword-only
    `reconcile_eligible: Callable[[ExistingReservation], bool] | None = None`, honoured on both
    reconcile paths. `None` (all the TOML path passes) = unchanged. When set, only ELIGIBLE
    reservations are cancel candidates: the best eligible one is kept, other eligible ones
    cancelled, an ineligible one is never kept-in-place-of nor cancelled; with ≤1 eligible nothing
    is cancelled and the lock is not taken (an owned + a manual booking both stay held, §7.6).
    Survivors are returned eligible-first, so `matching[0]` is owned (except on a defer).
    **E5 does NOT guard the upgrade:** with zero eligible, or a single manual match, `matching[0]`
    is MANUAL and `_try_upgrade` can cancel it (pinned as TOML behaviour by
    `test_unadopted_manual_match_reaches_try_upgrade_unguarded`). The tenant watcher closes this
    with its own ownership gate (next section).

### Multi-user (tenant) path

Details per milestone: [docs/MULTIUSER_AS_BUILT.md](./docs/MULTIUSER_AS_BUILT.md).

- **The tenant path runs the UNMODIFIED `Orchestrator`/`WatchOrchestrator` per account.** Engine
  changes are the hooks E1–E7 only, each defaulting to today's behaviour (MULTIUSER_PLAN §2.3).
  Engine code never imports `tenant`.
- **The tenant recorder is one CONCRETE class per capability set, never a `__getattr__` proxy**
  (`tenant/recording.py`, §4.6 SF1). On Python ≥ 3.12 a `runtime_checkable` `isinstance` uses
  `inspect.getattr_static`, which ignores `__getattr__`; a forwarding proxy would fail
  `isinstance(proxy, ReservationCacheRefreshable)`, the re-guard would read the stale snapshot,
  and could double-book (same for `AuthStateReportable` and `ReservationSnapshotHealth`).
  `make_recording_adapter(inner, clock=…)` composes one memoised class per inner capability set,
  with an explicit mixin per opt-in Protocol, plus `BlindCapableRecordingAdapter` iff
  `inner.capabilities.blind_post` (a cast is not a check, so a missing
  `synthesize_blind_slots` would fail only at T0). That variant passes the MU-3 allowlist through
  and REFUSES at wrap time (~05:51) a blind-capable inner lacking it. Pinned by
  `test_recording_adapter_isinstance_mirrors_inner_for_each_capability`, which discovers every
  `runtime_checkable` Protocol in `core/adapter.py`. The same rule applies to the watcher's
  `SearchSnapshotAdapter` family (one class per capability set, 16).
  - **What it records** (in memory, zero I/O, instants from the SAME `Clock` the orchestrator
    uses): every BOOKED `book()` (raw id, slot, send instant); every `book()` raising anything but
    `SlotGoneError` (UNCERTAIN) by exception CLASS NAME only, flagging the `CaptchaError` family
    (the only place a challenge swallowed by the burst survives); every `SlotGoneError` as a
    `RecordedRejection` (slot, `reason`, send instant; never UNCERTAIN, never owned, read only by
    the operator summary's attempt list); every `cancel_reservation` outcome; call counts.
    Exceptions are re-raised as the same object.
  - `RecordingLog` derives ownership (§4.6): `owned_raw_ids()` = booked and not later cancelled
    OK (the kept best AND `held_extras()`, surplus whose cancel failed); `cancelled_extras()`;
    `needs_reconcile()` = any UNCERTAIN book or a BOOKED with no confirmation code. A guard's
    `ALREADY_BOOKED` recorded no book, so it is unowned unless the re-guard found a POST the
    recorder logged UNCERTAIN (then the watcher adopts by EXACT tee time).
- **Ownership gates every cancel and upgrade.** A reservation is OWNED iff its raw id is ledgered
  `held`/`held_extra`. The tenant watcher applies `upgrade_allowed` before the engine can reach
  `_try_upgrade`: it pre-seeds a `TTB:` terminal only for an owned BOOKED row
  (`seeded_terminal`) and hands the upgrade policy only to such rows, and passes
  `reconcile_eligible` = owned. A manual reservation is never upgraded or cancelled by the bot.
- **Dry-run environments never mutate reservations** (§7.8): no upgrade, no reconcile-cancel, no
  `cancelled(external)` write, and the web refuses cancel.
- **Tenant exit codes are non-zero only for systemic causes** (`runner.exit_code_for`,
  `watch_exit_status`, §4.5/§7.9): store failures, keyring, decrypt failures, CAPTCHA/OTP,
  UNCERTAIN (booker), the self-deadline, a failed outcome write, a failed operator summary. One
  user's miss or `AuthError` exits 0 and is carried by email. A 429 aborts the watch run with 0.
- **No store call inside the race window.** The booker reads and claims rows before T0, writes
  outcomes one row at a time only after T0 + `post_burst_quiet_s`, and every pre-T0 store call is
  bounded by `STORE_CALL_TIMEOUT_S` (20 s).
- **One store builder, and the database is never defaulted.** `tenant/wiring.py::open_tenant_store`
  serves every tenant command: `TENANT_COSMOS_ENDPOINT` set → Cosmos (Entra auth only; a
  key-shaped credential is refused); nothing set → in-memory with a loud `IN-MEMORY` WARNING; half
  configured → `TenantStoreConfigError`. `TENANT_COSMOS_DATABASE` is required with the endpoint so
  prod can never silently use dev data. The `-ci` containers are selected only by
  `TENANT_COSMOS_CONTAINER_SUFFIX=-ci`.
- **The Cosmos index policy must equal `CosmosTenantStore.QUERIED_PATHS`** (Cosmos rejects a
  filter on an unindexed path). A new query filter path means updating `cosmos.bicep` too;
  `tests/test_cosmos_bicep.py` fails CI otherwise.
- **The materializer walks the FULL horizon every call and decides by HISTORY, never by an id
  collision** (`tenant/materialize.py`, §7.7). It covers
  `[local_today, local_today + max(21, advance_days + 7)]` in the COURSE timezone and, per date,
  classifies the row history (`classify_date_history`): frozen (`frozen_reason`, or past) →
  user-terminal row (a cancelled `user`/`external`/`already_gone` row blocks the date for every
  rule; a withdrawn one-off does not) → own row (pending/booked/skipped/superseded/lost: nothing;
  system-withdrawn + slot free: `reactivate_rule_row`; slot held: nothing) → no own row (slot free:
  create; held: create SUPERSEDED). Why the full walk: deactivation is not atomic (reset →
  withdraw → reset → inactive, pinned by `test_deactivation_order_reset_withdraw_reset_inactive`),
  so a crash can strand withdrawn rows before the watermark; the tick reactivates them.
  `apply_rule_edit` rewrites only PENDING, unleased, not-frozen rows; a weekday move withdraws the
  old weekday's PENDING + SUPERSEDED rows and materializes the new one; deactivation never touches
  BOOKED or SKIPPED rows. **Leased rows are never touched by an edit** (`skipped_leased`); the
  tick's `rows_no_longer_covered` sweep withdraws them later with a reason from the stored rule.
  Conflict errors propagate out of `materialize_rule`/`apply_rule_edit` unswallowed. Rule
  DELETION is not on the Protocol yet.
- **Public repo: no email address in any param file.** The operator email is read from the
  `OPERATOR-NOTIFY-EMAIL` Key Vault secret and the ACS sender is derived from the email module
  output (pinned by `tests/test_webapp_bicep.py`).
- **The prod site has ONE canonical host** (`https://spicyteetimebooker.com`, 2026-09-28). The OAuth
  `state` lives in the session cookie of the host sign-in started on, so with
  `TEETIME_CANONICAL_HOST_REDIRECT=true` (Bicep sets it iff `webCustomDomain` is set)
  `web/app.py::_CanonicalHostMiddleware` redirects every other host (`www.`, the old
  `*.azurecontainerapps.io` name) to the same path on `TEETIME_PUBLIC_BASE_URL` (301 GET/HEAD, 308
  otherwise; `/healthz` exempt; the target is always the configured origin). The managed
  certificates `mc-<host-dashed>` are created once by AZURE_PLAN §10.9 before any deploy that sets
  the domain, or that deploy fails.
- **The website works with JavaScript off, and never shows a raw course id.** CSP is
  `script-src 'self'; style-src 'self'`: no inline script or style. The only script is the
  same-origin `web/static/app.js`, progressive enhancement over plain forms (the ranked form's
  rows 2-6 live in a `<details>` the server parses whether or not JS ran). Every course name shown
  to a person, on pages, in messages and in emails, comes from
  `courses/names.py::course_display_name`; add a new course there. Every form is a CSRF-guarded POST
  and every id is resolved through user-scoped reads (a foreign id is the uniform 404). Pinned by
  `tests/web/test_web_ui_polish.py` and the web security tests.
- **Static assets are linked through `static_url(name)`, never a bare `/static/...` path.** It
  appends `?v=<content hash>` (`web/app.py::static_asset_versions`, computed at startup), and
  every static response carries `Cache-Control: no-cache`, so a deploy can never leave browsers on
  stale CSS/JS (dev showed the pre-#268 pickers for hours). Pinned by
  `test_no_template_links_a_static_file_without_its_version`.
- **Adopting reservations is operator-only and confirmed** (MU-16b, `web/adopt.py`): it re-plans
  server-side from a trusted snapshot at most 15 minutes old and writes nothing without the
  confirm box; it cannot tell the bot's bookings from a manual one for the same slot, which is why
  the operator confirms.

### Email OTP

- **Mangrove Bay requires an emailed six-digit code from 2026-07-15, but enforcement is UI-only.**
  Live recon showed the bot's direct API book POST is not challenged (HTTP 200 + instant
  confirmation), so the OTP source is NOT on the booking critical path.
  - `core/otp.py` holds the `OtpSource` Protocol, `ImapOtpSource` (polls the dedicated Gmail
    inbox, fresh IMAP connection per poll, checks Spam, clock-injected, never logs the code) and
    `FakeOtpSource`. `fetch_code(sent_after=..., timeout_s=...)` scopes to the current attempt
    minus `freshness_grace_s` (default 60 s), because the mail server's clock may lag and
    rejecting a live code is the fatal direction. A blip consumes one poll, not the window;
    `connect_timeout_s` bounds a hung connect.
  - **Detection is wired:** `ForeUpAdapter._guard_otp_challenge` runs in `book()` after
    `_guard_captcha` and before the 400 → SlotGone mapping, and raises `OtpChallengeError`, a
    `CaptchaError` subclass, so every operator-loud CaptchaError path fires (booking exits
    non-zero; the watcher notifies and re-raises). Markers (`_OTP_CHALLENGE_MARKERS`) are
    best-effort since the API wording is unobserved; a captcha match wins if both match
    (test-pinned). Without it a challenge would read as SlotGone and end as a clean NO_INVENTORY.
  - Carve-outs inherited from CaptchaError: the upgrade's rebook-after-cancel logs and continues
    (the week's slot can be lost; accepted), and a challenge on a blind-burst POST is dropped like
    any non-SlotGone error (if the fresh search then finds nothing, the run ends as a non-zero
    NO_INVENTORY with the challenge only in a WARNING; accepted residual).
  - Recon extras: a UI pending hold shows in `list_reservations`, blocks the slot, self-expires,
    and does NOT count toward the 1-per-day limit.
  - **Open constraint for any future fetch → verify wiring:** `fetch_code` has no per-attempt
    correlation, so concurrent books sharing a mailbox could steal each other's codes. With the
    burst at 3, the wiring must serialize OTP-requiring books or extend the Protocol first.

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

Start from what your PR changes; each row lists every doc site that claim lives in. The recurring
full-repo-scan finding is a claim updated in most but not all of its homes, so grep and update ALL
of them in the same PR.

| Change | Doc sites to update (all of them) |
|--------|-----------------------------------|
| Prod infra tag bump / deploy | README.md Status + Azure hosting, CLAUDE.md Current status table, PLAN.md (scope note + §16 M6.T3 row), a new section in `docs/RELEASES.md`. Enforced: `tests/test_docs_consistency.py` fails if README/CLAUDE/PLAN name different "latest infra tag" versions |
| Prod mode change (`bookingMode`/`watchMode` in `main.bicepparam.prod`) | Every current-state line that says what prod runs: README Status + Azure hosting table, CLAUDE.md "Two paths" intro + Current status + milestone table, MULTIUSER_PLAN status note + §12, `docs/MULTIUSER_AS_BUILT.md`, AZURE_PLAN status banner, PLAN.md status, BACKLOG. Mechanically enforced: `tests/test_docs_consistency.py::test_no_doc_says_prod_runs_toml_once_prod_runs_the_tenant_path` (added after four review rounds each found another stale wording) |
| Dependency floor bump / dep-comment edit | `pyproject.toml`: bump the floor AND check the comment above it. A dep comment must name NO tracking version. Enforced for `idna` (only the CVE boundary may be named; the floor may not drop below it). Drifted twice (#106, #204) |
| New/changed config key or default | `core/config.py` field comment, `config/example.toml` + `container.toml` + `local.toml`, README Configuration, `tests/test_container_config_parity.py` pin, CLAUDE.md invariant if load-bearing |
| Engine orchestrator/watcher behaviour (`core/*orchestrator*.py`, gates) | CLAUDE.md invariants, PLAN.md §9/§9.1/§12, the owning plan's status banner (and a supersession banner on any plan it retires) |
| Adapter capability / course quirk (`courses/**`) | `src/teetime/courses/CLAUDE.md`, CLAUDE.md capability bullet |
| Tenant logic (`src/teetime/tenant/**`) | `docs/MULTIUSER_AS_BUILT.md` (milestone section), MULTIUSER_PLAN.md (§12 row + the section it implements), CLAUDE.md Multi-user invariants and milestone table if state changes |
| Tenant store semantics or schema (`tenant/store.py`, `in_memory_store.py`, `cosmos/**`, `semantics.py`) | `tests/tenant/conformance.py` (the contract), MULTIUSER_PLAN §3, `QUERIED_PATHS` + `cosmos.bicep` index policy if a new filter path, CLAUDE.md Two-stores bullet |
| Web app (`src/teetime/web/**`, templates, routes) | README Multi-user web app section (pages, env vars), MULTIUSER_PLAN §8 (`web/routes.py::ROUTES` is the contract table), `docs/MULTIUSER_AS_BUILT.md` |
| New CLI command, flag or env var | README, CLAUDE.md Common commands, AZURE_PLAN §7.3 env inventory + `compute.bicep`/`webapp.bicep`/`keyvault.bicep` if deployed |
| Key Vault secret added/renamed | `keyvault.bicep` / the consuming module, AZURE_PLAN §7.3 secret inventory, README env-var tables, `tests/test_keyvault_bicep.py` or the module's test |
| ACA job, cron or timeout (`compute.bicep`) | `compute.bicep` comments, CLAUDE.md + README schedule claims, AZURE_PLAN §5, killswitch job-name coupling (`killswitch.bicep` + `tests/test_killswitch_job_parity.py`) |
| Release event (`infra/bicep/release_events.json`, `core/release_policy.py`) | `tests/test_release_events_parity.py`, MULTIUSER_PLAN §6.2, AZURE_PLAN §5, `src/teetime/courses/CLAUDE.md` per-course policy |
| New Bicep module or param (`infra/bicep/**`: `cosmos`, `webapp`, `email`, …) | `infra/CLAUDE.md` module tree, AZURE_PLAN (module + runbook), both `.bicepparam` files, `azure-iac.yml` inline params (pinned by `test_every_param_file_value_reaches_every_ci_deploy`), killswitch levers if it runs compute |
| CI workflow change (`.github/workflows/**`) | CLAUDE.md Required CI checks + branch protection for a new validation job, `.githooks/pre-push` (pinned by `tests/test_prepush_hook.py`) |
| Milestone or feature done or cut | PLAN.md §16 row or MULTIUSER_PLAN §12 row, README status/roadmap, CLAUDE.md Current status, BACKLOG.md if it retires an item |
| Retry behaviour (a new retry, or a call that must never retry) | CLAUDE.md "Retries everywhere else" bullet (the per-call list), `tenant/retry.py` module docstring if the SDK baseline changes, `docs/MULTIUSER_AS_BUILT.md` "Retry audit" section, and a test that pins both the retry AND its bound |
| Doc moved, renamed or retired | Every link to it (grep the whole repo incl. src docstrings, tests, bicep, workflows), the Where-the-docs-live table here, README Documentation list. Enforced: `tests/test_docs_consistency.py` fails on a broken relative markdown link |

When a sweep fixes a stale claim, ask whether a cheap test can pin it; add sibling checks to
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
`pytest -m "not integration"`; pip-audit stays CI-only) with `set -euo pipefail` and blocks the
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
