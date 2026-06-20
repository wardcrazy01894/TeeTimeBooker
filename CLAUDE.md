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
v0 is single-user, run as Azure Container Apps Jobs (GitHub Actions is CI/deploy only). No frontend.

## Status

M1 + M5 + M-feature-1 (watch job) + M-feature-2 (auto-upgrade) + M-feature-3 (slot ranking)
complete. M2 is
PARTIAL — the core orchestrator is done, but M2.T3 (post-mortem reconciliation,
the UNCERTAIN→RECONCILING→BOOKED/LOST path) is not yet implemented. ForeUP adapter
fully implemented; live dry-run confirmed against Mangrove Bay. TeeItUp adapter
fully implemented; live booking + cancel confirmed against Sydney Marovitz
(2026-05-29). M3 (SQLite) and M4 (email notifications) are CUT — `InMemoryStore`
+ `ConsoleNotifier` are the final production wiring, not stubs.

**M6 wiring is DONE** (PRs 1–6): `run --wait` busy-waits to the 06:00:00 ET drop;
`core/dst_gate.py` exits the wrong-season cron; watcher enabled; `bookingReplicaTimeout=1200`;
the `enableSchedules` bicep param can silence an env. Verification + cutover runbook in
AZURE_PLAN §10.4/§10.5. **Prod is DEPLOYED** (`dryRun=false`; latest infra tag `infra/v2.3.0`).

**Multi-day re-architecture is DONE in code** (MULTIDAY_PLAN.md, PRs #70/#71/#72/#73/#74,
ratified via plan-with-review). The bot now books BOTH **Saturday and Sunday** mornings
(wanted days derived from the per-day `[[request.time_windows]]` weekdays), holding one
reservation PER day:
- Booking crons fire **DAILY** — two crons, one per DST half: `50 9 * * *` (EDT) and
  `50 10 * * *` (EST), both 05:50 ET. Jobs renamed `teetime-job-<env>-edt`/`-est`
  (the `-sun` suffix dropped). Each run computes `today+7` and **fast-exits 0** unless that
  weekday is wanted (`core/booking_day_gate.py`), after the DST gate.
- The watcher **polls on every run** (the time-of-day gate was removed) and, on EVERY run
  regardless of which weekday it executes, checks the next occurrence of EACH wanted weekday
  within the horizon (`core/target_date.next_occurrences_within_horizon`) — e.g. a run on any
  day checks the upcoming Saturday AND the upcoming Sunday and can book/upgrade either. The
  per-date scoping is about pairing each TARGET DATE with its own slots/windows: the search is
  `dc_replace`d to one target date, so the check for the Saturday *target* books only a Saturday
  slot and the check for the Sunday *target* only a Sunday slot (it is NOT a restriction based
  on the day the watcher runs). Removing the hours gate also enables an early-morning recovery
  booking.
- Also merged earlier: race-path CAPTCHA pre-fetch (#68) and book-POST 4xx → SlotGoneError
  multi-slot fallback (#67).

**LIVE in PROD** (`infra/v2.3.0` = `main`@`f2b7032`, deployed 2026-06-15, `dryRun=false`):
multi-day Sat+Sun booking, the 4PM-day-before booking cutoff, the Portal-editable
skip-days (`TEETIME-SKIP-DATES` KV secret, present in both vaults), within-window
upgrade (strictly-closer-to-midpoint slot in same tier triggers cancel-before-book; `infra/v2.2.0`),
watcher today+7 horizon (same-weekday target included when today is a wanted weekday;
`#119`), and captcha TimeoutError recovery (`book()`/`prepare_book()` → `CaptchaError`,
lead = 120 s; `#120`) are all active. The
renamed `-edt`/`-est` jobs are deployed in both envs and the old `-edt-sun`/`-est-sun`
orphans were deleted per the AZURE_PLAN.md §10.2 runbook (the ARM-incremental orphan risk
is resolved). Known benign quirk: a watch-cron fire that lands mid-deploy can lose one
10-min cycle (placeholder-image / transient ACR 401) — it self-heals on the next fire.
Remaining v0 task: **M2.T3** (post-mortem reconciliation) — still unimplemented,
independent of all the above.

**Azure v1 IaC is implemented.** All Bicep modules are complete (`identity`,
`registry`, `keyvault`, `logs`, `compute`, `budget`). Dev auto-deploys on merge
to main via `.github/workflows/azure-iac.yml` with `dryRun = true` — no real
bookings fire in dev. State is in-process only (`InMemoryStore`); the bot makes
no authenticated Azure SDK calls at runtime.

**Cost killswitch (PR-KS1 + PR-KS2) implemented and LIVE in dev.** `killswitch.bicep` + `killswitch-rbac-prod.bicep`
deploy a Logic App (Consumption) + Action Group in `rg-teetime-dev` that issues 12 HTTP
calls (6 PATCH + 6 POST /stop) to silence all six ACA Job crons when the $50 actual budget
threshold fires. Deployed only in dev (Logic App manages both envs via cross-RG RBAC). Gated
on `enableKillswitch && !empty(killswitchRbacRoleId) && envName=='dev'`. The "ACA Job Schedule
Manager" custom role (GUID `3e2d5a14-96bd-4469-9f96-b9c3270aa9e6`) is created; the GUID is set
in both param files and in `azure-iac.yml` — the killswitch **arms on every dev auto-deploy** and
is live in dev. The `killswitchFired` param (already in both param files) is the CI deploy-clobber
guard: once set to `true`, no subsequent CI deploy can re-arm the cron schedules. PR-KS2 added the
separate $50 `budget-teetime-killswitch` resource to `budget.bicep` (conditional on
`killswitchActionGroupId`; the $20 email budget is untouched) — deployed manually, subscription-
scoped. **Both budget tiers are DEPLOYED and ARMED (verified live 2026-05-31):** `budget-teetime`
($20) emails on 80%-actual/100%-forecast, and `budget-teetime-killswitch` ($50) is wired to the
`ag-teetime-killswitch-dev` Action Group at 100%-actual. The killswitch chain is fully armed
end-to-end across dev + prod; no operator step remains. See `infra/COST_KILLSWITCH_PLAN.md` and
`infra/AZURE_PLAN.md §9.2`.

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
.github/workflows/  # ci.yml (lint / type-check / test / docker-smoke / secret-scan / bicep-lint) + azure-iac.yml (Bicep deploy)
tests/              # pytest; respx for httpx mocking (no vcrpy cassettes)
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
- **Hard booking cutoff (LEADTIME_SKIP_PLAN F1).** `request.booking_cutoff`
  (`{days_before, time_of_day}`, default 16:00 ET the day before) FREEZES a target
  date once wall-clock passes `time_of_day` on `days_before` days before it: no new
  booking AND no upgrade — whatever is held at the cutoff is final (held bookings are
  never auto-cancelled). The pure, clock-injected, zoneinfo-correct predicate is
  `core/booking_cutoff.py::is_past_booking_cutoff`; it is composed into the watcher's
  stop-acting gate and added defense-in-depth to the booking-day gate. It is booking
  POLICY, not request identity — it does NOT feed the RequestId fingerprint.
- **Skip-days are FAIL-OPEN, resolved at load (LEADTIME_SKIP_PLAN F2).**
  `request.skip_dates_env` names an env var holding a comma/space-separated ISO date
  list; `core/skip_dates.parse_skip_dates` resolves it into `request.skip_dates`
  (`frozenset[date]`) in `load()`. Asymmetry vs credential `*_env`: an unset/empty/
  malformed skip value is NOT an error — it yields an empty set (absence = no skips), so a
  fat-fingered Portal edit can never crash the 06:00 booker or the watcher. In prod the env
  var is injected from a Key Vault secret editable in the Portal with no redeploy. The
  booking-day gate + watcher honor it; it does NOT feed the RequestId fingerprint.
- **Credit-card data is platform-specific.** ForeUP keeps card-on-file; the
  ForeUP path never POSTs PAN/CVV. TeeItUp has no wallet, so the TeeItUp adapter
  DOES POST PAN + CVV + expiry + billing to `tr.gnsvc.com` on every booking
  (sourced from `*_env` vars, never committed). This is a deliberate scope
  expansion past the original "no card data, ever" rule — see PLAN.md §7.
  Consequence: handling raw PAN/CVV brings PCI scope; card fields are dropped by
  `core.redaction.redact_payload`, which `BookingStore.append_attempt` applies at the
  store boundary on every `attempt_log` write (PLAN.md §10.1) — so no caller can leak
  card data by forgetting to scrub. The card POST uses `follow_redirects=False`.
- **Double-booking defense is layered.** Live pre-book `list_reservations`
  check, single-attempt-per-slot rule, in-process advisory lock, ACA Job
  concurrency (`parallelism=1`, one execution per job). There is no durable cross-run idempotency
  record; the live remote check is the primary cross-run guard. PLAN.md §9
  has the full flow; §9.1 has the explicit state machine that M2.T1
  implements. `list_reservations` is on the `CourseAdapter` Protocol from
  M0 — it is NOT optional.
- **A book-POST 4xx is a try-next-slot signal, not a crash — and slot exhaustion is
  graceful, not a crash either.** `ForeUpAdapter.book()` maps both `409` and `400` to
  `SlotGoneError`: a 4xx rejection means ForeUP definitively created NO reservation (the
  prod 2026-06-07 failure was a `400` when the prime slot was claimed in the ~100 s between
  search and book), so the orchestrator's candidate loop (`_run_course`) falls through to the
  next-ranked slot instead of dying with an uncaught `HTTPStatusError`. **When EVERY ranked
  candidate for a course is gone, `_run_course` raises the internal `_CourseSkippedError`
  (not the `SlotGoneError`)** — so `run()` advances to the next course preference, and if no
  course books, it records a `NO_INVENTORY` terminal and notifies, rather than crashing the
  job with a non-zero exit and no terminal. **`TeeItUpAdapter.book()` has the same parity**
  via `_raise_for_booking_step`: a non-409 4xx at any pre-payment step (cart-item, lock,
  create-order, order-teetime) also maps to `SlotGoneError`. In BOTH adapters this is
  distinct from the §9 UNCERTAIN case (timeout/5xx — ambiguous whether the POST landed),
  which still propagates (a 5xx at a TeeItUp pre-payment step goes through `raise_for_status`,
  not the SlotGone mapping). `ForeUpAdapter.book()` ALSO logs the full status + response body
  on any non-2xx before raising (the body used to be discarded by `raise_for_status`, leaving
  us blind to the reason). A captcha-challenge `400` is still classified as `CaptchaError`
  first (the `_guard_captcha` check runs before the 400→SlotGone mapping). Caveat: each
  fallback candidate re-solves a fresh CAPTCHA (~75 s, single-use token), so at a competitive
  drop the fallbacks are best-effort.
- **Transient-failure retry is for IDEMPOTENT ForeUP calls only.** `ForeUpAdapter.
  _send_with_retry` retries on `httpx.TransportError` (read/connect timeouts,
  network blips — the observed prod failure was a lone `httpx.ReadTimeout` that
  wasted a whole 10-min watch cycle) around the warm-up GET, login POST, search
  GET, and cancel DELETE. It does NOT retry HTTP status errors (those surface via
  `raise_for_status` after it returns) and **`book()`'s POST is never wrapped** —
  that stays single-attempt (§9; a timed-out book is the UNCERTAIN case M2.T3
  owns, not a safe re-fire). Tuned by ctor `max_retries` (default 2) /
  `retry_backoff_s` (default 0.5s, linear); tests pass `retry_backoff_s=0`. The
  watch ACA Job's `replicaTimeout` is 300s (not 120s) to give these retries
  headroom — see `compute.bicep` / AZURE_PLAN §5.4.
- **DST handled by `zoneinfo`** (the bot computes T0 in `America/New_York`,
  which resolves the ambiguous/skipped-hour edge cases) + two DAILY ACA Job
  booking crons (one per DST half; the booking-day gate restricts to wanted weekdays) in
  `infra/bicep/modules/compute.bicep`. Math
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
  (cancel+rebook upgrade) is **ENABLED** (`config/*.toml`): when a higher-ranked slot
  opens for a booked day — a higher-priority tier, or the SAME tier strictly closer
  to that day's window midpoint (within-window upgrade; midpoint ties never upgrade)
  — the watcher cancels and rebooks it. Safe because the watch request is scoped per target date, so it
  only ever upgrades within the intended date+window; real effect is prod-only (dry-run suppresses the
  POSTs). The watch cron runs every 10 min year-round.
- **The watcher POLLS ON EVERY RUN — no time-of-day gate** (multi-day PR4; the old
  `polling_start_hour`/`polling_end_hour` gate + config fields are REMOVED). It blinded us at
  the 6 AM drop. The only remaining skip is `_is_past_watch_deadline` (don't poll a date that
  already passed). Rate limiting = the 10-min cron cadence + the `poll_interval_s >= 300`
  floor. Consequence: an early-morning run that finds the just-dropped window open will BOOK it
  (a recovery path if the 06:00 booker raced/failed) — safe per-date via the in-lock
  `get_terminal` re-check.
- **A `RateLimitError` (HTTP 429) ABORTS the whole watch run — it is NOT a try-next-course
  transient blip.** `check_once` catches `RateLimitError` explicitly (before the generic
  `except Exception`), logs it honouring `retry_after_s`, and re-raises — so it does NOT fall
  through to the next course (which would keep hammering the throttled platform) and `_watch`
  stops polling further dates. `_watch` catches it at the date loop and **exits 0** (the 10-min
  cron is the backoff floor; PLAN §12). Non-zero watch exit stays reserved for `CaptchaError`/
  `AuthError` (operator action). Distinct from the generic transient handler, which DOES
  `continue` to the next course on a network blip.
- **The watcher checks MULTIPLE dates per run — the next occurrence of each wanted weekday**
  within the horizon (`core/target_date.next_occurrences_within_horizon`, multi-day PR4);
  `_watch` loops `check_once` over them (no `break`). **`_check_course` scopes the search to
  each `target_date` (`dc_replace`) AND filters ranked candidates to that date** — a Saturday
  watch can NEVER book a Sunday slot (one reservation PER date; the per-date `(RequestId,
  date)` store key keeps Sat and Sun independent). `--date` still overrides to a single date.
- **Time windows are bound to weekdays; wanted days are DERIVED from them** (per-day windows,
  PERDAY_WINDOWS_PLAN). Each `[[request.time_windows]]` carries a `weekday`; multiple windows
  may share a day (one reservation per day — best window wins; list order = preference).
  `RequestConfig.wanted_weekday_indices` is derived from the windows' weekdays (the separate
  `target_weekdays`/`target_weekday` keys were REMOVED — hard cutover, un-tagged config errors
  loudly). The domain `TimeWindow` stays weekday-free; per-invocation scoping narrows the
  request's windows to the TARGET DATE's weekday: `_build_booking_request` (booker) and
  `_scope_request_to_date` (the `_watch` loop, called once per target date) pin
  `time_windows=_windows_for_date(...)`, so the check for a Saturday-dated target searches/ranks
  Saturday's windows and a Sunday-dated target uses Sunday's. (This is per TARGET DATE, not per
  execution day — a single watcher run still checks every wanted upcoming date.) The RequestId
  fingerprint encodes the window weekday (`<wd>:HH:MM-HH:MM`) so a Sat vs Sun window is a
  distinct identity.
- **Target date(s):** the booking job books a SINGLE gated date (`today + offset`, gated by
  `core/booking_day_gate.py` to a wanted weekday); the watcher uses
  `next_occurrences_within_horizon` over the derived wanted days. `--date` still overrides for
  the watch command (errors if that weekday has no window).
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
- **`prepare_book(slot, request, *, count=1)` on `CourseAdapter` Protocol**: pre-fetches
  expensive prerequisites (CAPTCHA tokens, ~15-60 s each). `ForeUpAdapter.prepare_book()`
  solves `count` tokens **CONCURRENTLY** (one `asyncio.gather(return_exceptions=True)`) and
  appends each success to a FIFO pool `self._captcha_tokens` (a `collections.deque`); `book()`
  pops the OLDEST (`popleft`, single-use, never returned) so a late-firing fallback keeps the
  freshest token, falling back to an inline solve when the pool is dry (RACE_PREWARM_PLAN
  Change C). **NI10 raise contract:** `count == 1` + total solve failure RE-RAISES (a
  `TimeoutError` → `CaptchaError`) so the upgrade caller aborts; `count > 1` (race prefetch)
  NEVER raises (best-effort; pool ends up with however many succeeded, possibly zero).
  **MF1 stale-token recovery:** a POOLED token rejected as a captcha challenge (likely solved
  pre-T0 and now expired) triggers exactly ONE inline re-solve + re-POST of the same slot
  (`_is_captcha_challenge` is the non-raising sibling of `_guard_captcha`); the re-POST is
  classified normally (a 2nd challenge → `CaptchaError`, no loop). An INLINE-solved token gets
  no such retry. Adapters with no pre-fetch cost (FakeAdapter, TeeItUpAdapter, future
  Chronogolf) implement it as a no-op (they accept `count` for parity and ignore it). Its
  `slot` arg is `TeeTimeSlot | None` (the CAPTCHA is page-level, slot-independent). **Two
  callers:** (1) `UpgradeOrchestrator` calls it with the chosen slot + `count=1` BEFORE
  `cancel_reservation()`, shrinking the cancel-to-book no-booking window from ~60 s to ~1-2 s;
  (2) the main booking `Orchestrator`, on the race path only, calls it with `slot=None` +
  `count=scheduler.captcha_prefetch_count` (default **3**) DURING the pre-T0 busy-wait (next bullet).
- **The booking race pre-fetches the CAPTCHA before T0 (`Orchestrator(prefetch_book=True)`).**
  The 2026-06-07 prod Sunday booker fired at T0 perfectly but then solved the CAPTCHA
  (~78 s) AFTER the drop, posting the booking ~100 s late → the prime slot was gone →
  HTTP 400 → no tee time. Fix: on the `--wait` race path the orchestrator does a
  TWO-PHASE busy-wait — wait to `T0 − scheduler.captcha_prefetch_lead_s` (default 120 s =
  2 min), `_prefetch_captcha()` (first-preference adapter, best-effort: failures are logged
  and swallowed, book() then solves inline) which now pre-solves
  `scheduler.captcha_prefetch_count` tokens (default **3**) CONCURRENTLY into the FIFO pool so
  the first 3 ranked candidates each fire near-instantly instead of re-solving inline, then
  wait the remainder to exactly T0 — so the post-T0 `book()` POST fires within seconds of the
  drop with a token already in hand.
  `prefetch_book` is set **only** by the `--wait` ACA booking job (`__main__._run` passes
  `prefetch_book=wait`). The watcher and local-demo runs leave it False: a token is
  solved only when actually about to book (the watcher's upgrade path still pre-fetches
  inside `maybe_upgrade`, just-in-time). Lead = 120 s = 24 polls × 5 s (the provider
  timeout), so the pre-fetch typically completes before T0; and token age at T0 ≤ lead ≤ the
  ~120 s reCAPTCHA freshness window. Both `book()` and `prepare_book()` catch `TimeoutError`
  from the inline/prefetch solve and re-raise as `CaptchaError` — on the prefetch (race) path
  `_prefetch_captcha` swallows `CaptchaError` and the job continues with an inline solve; on
  the inline `book()` path or the upgrade path it surfaces as a clean non-zero exit.
  If the run STARTS past `T0 − lead` (the DST gate admits all of hour 5, so a late-landing
  cron can begin with too little runway), the lead can't be honored and the POST may fire
  after T0 — the orchestrator logs a `prefetch lead not fully honored` WARNING and still
  pre-fetches immediately.
- **The race ALSO pre-warms the ForeUP login before T0 (RACE_PREWARM_PLAN PR1).** On the
  `--wait` race path the orchestrator's `_prewarm_primary()` runs, CONCURRENTLY with the
  CAPTCHA solve (one `asyncio.gather(return_exceptions=True)`), `_prewarm_login()` for the
  first-preference adapter: `authenticate()` (warm-up GET + login POST, ~2 s) + the layer-2
  `list_reservations` "already-booked" guard. So at T0 only `search` + `book` remain on the
  critical path (login no longer costs ~2 s post-T0). Both legs are best-effort (catch +
  swallow + log); a pre-warm hiccup never costs the booking. **The post-T0 re-auth skip is
  ORCHESTRATOR-owned** (`self._prewarmed_course_ids`, threaded into `_run_course` as
  `prewarmed_course_ids`): a course in the set is NOT re-authenticated at T0. This is
  deliberately independent of any adapter idempotency guard (FakeAdapter/TeeItUp have none);
  `ForeUpAdapter.authenticate()` ALSO has a defensive `if self._logged_in: return` guard, but
  it is hygiene, not load-bearing. Only the PRIMARY (first-preference) adapter is pre-warmed —
  a fallback course authenticates + solves inline at T0 (no shared session/token pool). If the
  pre-T0 guard finds we are **already booked**, the run **short-circuits before T0**: it logs
  `race: short-circuited pre-T0 …` (the SF6 verification surface, since the normal
  `race: busy-wait complete` line is skipped), records `ALREADY_BOOKED`, notifies, and returns
  WITHOUT busy-waiting to T0 or searching. `prefetch_book=True` (the `--wait` job) is the only
  thing that enables any of this; the watcher/local-demo never pre-warm.
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

## Required CI checks

Any NEW CI validation job added to `ci.yml` (a job that runs on PRs and should
gate merge — e.g. a new lint/test/scan/build check) **MUST be added to `main`'s
branch-protection required status checks in the same PR**:

```bash
gh api -X PATCH repos/<owner>/<repo>/branches/main/protection/required_status_checks \
  -F strict=true \
  -f 'contexts[]=<job-name-1>' \
  -f 'contexts[]=<job-name-2>' \
  # ... include the FULL current list every time (replaces, not appends)
```

Validation checks are required by default; do NOT add a merge-gating check that
is only advisory. Deploy jobs (`deploy-dev` / `deploy-prod`) are NOT required
checks — they run on push/tags, not PRs.

**Current required checks:** `test / lint / typecheck`, `docker build`,
`docker smoke`, `bicep lint`, `secret scan`.

## When in doubt

- Implementing a new milestone task? Read PLAN.md §16 for inputs/outputs/deps.
- Adding a new course (ForeUP / TeeItUp / Chronogolf)? See the step-by-step and
  per-course IDs in [`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md).
- Touching the orchestrator? Make sure FakeAdapter + FakeClock + InMemoryStore
  tests still cover your change. Tests construct these collaborators inline (see
  `tests/test_orchestrator.py::_build`); the race-window test is the canary.
- Modifying anti-bot etiquette? Re-read PLAN.md §12 first. ToS posture is not
  ours to negotiate around.

## v1 Azure infra → `infra/CLAUDE.md`

The Azure hosting work (Bicep layout, `az login` runbook, the **agent deploy
safety rules**, and the open-questions pointer) lives in
[`infra/CLAUDE.md`](./infra/CLAUDE.md), with the authoritative design in
`infra/AZURE_PLAN.md`. Both load automatically when you work under `infra/`.

**Safety rule that always applies (also enforced by `.claude/hooks/az-deploy-guard.sh`):**
an agent MUST NOT run `az deployment … create`, `az containerapp job start`,
`az keyvault secret set/delete` or vault-level `az keyvault purge/delete`, or
`az group delete` without explicit user approval. Read-only `az` (list/show/validate/
what-if) and `az bicep build` are fine. (The guard's block-list is regression-tested in
`tests/test_az_deploy_guard.py`.)
