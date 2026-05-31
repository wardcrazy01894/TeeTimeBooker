# TeeTimeBooker — Frontend Plan (v2)

Design doc for putting a UI around the booking engine. Subordinate to
[PLAN.md](./PLAN.md) (v0 booking engine) and `infra/AZURE_PLAN.md` (v1 hosting) —
read those first. This file covers ONLY the frontend + the API layer it talks to.

Status: **proposed.** No code written. Decisions below were ratified in a design
conversation; the open questions in §7 are the only unsettled points.

---

## 1. Goal

A single-user web UI (just the operator) that can, on demand:

1. **List every reservation** across all configured courses.
2. **Cancel one** reservation via a button next to it.
3. **Cancel all** reservations.
4. **Edit booking preferences** (time windows, day/offset preferences).

v0 stays as-is (cron/ACA-Jobs booking + watch). The frontend is additive — a new
read/write surface over the same engine, not a replacement for the scheduled jobs.

---

## 2. Core decision: live-fetch for reads, store-mediated for writes

**The course backends are the source of truth for "what reservations exist."**
The UI fetches live on request; it does NOT read reservations from `BookingStore`.

Rationale:
- A store-backed reservation list is a *copy* of the truth → introduces a sync
  problem (manual bookings/cancels on the course site, `actions/cache` loss,
  drift). For a single-user dashboard opened occasionally, a round-trip on click
  is simpler and always correct.
- **`BookingStore` is the bot's operational memory** (idempotency keys, advisory
  locks, attempt log) — NOT a mirror of reservations. These are two different
  jobs; conflating them is the trap to avoid.
- Live-fetch lets list + cancel + cancel-all ship **independently, with no store
  dependency**. Preference editing (§5.4) needs durable mutable state — M3 was
  cut from the v0 roadmap, so this is an open decision (§7 Q1).

**Writes are the exception.** Cancelling a *managed* booking must also clear its
idempotency record (`delete_terminal` under `request_lock`), or the one-booking
policy and idempotency drift out of sync with reality. So cancel routes through
orchestrator-mediated logic, never a raw `adapter.cancel_reservation()`.

---

## 3. Read/write shapes

| Operation        | Path                                                                 | Touches store? |
|------------------|---------------------------------------------------------------------|----------------|
| List / list-all  | for each course: `authenticate()` → `list_reservations()` → merge   | No             |
| Cancel (managed) | orchestrator: `cancel_reservation()` + `delete_terminal()` (locked) | Yes (write)    |
| Cancel (manual)  | straight `adapter.cancel_reservation()` (no store record to clear)  | No             |
| Cancel all       | list → per-item cancel (managed vs manual via `is_managed`)          | Some           |
| Edit preferences | read/write the `[request]` config promoted into the store           | Yes (write)    |

`ExistingReservation.is_managed` (the `TTB:` prefix on `confirmation_code`)
selects the managed-vs-manual cancel path.

---

## 4. Architecture

### 4.1 What the design already gives us for free

- **Orchestrators are plain injectable classes.** `Orchestrator`,
  `WatchOrchestrator`, `UpgradeOrchestrator` take collaborators by injection — an
  HTTP handler constructs and calls them exactly like the CLI does.
- **Protocols are the cut line.** `core/` depends on nothing; an API layer is
  "just another caller" of the same Protocols the CLI uses.
- **`CourseAdapter` already exposes everything reads/cancels need:**
  `list_reservations()`, `cancel_reservation()`, `is_managed`.

### 4.2 New components

1. **API service** — an HTTP layer wrapping the existing engine. New long-running
   process (contrast with the one-shot CLI jobs). Endpoints map 1:1 to §3.
2. **Shared engine-wiring module** — lift `_build_adapters` / `_resolve_creds` /
   `_resolve_site_keys` out of `__main__.py` into a module both the CLI and the
   API import. (Today they're private to the CLI.)
3. **TTL read-cache** — small in-memory cache (~30–60 s) in the API process so a
   re-render or double-click doesn't trigger a second login. NOT a data store,
   not persistent, never the source of truth — a courtesy throttle only.
4. **Frontend** — single-user UI. Framework TBD (§7 Q3).

### 4.3 Anti-bot etiquette (PLAN.md §12)

- **On-demand, not auto-poll.** The UI fetches on an explicit button/refresh; it
  does NOT background-poll the courses on a timer.
- Each ForeUP "list" is a **full login** — the reservation list comes back in the
  ~6 MB `POST /login` body (no cheap GET; session caching doesn't help because
  refreshing the list means re-logging-in anyway). The TTL cache (§4.2) absorbs
  accidental repeats.

---

## 5. Milestones

### M-fe-T1 — Engine-wiring extraction (prerequisite, no behavior change)
Lift adapter/creds/site-key construction from `__main__.py` into a shared module.
CLI keeps working identically (regression-guarded by existing tests). Pure
refactor — red/green is "CLI tests still pass."

### M-fe-T2 — `list_reservations` aggregation + API read endpoint
Add a service function: for each configured course, authenticate + list, merge,
return a UI-shaped DTO (course, tee_time, party_size, `is_managed`,
confirmation_code). Wire one read endpoint. Add the TTL cache. **No store
dependency** → ships independently.

### M-fe-T3 — Cancel (single) endpoint
Orchestrator-mediated cancel. Managed → cancel + `delete_terminal` under
`request_lock` (reuse the `UpgradeOrchestrator` pattern). Manual → straight
adapter cancel. Idempotent (404 = success). **No durable store required** —
`delete_terminal` is implemented on `InMemoryStore`. For a single long-running
frontend process, the managed-booking record lives for the server's uptime, which
covers the normal create→cancel lifecycle. Caveat: a server restart between book
and cancel loses the in-process record; the cancel still succeeds via the live
`list_reservations` + manual-cancel path, but the idempotency record is gone.

### M-fe-T4 — Cancel-all endpoint
Composition of T2 (list) + T3 (cancel). Per-item managed/manual routing. Report
per-item success/failure (partial-failure is expected and must be surfaced, not
swallowed).

### M-fe-T5 — Preferences editing (needs durable mutable state — M3 was cut; see §7 Q1)
Promote the `[request]` block (time_windows, target_offsets) into editable state,
TOML as bootstrap defaults. The store backing this is currently `InMemoryStore`
only — edits do not survive a server restart. M3 (`SqliteStore`) was cut from the
v0 roadmap, so there is an open decision about how to persist preferences across
restarts: keep `[request]` in TOML (not UI-editable) for now, or introduce a
durable store as part of the frontend work (§7 Q1). Note: editing windows/offsets
rotates the `RequestId` fingerprint (`derive_request_id` folds
`course_ids|offsets|windows|party`) — semantically correct (different prefs =
different request), but the UI must expect idempotency records to rotate.

### M-fe-T6 — Frontend UI
The actual web client over T2–T5 endpoints. Single-user; auth model per §7 Q2.

**Sequencing:** T1 → T2 → T3 → T4 can all ship with no durable-store dependency
(managed-cancel cleanup works on `InMemoryStore`). Only T5 (preferences editing)
carries an open durability question (§7 Q1).

---

## 6. Contract changes required

- **`BookingStore`**: gains a list/query read method if preferences move into the
  store (M-fe-T5). The reservation-read path needs none (it's live).
- **No `CourseAdapter` changes** — `list_reservations` / `cancel_reservation`
  already cover reads and cancels.
- **No durable store required for managed-cancel cleanup** — `delete_terminal` is
  implemented on `InMemoryStore` and works for a single long-running process.
- **Preferences persistence is an open decision** — M3 (`SqliteStore`) was cut
  from the v0 roadmap. Until a durable store is introduced, `[request]` prefs
  live in TOML only and are not UI-editable across restarts (§7 Q1).

---

## 7. Open questions

- **Q1 — How to persist editable preferences (M-fe-T5)?** Managed cancel is
  unblocked — `delete_terminal` is implemented on `InMemoryStore` and needs no
  durable store for a single long-running process. The real open question is
  preferences: M3 (`SqliteStore`) was cut from the v0 roadmap and is not coming.
  Option A: keep `[request]` prefs in TOML for now (not UI-editable; T5 deferred
  until a durable store is justified). Option B: introduce a lightweight durable
  store (e.g. SQLite via `SqliteStore`) as part of the frontend work, accepting it
  as a frontend-scoped dependency rather than a v0 booking-engine milestone.
- **Q2 — Frontend auth.** Single-user, but the API holds course credentials and
  can cancel bookings — it cannot be unauthenticated if exposed. Local-only? Basic
  auth? Behind the Azure perimeter? Resolve before M-fe-T6.
- **Q3 — Frontend framework.** Not yet chosen. Constraint: single-user, low
  surface, must talk to the API service.
- **Q4 — API hosting.** The one-shot ACA Jobs model doesn't fit a long-running
  API. Container App (not a Job)? Coordinate with `infra/AZURE_PLAN.md`.
- **Q5 — Credential availability.** The API process needs each course's creds at
  request time (today resolved once at CLI start from env). Confirm the Key Vault
  / managed-identity path from the Azure plan covers a long-running service.

---

## 8. What this plan deliberately does NOT do

- Does not make `BookingStore` a reservation cache (§2).
- Does not background-poll the courses (§4.3).
- Does not change the v0 scheduled booking/watch jobs.
- Does not add multi-user / multi-tenant support (single-user assumption holds).
