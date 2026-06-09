# TeeTimeBooker — v0 Plan

> **Scope of v0:** a Python bot that books one or more tee times at **Mangrove Bay Golf Course** (St. Petersburg, FL) at the moment its 7-day booking window opens (6:00 AM America/New_York). No frontend. No third-party booking sites that don't actually take the booking. No real bookings against ForeUP from these stubs — implementation lands in M2/M5 once Spike S1 confirms endpoint shapes.

This plan is structured for parallel execution. Milestones are sequential; tasks within a milestone are tagged with explicit dependencies, so an "army of agents" can pick up anything green.

---

## 1. Architecture overview

```
                     +-----------------------------+
                     |   ACA Job cron (v1)         |
                     | (UTC; daily × DST half)     |
                     +--------------+--------------+
                                    |  workflow_dispatch / cron fires ~10 min early
                                    v
+---------------------------------------------------------------------------+
|                              CLI (`teetime run`)                           |
+----------------+-----------------+----------------------+------------------+
                 |                 |                      |
                 v                 v                      v
        +-----------------+  +-----------+        +----------------+
        |  Config (TOML)  |  |  Clock    |        |  Notifier       |
        |  pydantic-load  |  |  (Real /  |        |  (Console;      |
        +-----------------+  |   Fake)   |        |   pluggable)    |
                             +-----------+        +----------------+
                                    |
                                    v
                         +---------------------+
                         |   Orchestrator       |
                         |   - idempotency      |
                         |   - 6 AM busy-wait   |
                         |   - course fallback  |
                         |   - retry policy     |
                         +----+--------+--------+
                              |        |
              +---------------+        +----------------+
              v                                         v
   +-------------------+                       +----------------------+
   |   BookingStore    |                       |   CourseAdapter      |
   |   (InMemoryStore) |                       |   (Protocol)         |
   |   - history       |                       +----------+-----------+
   |   - attempt log   |                                  |
   |   - session cache |                                  |
   |   - request lock  |                  +---------------+---------------+
   +-------------------+                  v                               v
                                +-------------------+         +-------------------+
                                | ForeUP base       |         | Chronogolf base   |
                                | (HTTP, auth,      |         | (placeholder;     |
                                |  rate-limit, UA)  |         |  Spike S2 later)  |
                                +---+------+--------+         +-------------------+
                                    |      |
                                    v      v
                         +----------+  +-----------------+
                         | Mangrove |  | (future ForeUP  |
                         | Bay IDs  |  |  munis: Twin    |
                         +----------+  |  Brooks, etc.)  |
                                       +-----------------+
```

**One-line summary:** an Orchestrator drives one or more CourseAdapters at T0, persists in-run state via BookingStore (InMemoryStore — not persisted across runs), and reports via Notifier. Every component except the orchestrator is a Protocol so we can swap impls (cloud KV, Slack notifier, another course adapter) without rewiring.

---

## 2. Module layout (committed)

```
TeeTimeBooker/
  pyproject.toml                # 3.12+, ruff, pytest, mypy strict, uv-managed
  PLAN.md                       # this file
  CLAUDE.md                     # operator/agent guide
  .github/workflows/ci.yml      # lint/test on PRs
  .github/workflows/azure-iac.yml # Bicep IaC deploy
  config/example.toml           # template; secrets via env-var refs
  src/teetime/
    __init__.py                 # version
    __main__.py                 # CLI entry (Click): run + watch + show-config
    core/
      __init__.py
      models.py                 # @dataclass: BookingRequest, TeeTimeSlot, BookingResult, ...
      adapter.py                # CourseAdapter Protocol + adapter exceptions
      orchestrator.py           # main 6 AM booking flow
      watch_orchestrator.py     # M-feature-1: cancellation-monitor flow (read-only check)
      upgrade_orchestrator.py   # M-feature-2: cancel-before-book rebook to a better slot
      slot_utils.py             # shared slot ranking (midpoint-distance sort)
      config.py                 # TOML loader (pydantic)
      clock.py                  # Clock Protocol + busy_wait_until
    persistence/
      __init__.py
      store.py                  # BookingStore Protocol + ConcurrentRunError
      in_memory_store.py        # production + test store (in-process only; not persisted)
    notifications/
      __init__.py
      notifier.py               # Notifier Protocol + Noop / Console
    courses/
      __init__.py
      foreup/
        __init__.py
        base.py                 # shared HTTP, auth, rate limit, error mapping
        captcha.py              # reCAPTCHA token harvest (2captcha)
        mangrove_bay.py         # course IDs only
      teeitup/
        __init__.py
        base.py                 # shared TeeItUp/Kenna HTTP + card payment flow
        sydney_marovitz.py      # course IDs only
      chronogolf/
        __init__.py             # README-only placeholder (Spike S2)
    dev/
      __init__.py
      fake_adapter.py           # FakeAdapter for orchestrator tests
  tests/
    __init__.py
    test_adapter_stub.py        # reference pattern; per-module tests added by tasks
```

**Why this layout:** `core/` is the only package that depends on nothing else. `persistence/`, `notifications/`, and `courses/` all depend on `core/` and never on each other. The orchestrator is the only thing that knows about all four — every other coupling is through a Protocol.

---

## 3. Interface contracts (cross-reference)

| Contract                | Stub file                                       | Consumed by                       |
|-------------------------|-------------------------------------------------|-----------------------------------|
| `CourseAdapter`         | `src/teetime/core/adapter.py`                   | `Orchestrator`                    |
| `BookingStore`          | `src/teetime/persistence/store.py`              | `Orchestrator`                    |
| `Notifier`              | `src/teetime/notifications/notifier.py`         | `Orchestrator`                    |
| `Clock`                 | `src/teetime/core/clock.py`                     | `Orchestrator`, busy-wait helper  |
| Domain dataclasses      | `src/teetime/core/models.py`                    | every layer                       |
| `AppConfig`             | `src/teetime/core/config.py`                    | CLI + `Orchestrator`              |

All Protocols are `runtime_checkable`. Type-checks under `mypy --strict`.

---

## 4. Configuration schema

**Choice: TOML.** Justification: stdlib `tomllib` (Python 3.11+) means zero extra runtime deps just to parse config; it has unambiguous types (no YAML's Norway problem); operators of v0 are not non-technical end-users.

See `config/example.toml`. Schema enforced by `core/config.py` via pydantic v2. Key rule: **no secrets in the file** — every credential is referenced by env-var name (e.g. `password_env = "MB_PASSWORD"`) and resolved at config-load time.

### 4.1 Booking cutoff (LEADTIME_SKIP_PLAN F1)

`request.booking_cutoff = { days_before = 1, time_of_day = 16:00:00 }` (the shipped default)
is a **hard freeze** on a target date: once wall-clock time has passed `time_of_day` in the
course-local timezone on the day `days_before` days before the reservation date, the bot makes
**no new booking and no upgrade** for that date — whatever is held at the cutoff is final. It is
an absolute wall-clock cutoff relative to the reservation date (tee-time-independent), so the
operator can never be surprised by a last-minute booking they don't learn about in time (the only
notifier is `ConsoleNotifier`/logs). The pure predicate lives in `core/booking_cutoff.py`
(`is_past_booking_cutoff`, clock-injected, zoneinfo-correct on the DST day-before); it is wired
into the watcher's stop-acting gate and (defense-in-depth) the booking-day gate. `booking_cutoff`
is booking POLICY, not request identity — it does NOT feed the RequestId fingerprint.

The watcher composes it into a single `_should_stop_acting_on_date(now, target_date)` predicate
at the top of `check_once` — it **composes (OR)** with the existing watch deadline (it does NOT
replace it): the cutoff is strictly EARLIER than the day-after deadline, so it bites first while
the deadline still covers the after-the-round case. The predicate sits ABOVE both upgrade entry
points and the search loop, so one check freezes new bookings AND upgrades. Each freeze reason
(deadline / cutoff; skip in F2) logs its OWN distinct line so an operator can tell WHY a date froze.

---

## 5. Persistence layer

**Decision: `InMemoryStore` for all runs (v0 and v1).** A durable cross-run store was considered and deliberately cut. The single-user, low-frequency bot does not benefit from a local record of past bookings: the live `list_reservations()` pre-book check (§9 layer 2) already prevents double-booking, and a stale in-process record of a prior booking risks MISSING a real booking more than it prevents anything. Cross-PROCESS serialization is handled at the scheduler level (ACA Job `parallelism=1` — one execution per job) — no advisory DB lock is needed across processes.

**What `InMemoryStore` tracks within a single run:**

| Method / concept      | Purpose                                                  | Scope                |
|-----------------------|----------------------------------------------------------|----------------------|
| `get_terminal` / `record_terminal` | Terminal `BookingResult` per `(RequestId, date)` (in-process idempotency). | This run only. |
| `delete_terminal`     | Clear an idempotency record before cancel+rebook (UpgradeOrchestrator). | This run only. |
| `append_attempt`      | Append-only in-memory audit log (never written to disk). | This run only. |
| `cache_session` / `load_session` | Adapter session blobs (cookies, JWT) per course. | This run only. |
| `request_lock`        | In-process advisory lock; prevents two concurrent code paths in the same run from double-booking. | This run only. |

Nothing is persisted to disk. The `BookingStore` Protocol remains the cut line — a durable store can slot in without touching the orchestrator.

---

## 6. Scheduling: the 6:00 AM ET race

### 6.1 Strategy

```
T0 = today + target_offset days, 06:00:00.000 America/New_York
     (target_offset comes from RequestConfig.target_offsets; default [7])

ACA Job cron fires ~T0 - 10 min  (v1; was GH Actions cron in v0. jitter is best-effort)
    -> "DST-half check" reads ZoneInfo("America/New_York") wall-clock
       and short-circuits the rest of the job if not in 5:xx ET. NOT a TODO;
       the gate now lives in core/dst_gate.py (was the book.yml dst step; M6 PR2).
    -> uv sync; bot starts ~T0 - 8 min in steady state
Bot:
    1. Resolve T0 in tz-aware datetime via `zoneinfo.ZoneInfo("America/New_York")`.
       Resolved date = today + target_offset.
    2. Load config; in-process idempotency check on (RequestId, resolved_date); build
       adapter; PRE-AUTH (login NOW so the race window is just GET /times +
       POST /reservations).
    3. busy_wait_until(T0 - 500ms): coarse asyncio.sleep down to ~2s, then a
       1ms-cadence fine loop with explicit OS yield (see core/clock.py).
       Sub-second accuracy without CPU starvation.
       RACE PATH ONLY (Orchestrator prefetch_book=True, set by `--wait`): this is a
       TWO-PHASE wait — first to (T0 - captcha_prefetch_lead_s, default 90 s), where the
       CAPTCHA token is pre-solved (`_prefetch_captcha` → adapter.prepare_book(None,…)),
       then the remainder to T0. This moves the ~75 s CAPTCHA solve OFF the post-T0
       critical path (the 2026-06-07 prod failure: solve ran after T0 → book POST ~100 s
       late → prime slot gone → HTTP 400). Pre-fetch is best-effort; on failure book()
       solves the token inline. The watcher never pre-fetches (prefetch_book=False).
    4. Fire first GET /times. Response disambiguation (per Spike S1, item 7):
       - 200 + empty + pre-T0  -> InventoryNotPublishedError; poll
       - 200 + empty + post-T0 -> NoInventoryError; do NOT poll
       - 200 + non-empty       -> filter & rank
       - 4xx 'too far in advance' -> InventoryNotPublishedError; poll
    5. Pick best slot, POST. Enter §9 state machine for the POST/result phase.
    6. Persist terminal result; notify.
```

### 6.2 Cron jitter

> Historical note: this section was written for v0's GitHub Actions cron. v1 runs on **ACA Job crons** (also best-effort/UTC; see AZURE_PLAN §5.1). The mitigations below carried over to ACA unchanged — fire 10 min early + the bot's own busy-wait nails the second.

GH Actions cron is documented as best-effort with potentially **15+ minute** delays under load. Mitigations:
- **Schedule 10 min early.** Both DST entries fire at `:50` past the hour preceding 06:00 ET.
- **Bot does its own busy-wait.** The cron only needs to land the runner with at least 1–2 minutes of slack before T0; the bot itself nails the second.
- **If the runner isn't scheduled at all that day** (rare but real): we lose the race. This is a known v0 risk, accepted, and is the headline reason for the v1 upgrade to Azure Container Apps Jobs (more reliable cron firing than GH Actions). Note ACA cron is still cron — better than GH Actions, but not a sub-second-precise scheduler; the bot's own busy-wait is what nails T0. Documented in §15 and `infra/AZURE_PLAN.md`.

### 6.3 DST math (showing the work)

`America/New_York` switches between EST (UTC-5) and EDT (UTC-4). Spring-forward 2nd Sunday of March; fall-back 1st Sunday of November.

| Local target | EDT (Mar–Nov) | EST (Nov–Mar) |
|--------------|---------------|---------------|
| 06:00 ET     | 10:00 UTC     | 11:00 UTC     |
| Cron (daily)    | `50 9 * * *` (09:50 UTC, 10 min early in EDT) | `50 10 * * *` (10:50 UTC, 10 min early in EST) |

We register **two DAILY** crons (one per DST half), year-round (multi-day re-arch — supersedes
the M6 Sunday-only schedule; see MULTIDAY_PLAN.md). To avoid the maintenance burden of seasonal
cron edits, both same-day crons fire and the DST-half gate (`core/dst_gate.py`) proceeds only
when the ET wall-clock hour equals 5 (the cron fires at :50 of the hour preceding T0=06:00 ET) —
otherwise the wrong-season cron exits without booking (prevents the "second cron of the day runs
anyway" failure mode, review item 1). On top of the DST gate, the **booking-day gate**
(`core/booking_day_gate.py`) fast-exits 0 on mornings whose `today+offset` weekday has no
configured window (the wanted days are derived from `[[request.time_windows]]`; default
Sat+Sun) — so the daily crons book only the wanted days, one per day.

> **Status (M6):** the gate has been re-homed from the removed `book.yml` `dst` step
> into the container entrypoint. It now lives in `core/dst_gate.py` (`should_proceed`,
> M6 PR2) — a pure function of `(clock, timezone, fire_time)` returning
> `proceed ⇔ ET wall-clock hour == fire_time.hour - 1`. `_run` evaluates it ONLY on the
> real-timing `--wait` path (M6 PR1), BEFORE the busy-wait; a wrong-season cron logs and
> exits 0. The real-scheduler T0 busy-wait is wired via `teetime run --wait` (the ACA
> booking job passes `--wait` in M6 PR3); `--no-wait` (default) keeps immediate timing
> and bypasses the gate.

`--no-wait` runs (manual `gh`/ACA on-demand executions and all local runs) bypass the gate, matching the old `workflow_dispatch` always-proceed semantics, so manual dry-runs aren't blocked.

The bot itself uses `zoneinfo` to compute T0 — that handles the ambiguous-hour and skipped-hour edge cases automatically. Mangrove Bay's booking window opening on a fall-back morning is unambiguous (06:00 EST, the second 06:00 of the night) by the standard `fold=0` semantics; we accept that.

---

## 7. Auth & secrets

| Stage  | Storage                                  | Notes |
|--------|------------------------------------------|-------|
| v0     | GitHub Actions repo secrets              | One per credential; loaded into env in `book.yml`. |
| v0 dev | `.envrc` / direnv (gitignored)           | Same names as Actions secrets. |
| v1     | Azure Key Vault (user-assigned managed identity) | Secrets injected as env via native `keyVaultUrl` references; MI has KV Secrets User. See `infra/AZURE_PLAN.md`. |

**Credit card data — by platform (updated; original v0 plan was "never, for any platform"):**
- **ForeUP (Mangrove Bay):** no card data handled. ForeUP keeps card-on-file per user account; the booking POST includes no PAN/CVV.
- **TeeItUp (Sydney Marovitz):** card data **is** handled. TeeItUp native accounts have no card-on-file wallet, so the adapter passes PAN + CVV + expiry + billing to the payment endpoint (`tr.gnsvc.com/AddReservation`) on every booking. Card fields are sourced from env vars (`*_env` convention), never committed. **PCI note:** handling raw PAN/CVV brings PCI-DSS scope; the credentials transit GitHub Actions secrets / Azure Key Vault and must be added to the §10.1 redaction list so they never reach `attempt_log`. The card POST sets `follow_redirects=False` to prevent re-POSTing card data to an attacker-controlled redirect target.

This is a deliberate scope expansion past the original "no card data, ever" posture, made to support TeeItUp booking. The higher ToS/PCI exposure is accepted for single-user personal use.

---

## 8. Failure modes & retry policy

| Failure                          | Detection                                 | Response                                          |
|----------------------------------|-------------------------------------------|---------------------------------------------------|
| Inventory not yet published      | 200 + empty list, or specific 4xx         | Poll every 250 ms up to `max_poll_seconds` (30 s) |
| Inventory published, no match    | 200 + non-empty, but criteria filter empties | Try next course in `course_preferences`; else NO_INVENTORY |
| Slot gone between search & book  | book() 4xx (400/409) -> `SlotGoneError`   | Fall through to the next-ranked candidate (each re-solves a fresh CAPTCHA); the book POST itself is single-attempt, never retried |
| Transient network error          | httpx `TransportError` (read/connect)     | Hand-rolled retry in `ForeUpAdapter._send_with_retry` (linear backoff, default 2 attempts) around IDEMPOTENT calls only — book()'s POST is never wrapped |
| Rate limited (429)               | HTTP 429 + Retry-After                    | Honor Retry-After up to a 10 s cap; else abort    |
| Captcha challenge                | Adapter detects challenge response shape  | ForeUP path SOLVES the reCAPTCHA via the 2captcha provider (ToS posture reversed, §7/§12); a solve failure raises `CaptchaError` -> notify, stop. |
| Auth failed                      | 401/403 on login                          | One retry after 2s (transient JWT). Then `AUTH_FAILED`, **lock cooldown** (§8.1). |
| Account lockout risk             | Three login failures in 1 hour            | Halt all runs for 24 h; record in store; notify.  |
| Partial-book state (booked but no confirmation) | book() raised but POST may have landed | See §9. |
| Mid-run runner kill              | Process gone                              | Next run has no in-memory state; §9 layer 2 (`list_reservations`) detects any existing reservation before re-POSTing. |
| Already booked (same run)        | In-process `booking_history` has a terminal BOOKED for this (RequestId, date) | Return cached result; no network calls. |

### 8.1 Account-lockout cooldown
Three consecutive `AuthError` outcomes for the same course within 1 hour triggers a 24 h cooldown. Because state is in-process only, the cooldown cannot persist across runs — repeated failures in separate runs will each retry login. The orchestrator halts the current run and notifies; a human must disable the workflow before re-enabling.

---

## 9. Double-book prevention

This is the subtlest correctness property. Scenario: bot calls `POST /reservations`, the request lands and creates a booking, but the response is lost (TCP reset, Lambda-style timeout). Naively, retry produces a second booking.

**Defense, in order:**

1. **Pre-flight in-process idempotency.** Before any work: `store.get_terminal(request_id, date)`. If a terminal `BOOKED` exists in this run's in-memory store, return it. (Guards against re-entrant calls within the same process — unlikely but cheap.)
2. **Pre-book remote check.** Right before POST, the adapter calls `list_reservations()` and aborts if a reservation already exists for the target date. **This is the load-bearing double-booking guard** — it works regardless of whether a prior run's history is available.
3. **Single attempt per slot, by default.** `book()` is non-retryable EXCEPT on `SlotGoneError`. A book-POST **4xx** (both `409` and `400`) is mapped to `SlotGoneError`: a 4xx means ForeUP definitively created no reservation, so the orchestrator advances to the next-ranked slot (prod 2026-06-07: a `400` when the prime slot was claimed mid-race must NOT crash the job and abandon the other ranked slots). The full response body is logged before raising. Anything ambiguous (timeout/5xx) raises as-is and the orchestrator reaches the post-mortem path:
4. **Post-mortem reconciliation.** Any failure during/after POST that didn't return a clean adapter error triggers: wait 5 s, list reservations again, match by tee_time + party_size. If found, treat as success and persist the confirmation. If not, treat as no-op and try next course.
5. **In-process advisory lock.** `BookingStore.request_lock(request_id)` is held for the duration of `Orchestrator.run`. Attempting a second concurrent code path in the same process raises `ConcurrentRunError` immediately (no waiting).
6. **ACA Job concurrency.** ACA Jobs run with `parallelism=1` — at most one replica per job runs at a time, so two cron-fired runs of the same job can't overlap. This is the cross-PROCESS serialization layer — it replaces any need for a cross-run durable lock. (The former `book.yml` GH Actions `concurrency:` group served this role before scheduling moved to ACA Jobs.)

This is belt-and-suspenders by design. The single most important rule: **after any POST whose result is uncertain, ALWAYS reconcile via list-reservations before doing anything else.**

### 9.1 Booking state machine (M2 implementation contract)

The orchestrator's per-(course, slot) booking attempt is a state machine with explicit write-to-store-first transitions (in-memory). Implementers MUST encode these states verbatim — no derived enum, no "happy path skips a state":

```
                      +-------------+
                      |  PRE_BOOK   |  layer 1 idempotency check
                      +------+------+  layer 2 list_reservations
                             |          (writes attempt_log row)
        already-exists       |
        on remote ----+      | clear
                      |      v
                      |   +---------+
                      |   | POSTING |  single book() call in flight
                      |   +----+----+  (writes BOOK_POST attempt_log row
                      |        |        BEFORE the await)
                      |        |
            +---------+--------+--------+----------+
            |                  |        |          |
            v                  v        v          v
        SlotGoneError       clean   network/    no exception
        (retryable)         success ambiguous   but suspicious
            |                  |    response       response
            v                  v        |          |
   RETRY_DIFFERENT_SLOT     BOOKED      v          v
   (re-search, pick                +-----------+
   next; max once)                 | UNCERTAIN |
            |                      +-----+-----+
            +-> back to PRE_BOOK         | (writes UNCERTAIN row)
                                         | wait reconcile_delay_s (5s)
                                         v
                                  +--------------+
                                  | RECONCILING  | calls list_reservations
                                  +------+-------+ matches by (tee_time,
                                         |         party_size, course_id)
                          +--------------+--------------+
                          |                             |
                          v                             v
                       match found                no match
                          |                             |
                          v                             v
                     BOOKED                          LOST
                  (write terminal,             (record terminal NO_INVENTORY
                   confirmation from            for THIS course+slot only;
                   reconcile)                   orchestrator MAY try next
                                                course but MUST NOT retry
                                                this same course in the
                                                same run — phantom-booking
                                                risk)
```

**Invariants:**

1. `book()` is called AT MOST ONCE per `POSTING` entry. Re-entering `POSTING`
   requires going through `PRE_BOOK` (which re-runs `list_reservations` —
   layer 2). Two concurrent `POSTING` states for the same RequestId are
   impossible because `request_lock` (layer 5) is held for the whole run AND
   the orchestrator is single-threaded within a run.
2. Every state transition appends to the in-memory `attempt_log`. If the
   runner is killed between two states, the in-memory log is lost; the next
   run has no UNCERTAIN state to resume from — instead it re-runs `PRE_BOOK`,
   hits `list_reservations` (layer 2), and discovers any existing booking
   before ever POSTing again. Never re-POST without going through PRE_BOOK.
3. From `UNCERTAIN`, the only legal next state is `RECONCILING`. The
   orchestrator MUST NOT call `book()` again before reconcile completes.
4. `LOST` is terminal for THIS slot. The orchestrator may pick another
   course (next in `course_preferences`), but never the same (course, slot).

The Protocol's `book()` docstring references this diagram. `list_reservations`
is part of the Protocol from M0 (review item 3) — not deferred to M2.T3.

### 9.2 Cross-run double-booking defense

`InMemoryStore` does not persist across runs — each run starts with no booking history.
The SINGLE source of truth for "do I already have a booking on this date?" is the live
`list_reservations()` call at PRE_BOOK (layer 2). This is by design: a stale local
record can go wrong in both directions (false "already booked" OR false "nothing booked"),
while the live remote check is always authoritative.

Cross-PROCESS serialization is enforced at the scheduler level:
- **ACA Jobs:** `parallelism=1` — at most one replica per job runs at a time. (Before
  scheduling moved to ACA Jobs, the removed `book.yml`/`watch-tee-time.yml` workflows used a
  GH Actions `concurrency:` group for this.)

A mid-run runner kill leaves no UNCERTAIN state on disk. The next run re-authenticates,
calls `list_reservations`, and sees any booking that landed — no phantom booking risk.

---

## 10. Observability

- **Logs** via the stdlib `logging` module -> plain text to stderr (NOT `structlog`/JSON; that dep was dropped). Captured by the ACA Job logs; tailed locally. Critical-path lines (T0 fire, DST/booking-day skip, booking outcome, NO_INVENTORY) are greppable.
- **In-memory event log** (`attempt_log`). Every state transition (per §9.1 state machine) gets an entry: `T_RACE_BEGIN`, `SEARCH_START`, `SEARCH_OK`, `SEARCH_EMPTY`, `PRE_BOOK`, `BOOK_POST`, `UNCERTAIN`, `RECONCILING`, `BOOKED`, `LOST`, etc. Lives in process memory only; useful for within-run correlation and log emission.
- **Notify-on-failure** is the alert. Non-`BOOKED` outcomes print to console (ConsoleNotifier). The golf course sends booking confirmation emails directly to the user's account email.
- **Metric surface (future, UNIMPLEMENTED):** count + duration of each event keyed by course; alert if `T_RACE_BEGIN -> BOOK_OK` latency exceeds 5s for two consecutive runs. v0/v1 ship no metrics emission or latency alerting — state is in-process and ConsoleNotifier is the only sink.

### 10.1 PII handling (review failure mode "Player PII in attempt_log")

`attempt_log` entries are written by the orchestrator into the in-memory store (never to disk). Even so, the redaction rule is mandatory — structured log output goes to GH Actions / ACA Job logs, which can be visible to anyone with repo read access.

**Rule: redact before write.** Before appending any payload that originated
from `BookingRequest.players` or `CourseCredentials`, the orchestrator MUST:

- Replace `email`, `phone`, `member_number` with SHA-256 prefixes (first 8 hex
  chars of the digest) — enough to correlate two log rows referring to the
  same user, not enough to recover the value.
- NEVER write `password` (raw or hashed). It does not belong in attempt_log.
- NEVER write TeeItUp **card fields** — `card_number`, `cvv`, `expiry_month`,
  `expiry_year`, `billing_address`, `billing_postal_code`. These must be dropped
  entirely (not hashed). Raw PAN/CVV in any log is a PCI incident.
- Confirmation codes ARE persisted (we need them for reconciliation).

A helper `redact_payload(d: dict) -> dict` lives in `core/redaction.py` (recursive — nested
dicts AND lists). It drops to `"***"` both the CARD fields (the `Payment.*`/`Payments_*` GNSVC
namespace + the cred-style keys: card_number, cvv, expiry_*, billing_*, name_on_card, password)
AND player PII (email, phone, mobile, member number, first/last name). (It DROPS PII rather than
SHA-256-hashing it — stronger for an audit blob, since a hash of a low-entropy phone number is
reversible.) `BookingStore.append_attempt` applies `redact_payload` at the store boundary on
every write, so redaction is non-bypassable — a caller cannot leak card data by forgetting to
scrub. NOTE: `append_attempt` is not yet called by any flow — the post-mortem reconciliation
path (M2.T3) is the intended first caller; the store-boundary guard means its writes are
redacted by default. The guard is in place before that wiring lands.

---

## 11. Testing strategy

| Layer       | Tool                  | What we cover                                                    |
|-------------|-----------------------|-------------------------------------------------------------------|
| Unit (pure) | pytest                | models invariants, config validation, busy-wait math (FakeClock) |
| Adapter unit | respx                | ForeUP request shape, error mapping, captcha detection            |
| Adapter integration | respx + the `test_foreup_canary.py` live-drift canary | Full search+book against mocked responses; canary catches real ForeUP drift |
| Orchestrator | FakeAdapter + FakeClock + InMemoryStore | the race, fallback, idempotency |
| End-to-end dry-run | live ForeUP, `--dry-run` | All HTTP up to but not including the final POST |
| Cron lint    | actionlint            | Workflow syntax + cron expression sanity |

**6:00 AM race testing without waiting 7 days:** `FakeClock` lets a test set "now" to T0 - 1.5 s and assert the orchestrator's first `search()` lands within ±50 ms of T0. This is the spec for `clock.busy_wait_until`.

**Live-drift policy:** there are no recorded cassettes (vcrpy was dropped in #82 — cassettes were never recorded). `tests/test_foreup_canary.py` is an opt-in `integration`-marked canary that hits live ForeUP (gated on real creds) to catch endpoint/shape drift; respx mocks cover the deterministic adapter-unit layer.

---

## 12. Anti-bot etiquette & ToS posture

> **Posture changed since the original plan.** The original v0 plan stated the
> bot would NOT solve captchas, would NOT impersonate a browser, and would NOT
> handle card data. Supporting live ForeUP and TeeItUp booking required all
> three, and the implementation now does them. This section documents reality;
> the higher ToS/detection/PCI exposure is accepted for single-user personal use.

What we DO:
- For **API calls**, an honest `User-Agent: TeeTimeBooker/0.0.0 (+contact)` (`foreup/base.py`).
- Self-imposed minimum 250 ms between calls outside the T0 race window.
- Honor `Retry-After`. Cap polling at 30 s after T0.
- One booking per request, ever (idempotency + reconciliation).
- **Solve the ForeUP reCAPTCHA** on the booking step via a third-party human-solver service (2captcha) (`foreup/captcha.py`). Delegating the solve to a paid human/AI solver pool is itself a deliberate circumvention of a technical control — see §12 ToS posture. (A headless-Playwright fallback that impersonated a browser to pass reCAPTCHA's automation scoring was removed: it was unreliable and the deployed image carries no browser.)
- **Handle card data** for TeeItUp (PAN/CVV passed to the payment endpoint each booking; see §7).

What we still DO NOT:
- Rotate IPs / use residential proxies.
- Create multiple accounts.
- Hammer login on auth failure.
- Bulk-book-and-cancel (the scalper pattern). One booking per request.

**ToS posture (stated explicitly).** The user has a legitimate account and is automating a booking they are eligible to make. ForeUP's public stance ([zendesk article](https://foreup.zendesk.com/hc/en-us/articles/34034774453403-Preventing-Bots-From-Making-Tee-Times)) is that bot-driven bookings are unwelcome and they may add countermeasures. Risk areas the user accepts:
- Solving the captcha and impersonating a browser to do so are evasions of a technical control; this materially raises ToS/detection risk versus the original notify-and-stop design.
- The course may revoke the user's online booking privileges if they detect bot activity. Accepted for v0.
- ForeUP's new "One-Time Booking Code" countermeasure (announced 2025) could break the booking POST without warning; the bot should fail loudly, not attempt to defeat it.

---

## 13. Stop conditions

The orchestrator stops the current run when ANY of:
1. A `BOOKED` outcome is achieved for any (course, date) pair satisfying the request.
2. All courses in `course_preferences` have been tried and yielded `NO_INVENTORY` / `PRICE_REJECTED` / non-retryable error.
3. `max_poll_seconds` exceeded with no inventory anywhere.
4. A `CAPTCHA_BLOCKED` or post-cooldown `AUTH_FAILED` is observed.
5. Wall-clock budget exceeds 5 minutes from T0 (hard ceiling — runner timeout is 15 min, leaves slack for notifier).

Across runs (multiple days):
- There is no durable cross-run record. Each run's pre-book `list_reservations` check (§9 layer 2) detects an already-booked slot and aborts before POSTing again — this is the cross-run idempotency guard.
- A 24 h auth cooldown (§8.1) is enforced within a run only; there is no persistent cooldown across runs.

### 13.1 RequestId derivation rule (review item 5)

`RequestId` is a UUIDv5 derived from a deterministic config fingerprint via
`teetime.core.models.derive_request_id`. The fingerprint string format:

    course_ids|target_offsets|time_windows|party_fingerprint

- `course_ids`: sorted, comma-joined values from `request.course_preferences`
  (NOT from `[[courses]]` — standby courses in `[[courses]]` that aren't in
  `course_preferences` must not change the RequestId).
- `target_offsets`: sorted, comma-joined integers (e.g. `"7"` or `"7,14"`).
- `time_windows`: sorted by `(earliest, latest)`, joined as `"HH:MM-HH:MM"`,
  comma-joined.
- `party_fingerprint`: sorted, comma-joined `(first_name|last_name)` per
  player. Email/phone are EXCLUDED (so rotating a player's contact info
  doesn't break idempotency).

**Resolved dates are excluded from the fingerprint.** The reason: `target_offsets = [7]` books
each wanted day 7 days out (the wanted weekdays are derived from the per-day windows, currently
Sat+Sun); the resolved date changes but the goal is the same. The in-process idempotency key is
`(RequestId, resolved_date)` — composite. This:

- Lets the daily cron book day N+7 and a later run book day N+14 without conflict within a run.
- Keeps the user's "I always want these morning windows 7 days out" rule a single
  RequestId for the §9 layer-5 in-process advisory lock.
  (NOTE: the per-day window WEEKDAY is now in the fingerprint — a Sat vs Sun window is a
  distinct RequestId; see PERDAY_WINDOWS_PLAN §6.)
- A BOOKED terminal for resolved_date X does NOT block resolved_date Y for the same
  RequestId within the same run. (See review failure mode "Two target_dates with overlapping
  windows" — explicitly NOT closing all dates.)

---

## 14. Bot identity

| Item              | Value (v0)                                                       |
|-------------------|------------------------------------------------------------------|
| User-Agent        | `TeeTimeBooker/0.0.0 (+https://github.com/alanc3939/TeeTimeBooker)` |
| `api-key` header  | `no_limits` for search; `""` (empty) for login POST — confirmed by browser capture (S1) |
| Accept-Language   | `en-US,en;q=0.9`                                                 |
| Session lifetime  | One HTTP session per orchestrator run; reused across search+book. JWT is NOT cached across runs — the weekend-only cron runs at least every few days, far exceeding any reasonable JWT TTL, and re-login is cheap. `cache_session`/`load_session` exist on the Protocol but are deferred to v1 (review item 10). |
| Cookies           | PHPSESSID + ForeUP JWT, held in `session_cache` in-memory for the duration of the run. |
| Concurrency       | At most 1 in-flight HTTP request per adapter at a time.          |

---

## 15. Deployment / runbook

> **Superseded by v1 (Azure).** The v0 GitHub Actions operation below is historical: the
> `book.yml` / `watch-tee-time.yml` cron workflows were **removed in #43**, and the bot now
> runs as Azure Container Apps Jobs (daily booking crons + booking-day gate). The authoritative deploy + verification +
> cutover runbook is **infra/AZURE_PLAN.md §10**; the DST gate now lives in `core/dst_gate.py`.

**v0 (historical):**
1. Set GH Actions repo secrets: `MB_USERNAME`, `MB_PASSWORD`, plus per-player `PLAYER1_EMAIL`, `PLAYER1_PHONE` etc. as referenced by `config/local.toml`.
2. Commit `config/local.toml` to a private fork OR pass via `workflow_dispatch` input file.
3. Cron fired Sunday at 6:00 AM ET; a `dst` step gated on ET wall-clock hour == 5 (that gate now lives in `core/dst_gate.py`). Wrong DST-half cron exits early as success.
4. After each run, check the GH Actions log output (ConsoleNotifier). No state is preserved between runs — each run begins fresh and relies on `list_reservations` to detect any existing booking (§9.2).
5. On `CAPTCHA_BLOCKED` or repeated `AUTH_FAILED`: `gh workflow disable book-tee-time`. Investigate manually. Re-enable with `gh workflow enable book-tee-time`. NO auto-re-enable — a human MUST confirm the cause is resolved.

**v1 upgrade path — Azure** (ratified; see `infra/AZURE_PLAN.md` for the authoritative design):
- **Azure Container Apps Jobs** (Consumption) on cron for the trigger — more reliable firing than GH Actions cron, though still cron, not a sub-second scheduler. Two booking jobs (DST halves) + one watch job.
- **Same `InMemoryStore`** at runtime — no Azure SDK calls needed for state. Cross-process serialization is handled by ACA Job `parallelism=1`.
- **Azure Key Vault** for credentials, injected via native `keyVaultUrl` secret references resolved by a user-assigned managed identity.
- Logs/metrics into **Log Analytics + Application Insights**.
- IaC in **Bicep**; CI deploy via **OIDC federated credential** (no client secret).
- *Rejected:* Azure Functions (cold-start variance fights the race), service-principal secrets (rotation burden). See `infra/AZURE_PLAN.md` §2 for the full comparison.

---

## 16. Milestones & tasks

Tasks are sized for a single focused agent session. Dependencies are explicit. Where two tasks list each other in **No deps** they can run in parallel.

### M0 — Repo bring-up (DONE in this PR)
| ID    | Task                                                                                  | Inputs           | Outputs                    | Owner-files | Deps |
|-------|---------------------------------------------------------------------------------------|------------------|----------------------------|-------------|------|
| M0.T1 | Lay down package skeleton, pyproject.toml, stubs, example.toml, workflow shell, CLAUDE.md | this plan        | the files in this PR       | all stubs   | —    |

### M1 — Foundations (DONE)
| ID    | Task                                              | Inputs                       | Outputs                              | Owner-files                                       | Parallelizable with |
|-------|---------------------------------------------------|------------------------------|--------------------------------------|---------------------------------------------------|---------------------|
| M1.T1 | Implement `Clock` (`RealClock`, `busy_wait_until`) + `FakeClock` test util + `tests/conftest.py::fake_clock` fixture body | `core/clock.py` stub         | working clock + tests proving ±50 ms accuracy under FakeClock; loop yields each fine-step | `core/clock.py`, `tests/test_clock.py`, `tests/conftest.py` | M1.T2, M1.T3 |
| M1.T2 | Implement `core/config.py::load` (incl. `PlayerConfig`, `target_offsets` -> resolved-date helper, env-var ref resolution for player PII) | `config/example.toml`        | TOML round-trip; secret-env resolution; clear errors on missing env; PlayerConfig.email_env -> Player.email | `core/config.py`, `tests/test_config.py` | M1.T1, M1.T3 |
| M1.T3 | Wire CLI (`teetime run`, `teetime show-config`) via Click | `core/config.py` shape       | `__main__.py` real impl              | `src/teetime/__main__.py`, `tests/test_cli.py`    | M1.T1, M1.T2 |

### M2 — Orchestrator core (PARTIAL — M2.T3 reconciliation pending)
| ID    | Task                                                | Inputs                  | Outputs                                                              | Owner-files                                | Deps        |
|-------|-----------------------------------------------------|-------------------------|----------------------------------------------------------------------|--------------------------------------------|-------------|
| M2.T1 | Implement `Orchestrator.run` with FakeAdapter, **InMemoryStore** (already a stub in `persistence/in_memory_store.py`), NoopNotifier; encode the §9.1 state machine | M1, all stubs           | end-to-end happy path, fallback path, idempotency path passing; state machine transitions match §9.1 diagram exactly | `core/orchestrator.py`, `persistence/in_memory_store.py`, `tests/test_orchestrator.py`, `tests/conftest.py` | M1.*  |
| M2.T2 | Implement the `derive_request_id` helper body (the signature already exists in `core/models.py`); fingerprint per §13.1 | M1.T2                   | given identical config, identical RequestId across processes         | `core/models.py` (helper body), `tests/test_request_id.py` | M1.T2 |
| M2.T3 | Implement post-mortem reconciliation: orchestrator calls the **already-defined** `adapter.list_reservations()` on UNCERTAIN, drives RECONCILING → BOOKED/LOST per §9.1 | M2.T1                   | reconciliation works against a scripted FakeAdapter that simulates connection-drop after server-side commit | `core/orchestrator.py` (no Protocol changes — `list_reservations` already on Protocol) | M2.T1 |

### M3 — Persistence — **DROPPED**

> Dropped. `SqliteStore` and `sqlite_store.py` were removed from the codebase. The project
> uses `InMemoryStore` permanently. Cross-run idempotency is handled by `list_reservations`
> (§9 layer 2), not a durable store. No tasks remaining.

### M4 — Notifications — **DROPPED**

> Dropped. `EmailNotifier` / SMTP were removed from the codebase. The only notifier is
> `ConsoleNotifier` (already done). The golf course emails booking confirmations directly
> to the user's account. No tasks remaining.

### M5 — ForeUP adapter (DONE — live dry-run confirmed)
| ID    | Task                                                        | Inputs              | Outputs                                                          | Owner-files                                                          | Deps         |
|-------|-------------------------------------------------------------|---------------------|------------------------------------------------------------------|----------------------------------------------------------------------|--------------|
| **S1** | **Spike: confirm ForeUP endpoints, request shapes, captcha posture, schedule_id** for Mangrove Bay | live ForeUP, browser devtools | the `test_foreup_canary.py` live-drift canary + a 1-page note in `docs/foreup-spike.md` (committed) | `tests/test_foreup_canary.py`, `docs/foreup-spike.md`              | M1.*         |
| M5.T1 | Implement `ForeUpAdapter.authenticate`                      | S1 findings         | auth round-trip; JWT extracted; AuthError on bad creds          | `courses/foreup/base.py`, `tests/test_foreup_auth.py`                | S1           |
| M5.T2 | Implement `ForeUpAdapter.search` + criteria filtering       | S1, M5.T1           | parse `/api/booking/times`; map to `TeeTimeSlot`; raise InventoryNotPublishedError for empty-pre-T0 | `courses/foreup/base.py`, `tests/test_foreup_search.py` | M5.T1        |
| M5.T3 | Implement `ForeUpAdapter.book` + `ForeUpAdapter.list_reservations` (Protocol method already defined in `core/adapter.py`; M5.T3 implements its body) | S1, M5.T1, M5.T2    | book POST happy path; conflict → SlotGoneError; list_reservations returns matching ExistingReservation; orchestrator-driven reconciliation works end-to-end | `courses/foreup/base.py`, `tests/test_foreup_book.py`                | M5.T2        |
| M5.T4 | Captcha + rate-limit detection                              | S1                  | adapter raises `CaptchaError` / `RateLimitError` from canned responses | `courses/foreup/base.py`, `tests/test_foreup_protection.py`           | M5.T1        |
| M5.T5 | Wire `MangroveBayAdapter` and confirm `schedule_id`         | S1                  | end-to-end dry-run against live ForeUP succeeds                 | `courses/foreup/mangrove_bay.py`                                     | M5.T1..T4    |

### M6 — End-to-end (depends on M2 + M5)
| ID    | Task                                              | Inputs           | Outputs                                              | Owner-files                          | Deps        |
|-------|---------------------------------------------------|------------------|------------------------------------------------------|--------------------------------------|-------------|
| M6.T1 | Real-timing booker wiring + DST gate + watcher enable (PRs 1–6). **DONE** (the Sunday-only schedule + `target_weekday` anchor were later SUPERSEDED by the multi-day re-arch — daily crons + booking-day gate + per-day windows). | all stubs done | `run --wait` busy-waits to 06:00:00 ET (`core/dst_gate.py`, `bookingReplicaTimeout=1200`); watcher enabled. Full suite green. | `__main__.py`, `core/dst_gate.py`, `core/booking_day_gate.py`, `compute.bicep`, configs | M5.* |
| M6.T2 | First production dry-run against Mangrove Bay     | M6.T1            | dry-run log proof (AZURE_PLAN §10.4): `race: busy-wait complete` + watcher `Watch check`/`DRY_RUN`; one clean dev dry-run Sunday | runbook §10.4 | M6.T1 |
| M6.T3 | First live booking (prod cutover §10.5)           | M6.T2 green      | **Prod DEPLOYED** (jobs live, `dryRun=false`, secrets set, watcher + auto-upgrade on; latest infra tag `infra/v1.1.1`). A real booking race ran **2026-06-07** (fired at T0 but lost on CAPTCHA latency → fixed in #67/#68). The multi-day + per-day code is on `main` but awaits its own `infra/v*` prod tag. | runbook §10.5 | M6.T2 |

**Parallel-execution map (post M1):**
```
M2.T1 ─┐
        ├─► M6.T1 ─► M6.T2 ─► M6.T3
S1    ──► M5.T1 ─► M5.T2 ─► M5.T3 ─► M5.T5 ─┘
                       └─► M5.T4 ─┘
```

---

## 17. Spikes (open questions, time-boxed)

| ID  | Question                                                                                    | Exit criterion                                                                    | Suggested time |
|-----|---------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|----------------|
| **S1 (DONE)** | Are the ForeUP endpoints documented in `courses/foreup/base.py` correct and stable for Mangrove Bay? Does login carry CSRF? Is there a captcha on login or only on booking? What is the exact `schedule_id`? Is `api-key: no_limits` the right header value to send in 2026? Does `list_reservations` exist as a single GET, or must we paginate? | **Confirmed via live browser capture + httpx testing:** `schedule_id=2149`; login uses `api_key=""` (empty) + `booking_class_id=12239` (Public class from SCHEDULES JSON); search uses `api_key="no_limits"`; no CSRF token required; no login captcha observed; `/api/booking/users/reservations` is a single GET (no pagination). Cassettes not recorded (respx unit tests cover the shapes instead). Live dry-run `outcome=dry_run` confirmed. | 1 session |
| S2  | Chronogolf API shape for any future backed-on-Chronogolf course (deferred — no v0 course needs it). | Documented endpoints + auth flow when first such course is added.                | future         |
| S3  | Does ForeUP rate-limit pre-T0 polling? Could a 5 ms poll interval get the user banned?      | Empirical: poll at progressively faster rates from 1000 ms down; record response codes / latency. | 1 session |

---

## 18. Adversarial review checklist

Each item from the brief, addressed:

- **GH Actions cron jitter (~1–15 min):** §6.2. Fire 10 min early; bot's busy-wait nails T0. Worst case (no run that day) is accepted; v1 mitigation is Cloud Scheduler.
- **DST and 6:00 AM ET:** §6.3. Two crons + DST-half check + `zoneinfo` for actual T0 computation. Math shown.
- **Double-booking risk:** §9. Six layers of defense; reconciliation is the load-bearing one.
- **ForeUP captcha:** §8 row "Captcha challenge"; §12. The booking step is gated by an invisible reCAPTCHA, which the bot solves via 2captcha — see `foreup/captcha.py`. S1 confirmed no login captcha. Caveat: a synchronous solve (~15–30 s) in the booking POST can blow the T0 race window — see §19 / `infra/AZURE_PLAN.md` for the pre-fetch consideration.
- **Account lockout:** §8.1. Three auth failures → halt current run + notify (in-run only; no durable cooldown across runs).
- **Credit-card storage:** §7. **By platform.** ForeUP: none (card-on-file). TeeItUp: PAN/CVV/expiry/billing are passed to `tr.gnsvc.com` on each booking (no wallet); sourced from env vars, never committed, and dropped by `redact_payload` at the `append_attempt` store boundary on every `attempt_log` write (§10.1).
- **ForeUP ToS:** §12, stated honestly. Risks accepted by user.
- **Concurrency / accidental double trigger:** §9 layers 5 & 6. ACA Job `parallelism=1` (one execution per job) + in-process advisory lock. `ConcurrentRunError` fails fast.
- **Mid-run runner kill:** Next run has no prior state; §9 layer 2 (`list_reservations`) catches any existing booking before re-POSTing. See §9.2.
- **Testing the 6 AM race without waiting 7 days:** §11. `FakeClock` + `clock.busy_wait_until` test asserts ±50 ms.
- **Stop conditions:** §13.
- **Bot identity:** §14 + §12.

---

## 19. Open risks (eyes open)

1. **GH Actions doesn't actually fire.** Mitigation in v1; v0 accepts the loss.
2. **ForeUP changes endpoints.** Adapter is one file; the `test_foreup_canary.py` live-drift canary goes red loud. Manageable.
3. ~~**`api-key: no_limits` is a known-bot signal.**~~ Resolved in S1: login uses `api_key=""` (empty); search uses `api_key="no_limits"`. No adverse response observed.
4. **The user's ForeUP account gets restricted.** §12. Accepted v0 risk; would invalidate v0 entirely until manual unlock.
5. **Time-window picker logic ("best slot")** is under-specified. v0 picks the slot whose `tee_time` is closest to the midpoint of the user's window. Revisit in v1 with explicit ranking config.
6. ~~**Cross-run cache eviction.**~~ N/A — there is no cross-run state cache. Each run relies on `list_reservations` (§9 layer 2) as the authoritative source of booking state.
7. **DST spring-forward day** (review failure mode). 06:00 ET still exists on 2nd Sunday of March (the skipped hour is 02:00–03:00). Add `tests/test_dst_edge.py::test_spring_forward_t0_resolves` in M1.T1: assert `zoneinfo` returns 06:00 EDT on March 8 2026 with no ambiguity exception.
8. **Workflow disabled by `gh workflow disable` after CAPTCHA_BLOCKED** (review failure mode). No automation re-enables it. Documented runbook step §15: after manual investigation, run `gh workflow enable book-tee-time`. We do NOT auto-re-enable — a human MUST verify the cause is gone.
9. ~~**`api-key: no_limits` is a community-observed magic string.**~~ Resolved in S1: search header confirmed; login uses empty string. Behaviour stable as of 2026-04-29.

### 19.1 Disagreements with v0 review

- **Item 11 (asyncio_mode + @pytest.mark.asyncio)**: agreed and fixed; the marker was redundant under `asyncio_mode = "auto"`.
- **Item 10 (defer cache_session)**: agreed and applied — weekend cron means JWT cache buys nothing in v0.
- **Nit (cron defaults `dry_run=false`)**: KEEPING this. With the DST gate fixed (item 1), only one cron of the day proceeds. The user explicitly wants real bookings on cron — the bot's purpose is to book, not dry-run. Manual `workflow_dispatch` defaults to `dry_run=true` (safe testing default). This is intentional asymmetry.

---

## 20. Feature milestones (v0.5 — watch, one-booking, sort)

These three features build on the completed v0 foundation (M1, M2 partial, M5 done; M3/M4 dropped; M6 pending).
They share a dependency: M-feature-3 (sort) is a prerequisite for both M-feature-1 and
M-feature-2 because those features select slots and must use the same ranking logic.

**Parallel-execution map:**
```
M-feature-3 ──► M-feature-1.T1 ──► M-feature-1.T2 ──► M-feature-1.T3 ──► M-feature-1.T4
                                                                          (GH Actions + ACA)
               M-feature-2.T1 (Spike S4) ──► M-feature-2.T2 ──► M-feature-2.T3
                                                                ──► M-feature-2.T4 ──► M-feature-2.T5
```
M-feature-1 and M-feature-2 share Spike S4 (cancel endpoint). M-feature-2.T3 depends on
M-feature-1.T2 (WatchOrchestrator.check_once must exist for UpgradeOrchestrator to be called from it).

---

### M-feature-3 — Slot Ranking Within the Window (DONE)

**Design decision (as shipped, commit dc3ae48):** rank slots by **distance from the
window midpoint**, tie-broken by ascending tee_time. For the 08:45–10:00 ET window
(midpoint 09:22:30) the slot closest to 09:22:30 wins. The ranking lives in
`core/slot_utils.py::rank_slots_for_request` and is shared by all three orchestrators.

> **History / correction:** an earlier draft of this milestone proposed replacing
> midpoint with a plain ascending tee_time sort. That pivot was reversed in commit
> dc3ae48, which re-adopted midpoint-distance (and widened the window to 08:45–10:00).
> The code and CLAUDE.md use midpoint-distance; this section was stale and is now
> corrected. The shared ranking also moved out of `Orchestrator._rank_slots` into
> `slot_utils.py` so the watch/upgrade orchestrators reuse it.

| ID | Task | Inputs | Outputs | Owner-files | Deps |
|----|------|--------|---------|-------------|------|
| M-feature-3.T1 | Write failing tests for ascending-time sort in `tests/test_sort_priority.py` (stub already on disk — remove NotImplementedError from each test and add real assertions, then verify they fail against the midpoint-sort implementation) | `test_sort_priority.py` stub | red test suite for Feature 3 | `tests/test_sort_priority.py` | M1.* done |
| M-feature-3.T2 | Implement ascending sort in `Orchestrator._rank_slots`; remove `_window_midpoint_utc` | M-feature-3.T1 (red tests) | green tests; `mypy --strict` passes | `src/teetime/core/orchestrator.py` | M-feature-3.T1 |
| M-feature-3.T3 | Update `tests/test_orchestrator.py` for any assertions that relied on midpoint ordering (e.g. "picked hour=8 when both 7 and 8 exist") | M-feature-3.T2 | all orchestrator tests green | `tests/test_orchestrator.py` | M-feature-3.T2 |

**NOTE:** shipped as midpoint-distance sort in `core/slot_utils.py` with coverage in
`tests/test_sort_priority.py`. Tasks below are retained for history.

---

### M-feature-1 — Cancellation Monitor (Watch Job) (DONE)

**Design decisions:**

**Polling interval:** 10 minutes (600 s), minimum floor 5 minutes (300 s). Rationale: the
watch job is NOT racing anyone — it monitors for cancellations on a day that has already
opened at 6 AM. Cancellations at Mangrove Bay are rare; a 10-minute poll is respectful and
keeps us well within any reasonable rate-limit threshold. PLAN.md §12 ("self-imposed minimum
250 ms between calls OUTSIDE the T0 race window") sets no upper bound on inter-poll gaps;
10 minutes is far above the 250 ms floor. At 10 minutes, 144 polls/day — the HTTP footprint
of a user checking the ForeUP website roughly every 10 minutes, which is unremarkable.

**ACA Job scheduling:** ACA Jobs are batch jobs (start, run, exit). They are NOT long-running
processes. The watch job runs as a separate ACA Job with cron `*/10 * * * *` (every 10 min).
Each invocation: check once, exit. This costs ~$0 per run under ACA Consumption pricing (no
minimum, sub-second execution billed to the sub-cent). A long-running process alternative
(ACA Container App) would cost ~$5-10/month idle — unacceptable for a twice-weekly use case.

**NOTE on `*/10` cron imprecision (GH Actions):** GH Actions `*/10` may not fire at exactly
:00/:10/:20 — real-world intervals can be 10-20 minutes depending on runner load. The "144
polls/day" figure in the polling-interval rationale assumes exact 10-minute intervals and is
an upper bound. This is acceptable: the watch job is opportunistic (finding cancellations),
not racing a fixed window. ACA Job crons have better reliability guarantees than GH Actions.

ACA Job execution time limit: the Consumption plan imposes a 10-minute timeout by default
(configurable up to 600 seconds via `replicaTimeout`). Our invocation takes at most ~5 seconds
(one search HTTP call), so there is no timeout risk.

**DST for the watch cron:** The watch cron (`*/10 * * * *`) fires every 10 minutes regardless
of DST. It does not need a DST gate because it is not racing a wall-clock window — it just
polls whenever it fires. (SUPERSEDED: the original 7 AM–10 PM polling-hours gate was REMOVED
in the multi-day re-arch — the watcher now polls on every run so it sees the 6 AM drop and
early-morning cancellations; only the past-deadline gate remains.) No extra cron entries needed.

**Scheduler (SUPERSEDED — the `watch-tee-time.yml` GH Actions workflow was REMOVED):** the
watch runs only as an ACA Job cron (`*/10 * * * *`), year-round, with no time-of-day gate (the
WatchOrchestrator polls on every run; only the past-deadline gate remains). The original plan
ran a separate GH Actions workflow gated on 7-22 ET polling hours — both the workflow and the
polling-hours gate are gone.

**Race condition with 6 AM run:** Both the watch job and the 6 AM booking job acquire
`request_lock` before any mutating operation. ACA Jobs run at most one replica simultaneously
(parallelism=1; the ACA Job `concurrency` settings prevent simultaneous watch runs). A watch
run that fires simultaneously with the 6 AM run will lose the advisory
lock and exit cleanly (ConcurrentRunError caught, returns None). The next 10-minute poll
will see the BOOKED terminal and short-circuit.

**Polling hours gate (SUPERSEDED — removed in the multi-day re-arch):** Originally polling was
suppressed 10 PM–7 AM course-local as anti-bot etiquette (§12). This gate was REMOVED so the
watcher polls on every run (it was blinding us at the 6 AM drop and to early-morning
cancellations); the 10-min cron cadence + the `poll_interval_s >= 300` floor remain the rate limit.

**Watch deadline:** Polling stops when `now > target_date midnight (course-local)`. The round
has passed; the booking window has closed.

**State management:** The watch job queries the live course via `list_reservations` to determine booking state — it does not rely on a persisted BookingStore record across runs.
After the 6 AM run completes with any non-BOOKED outcome (NO_INVENTORY, DRY_RUN), the date
is eligible for watching. Within a run, the WatchOrchestrator checks `store.get_terminal(request_id, date)`:
- BOOKED → short-circuit, return that result (nothing to watch for)
- None / NO_INVENTORY → proceed with check

| ID | Task | Inputs | Outputs | Owner-files | Deps |
|----|------|--------|---------|-------------|------|
| M-feature-1.T1 | Add `WatchConfig` to `AppConfig` (pydantic model + TOML section `[watcher]`); `WatchConfig` validation tests; `teetime watch` CLI command stub | `src/teetime/core/config.py` (done), `src/teetime/__main__.py` | `teetime watch --config ... --date YYYY-MM-DD` CLI entry; WatchConfig validation tests green | `src/teetime/core/config.py`, `src/teetime/__main__.py`, `tests/test_config.py` (extended) | M1.* |
| M-feature-1.T2 | Implement `WatchOrchestrator.check_once` with full §9.1 state machine for any booking POST; deadline gate; idempotency guards; error handling contract (NOTE: the polling-hours gate this row mentions was later REMOVED in the multi-day re-arch) | `tests/test_watch_orchestrator.py` (red tests on disk) | all watch tests green; `mypy --strict` passes | `src/teetime/core/watch_orchestrator.py`, `tests/test_watch_orchestrator.py` | M-feature-3.T2 |
| M-feature-1.T3 | ~~GH Actions `watch-tee-time.yml`~~ (SUPERSEDED) — shipped as an ACA Job watch cron instead; the GH Actions watch workflow was removed | M-feature-1.T2 | watch ACA Job runs `*/10 * * * *` | (no `watch-tee-time.yml`) | M-feature-1.T2 |
| M-feature-1.T4 | ACA Bicep watch cron — shipped INSIDE `compute.bicep` (the `teetime-watch-job-<env>` job, `*/10 * * * *`, `replicaTimeout=300`), NOT a separate `compute-watch.bicep` module | M-feature-1.T3 | `az deployment group validate` passes | `infra/bicep/modules/compute.bicep` | M-feature-1.T3 |

**Reviewer pre-emption (adversarial checklist):**

1. **Race condition on 6 AM open:** WatchOrchestrator does NOT try to race the 6 AM window.
   It only runs AFTER T0. The `check_once` method has no busy_wait; it simply calls search()
   once and exits. It cannot accidentally compete with the 6 AM booking run because that run
   holds the advisory lock for its entire duration.

2. **Anti-bot etiquette on 10-min poll:** 600-second floor enforced in WatchConfig.__post_init__.
   (The original "polls only during 7 AM – 10 PM" gate was REMOVED in the multi-day re-arch — the
   watcher now polls every run; the 10-min cadence + 300s floor are the rate limit.) Each poll is a single HTTP GET (same as a search). The
   ForeUP terms (§12) do not prohibit checking availability; they prohibit bulk booking bots.
   One GET per 10 minutes is comparable to a human checking the website.

3. **Multiple watch invocations simultaneously (ACA):** Each watch invocation attempts
   request_lock immediately. If the lock is held (by the 6 AM job or another watch invocation),
   ConcurrentRunError is caught and the invocation exits cleanly. This is correct — the next
   10-minute interval will try again.

4. **What if the watch job fires while the 6 AM job is running?** The 6 AM job holds the
   advisory lock. WatchOrchestrator.check_once attempts the lock at entry and raises
   ConcurrentRunError, which is caught and returned as None. Safe.

5. **No shared state file:** The watch job and the main booking job each start with a fresh `InMemoryStore`. The single source of truth for booking state is the live `list_reservations()` call. ACA Job-level concurrency (one execution per job) prevents simultaneous runs of the same job; a watch+book overlap is safe because the in-process advisory lock serializes the booking phase. (The former `book.yml`/`watch-tee-time.yml` GH Actions concurrency groups were removed when those workflows were superseded by ACA Jobs.) See §20.1 Q1 (resolved — the shared-cache approach was superseded when the durable store was dropped).

---

### M-feature-2 — Account Booking Management + One Booking Policy

**Design decisions:**

**cancel_reservation() Protocol addition:** `CourseAdapter` now has a `cancel_reservation()`
method. This is a Protocol extension — all adapters must implement it. The ForeUP adapter
implements it (Spike S4 resolved — endpoint confirmed; see the cancel-before-book note below).
The FakeAdapter is fully scripted for tests. Existing adapters that do NOT support
cancellation should raise `CancelError` with a clear message.

**cancel_reservation() idempotency:** A 404 from the cancellation endpoint means the booking
is already gone — the desired post-condition is satisfied, so 404 MUST NOT raise `CancelError`.
Any other 4xx or 5xx raises `CancelError` because the booking's status is uncertain.

**"Our" vs "manual" booking detection (Option A — LOCKED IN):** `ForeUpAdapter.book()` stamps
`"TTB:" + raw_foreup_id` into `BookingResult.confirmation_code`. This is the value stored
in `booking_history` by `BookingStore.record_terminal`. `ForeUpAdapter.cancel_reservation()`
strips the `TTB:` prefix before passing the raw id to ForeUP. `list_reservations()` returns
raw server ids (no prefix) in `ExistingReservation.confirmation_code`, so `is_managed` is
False for server-sourced reservations — correct, since manual bookings will not have the
prefix. `FakeAdapter.book()` mirrors this: `BookingResult.confirmation_code` gets the `TTB:`
prefix; `_existing` stores the raw id (no prefix).

Option B (local `managed_bookings` SQLite table) is NOT needed and NOT implemented. The TTB:
prefix approach is simpler, requires no new Protocol methods, and is robust across cache
eviction (the prefix is in `booking_history` which survives). See §20.1 Q3 (resolved).

**Cancel-before-book protocol (as shipped — reversed from the original draft):**
UpgradeOrchestrator cancels the old slot BEFORE booking the new one. The original plan
called for book-before-cancel (to avoid a no-booking window), but **Spike S4 found ForeUP
rejects a second `book` POST with HTTP 400 while an existing reservation is live** — it
enforces one active booking per user per day server-side. Booking-first is therefore
impossible. To minimise the resulting ~1–2 s no-booking window, `prepare_book()` pre-fetches
the expensive CAPTCHA token (~15–60 s) BEFORE the cancel, so the cancel→book gap is just two
HTTP round-trips. If `book()` fails after the cancel, the next watch invocation recovers by
booking any available slot. See `upgrade_orchestrator.py` module docstring.

**Idempotency key on rebook:** After cancel+rebook, the new booking shares the same
`(RequestId, resolved_date)`. The `BookingStore.delete_terminal()` method (added in this
milestone) clears the old record before the new one is inserted. This MUST happen under the
advisory lock to prevent any concurrent run from observing the gap. If the process dies
between delete and re-insert, the next run's list_reservations check (§9 layer 2) sees the
new booking and records ALREADY_BOOKED — no phantom booking.

**Priority ranking:** `OneBookingPolicyConfig.priority_slots` is an ordered list where
`priority=0` is most preferred. Within a single priority index, slots are ranked by
midpoint-distance, tie-broken by tee_time (Feature 3). The upgrade only fires when a slot at a LOWER priority index
(higher preference) is available. Equal-priority slots do not trigger an upgrade.

**Priority default (when priority_slots is empty):** Derived from `course_preferences` order
in `[request]`, with the same time_window as `time_windows[0]`. This means existing users
who do not configure `[one_booking_policy]` get the expected behavior: first course in the
list is the most preferred.

**One booking invariant:** The system ensures at most one MANAGED booking exists at any time
by acquiring the advisory lock before any cancel+rebook operation. Manual bookings are never
touched. If the user manually creates a second booking, the system ignores it.

| ID | Task | Inputs | Outputs | Owner-files | Deps |
|----|------|--------|---------|-------------|------|
| **S4 (Spike)** | Confirm ForeUP cancellation endpoint. Questions: (1) What HTTP method and path? (2) What identifier does the server expect (pending_reservation_id, confirmation_code, other)? (3) Is there a notes/comments field in list_reservations that echoes user-supplied text from the booking POST? (4) Is 404 returned for already-cancelled reservations? (5) What is the cancellation window (minutes before tee time)? | Browser devtools on ForeUP booking page | Exit criterion: `cancel_endpoint`, `cancel_id_field`, `notes_field_echo`, `cancel_404_on_gone`, `cancel_window_minutes` all confirmed and documented in `docs/foreup-cancel-spike.md` | `docs/foreup-cancel-spike.md` | M5 done |
| ~~M-feature-2.T1~~ | ~~Implement `ForeUpAdapter.cancel_reservation()` using confirmed S4 endpoint; handle 404 as success; raise `CancelError` on other 4xx/5xx~~ **DONE** | S4, `tests/test_foreup_book.py` (add cancel tests) | `ForeUpAdapter.cancel_reservation` passing tests | `src/teetime/courses/foreup/base.py`, `tests/test_foreup_book.py` | S4 |
| ~~M-feature-2.T2~~ | ~~Implement `UpgradeOrchestrator._build_priority_list()` and `_current_booking_priority()`~~ **DONE** | `core/upgrade_orchestrator.py` stub | unit tests for priority list construction | `src/teetime/core/upgrade_orchestrator.py`, `tests/test_upgrade_orchestrator.py` | M-feature-3.T2 |
| ~~M-feature-2.T3~~ | ~~Implement `UpgradeOrchestrator.maybe_upgrade()` with full cancel-before-book protocol (ForeUP enforces one active booking/day, so the new slot cannot be booked while the old one is live; `prepare_book()` pre-fetches the CAPTCHA token to shrink the cancel→book gap); cancel-failure path; idempotency key handling; notification~~ **DONE** | `tests/test_upgrade_orchestrator.py` (red tests on disk), M-feature-2.T1, M-feature-2.T2 | all upgrade tests green; state machine correct under FakeAdapter | `src/teetime/core/upgrade_orchestrator.py`, `tests/test_upgrade_orchestrator.py` | M-feature-2.T1, M-feature-2.T2 |
| ~~M-feature-2.T4~~ | ~~Implement `SqliteStore.delete_terminal()`~~ **DONE / DROPPED**: `delete_terminal` is implemented on `InMemoryStore` (M3 is dropped; `SqliteStore` removed). No further work needed. | — | — | — | — |
| ~~M-feature-2.T5~~ | ~~Wire `UpgradeOrchestrator` into `WatchOrchestrator.check_once()` — after finding a slot, check if it is higher priority than the current booking; if so, delegate to `UpgradeOrchestrator.maybe_upgrade()`~~ **DONE** | M-feature-1.T2, M-feature-2.T3 | integration test: watch finds higher-priority slot, triggers upgrade | `src/teetime/core/watch_orchestrator.py` | M-feature-1.T2, M-feature-2.T3 |
| ~~M-feature-2.T6~~ | ~~GH Actions + ACA Bicep: the watch workflow already fires UpgradeOrchestrator via WatchOrchestrator — no separate job needed~~ **DONE / SUPERSEDED**: the watch schedule now runs as an ACA Job (`compute.bicep`); `watch-tee-time.yml` was removed. `one_booking_policy` is enabled via `config/container.toml`, not a CLI flag. | M-feature-1.T3, M-feature-2.T5 | one-booking policy active in the watch ACA Job (dry-run in dev) | `infra/bicep/modules/compute.bicep`, `config/container.toml` | M-feature-1.T3, M-feature-2.T5 |

**Reviewer pre-emption (adversarial checklist):**

1. **Race condition on cancel+rebook:** Cancel-before-book protocol (above). If cancel
   fails after rebook, we persist the new booking and notify the user. If rebook fails,
   the original booking is untouched. The only unrecoverable failure mode is: rebook
   succeeds AND cancel succeeds but process dies before `delete_terminal` + `record_terminal`.
   Recovery: next run's list_reservations sees the new booking and records ALREADY_BOOKED.
   The old BOOKED terminal in the store will not trigger a re-cancellation because the
   `is_managed` check is performed on the STORE RECORD (BookingResult from the store,
   which carries the TTB: prefix), not on the raw ExistingReservation from
   list_reservations() (which always has a raw server id with no TTB: prefix). If the
   process died between delete and re-insert, the store has no BOOKED terminal for that
   (RequestId, date), so maybe_upgrade's lookup returns None and no cancellation fires.

2. **"Our" vs "manual" booking:** `is_managed` is derived from `BookingResult.confirmation_code`
   prefix (TTB:) as stored in the booking_history by `BookingStore.record_terminal`. The
   UpgradeOrchestrator receives a `BookingResult` (store record) from `WatchOrchestrator`,
   not an `ExistingReservation` from `list_reservations()`. It never calls
   `cancel_reservation` on a booking whose `confirmation_code` lacks the TTB: prefix.
   Period. No config flag, no override.

3. **Idempotency key collision on rebook:** Handled by `delete_terminal` under advisory lock.
   See above. The implementation contract is in `store.py` docstring.

4. **Priority tie-breaking:** Only strict priority improvement triggers upgrade. Equal priority
   = no action. This is verified by `test_upgrade_does_not_upgrade_to_equal_priority`.

5. **ForeUP max-bookings enforcement:** If ForeUP rejects the new booking because a
   booking already exists, the new booking POST returns a non-200 response. This
   surfaces as a generic adapter error (not `SlotGoneError`), which is caught by
   `maybe_upgrade()` and returned as None (no upgrade this pass). The original booking
   is safe. The watch job tries again on the next 10-minute interval (by which time
   a human-made booking might have freed the max-booking limit, which is unlikely but
   harmless to check).

6. **S4 risk: ForeUP has no cancellation API or blocks it:** If Spike S4 reveals that
   ForeUP does not expose a cancellation API (or restricts it programmatically), Feature 2
   is not implementable without browser automation (Playwright). That would be a v1 feature
   and would require re-evaluating the ToS posture (§12). Feature 1 (watch) and Feature 3
   (sort) remain unaffected.

7. **MANAGED_BOOKING_TAG echo risk:** If ForeUP does not echo a user-supplied field back
   in `list_reservations` (confirmed in S4), the chosen approach is Option A (TTB: prefix in
   `BookingResult.confirmation_code` — see §20 "MANAGED_BOOKING_TAG implementation"). No
   separate `managed_bookings` table is needed. `is_managed` is derived from the TTB: prefix
   in the in-memory booking_history record. A mid-run crash loses the in-memory record; on
   the next run the pre-book `list_reservations` check catches any existing booking and records
   ALREADY_BOOKED — the managed/non-managed question does not arise until there is a booking
   to upgrade.

---

### 20.1 Open Questions for User Input

These items cannot be resolved without user input or live-system investigation.
None of them block M-feature-3 (sort). S4 blocks M-feature-2. M-feature-1 can
proceed to T2 without S4 since WatchOrchestrator.check_once does not call cancel.

| # | Question | Blocks | Exit criterion |
|---|----------|--------|----------------|
| ~~Q1~~ | ~~**Shared vs separate SQLite file for watch job.**~~ **SUPERSEDED**: the durable store (M3) was dropped entirely. Both jobs use `InMemoryStore` and start each run fresh. The single source of truth for booking state is `list_reservations()`. Concurrent-run serialization is handled by ACA Job `parallelism=1` (one execution per job) — no shared file needed. | — | Resolved/superseded |
| ~~Q2~~ | ~~**Watch job enabled by default or opt-in?**~~ **RESOLVED**: When `watcher.enabled = false`, the watch job logs a **warning** (`"Watch job is disabled in config — set watcher.enabled = true to activate"`) and exits cleanly (exit 0). GH Actions run must not show as ❌ for an intentionally disabled feature. | — | Resolved |
| ~~Q3~~ | ~~**MANAGED_BOOKING_TAG echo.**~~ **RESOLVED**: We use **Option A** (TTB: prefix stored in `BookingResult.confirmation_code`, not echoed to/from server). `is_managed` works by checking the stored `confirmation_code` in `booking_history`, not by reading a notes field from the server. Whether or not ForeUP echoes a notes field is irrelevant. See §20 "MANAGED_BOOKING_TAG implementation (Option A — LOCKED IN)". | — | Resolved |
| ~~Q4~~ | ~~**Cancellation window.**~~ **RESOLVED**: Use **18-hour safe default** (`cancellation_deadline_hours = 18`). User believes Mangrove Bay permits same-day cancellations but wants 18h as a guard against attempting a cancel+rebook so close to tee time that the player drives to the wrong course. Spike S4 will confirm the actual policy. | — | Resolved (default set; S4 confirms) |
| ~~Q5~~ | ~~**One-booking policy scope.**~~ **RESOLVED**: **Cross-course upgrades enabled.** The priority list can mix courses (e.g. `mangrove_bay 09:00` → `twin_brooks 08:45` → `mangrove_bay 09:30`). If a higher-ranked (course, time) combination opens, the current booking — regardless of course — is cancelled and the better slot is booked. | — | Resolved |

---

### 20.2 New Spikes

| ID | Question | Exit criterion | Suggested time |
|----|----------|----------------|----------------|
| S4 | ForeUP cancellation endpoint. See M-feature-2 §S4 task above for the specific questions. | `docs/foreup-cancel-spike.md` with all 5 questions answered via browser devtools | 1 session |
| S5 | Does ForeUP rate-limit `GET /times` at 10-minute intervals over an extended period (days)? The 6-AM race testing (S3) tested short bursts; S5 tests sustained low-frequency polling over multiple days. | No 429 or account restriction observed over 3 days of polling at 10-min intervals | 3-day passive observation |

---

## 21. Post-Azure cutover cleanup (DONE)

`book.yml` and `watch-tee-time.yml` have been removed. Their schedules now run as ACA Jobs
defined in `infra/bicep/modules/compute.bicep`. The `.github/workflows/` directory now
contains only `ci.yml` (lint/test on PRs) and `azure-iac.yml` (Bicep IaC deploy).
