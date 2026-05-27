# TeeTimeBooker — v0 Plan

> **Scope of v0:** a Python bot that books one or more tee times at **Mangrove Bay Golf Course** (St. Petersburg, FL) at the moment its 7-day booking window opens (6:00 AM America/New_York). No frontend. No third-party booking sites that don't actually take the booking. No real bookings against ForeUP from these stubs — implementation lands in M2/M5 once Spike S1 confirms endpoint shapes.

This plan is structured for parallel execution. Milestones are sequential; tasks within a milestone are tagged with explicit dependencies, so an "army of agents" can pick up anything green.

---

## 1. Architecture overview

```
                     +-----------------------------+
                     |   GitHub Actions cron       |
                     | (UTC; 4 entries: Sat+Sun×DST)|
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
        |  pydantic-load  |  |  (Real /  |        |  (Email v0,     |
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
   |   (SqliteStore)   |                       |   (Protocol)         |
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

**One-line summary:** an Orchestrator drives one or more CourseAdapters at T0, persists state via BookingStore (SQLite v0), and reports via Notifier. Every component except the orchestrator is a Protocol so we can swap impls (cloud KV, Slack, Playwright fallback) without rewiring.

---

## 2. Module layout (committed)

```
TeeTimeBooker/
  pyproject.toml                # 3.12+, ruff, pytest, mypy strict, uv-managed
  PLAN.md                       # this file
  CLAUDE.md                     # operator/agent guide
  .github/workflows/book.yml    # cron + workflow_dispatch
  config/example.toml           # template; secrets via env-var refs
  src/teetime/
    __init__.py                 # version
    __main__.py                 # CLI entry (Click)
    core/
      __init__.py
      models.py                 # @dataclass: BookingRequest, TeeTimeSlot, BookingResult, ...
      adapter.py                # CourseAdapter Protocol + adapter exceptions
      orchestrator.py           # main flow
      config.py                 # TOML loader (pydantic)
      clock.py                  # Clock Protocol + busy_wait_until
    persistence/
      __init__.py
      store.py                  # BookingStore Protocol + ConcurrentRunError
      sqlite_store.py           # v0 default
    notifications/
      __init__.py
      notifier.py               # Notifier Protocol + Noop / Console
      email_notifier.py         # SMTP impl
    courses/
      __init__.py
      foreup/
        __init__.py
        base.py                 # shared HTTP, auth, rate limit, error mapping
        mangrove_bay.py         # course IDs only
      chronogolf/
        __init__.py             # README-only placeholder (Spike S2)
  tests/
    __init__.py
    test_adapter_stub.py        # reference pattern; per-module tests added by tasks
```

**Why this layout:** `core/` is the only package that depends on nothing else. `persistence/`, `notifications/`, and `courses/` all depend on `core/` and never on each other. The orchestrator is the only thing that knows about all four — every other coupling is through a Protocol. This means M3 (persistence), M4 (notifications), and M5 (ForeUP) can be developed in parallel branches against frozen stubs.

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

---

## 5. Persistence layer

**Decision: SQLite (single file) for v0; swap to Cloud SQL / Firestore at v1.**

**Why SQLite:**
- One process, one file. No service to run, no IAM. Fits GH Actions + local dev identically.
- ACID transactions and a real advisory-lock primitive (`BEGIN IMMEDIATE`) — we need both for the double-book defense (§9).
- Plain-JSON would lose us atomicity across the three tables we need.
- Cloud KV is overkill until v1 introduces concurrent users.

**What we persist:**

| Table             | Purpose                                                  | Retention            |
|-------------------|----------------------------------------------------------|----------------------|
| `booking_history` | Terminal `BookingResult` per `RequestId` (idempotency).  | Forever (small).     |
| `attempt_log`     | Append-only audit: every search/book attempt, ms-stamped | 90 days, then prune. |
| `session_cache`   | Adapter-supplied opaque blobs (cookies, JWT) per course. | TTL on each blob.    |
| `request_locks`   | Advisory locks for concurrent-run defense.               | Lifetime of run.     |

**v1 swap path:** the `BookingStore` Protocol is the cut line. `SqliteStore` becomes a sibling of e.g. `FirestoreStore`. The orchestrator never sees SQLite directly. Add a config knob (`persistence.backend = "firestore"`) in M-future.

---

## 6. Scheduling: the 6:00 AM ET race

### 6.1 Strategy

```
T0 = today + target_offset days, 06:00:00.000 America/New_York
     (target_offset comes from RequestConfig.target_offsets; default [7])

GH Actions cron fires ~T0 - 10 min  (jitter is 1–15 min in our experience)
    -> step "DST-half check" reads ZoneInfo("America/New_York") wall-clock
       and short-circuits the rest of the job if not in 5:xx ET. NOT a TODO;
       the gate is implemented in book.yml (see review item 1).
    -> uv sync; bot starts ~T0 - 8 min in steady state
Bot:
    1. Resolve T0 in tz-aware datetime via `zoneinfo.ZoneInfo("America/New_York")`.
       Resolved date = today + target_offset.
    2. Load config; idempotency check on (RequestId, resolved_date); build
       adapter; PRE-AUTH (login NOW so the race window is just GET /times +
       POST /reservations).
    3. busy_wait_until(T0 - 500ms): coarse asyncio.sleep down to ~2s, then a
       1ms-cadence fine loop with explicit OS yield (see core/clock.py).
       Sub-second accuracy without CPU starvation.
    4. Fire first GET /times. Response disambiguation (per Spike S1, item 7):
       - 200 + empty + pre-T0  -> InventoryNotPublishedError; poll
       - 200 + empty + post-T0 -> NoInventoryError; do NOT poll
       - 200 + non-empty       -> filter & rank
       - 4xx 'too far in advance' -> InventoryNotPublishedError; poll
    5. Pick best slot, POST. Enter §9 state machine for the POST/result phase.
    6. Persist terminal result; notify.
```

### 6.2 GH Actions cron jitter

GH Actions cron is documented as best-effort with potentially **15+ minute** delays under load. Mitigations:
- **Schedule 10 min early.** Both DST entries fire at `:50` past the hour preceding 06:00 ET.
- **Bot does its own busy-wait.** The cron only needs to land the runner with at least 1–2 minutes of slack before T0; the bot itself nails the second.
- **If the runner isn't scheduled at all that day** (rare but real): we lose the race. This is a known v0 risk, accepted, and is the headline reason the v1 upgrade is Cloud Run + Cloud Scheduler (which has SLA-backed firing). Documented in §15.

### 6.3 DST math (showing the work)

`America/New_York` switches between EST (UTC-5) and EDT (UTC-4). Spring-forward 2nd Sunday of March; fall-back 1st Sunday of November.

| Local target | EDT (Mar–Nov) | EST (Nov–Mar) |
|--------------|---------------|---------------|
| 06:00 ET     | 10:00 UTC     | 11:00 UTC     |
| Cron (Saturday) | `50 9 * * 6` (09:50 UTC, 10 min early in EDT) | `50 10 * * 6` (10:50 UTC, 10 min early in EST) |
| Cron (Sunday)   | `50 9 * * 0` (09:50 UTC, 10 min early in EDT) | `50 10 * * 0` (10:50 UTC, 10 min early in EST) |

We register **all four** crons (two per day) on Saturdays and Sundays, year-round. The job's first step ("DST-half check", implemented in `.github/workflows/book.yml`) computes `datetime.now(ZoneInfo("America/New_York"))` and writes `proceed=true|false` based on whether the ET wall-clock hour equals 5 (the cron fires at :50 of the hour preceding T0=06:00 ET). Subsequent steps gate on `steps.dst.outputs.proceed == 'true'`. This avoids the maintenance burden of seasonal workflow edits AND the "second cron of the day runs anyway" failure mode (review item 1).

`workflow_dispatch` always proceeds (the gate is `if: github.event_name == 'schedule'`-equivalent), so manual dry-runs aren't blocked by the gate.

The bot itself uses `zoneinfo` to compute T0 — that handles the ambiguous-hour and skipped-hour edge cases automatically. Mangrove Bay's booking window opening on a fall-back morning is unambiguous (06:00 EST, the second 06:00 of the night) by the standard `fold=0` semantics; we accept that.

---

## 7. Auth & secrets

| Stage  | Storage                                  | Notes |
|--------|------------------------------------------|-------|
| v0     | GitHub Actions repo secrets              | One per credential; loaded into env in `book.yml`. |
| v0 dev | `.envrc` / direnv (gitignored)           | Same names as Actions secrets. |
| v1     | GCP Secret Manager (Cloud Run service account) | Rotation via Workload Identity. |

**Credit card data: NEVER stored or transmitted by us.** ForeUP keeps card-on-file per user account; the booking POST does not include a CVV/PAN. If a course requires re-entering a card, that is an explicit out-of-scope failure mode — bot reports CAPTCHA-equivalent error and stops.

---

## 8. Failure modes & retry policy

| Failure                          | Detection                                 | Response                                          |
|----------------------------------|-------------------------------------------|---------------------------------------------------|
| Inventory not yet published      | 200 + empty list, or specific 4xx         | Poll every 250 ms up to `max_poll_seconds` (30 s) |
| Inventory published, no match    | 200 + non-empty, but criteria filter empties | Try next course in `course_preferences`; else NO_INVENTORY |
| Slot gone between search & book  | book() returns specific error             | Re-search once, pick next-best, retry book once   |
| Transient network error          | httpx `RequestError`, 502/503             | tenacity exponential backoff, max 3 attempts      |
| Rate limited (429)               | HTTP 429 + Retry-After                    | Honor Retry-After up to a 10 s cap; else abort    |
| Captcha challenge                | Adapter detects challenge response shape  | `CaptchaError` -> notify user, stop. v0 does not solve. |
| Auth failed                      | 401/403 on login                          | One retry after 2s (transient JWT). Then `AUTH_FAILED`, **lock cooldown** (§8.1). |
| Account lockout risk             | Three login failures in 1 hour            | Halt all runs for 24 h; record in store; notify.  |
| Partial-book state (booked but no confirmation) | book() raised but POST may have landed | See §9. |
| Mid-run runner kill              | Process gone                              | Next run reads `attempt_log`, sees outstanding attempt, queries adapter for confirmation; cf. §9. |
| Already booked                   | `booking_history` has a terminal BOOKED for this RequestId | Return cached result; no network calls. |

### 8.1 Account-lockout cooldown
Three consecutive `AuthError` outcomes for the same course within 1 hour triggers a 24 h cooldown stored in `booking_history` with a synthetic key. The orchestrator checks for this row before any login attempt and short-circuits to a notify-only run.

---

## 9. Double-book prevention

This is the subtlest correctness property. Scenario: bot calls `POST /reservations`, the request lands and creates a booking, but the response is lost (TCP reset, Lambda-style timeout). Naively, retry produces a second booking.

**Defense, in order:**

1. **Pre-flight idempotency.** Before any work: `store.get_terminal(request_id)`. If a terminal `BOOKED` exists, return it.
2. **Pre-book remote check.** Right before POST, the adapter calls a "list my reservations" endpoint (ForeUP exposes this — confirm in Spike S1) and aborts if a reservation already exists for the target date.
3. **Single attempt per slot, by default.** `book()` is non-retryable EXCEPT on `SlotGoneError`. Anything else raises and the orchestrator reaches the post-mortem path:
4. **Post-mortem reconciliation.** Any failure during/after POST that didn't return a clean adapter error triggers: wait 5 s, list reservations again, match by tee_time + party_size. If found, treat as success and persist the confirmation. If not, treat as no-op and try next course.
5. **Advisory lock.** `BookingStore.request_lock(request_id)` is held for the duration of `Orchestrator.run`. Attempting a second concurrent run on the same RequestId raises `ConcurrentRunError` immediately (no waiting).
6. **GH Actions concurrency group.** `concurrency: { group: book-tee-time, cancel-in-progress: false }` in the workflow stops two cron-fired runs from overlapping at all.

This is belt-and-suspenders by design. The single most important rule: **after any POST whose result is uncertain, ALWAYS reconcile via list-reservations before doing anything else.**

### 9.1 Booking state machine (M2 implementation contract)

The orchestrator's per-(course, slot) booking attempt is a state machine with explicit write-to-DB-first transitions. Implementers MUST encode these states verbatim — no derived enum, no "happy path skips a state":

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
2. Every state transition writes to `attempt_log` BEFORE the next await. If
   the runner is killed between two states, the next run reads `attempt_log`,
   sees the dangling state, and resumes via `RECONCILING` — never re-POST.
3. From `UNCERTAIN`, the only legal next state is `RECONCILING`. The
   orchestrator MUST NOT call `book()` again before reconcile completes.
4. `LOST` is terminal for THIS slot. The orchestrator may pick another
   course (next in `course_preferences`), but never the same (course, slot).

The Protocol's `book()` docstring references this diagram. `list_reservations`
is part of the Protocol from M0 (review item 3) — not deferred to M2.T3.

### 9.2 Cross-run state (review item 9)

Idempotency layer 1 (`get_terminal`) requires the SQLite file to survive across
runs. GH Actions runners are ephemeral, so we restore/save via `actions/cache`
in `book.yml`:

- **Restore step** at the start of the job: `actions/cache/restore@v4` with key
  `teetime-state-v1` (no `restore-keys`; partial matches are worse than empty).
- **Save step** at the end (`if: always()`) so attempt_log persists on failure.
- **Forensic upload** still happens via `upload-artifact` for human review.

**Risk: catastrophic cache eviction.** If the cache entry is evicted (GH
retains for 7 days of inactivity; the weekend-only cron runs at most 6 days
apart so eviction is rare but not impossible if a weekend run is skipped), the
next run starts with an empty DB and `get_terminal` returns `None` for an
already-booked RequestId. Layer 2 (`list_reservations`) catches this: pre-book remote
check sees the existing reservation and the orchestrator records `ALREADY_BOOKED`
without POSTing again. So cache loss = one extra round-trip on the next run,
NOT a phantom booking. Documented as accepted v0 risk. v1 moves to S3/GCS.

---

## 10. Observability

- **Structured logs** via `structlog` -> JSON to stderr. Every log line carries `request_id`, `course_id`, `attempt`. Captured by GH Actions; tailed locally.
- **Event log** in `attempt_log` table. Every state transition (per §9.1 state machine) gets a row: `T_RACE_BEGIN`, `SEARCH_START`, `SEARCH_OK`, `SEARCH_EMPTY`, `PRE_BOOK`, `BOOK_POST`, `UNCERTAIN`, `RECONCILING`, `BOOKED`, `LOST`, etc. Useful for retroactive timing analysis.
- **Notify-on-failure** is the alert. Non-`BOOKED` outcomes always email. v1 adds a Slack webhook backend.
- **Metric surface (v1):** count + duration of each event keyed by course; alert if `T_RACE_BEGIN -> BOOK_OK` latency exceeds 5s for two consecutive runs.

### 10.1 PII handling (review failure mode "Player PII in attempt_log")

`attempt_log.payload` is a JSON blob written by the orchestrator. The SQLite
file is uploaded as a workflow artifact AND saved in `actions/cache`. Both
are scoped to the repo, but artifacts are downloadable by anyone with read
access.

**Rule: redact before write.** Before persisting any payload that originated
from `BookingRequest.players` or `CourseCredentials`, the orchestrator MUST:

- Replace `email`, `phone`, `member_number` with SHA-256 prefixes (first 8 hex
  chars of the digest) — enough to correlate two log rows referring to the
  same user, not enough to recover the value.
- NEVER persist `password` (raw or hashed). It does not belong in attempt_log.
- Confirmation codes ARE persisted (we need them for reconciliation).

A helper `_redact_payload(d: dict) -> dict` lives in `core/orchestrator.py`
(M2.T1). Adapters that build payloads must call it before passing to
`store.append_attempt`.

### 10.2 Cross-run state location

See §9.2. SQLite file lives in `state/teetime.db`, restored from
`actions/cache` at job start, saved at job end. Catastrophic cache loss is
caught by §9 layer 2 — never produces a phantom booking.

---

## 11. Testing strategy

| Layer       | Tool                  | What we cover                                                    |
|-------------|-----------------------|-------------------------------------------------------------------|
| Unit (pure) | pytest                | models invariants, config validation, busy-wait math (FakeClock) |
| Adapter unit | respx                | ForeUP request shape, error mapping, captcha detection            |
| Adapter integration | vcrpy cassettes recorded in Spike S1 | Full search+book against canned responses |
| Orchestrator | FakeAdapter + FakeClock + InMemoryStore | the race, fallback, idempotency |
| End-to-end dry-run | live ForeUP, `--dry-run` | All HTTP up to but not including the final POST |
| Cron lint    | actionlint            | Workflow syntax + cron expression sanity |

**6:00 AM race testing without waiting 7 days:** `FakeClock` lets a test set "now" to T0 - 1.5 s and assert the orchestrator's first `search()` lands within ±50 ms of T0. This is the spec for `clock.busy_wait_until`.

**Cassette policy:** cassettes go in `tests/cassettes/`, scrubbed of cookies, JWTs, and personally identifying info before commit. Re-recording is a Spike S1 sub-task.

---

## 12. Anti-bot etiquette & ToS posture

What we DO:
- Honest `User-Agent: TeeTimeBooker/0.0.0 (+contact)`. No browser impersonation beyond what ForeUP's API actually requires.
- Self-imposed minimum 250 ms between calls outside the T0 race window.
- Honor `Retry-After`.
- Cap polling at 30 s after T0.
- One booking per request, ever (idempotency + reconciliation).

What we DO NOT:
- Solve captchas.
- Rotate IPs / use residential proxies.
- Create multiple ForeUP accounts.
- Hammer login on auth failure.

**ToS posture (stated explicitly).** The user has a legitimate account at Mangrove Bay and is automating a booking they are eligible to make. ForeUP's public stance ([zendesk article](https://foreup.zendesk.com/hc/en-us/articles/34034774453403-Preventing-Bots-From-Making-Tee-Times)) is that bot-driven bookings are unwelcome and they may add countermeasures. Risk areas the user accepts:
- ForeUP may add captcha at any time. We will not bypass; bot will surface a notification and stop.
- The municipal course may revoke the user's online booking privileges if they detect bot activity. The user has accepted this risk for v0.
- We will NOT advise or implement evasions of technical controls.

---

## 13. Stop conditions

The orchestrator stops the current run when ANY of:
1. A `BOOKED` outcome is achieved for any (course, date) pair satisfying the request.
2. All courses in `course_preferences` have been tried and yielded `NO_INVENTORY` / `PRICE_REJECTED` / non-retryable error.
3. `max_poll_seconds` exceeded with no inventory anywhere.
4. A `CAPTCHA_BLOCKED` or post-cooldown `AUTH_FAILED` is observed.
5. Wall-clock budget exceeds 5 minutes from T0 (hard ceiling — runner timeout is 15 min, leaves slack for notifier).

Across runs (multiple days):
- A `BOOKED` record blocks future runs for the same `(RequestId, resolved_date)` pair.
- A 24 h auth cooldown (§8.1) blocks runs against that course only.

### 13.1 RequestId derivation rule (review item 5)

`RequestId` is a UUIDv5 derived from a deterministic config fingerprint via
`teetime.core.models.derive_request_id`. The fingerprint string format:

    course_ids|target_offsets|time_windows|party_fingerprint

- `course_ids`: sorted, comma-joined `CourseConfig.id` values.
- `target_offsets`: sorted, comma-joined integers (e.g. `"7"` or `"7,14"`).
- `time_windows`: sorted by `(earliest, latest)`, joined as `"HH:MM-HH:MM"`,
  comma-joined.
- `party_fingerprint`: sorted, comma-joined `(first_name|last_name)` per
  player. Email/phone are EXCLUDED (so rotating a player's contact info
  doesn't break idempotency).

**Resolved dates are excluded from the fingerprint.** The reason: `target_offsets = [7]`
firing on Saturday books the Saturday 7 days out; the resolved date changes each
week but the goal is the same. The actual idempotency key in `booking_history` is
`(RequestId, resolved_date)` — composite primary key. This:

- Lets the weekend cron book day N+7 this weekend and day N+14 next weekend without conflict.
- Keeps the user's "I always want a Saturday at 9:00-10:30 AM 7 days out" rule a single
  RequestId for analytics and §9 layer-5 advisory locking.
- BOOKED on resolved_date X does NOT block resolved_date Y for the same
  RequestId. (See review failure mode "Two target_dates with overlapping
  windows" — explicitly NOT closing all dates.)

---

## 14. Bot identity

| Item              | Value (v0)                                                       |
|-------------------|------------------------------------------------------------------|
| User-Agent        | `TeeTimeBooker/0.0.0 (+https://github.com/alanc3939/TeeTimeBooker)` |
| `api-key` header  | `no_limits` for search; `""` (empty) for login POST — confirmed by browser capture (S1) |
| Accept-Language   | `en-US,en;q=0.9`                                                 |
| Session lifetime  | One HTTP session per orchestrator run; reused across search+book. JWT is **NOT** cached cross-run in v0 — the weekend-only cron runs at least every few days, far exceeding any reasonable JWT TTL, and `workflow_dispatch` testing happens rarely enough that re-login is cheap. The `session_cache` table exists but `cache_session`/`load_session` are deferred to v1 (review item 10). |
| Cookies           | PHPSESSID + ForeUP JWT, persisted via `session_cache` blob.      |
| Concurrency       | At most 1 in-flight HTTP request per adapter at a time.          |

---

## 15. Deployment / runbook

**v0:**
1. Set GH Actions repo secrets: `MB_USERNAME`, `MB_PASSWORD`, `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, plus per-player `PLAYER1_EMAIL`, `PLAYER1_PHONE` etc. as referenced by `config/local.toml`.
2. Commit `config/local.toml` to a private fork OR pass via `workflow_dispatch` input file.
3. Cron fires on Saturday and Sunday at 6:00 AM ET; the workflow's `dst` step gates downstream steps on ET wall-clock hour == 5 (see book.yml). Wrong DST-half cron exits early as success.
4. After each run, check email. State is restored/saved via `actions/cache` (key `teetime-state-v1`). The SQLite file is also uploaded as a workflow artifact for forensic review (downloadable for 90 days; PII redacted per §10.1).
5. On `CAPTCHA_BLOCKED` or repeated `AUTH_FAILED`: `gh workflow disable book-tee-time`. Investigate manually. Re-enable with `gh workflow enable book-tee-time`. NO auto-re-enable — a human MUST confirm the cause is resolved.
6. If the `actions/cache` entry is evicted (rare): the next run treats history as empty, but §9 layer 2 (`list_reservations`) catches any phantom booking before re-POSTing. One extra round-trip; no double-book risk.

**v1 upgrade path** (when jitter / cold-start matters):
- Cloud Run job + Cloud Scheduler (sub-second precision) for the trigger.
- Cloud SQL / Firestore for the BookingStore.
- GCP Secret Manager for credentials.
- Logs/metrics into Cloud Logging.
- AWS Lambda is **not recommended for this workload** because (a) cold-start variance fights the 6:00 AM race, and (b) if we ever fall back to Playwright, Lambda's package-size and binary ergonomics for headless browsers are notably worse than Cloud Run.

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

### M2 — Orchestrator core (DONE)
| ID    | Task                                                | Inputs                  | Outputs                                                              | Owner-files                                | Deps        |
|-------|-----------------------------------------------------|-------------------------|----------------------------------------------------------------------|--------------------------------------------|-------------|
| M2.T1 | Implement `Orchestrator.run` with FakeAdapter, **InMemoryStore** (already a stub in `persistence/in_memory_store.py`), NoopNotifier; encode the §9.1 state machine | M1, all stubs           | end-to-end happy path, fallback path, idempotency path passing; state machine transitions match §9.1 diagram exactly | `core/orchestrator.py`, `persistence/in_memory_store.py`, `tests/test_orchestrator.py`, `tests/conftest.py` | M1.*  |
| M2.T2 | Implement the `derive_request_id` helper body (the signature already exists in `core/models.py`); fingerprint per §13.1 | M1.T2                   | given identical config, identical RequestId across processes         | `core/models.py` (helper body), `tests/test_request_id.py` | M1.T2 |
| M2.T3 | Implement post-mortem reconciliation: orchestrator calls the **already-defined** `adapter.list_reservations()` on UNCERTAIN, drives RECONCILING → BOOKED/LOST per §9.1 | M2.T1                   | reconciliation works against a scripted FakeAdapter that simulates connection-drop after server-side commit | `core/orchestrator.py` (no Protocol changes — `list_reservations` already on Protocol) | M2.T1 |

### M3 — Persistence (parallel with M2 once M1 lands)
| ID    | Task                                          | Inputs                            | Outputs                                  | Owner-files                                                | Deps  |
|-------|-----------------------------------------------|-----------------------------------|------------------------------------------|------------------------------------------------------------|-------|
| M3.T1 | Schema + migrations + `initialize`            | DDL in `sqlite_store.py` docstring| schema applied; idempotent re-init       | `persistence/sqlite_store.py`, `tests/test_sqlite_schema.py` | M1.T2 |
| M3.T2 | `record_terminal`, `get_terminal`, idempotent guard against conflicting outcome | M3.T1 | tests: writing a different outcome for an existing RequestId raises | `persistence/sqlite_store.py`, `tests/test_sqlite_history.py` | M3.T1 |
| M3.T3 | `request_lock` via `BEGIN IMMEDIATE` + holder PID row | M3.T1                            | second concurrent acquisition raises `ConcurrentRunError` immediately | `persistence/sqlite_store.py`, `tests/test_sqlite_lock.py` | M3.T1 |
| ~~M3.T4~~ | ~~`cache_session` / `load_session` with TTL~~ DEFERRED to v1 (review item 10): weekend cron cadence makes a 12 h JWT cache pointless. Stubs remain in `SqliteStore` to keep the Protocol shape stable. | — | — | — | — |

### M4 — Notifications (parallel with M2/M3)
| ID    | Task                                       | Inputs            | Outputs                       | Owner-files                                           | Deps  |
|-------|--------------------------------------------|-------------------|-------------------------------|-------------------------------------------------------|-------|
| M4.T1 | Implement `EmailNotifier` (smtplib + STARTTLS) | NotifierConfig | sends one email per outcome; templates per outcome | `notifications/email_notifier.py`, `tests/test_email_notifier.py` (smtpd fake) | M1.T2 |
| M4.T2 | Implement `ConsoleNotifier`                | —                 | prints structured outcome to stdout | `notifications/notifier.py`                          | —     |
| M4.T3 | Render templates (success / failure / no inventory) | M4.T1     | clean subject + body per outcome | `notifications/email_notifier.py` (templates module) | M4.T1 |

### M5 — ForeUP adapter (DONE — live dry-run confirmed)
| ID    | Task                                                        | Inputs              | Outputs                                                          | Owner-files                                                          | Deps         |
|-------|-------------------------------------------------------------|---------------------|------------------------------------------------------------------|----------------------------------------------------------------------|--------------|
| **S1** | **Spike: confirm ForeUP endpoints, request shapes, captcha posture, schedule_id** for Mangrove Bay | live ForeUP, browser devtools | recorded vcrpy cassettes + a 1-page note in `docs/foreup-spike.md` (committed) | `tests/cassettes/foreup_*.yaml`, `docs/foreup-spike.md`              | M1.*         |
| M5.T1 | Implement `ForeUpAdapter.authenticate`                      | S1 cassettes        | auth round-trip; JWT extracted; AuthError on bad creds          | `courses/foreup/base.py`, `tests/test_foreup_auth.py`                | S1           |
| M5.T2 | Implement `ForeUpAdapter.search` + criteria filtering       | S1, M5.T1           | parse `/api/booking/times`; map to `TeeTimeSlot`; raise InventoryNotPublishedError for empty-pre-T0 | `courses/foreup/base.py`, `tests/test_foreup_search.py` | M5.T1        |
| M5.T3 | Implement `ForeUpAdapter.book` + `ForeUpAdapter.list_reservations` (Protocol method already defined in `core/adapter.py`; M5.T3 implements its body) | S1, M5.T1, M5.T2    | book POST happy path; conflict → SlotGoneError; list_reservations returns matching ExistingReservation; orchestrator-driven reconciliation works end-to-end | `courses/foreup/base.py`, `tests/test_foreup_book.py`                | M5.T2        |
| M5.T4 | Captcha + rate-limit detection                              | S1                  | adapter raises `CaptchaError` / `RateLimitError` from canned responses | `courses/foreup/base.py`, `tests/test_foreup_protection.py`           | M5.T1        |
| M5.T5 | Wire `MangroveBayAdapter` and confirm `schedule_id`         | S1                  | end-to-end dry-run against live ForeUP succeeds                 | `courses/foreup/mangrove_bay.py`                                     | M5.T1..T4    |

### M6 — End-to-end (depends on M2 + M3 + M4 + M5)
| ID    | Task                                              | Inputs           | Outputs                                              | Owner-files                          | Deps        |
|-------|---------------------------------------------------|------------------|------------------------------------------------------|--------------------------------------|-------------|
| M6.T1 | Workflow `book.yml` real impl (DST gate, secrets) | all stubs done   | `gh workflow run` succeeds in dry-run mode           | `.github/workflows/book.yml`         | M5.*        |
| M6.T2 | First production dry-run against Mangrove Bay     | M6.T1            | dry-run email arrives at 6:00:00 ± 1 s ET on first cron | runbook entry                     | M6.T1       |
| M6.T3 | First live booking                                | M6.T2 green      | a real reservation; email confirmation; SQLite history persisted | runbook entry              | M6.T2       |

**Parallel-execution map (post M1):**
```
M2.T1 ─┐
M3.T1 ─┼─► M3.T2,T3,T4 ─┐
M4.T1 ─┘                ├─► M6.T1 ─► M6.T2 ─► M6.T3
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
- **ForeUP captcha:** §8 row "Captcha challenge"; §12. We do not solve. Bot stops, notifies. Spike S1 will tell us if captcha is on the booking step (treatable) vs login (fatal).
- **Account lockout:** §8.1. Three auth failures → 24 h cooldown stored in DB + notify.
- **Credit-card storage:** §7. **None.** ForeUP keeps card on file; we never see PAN/CVV. If a course requires re-entry, we surface as fatal error.
- **ForeUP ToS:** §12, stated honestly. Risks accepted by user.
- **Concurrency / accidental double trigger:** §9 layers 5 & 6. Workflow concurrency group + DB advisory lock. `ConcurrentRunError` fails fast.
- **Mid-run runner kill:** Recovery via `attempt_log` + post-mortem reconciliation (§9, §8 row "Mid-run runner kill"). Worst case: a phantom booking we don't know about — caught on next run's pre-flight `list_reservations`.
- **Testing the 6 AM race without waiting 7 days:** §11. `FakeClock` + `clock.busy_wait_until` test asserts ±50 ms.
- **Stop conditions:** §13.
- **Bot identity:** §14 + §12.

---

## 19. Open risks (eyes open)

1. **GH Actions doesn't actually fire.** Mitigation in v1; v0 accepts the loss.
2. **ForeUP changes endpoints.** Adapter is one file; vcrpy cassettes go red loud. Manageable.
3. ~~**`api-key: no_limits` is a known-bot signal.**~~ Resolved in S1: login uses `api_key=""` (empty); search uses `api_key="no_limits"`. No adverse response observed.
4. **The user's ForeUP account gets restricted.** §12. Accepted v0 risk; would invalidate v0 entirely until manual unlock.
5. **Time-window picker logic ("best slot")** is under-specified. v0 picks the slot whose `tee_time` is closest to the midpoint of the user's window. Revisit in v1 with explicit ranking config.
6. **Cross-run cache eviction** (review item 9). Mitigation: §9 layer 2 (`list_reservations`) catches the missing-history case — see §9.2. v1 moves to S3/GCS.
7. **DST spring-forward day** (review failure mode). 06:00 ET still exists on 2nd Sunday of March (the skipped hour is 02:00–03:00). Add `tests/test_dst_edge.py::test_spring_forward_t0_resolves` in M1.T1: assert `zoneinfo` returns 06:00 EDT on March 8 2026 with no ambiguity exception.
8. **Workflow disabled by `gh workflow disable` after CAPTCHA_BLOCKED** (review failure mode). No automation re-enables it. Documented runbook step §15: after manual investigation, run `gh workflow enable book-tee-time`. We do NOT auto-re-enable — a human MUST verify the cause is gone.
9. ~~**`api-key: no_limits` is a community-observed magic string.**~~ Resolved in S1: search header confirmed; login uses empty string. Behaviour stable as of 2026-04-29.

### 19.1 Disagreements with v0 review

- **Item 11 (asyncio_mode + @pytest.mark.asyncio)**: agreed and fixed; the marker was redundant under `asyncio_mode = "auto"`.
- **Item 10 (defer cache_session)**: agreed and applied — weekend cron means JWT cache buys nothing in v0.
- **Nit (cron defaults `dry_run=false`)**: KEEPING this. With the DST gate fixed (item 1), only one cron of the day proceeds. The user explicitly wants real bookings on cron — the bot's purpose is to book, not dry-run. Manual `workflow_dispatch` defaults to `dry_run=true` (safe testing default). This is intentional asymmetry.
