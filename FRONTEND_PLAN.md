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
- Live-fetch lets list + cancel + cancel-all ship **without M3**. Only preference
  editing (§5.4) needs durable mutable state.

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
dependency** → ships before M3.

### M-fe-T3 — Cancel (single) endpoint
Orchestrator-mediated cancel. Managed → cancel + `delete_terminal` under
`request_lock` (reuse the `UpgradeOrchestrator` pattern). Manual → straight
adapter cancel. Idempotent (404 = success). **Requires M3** for the managed path
(`delete_terminal` is a `SqliteStore` method, currently a stub) — OR scope the
first cut to manual-only cancels if M3 hasn't landed (§7 Q1).

### M-fe-T4 — Cancel-all endpoint
Composition of T2 (list) + T3 (cancel). Per-item managed/manual routing. Report
per-item success/failure (partial-failure is expected and must be surfaced, not
swallowed).

### M-fe-T5 — Preferences editing (gated on M3)
Promote the `[request]` block (time_windows, target_offsets) into the store as
editable state, TOML as bootstrap defaults. Note: editing windows/offsets rotates
the `RequestId` fingerprint (`derive_request_id` folds
`course_ids|offsets|windows|party`) — semantically correct (different prefs =
different request), but the UI must expect idempotency records to rotate.

### M-fe-T6 — Frontend UI
The actual web client over T2–T5 endpoints. Single-user; auth model per §7 Q2.

**Sequencing:** T1 → T2 ship first and unblock the "see my reservations" + (manual)
cancel features with zero M3 dependency. M3 then gates the managed-cancel cleanup
and the preferences screen.

---

## 6. Contract changes required

- **`BookingStore`**: gains a list/query read method if preferences move into the
  store (M-fe-T5). The reservation-read path needs none (it's live).
- **No `CourseAdapter` changes** — `list_reservations` / `cancel_reservation`
  already cover reads and cancels.
- **M3 (`SqliteStore`)** must be implemented for managed-cancel cleanup and
  preferences. It's already on the v0 roadmap.

---

## 7. Open questions

- **Q1 — Ship manual-only cancel before M3?** T3's managed path needs
  `delete_terminal` (M3). Option A: wait for M3, ship full cancel. Option B: ship
  manual-only cancel first, add managed cancel when M3 lands. (Leaning A unless
  M3 slips.)
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
