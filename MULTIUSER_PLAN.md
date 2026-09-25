# TeeTimeBooker — Multi-User Website Plan (MULTIUSER_PLAN)

Status: **RATIFIED 2026-09-25** after three adversarial review rounds (round 1 BLOCK, round 2 BLOCK, round 3 BLOCK on a single one-line item fixed by the coordinator); every item addressed, see the §15 ledgers. Nothing is wired yet — MU-1+ implement it. Stubs on disk (`src/teetime/tenant/`, `src/teetime/web/`,
`src/teetime/core/release_policy.py`, `src/teetime/courses/foreup/token_pool.py`); **nothing is
wired**. Nothing in this document changes current prod behaviour until the cutover PRs (§11, §12)
land. Subordinate to [PLAN.md](./PLAN.md) (engine), [infra/AZURE_PLAN.md](./infra/AZURE_PLAN.md)
(hosting), and the ratified race-path plans
([BLIND_POST_PLAN.md](./BLIND_POST_PLAN.md), [RACE_PREWARM_PLAN.md](./RACE_PREWARM_PLAN.md),
[RESEARCH_FALLBACK_PLAN.md](./RESEARCH_FALLBACK_PLAN.md), [STAGGER_PLAN.md](./STAGGER_PLAN.md)).
Supersedes [FRONTEND_PLAN.md](./FRONTEND_PLAN.md) once ratified (that plan was single-user by
design; §8 below keeps its live-read / store-mediated-write principles).

Operator decisions from the 2026-09-25 spike are **inputs**, not open questions. They are cited as
**[D1]–[D10]**. Where the arithmetic shows one decision conflicts with another, this plan says so
and asks, rather than silently picking (§13 Q1 is the only blocking one).

---

## 1. Goal and scope

### 1.1 Goal

Turn the single-user bot into a small invite-only website. Each user connects their own ForeUP
account, says which dates and windows they want (standing weekly rules, one-off dates, and skips),
and the same engine books for all of them at each drop. The watcher keeps recovering and upgrading
for all of them. The site shows each user's rows, their status, and the booked tee time.

### 1.2 IN scope (v1)

- **BYO course accounts [D1]**: `CourseAccount` per (user, course) with `provenance`. Password is
  encrypted at rest (AES-256-GCM, keyring from one KV secret, §9.2). A live login verifies it.
- **Dated request rows + standing rules + skips [D2]**, with the row state machine (§3.4).
- **Tenant booking runner [D3]**: one job per distinct release event (§6). One DB read plus one
  claim write at start, then per-account orchestrators run concurrently on the **unchanged** race
  path (§4).
- **Shared per-course CAPTCHA pool [D4]** with round-robin lease semantics, a bounded fill, and a
  stated account cap (§5).
- **`ReleasePolicy` per adapter [D5]**, and Bicep job derivation from one release-event table (§6).
- **Tenant watcher [D6]**: one DB query per run, one shared search per (course, date, party_size),
  per-account login only when warranted, persisted snapshots, per-account reconcile + upgrade (§7).
- **Web app [D7]**: one scale-to-zero Container App (FastAPI + Jinja + HTMX). OAuth login, signed
  sessions, CSRF protection, dashboard, account connect, rules/dates/skips, refresh, and cancel (§8).
- **Per-user email** for booked / lost / cancelled / auth-failed (§8.7).
- **Killswitch** coverage for the new jobs plus a Container App stop step [D8] (§10.3).
- **Migration** of the operator's current config to a rule + account, with zero missed drops
  (§11). Turk is a data change [D9] once MU-3 has widened the MB grid (§6.5).

### 1.3 OUT of scope (v1) — see also §14

- Shadow / system-provisioned accounts (the `provenance` field exists; no flow uses it) [D1].
- Hosted TeeItUp booking. The PAN/CVV path stays out of hosted scope. Sydney Marovitz gets a
  `ReleasePolicy` as data but `hosted_booking=False`, so no job is generated for it (§6.4).
- Cross-course ranked upgrade: the data hook is designed (§3.6), the behaviour is not built [D5].
- OTP wiring. `CourseAccount.otp_mailbox` exists, is nullable, and v1 never reads it. Enforcement
  is still UI-only (root CLAUDE.md, email-OTP bullet).
- Public sign-up, billing, or any user beyond an operator allowlist.

---

## 2. Architecture

### 2.1 Components

```
                        +---------------------------------------------+
   browser --HTTPS----> |  Web Container App  (scale 0..1)            |
   (OAuth: GitHub/Google)| FastAPI + Jinja + HTMX  `teetime web`       |
                        |  routes -> web/services.py ----------------+ |
                        +--------|-----------------|-----------------|-+
                                 | TenantStore     | engine (live)   | UserNotifier
                                 v                 v                 v
   +-------------------------------+   +-----------------------+  +------------------+
   | Cosmos DB free tier (§10.2)   |   | CourseAdapter per     |  | ACS Email (REST) |
   |  tenant: account, rule, row,  |   | account (ForeUP)      |  +------------------+
   |    slot, booking, snapshot    |   +-----------+-----------+
   |  global: user, claim, probe,  |               ^
   |    audit (per-item TTL)       |               |
   +------^----------------^-------+               |
          |                |                       |
  1 read + 1 claim   1 query + outcome /     same adapters, same
  pre-T0; outcomes   snapshot writes         orchestrators
  post-burst                |                       |
          |                |                       |
   +------+--------------+ +-+---------------------+-----+
   | ACA Job per release | | ACA watch Job (*/10 prod)   |
   | event (EDT/EST pair)| | `teetime tenant-watch`       |
   | `teetime tenant-run | |  tenant/runner.py            |
   |   --event <key>`    | |  -> tenant/watcher.py        |
   |  tenant/runner.py   | |  -> WatchOrchestrator /      |
   |  -> Orchestrator xN | |     UpgradeOrchestrator xN   |
   |  -> SharedCaptcha-  | +------------------------------+
   |     Pool (per course)|
   +---------------------+
```

### 2.2 Who calls whom (layering)

```
core/  (models, adapter Protocol, orchestrators, cutoff, redaction, release_policy)
  ^                           no core module imports tenant/ or web/
courses/  (adapters; foreup/token_pool.py)      imports core only
  ^
tenant/   (models, store Protocol, materialize, allocation, crypto, runner, watcher, notify)
  ^                           imports core + courses; never web
web/      (app factory, routes, services, security)   imports tenant + core + courses
__main__  (CLI: existing run/watch/show-config + new tenant-run/tenant-watch/web/tenant-seed)
```

The cut line stays the same as today (root CLAUDE.md, "Package layout"). The three orchestrators,
the adapters, ranking, cutoff, redaction, OTP source, and retry are **reused per instance**. Only
the TOML / CLI / `InMemoryStore` / `ConsoleNotifier` **shell** is replaced by `tenant/` + `web/`.
`BookingStore` and `Notifier` Protocols are **unchanged** (§3.7, §8.7).

### 2.3 Engine changes (the complete list, each with a non-regression default)

| # | Change | Default preserves today? | PR |
|---|--------|--------------------------|----|
| E1 | `ForeUpAdapter(captcha_pool=...)` optional injected `SharedCaptchaPool`; the private deque becomes a private pool | Yes: `None` builds a private pool with byte-identical semantics; `tests/test_captcha_pool.py` must pass unmodified | MU-2 |
| E2 | `MangroveBayAdapter.set_blind_allowlist(frozenset[SlotId] \| None)`, filtering `synthesize_blind_slots` | Yes: default `None` means no filter | MU-3 |
| E3 | MB `BLIND_POST_MORNING_GRID` widened to the full morning (§6.5) | Yes for the operator: synthesize intersects with the request window, so the 08:45–10:00 burst emits the identical 3 slots | MU-3 |
| E4 | `release_policy: ClassVar[ReleasePolicy]` on MB + Sydney Marovitz (not on the Protocol) | Yes: nothing reads it yet | MU-1 |
| E5 | `WatchOrchestrator(..., reconcile_eligible: Callable[[ExistingReservation], bool] \| None = None)` | Yes: `None` makes every match eligible (today) | MU-4 |
| E6 | `ReservationSnapshotHealth` capability: ForeUP reports whether the last login produced a trustworthy reservation cache (§7.5). Untrusted when: the login soft-failed, the 200 body was non-JSON, **or** a JSON success body's `reservations` is missing or not a list (`base.py:494-496` leaves the cache empty in all three cases) | Yes: a new opt-in Protocol, read only by tenant code | MU-4 |
| E7 | `core.redaction.register_secret_literals(values)`: literal-value masking in `RedactingLogFilter` | Yes: an empty registry is a no-op | MU-4 |

**`orchestrator.py` is not modified at all.** That is the core of the T0 non-regression argument
(§4.4). None of the round-1 must-fixes required changing it. The runner learns what the engine did through
an in-memory **recording adapter decorator** (`tenant/recording.py`, §4.6). That is tenant code
wrapping the adapter, not an engine change.

---

## 3. Data model

### 3.1 Documents (Azure Cosmos DB for NoSQL, free tier; decided 2026-09-25, §10.2)

One Cosmos account (free tier) in `rg-teetime-shared`, following the shared-ACR precedent. It holds
two databases, **`prod`** and **`dev`**, each with **400 RU/s shared throughput** (800 RU/s total,
inside the 1000 RU/s free allowance). Every database has the same **two** containers (plus two CI
containers in `dev` only, §10.2). Keeping the count at 2–4 keeps each database at the 400 RU/s
shared-throughput minimum (to be confirmed by S-M9):

| Container | Partition key | Document types (`type` field) | Why this partition |
|-----------|---------------|-------------------------------|--------------------|
| `tenant` | `/accountId` (= `course_account_id`) | `account`, `rule`, `row`, `slot` (§3.2), `booking` (ownership ledger), `snapshot` | a row, its date-slot pointer, its ledger entries and its account's snapshot all live in **one logical partition**, so every state change is **one transactional batch** (atomic, ETag-conditional) |
| `global` | `/pk`, a prefixed key: `user:<userId>`, `claim:<sha256>`, `probe:<bucket>`, `audit:<userId>` | `user`; `claim` (`identity` = provider\|subject, `invite` = verified-email hash, `username` = course\|username hash, `course-count`); `probe` (per-item **TTL 2 h**); `audit` (per-item **TTL 400 days**) | cross-partition uniqueness via deterministic ids (§3.2); the container default TTL is `-1` (on, no default), so only probe and audit docs expire |

Fields are those of `tenant/models.py`. Abridged: `row` = `course_id`, `target_date`, `timezone`
(**course tz**), window, `party_size`, `status`, `status_reason`, `source`, `rule_id`, `cutoff_at`
(UTC, from `booking_cutoff.cutoff_instant`), `request_id`, `booked_*`, `needs_reconcile`,
`upgrade_started_at` (M2 marker), `lease_owner`, `lease_expires_at`, `last_outcome*`,
`group_id`/`group_rank`, `version`, `schemaVersion`. Deterministic **document ids** carry the
uniqueness keys:

| Doc | `id` | Guarantees |
|-----|------|-----------|
| account | `account` (the partition *is* the account; `accountId = uuid5(user_id, course_id)`) | UNIQUE(user, course): a second create with the same derived accountId conflicts (409) |
| rule row | `row|rule|<rule_id>|<date>` | UNIQUE(rule_id, date). A 409 on create means *a row exists*, **not** "handled": the materializer then consults the (account, date) history (§7.7) to reactivate or skip (round-2 M1) |
| explicit row | `row|x|<uuid>` | – |
| date slot | `slot|<date>` | **one ACTIVE row per (account, date)**, see §3.2 |
| ledger entry | `booking|<course_id>|<raw_id>` | UNIQUE(course, raw id) within the account |
| snapshot | `snapshot` | one latest per account |
| rule-weekday pointer | `ruleday|<weekday>` | **one ACTIVE rule per (account, weekday)** (round-4, MU-5 review SF6), see §3.2 |

`players` are **not stored**. ForeUP's book POST sends only the player count (root CLAUDE.md, and
AZURE_PLAN §7.1). The tenant runner builds `Player("Guest","Player","")` × `party_size`. This
retires the `PLAYER1-*` PII secrets for the tenant path (§9.3).

### 3.2 Uniqueness: how Cosmos holds the double-booking-critical invariants

Cosmos unique-key policies are per partition, **cannot be filtered**, and are **immutable** after
container creation, so they cannot express "unique among *active* rows". The plan does **not** use
them. It uses the one uniqueness Cosmos guarantees unconditionally: **`id` is unique within a
logical partition**, combined with **transactional batch** (all-or-nothing, within one partition).

- **"One active row per (account, date)"** is a **date-slot pointer document** `slot|<date>` in the
  account's partition, holding `activeRowId`. "Active" is **not a flag on the row**: it is *being
  pointed to by the slot*. The active statuses (pending/booked/skipped) are the only ones that
  hold a slot.
  - Creating or activating a row is one batch: create the row + **create** the slot (a 409 aborts
    the whole batch, so the date is already taken).
  - Superseding is one batch: replace the rule row's status (IfMatch its ETag) + create the
    explicit row + **replace** the slot (IfMatch) to point at the explicit row.
  - Leaving the active set (withdrawn, superseded without replacement, cancelled, lost) is one
    batch: replace the row + **delete** the slot (IfMatch).

  The slot doc and the row status can therefore never disagree. Pinned by
  `test_one_active_row_per_account_date` and `test_slot_and_row_never_diverge` (conformance, run
  against both stores).
- **"One active rule per (account, weekday)"** (round-3 SF1) is held the same way, by a
  **rule-weekday pointer doc** `ruleday|<weekday>` in the account's partition, holding
  `activeRuleId` (round-4, MU-5 review SF6). A scan-then-write ("is there another active rule on
  this weekday?") cannot hold the invariant under two concurrent web requests in Cosmos; the
  pointer can. Activating a rule is one batch: replace the rule (IfMatch its ETag) + **create**
  the pointer (409 = the weekday is taken, `RuleConflictError`). Moving a rule to another weekday
  or deactivating it deletes / re-points the pointer in the same batch. Rule replaces are
  IfMatch on the rule's `version` (a stale edit is `VersionConflictError`), and
  `materialized_through` never moves backwards. Pinned by
  `test_ruleday_pointer_tracks_the_active_rule` and `test_upsert_rule_refuses_stale_version`
  (conformance).
- **(rule_id, date)** uniqueness comes from the deterministic id `row|rule|<rule_id>|<date>`.
  **Whether** to (re)materialize a date is decided by the account's row history for that date
  (§7.7), never by the collision alone (round-2 M1).
- **UNIQUE(course, username)** and **UNIQUE(provider, subject)** span partitions, so they use
  `claim` docs in `global` with deterministic ids (a SHA-256 of the key). These are **not**
  transactional with the account write, so the protocol is (round-2 SF5):
  1. create the claim with `state: pending`, `accountId`, `created_at` (409 = taken);
  2. create the account doc;
  3. IfMatch-replace the claim to `state: bound`. **If that replace returns 412**, someone
     reclaimed it: the claimant **deletes the account doc it just created** and reports "already
     connected".

  A **pending** claim may be reclaimed by another connect only if it is **older than 10 min**
  (a connect completes in seconds) **and** its `accountId` has no account doc. Reclaim is itself
  an IfMatch replace. Two racing connects therefore cannot both end up bound: the loser either
  fails step 1, fails the reclaim precondition, or detects the 412 in step 3 and rolls back.
  Pinned by `test_claim_reclaim_requires_age_and_missing_account` and
  `test_claimant_rolls_back_account_on_lost_claim`. `max_accounts_per_course` uses a `course-count` doc incremented with
  IfMatch (a soft cap; a lost race retries).
- **Correctness verdict:** every invariant that prevents a double booking (one active row per
  account-date, idempotent materialization, lease + version checks, ledger + row written together)
  lives **inside one account partition** and is enforced atomically. The only cross-partition
  invariants are identity/username uniqueness and the account cap, and none of them can cause a
  double booking. **There is no correctness reason Cosmos cannot hold them.** The cross-course
  group hook (§3.6) is user-scoped and therefore cross-partition; it is a follow-up and would use
  the same claim-doc pattern.
- **Consistency:** the account default is **Strong** (single region, so no latency penalty; reads
  cost 2× RU, which is trivial at this scale, §10.2). Every mutation is ETag-conditional anyway.
- **Indexing:** a custom policy includes only the queried paths (`/type`, `/courseId`,
  `/targetDate`, `/status`, `/cutoffAt`, `/userId`), so writes stay cheap.

### 3.3 RequestId mapping (PLAN §13.1)

`derive_request_id(fingerprint)` is kept. For a tenant row the fingerprint is `tenant-row|<row.id>`
(`tenant.models.row_request_id`). **Why not the TOML fingerprint** (`course_ids|offsets|windows|
party`): two accounts with the same window and the same synthesized guest names would get the
**same** RequestId. They would then collide on `request_lock` inside one runner process (the second
account raises `ConcurrentRunError` and never books). Row identity is already durable, so the row
id is the natural idempotency identity. The store key `(RequestId, date)` becomes `(row, date)`,
which is the row. Editing a pending row's window does not rotate its RequestId. That is harmless
because `InMemoryStore` terminals live only for one run. Stored in the row doc's `request_id` so log
lines can be grepped. No durable data depends on the operator's old TOML RequestId (it was
in-process only).

### 3.4 Row state machine

`frozen` is **derived, never stored**: `frozen_reason(now, target_date, timezone=row.timezone,
cutoff=policy_cutoff, skip_dates=frozenset())`. The skip leg is retired for tenant rows (a skip is
the `skipped` status). `cutoff_at` is a denormalized copy of `cutoff_instant(...)` so the Cosmos
queries can filter on it. The Python `frozen_reason` re-check is authoritative (belt and braces;
a global cutoff-policy change needs a migration that recomputes `cutoff_at`, §13 Q13).

```
             create (web explicit / materializer rule)  [guard: not frozen, date >= today]
                                   |
                                   v
      +--------- unskip ------> PENDING <------ un-supersede / rule reactivated -----+
      |                        /  |  |  \                                           |
      |            skip (web)/    |  |   \ explicit row created (web)               |
      |                      v    |  |    v                                         |
   SKIPPED <-----------------+    |  |  SUPERSEDED (rule rows only) -----------------+
                                  |  |
      withdraw (web: delete   <---+  +---> booked (booker post-burst / watcher recovery)
      explicit; rule edit/            |           |
      deactivate) -> WITHDRAWN        |           +--> BOOKED --(upgrade, watcher)--> BOOKED
                                      |           |      |
      frozen & not booked             |           |      +--cancel (web, managed path)--> CANCELLED
      (watcher finalizer) -> LOST <---+           |      +--vanished x2 trusted snapshots--> CANCELLED(external)
                                                  |      +--upgrade cancel ok, rebook failed--> PENDING(+needs_reconcile)
                                                  |
                           skip on BOOKED is REFUSED (cancel is a separate action)
```

| From → To | Owner (only this actor may write it) | Guard |
|-----------|--------------------------------------|-------|
| ∅ → pending | web (explicit), materializer (rule) | not frozen; no other active row for (account,date), else explicit supersedes a pending/skipped rule row in the same transaction |
| pending → booked | booking runner (post-burst), tenant watcher | holds row lease; outcome BOOKED or ALREADY_BOOKED (§7.6 ownership) |
| pending → skipped | web | row not leased (a booker mid-attempt holds the lease, so the UI says "booking in progress") |
| skipped → pending | web | not frozen; no other active row; for a rule row, the STORED rule still covers the row (exists, active, same weekday and account: `_rule_covers_row`, round-5) and no user-terminal row for the date (round-5 SF1: unskip never revives a row whose rule moved weekday) |
| booked → skipped | **refused** | `TransitionRefusedError`; UI offers Cancel instead |
| pending/skipped(rule) → superseded | web | explicit row for the same (account,date) created in the same transaction; **rule row not leased** (a claimed row cannot be superseded: the web replies "booking in progress, try after 06:20"; M4). The pre-supersede status is stored on the row (`superseded_from`, round-4 D2) |
| superseded → pending **or skipped** | web | explicit row withdrawn; rule active; not frozen; superseded row not leased. It returns to **exactly its pre-supersede status** (`superseded_from`): a user's skip is honoured (round-4 D2). The materializer never writes this edge |
| superseded → withdrawn | materializer/web (**system** reasons only: rule deactivated, deleted, or weekday changed) | not leased (round-4 D1: superseded rows are not immune to rule edits) |
| pending → withdrawn | web (delete explicit: `status_reason=user_withdrawn` — **NOT user-terminal** (round-3 M1): it means "undo my one-off", not "never book this date", so a later rule may still materialize the date; withdrawing an explicit row that superseded a rule row restores that rule row to **its prior status** (pending or skipped, D2) **in the same batch**); materializer/web (**system** reasons: `rule_weekday_changed`, `rule_deactivated`, `rule_deleted`) | not leased |
| withdrawn(**any system reason**) → pending **or skipped** (round-5: back to `superseded_from` if the row was superseded before the withdraw) | materializer via `reactivate_rule_row` (rule becomes applicable to that date again: weekday flipped back, reactivated, or a different rule now covers it); **or** the web's one-off withdraw batch (round-6: the one-off held the slot, so reactivation was refused) | not frozen; **no user-terminal row for (account, date)** (§7.7); the STORED rule still covers the row (exists, active, same weekday and account: `_rule_covers_row`, round-5); slot free (or freed by the one-off in the same batch). IfMatch replace (refreshing window/party from the current rule) + create slot, in one batch (round-2 M1) |
| booked → booked (upgrade) | tenant watcher | via `UpgradeOrchestrator` under row lease |
| booked → cancelled | web (user cancel, §8.5: reason `user` or `already_gone`); watcher (reason `external` only; reasons are tied to the actor, MU-5 review) (`external`: the reservation is absent from **two consecutive trusted** snapshots, **and** `upgrade_started_at` is NULL, **and** the id is not ledgered `cancelled_upgrade`/`cancelled_extra`, **and** no same-(date, party) replacement reservation exists; a replacement is adopted instead, §7.5) | lease |
| booked → pending (+`needs_reconcile`) | tenant watcher | **refused unless the write sets `needs_reconcile`** (MU-5 review MF2; without it the §7.6 in-window adoption never applies). The upgrade cancelled the old slot and the rebook failed. **Detected by observation, not by the store terminal** (`delete_terminal` runs only after a *successful* rebook, `upgrade_orchestrator.py:492`; after a failed rebook Gate 3 returns the old `prior`). The recording decorator (§4.6) sees `cancel_reservation(booked_raw_id)` succeed and no new BOOKED. If the process dies before the write, the `upgrade_started_at` intent marker (set under the lease **before** the engine runs) makes the next run treat a missing reservation as bot-caused: pending + needs_reconcile, **not** cancelled(external) (M2) |
| pending → lost | tenant watcher finalizer | `frozen_reason(...) == "cutoff"` or date passed; a `lost` email goes out once |
| booked, frozen | none (stays booked) | a held booking is never auto-cancelled at cutoff (LEADTIME_SKIP F1) |

**Every web-initiated transition requires the row unleased** (skip, unskip, withdraw, supersede, edit), so a booker claim can never be pulled out from under WRITE #2 (M4).
A lease is "held" only until `lease_expires_at`: every unleased write clears an EXPIRED lease, and a
status change through `record_outcomes` needs an UNEXPIRED lease at the outcome's time, so a stale
holder never reclaims a row someone else wrote after its lease ran out (MU-5 review SF2).
**withdrawn → pending or skipped is written only by `reactivate_rule_row`** (and, round-6, by the one-off withdraw batch; never the generic web/materializer
transition), because only they re-check the user-terminal history and refresh window/party from
the rule (MU-5 review MF1). The round-6 restore applies only when the rule is ACTIVE, still covers the row (`rule.weekday == target_date.weekday()` and same account), the date is not frozen, and there is **no user-terminal row for (account, date)** (round-4 MF-A: it keys on the rule's CURRENT weekday, not on the withdraw reason, so a row whose rule moved away and back while a one-off held the slot is still restored).
**Round-7 decision (MU-5 review round 4): a user-terminal date stays BLOCKED for the standing rule regardless of later re-requests.** Withdrawing a re-request undoes the re-request, not the cancel, so the one-off withdraw restores NO rule row on a user-terminal date: a superseded rule row there stays SUPERSEDED, which is inert (the materializer's step 1 skips user-terminal dates, and `load_event_rows` / `load_watch_rows` never return superseded rows). Pinned by `test_withdraw_rerequest_after_user_cancel_keeps_date_blocked`.
**One "may become active" guard (MU-5 review round 5, systemic).** Every write that puts a rule row into pending/skipped from outside the active set (create, reactivate, un-supersede, the one-off withdraw restore) or unskips it passes ONE guard, enforced inside the store's batch so no writer can skip it: the STORED rule still covers the row (exists, active, same weekday and account: `_rule_covers_row`, round-5), and the date has no user-terminal row. Frozen and the slot are checked by the transition itself. The same `_rule_covers_row` predicate filters every read that offers rows for booking (`load_event_rows`, `load_watch_rows`) and drives `finalize_lost` and the sweep. `insert_rule_row_if_absent` and `reactivate_rule_row` also IfMatch the stored rule (a stale copy is refused), which MU-8b does by asserting the rule doc's (or its `ruleday|<weekday>` pointer's) ETag in the same batch. Pinned by `test_unsupersede_refused_on_user_terminal_date`, `test_unskip_refused_after_weekday_change`, `test_reactivate_refuses_stale_rule`, `test_reactivate_refuses_row_the_stored_rule_no_longer_covers` and `test_insert_rule_row_refuses_stale_rule`. Only PENDING and BOOKED rows are ever leased (round-5 nit).
**Round-6 decision: unskipping a row the rule no longer covers is REFUSED** (there is no rule to return to). The guard raises `RuleNoLongerCoversError` (a `TransitionRefusedError`), which the web renders as "This rule no longer covers <date>; add it as a one-off instead" (§8.2). Pinned by `test_unskip_refused_after_weekday_change`.

**Round-4 decisions (MU-5 review, coordinator, 2026-09-25):**
- **(D1) Superseded rule rows are NOT immune to rule edits.** Rule deactivation, rule deletion and
  a weekday change **withdraw** that rule's superseded rows too (system reason, same as pending
  rows); booked rows remain untouched. Consequence: after deactivate → one-off withdrawn →
  reactivate, the row is withdrawn(system) and the normal materializer reactivate path (own row
  system-withdrawn + slot free → REACTIVATE) brings it back. The materializer still never writes
  superseded → pending. Pinned by `test_rule_deactivate_withdraws_superseded_rows` and
  `test_deactivate_withdraw_reactivate_rematerializes_via_system_withdrawn`.
- **(D2) Withdrawing a one-off restores the superseded rule row to its PRE-SUPERSEDE status**
  (pending OR skipped), stored on the row at supersede time (`superseded_from`), so a user's skip
  is honoured. Pinned by `test_withdraw_explicit_restores_skipped_rule_row_as_skipped`.
- **(Round-5 decision, MU-5 review round 2) D1 must not undo D2.** `superseded_from` is KEPT
  through superseded → withdrawn, and `reactivate_rule_row` restores `superseded_from or
  pending` (a MATERIALIZER withdrawn → skipped edge). So rule row skipped → one-off supersedes →
  rule deactivated (row withdrawn) → one-off withdrawn → rule reactivated brings the row back
  SKIPPED, and the bot never books a date the user skipped. A plain skipped row already survives
  deactivate/reactivate (rule edits never touch skipped rows); this makes the hidden-skip case
  consistent. Pinned by `test_skip_survives_supersede_deactivate_withdraw_reactivate`.
- **Leased-edge allowlist (MU-5 review round 2, MF1).** `record_outcomes` (the leased path) may
  write ONLY pending → booked (runner, watcher), booked → booked (watcher upgrade), booked →
  pending + `needs_reconcile` (watcher) and booked → cancelled (watcher `external`; web `user` /
  `already_gone` for the §8.5 cancel). Every other edge goes through the unleased paths that
  carry its guards (user-terminal history, the D2 restore, rule active), so no lease holder can
  write it around them.

**Rule edits** never touch `booked`, `skipped`, or leased rows. A window/party change
re-writes **pending, unleased, not-frozen** rule rows in place (version bump) through `TenantStore.rewrite_pending_rule_row(row_id, *, rule, expected_version, now)` (round-6: row IfMatch, stored-rule IfMatch, and the coverage guard called explicitly, since a pending → pending write is not "becoming bookable"). A weekday change
withdraws the old-weekday pending **and superseded** rows and materializes the new weekday.
Deactivation (and deletion) withdraws pending **and superseded** rows
(`status_reason='rule_deactivated'` / `'rule_deleted'`; round-4 D1). Booked rows stay booked and the user cancels
them explicitly. **Resurrection is decided by the (account, date) history, never by a document-id
collision** (round-2 M1, §7.7). A system-withdrawn rule row comes back when a rule applies to that
date again. A **user-terminal** row for the date (cancelled for reason user/external/already_gone —
**not** a withdrawn one-off, round-3 M1) blocks every materialization for that date, including by a
brand-new rule with a fresh rule_id. Only the user's explicit "Re-request this date" reopens it.
A rule edit made after the booker's 05:51 claim (i.e. between ~05:51 and ~06:20 on a drop
morning) does not reach the claimed row: that morning books the OLD window/weekday, and the user
can cancel it. The UI states "edits apply from the next drop" (round-3 SF2).
A window/party edit skips a row that is leased at the time, so that row keeps its old window/party for the rest of that week (until its drop or its next unleased edit).
**One active rule per (account, weekday)** in v1 (round-3 SF1): a second active rule on the same
weekday is refused at create/edit time (`RuleConflictError`; pinned by
`test_second_active_rule_same_weekday_refused`), because a second rule's rows would be created
`superseded` and nothing re-classifies them when the first rule is later removed — a silent missed
drop. Multiple rules per weekday (window preference lists) are a follow-up.

### 3.5 Leases (cross-process exclusion)

The in-process `request_lock` only serializes coroutines inside one process (watch_orchestrator
module docstring). The site adds a third writer (web), so tenant mutations take a **row lease**: point-read the
row doc (1 RU), check `lease_expires_at` is NULL or past (and the fingerprint, below), then
**replace it with `IfMatch: <etag>`** setting `lease_owner` / `lease_expires_at`. A 412
(precondition failed) means another writer got there first: not acquired. This is Cosmos's native
optimistic concurrency. There is no held connection or server-side lock, and the in-memory store
reproduces it with a version counter.

| Holder | Lease length | When |
|--------|-------------|------|
| booking runner | until T0 + `bookingReplicaTimeout` (1200 s) | claimed at ~05:51 in the single pre-T0 write (§4.2) |
| watcher | now + 300 s (`watchReplicaTimeout`) | around each row's act |
| web | now + 60 s | cancel / skip / edit |

For the watcher and web paths, `tenant.store.LeasedBookingStore` (MU-9c) wraps `InMemoryStore` and
maps `request_lock(row_request_id)` to the row lease. On contention it raises `ConcurrentRunError`,
and the engine's **existing** defer handling applies unchanged.
**Revalidation under the lease (M5):** the watcher reads rows and logs in *before* the engine
reaches `request_lock`, so the row may have changed in between (user skip, web cancel, rule edit).
The acquire therefore also compares the `RowFingerprint`
(`status`, `version`, `booked_raw_id`) the runner registered when it read the row against the
doc it just point-read. The `IfMatch` replace makes check-and-set atomic. Every web write bumps
`version`. A mismatch means "not acquired": `ConcurrentRunError`, the engine defers,
and the next run re-reads. So the watcher can never book a date the user just skipped, or re-book
a row the user just cancelled. The booker uses a plain
`InMemoryStore` because its lease is already held from the claim. A crash leaves a lease that
expires; stale booker leases set `needs_reconcile` on the watcher's next run.

### 3.6 Cross-course hook (FOLLOW-UP, designed only)

The row doc's `group_id` + `group_rank` link one user's rows for the same date across courses
(rank 0 = most preferred). The v1 web rejects creating a second row for the same (user, date) on a
different course; only MB is hosted, so the case cannot arise. Follow-up semantics: when a
higher-ranked row in a group reaches `booked`, the watcher runs the §8.5 managed cancel on
lower-ranked `booked` rows in the same group. Lower-ranked rows are attempted only if their drop
comes earlier. No engine change is needed; it is a tenant-watcher rule over the group.

### 3.7 Store Protocols

`BookingStore` is **unchanged**. It stays the engine's in-run memory (`InMemoryStore` per process).
A **sibling** `tenant.store.TenantStore` Protocol carries the durable ownership/intent data (the
M3 cut is reversed for multi-user, which resolves the PLAN §12 residual). Implementations:
`InMemoryTenantStore` (tests and the reference for the conformance suite, MU-5) and
`CosmosTenantStore` (async `azure-cosmos`, MU-8). Both run the **same parametrized conformance
suite**: in CI against the in-memory store only, and `integration`-marked against the real
free-tier `dev` database (there is no emulator in CI, §10.2).

---

## 4. T0 timeline for N accounts, and the "zero new calls at T0" proof

### 4.1 Actors

One ACA job execution for release event `mb0600et`. Inside the process: one `SharedCaptchaPool`
per course, one `MangroveBayAdapter` per account (each with its **own** httpx client, cookies,
login cache, and JWT), one `Orchestrator` per account, one shared `InMemoryStore` (distinct
RequestIds per row, §3.3), and one `BufferingNotifier` per account (no email before the burst
finishes).

### 4.2 Exact sequence (times are ET, event = MB 06:00, advance 7)

| Time | Step | DB | ForeUP | 2captcha |
|------|------|----|--------|----------|
| 05:50 ±jitter | cron; container start (30–60 s, AZURE_PLAN §5.2) | – | – | – |
| ~05:51 | NTP offset (the existing `core.clock.measure_ntp_offset` → `RealClock(offset=…)`, exactly as `_run` does at `__main__.py:211`); **DST gate** `should_proceed(clock, timezone=event.tz, fire_time=event.release_time)`, pure. A wrong-season cron exits 0 before any I/O | – | – | – |
| ~05:51 | load env: `TENANT_COSMOS_ENDPOINT`, `TENANT_COSMOS_DATABASE`, `AZURE_CLIENT_ID`, keyring, `TWOCAPTCHA_API_KEY` | – | – | – |
| ~05:51 | token via `ManagedIdentityCredential` (cached ~24 h, reused post-race; §10.2) | IMDS | – | – |
| ~05:51 | **READ #1** (the only read phase): pending rows with `course_id ∈ event.courses`, `target_date = release_policy.target_date_for(now)` (course tz), `cutoff_at > now`, joined to active accounts (ciphertexts). Python re-checks `frozen_reason`. **This replaces the booking-day gate and the skip secret.** No rows means exit 0 | 2 queries (rows, then their account docs) | – | – |
| ~05:51 | **WRITE #1** (claim): conditional lease update on those rows (`owner=attempt_id`, `until=T0+1200 s`). Rows whose lease is held (a watcher mid-act) are retried every 15 s until T0−150 s, then skipped with a WARNING (the watcher cannot book today+7 before 06:00, because inventory is unpublished) | 1 IfMatch replace per row | – | – |
| ~05:51 | close the Cosmos client (no idle connection across the wait; the credential and its cached token are kept) | – | – | – |
| ~05:51 | decrypt each account's password (AES-GCM, µs) and `register_secret_literals`; a per-row decrypt failure skips that row (§4.5) | – | – | – |
| ~05:51 | site-key pre-flight GET **once per course** (the existing `_resolve_site_keys`) | – | 1 GET | – |
| ~05:51 | build adapters (shared pool), each wrapped in the in-memory **recording decorator** (§4.6); **allocation** (pure): `synthesize_blind_slots` per account, `allocate_blind_slots` over a rotating draft order, `set_blind_allowlist` per account (§5.4); register pool demand k_i per account **including over-cap accounts with k_i = 0** (so their `prepare_book(count=3)` joins the coordinated fill and receives nothing, instead of solving 3 tokens outside the C bound), plus shared reserve R | – | – | – |
| ~05:51 | build N `Orchestrator(prefetch_book=True, scheduler=S′)` where `S′ = scheduler` with `blind_post_fallback_token_reserve=0` (the pool holds R); overflow accounts get `blind_post_max_count=0` (§5.5) | – | – | – |
| ~05:51 | `asyncio.gather(*(run_account(i) ...), return_exceptions=True)`. Each `run_account` is `try: await orch.run(req) finally: pool.release(key)` | – | – | – |
| T0−120.5 s | every orchestrator's **unchanged** two-phase busy-wait reaches `prefetch_at`; `_prewarm_primary` gathers `_prewarm_login` ‖ `_prefetch_captcha_for` | – | N × (warm GET + login POST) | – |
| T0−120.5 s | the first `prepare_book` → `pool.prefetch(key, count)` starts **the one coordinated fill** (§5.3); the rest await it. The count argument is ignored in coordinated mode (demand was registered) | – | – | D = Σk_i + R solves, ≤ C concurrent |
| ~T0−45…T0−10 | fill ends at the deadline T0−10 s; leases granted round-robin (§5.2); prefetch returns | – | – | – |
| T0−0.5 s … T0 | per account: the **unchanged** staggered burst `(-500,-250,0)`, each POST popping from the account's lease | – | ≤ 3N POSTs | – |
| T0+… | per account, unchanged: keep-best + cancel-extras, **or** re-guard (`refresh_reservations`), then fresh search, then sequential book (lease → shared reserve → semaphore-bounded inline) | – | per account as today | only inline fallbacks |
| as each account returns | build its `AccountOutcome` from the returned `BookingResult` / captured exception **plus its recording decorator's log** (§4.6), and queue it | – | – | – |
| from T0 + `post_burst_quiet_s` (10 s) | **WRITE #2, streamed and per row (SF5, M4)**: a writer task starts at T0+10 s (when every blind burst plus the immediate `_cancel_extras` is long done) and writes each queued outcome in **its own transaction**: status, ledger entries, `last_outcome`, lease release, `needs_reconcile`. A refused transition (e.g. the row moved; impossible after the M4 lease guard, but defended anyway) still writes the ledger rows keyed by (account, target_date) and sets `needs_reconcile` on whichever row is active for that date. It **never rolls back other accounts**. Each write retries for up to 60 s; on final failure: CRITICAL + that outcome's JSON on stdout + non-zero exit (the watcher reconciles from live) | 1 txn per row | – | – |
| T0 + (replicaTimeout − elapsed_at_T0 − 90 s) | **self-deadline**: accounts still running (worst case: 8 accounts × several ~75 s inline fallback solves ÷ 6 concurrent) have their rows written `needs_reconcile` + lease released, and the run exits non-zero **before** ACA kills the replica. Outcomes already written are safe (SF5) | per row | – | – |
| after writes | drain `BufferingNotifier`s into per-user emails (§8.7); operator summary. **A failed operator-summary send makes the exit non-zero** (SF6) | – | – | – |

### 4.3 What each account does between T0−120 s and its return

It is **exactly** the single-user sequence of calls: `authenticate` → `list_reservations` (cache)
→ tokens → staggered POSTs → cancels/re-guard/search/book. Differences are internal to two
in-memory operations: (a) `book()` pops from a lease deque inside the shared pool instead of the
adapter's own deque; (b) `synthesize_blind_slots` filters by an allowlist computed at 05:51. Fewer
2captcha submissions happen overall (one fill of D instead of N fills of 5).

### 4.4 Proof obligations (each is a named red test in MU-9a/MU-9b)

1. **No DB call and no decrypt in [T0−lead−1 s, T0 + `post_burst_quiet_s`].** (The race window. Post-burst fallbacks may overlap a sibling's streamed write, which is harmless: by then nobody is racing the release flip.)
   `test_runner_no_store_calls_inside_race_window`: `InMemoryTenantStore` wrapped in a recording
   proxy that timestamps every call with `FakeClock`, and a crypto spy doing the same. Assert both
   call sets are disjoint from the window. The `ManagedIdentityCredential` is spied on too, so no
   IMDS token fetch or Cosmos call can hide in the window (§10.2).
2. **Same coroutine path.** `orchestrator.py` is not modified (a reviewer can `git diff --stat` it).
   `test_runner_uses_unmodified_orchestrator_per_account` asserts that the runner constructs
   `teetime.core.orchestrator.Orchestrator` (not a subclass) with `prefetch_book=True`.
3. **Per-account invariants hold.** Multi-account timing tests use a new **`VirtualClock`** (`src/teetime/dev/virtual_clock.py`, MU-9a). `FakeClock.sleep` advances one shared `_now` by each caller's delta (`clock.py:95-98`), so N concurrent sleepers would run time N× fast and scramble the measured offsets. `VirtualClock` keeps a heap of sleeper deadlines and advances `now` to the earliest one, waking only that sleeper (a discrete-event scheduler). `FakeClock` is untouched and single-account tests keep using it (SF4). The existing race-path suites (`test_orchestrator_blind_post.py`,
   `test_blind_post_stagger.py`, `test_captcha_pool.py`) run unmodified. A new
   `test_runner_two_accounts_each_get_stagger_and_rank0_first` asserts that each account's first
   POST is at `-early_arrival_ms` for its own rank-0 slot.
4. **Isolation.** `test_runner_one_account_auth_error_does_not_affect_other` and
   `test_runner_one_account_uncertain_does_not_cancel_siblings` (gather with
   `return_exceptions=True`).

5. **The runner sees everything the bot booked.** `test_runner_records_cancel_extras_failure_as_held_extra`, `test_runner_uncertain_blind_post_sets_needs_reconcile`, `test_runner_prewarm_already_booked_is_unowned`, `test_runner_reguard_already_booked_after_uncertain_is_owned`, `test_runner_swallowed_blind_captcha_error_exits_nonzero` (M1, §4.6).

The CLAUDE.md race-path invariants hold per account because each account runs the untouched
`Orchestrator`:
`stagger[0] == -early_arrival_ms` (same `scheduler.blind_post_stagger_ms`); the five-part gate
(`_should_blind_post` is untouched; overflow accounts fail the `blind_post_max_count > 0` leg by
construction); rank-before-pair (unchanged, and it applies to the allowlisted list); keep-best +
cancel-extras (per account, its own session); reguard-before-fallback with `refresh_reservations`
(per account, its own adapter); the prefetch-lead warning (per account, identical instants); and
the soft-login-skip gate (per account `_prewarmed_course_ids`).

### 4.5 Failure isolation and the exit contract

| Condition | Row effect | Notify | Exit |
|-----------|-----------|--------|------|
| no pending rows | – | – | 0 |
| outcome BOOKED / ALREADY_BOOKED / DRY_RUN | booked (DRY_RUN: unchanged, logged) | user | 0 |
| NO_INVENTORY / RATE_LIMITED (all courses skipped) | stays pending (watcher keeps trying until cutoff) | user ("missed the drop; watching for cancellations") | **0**, a deliberate change from today's `ClickException` (see below) |
| `AuthError` (per account) | stays pending; account `auth_failed` (no further logins until the user re-verifies, per PLAN §12 "don't hammer login") | user + operator | 0 |
| `CredentialDecryptError` (one row) | stays pending | operator | **non-zero** (operator bug) |
| keyring missing/invalid, DB read or claim failure | none | – | **non-zero**, before T0 |
| `CaptchaError` / `OtpChallengeError` (any account) | stays pending | operator + user | **non-zero** (the solver/pool is shared, so this is systemic; OTP means ForeUP changed the API) |
| UNCERTAIN (any non-contract exception out of `orch.run`, **or** a blind `book()` that raised a non-`SlotGoneError` which the orchestrator swallowed at `orchestrator.py:525-537`, seen via the recording decorator) | stays pending (or booked if a sibling won), `needs_reconcile=true` | operator | **non-zero** (keeps today's loud-exit invariant, PLAN §9.1 invariant 2) |
| `CaptchaError`/`OtpChallengeError` swallowed on a **blind** POST (visible only through the recording decorator) | per outcome | operator | **non-zero** (closes the CLAUDE.md "blind-burst OTP residual" for the hosted path) |
| operator-summary email send fails | – | – | **non-zero** (SF6: otherwise a broken ACS setup makes every miss invisible, since misses exit 0) |
| self-deadline reached (SF5) | unfinished rows `needs_reconcile` | operator | non-zero |
| WRITE #2 failure after retries | none written; outcomes on stdout | operator | non-zero |

**Behaviour change, stated plainly.** Today a missed drop exits non-zero, so ACA shows "Failed".
In tenant mode, one user's miss is a normal product outcome and must not mark the job Failed for
everyone. The operator's "a Failed execution means something went wrong" signal is kept for
systemic causes only. Misses are visible in the per-user email plus the operator summary email
(§8.7). This closes the BACKLOG "persistent failure is invisible" gap for the first time, because
there is now an alert sink.

### 4.6 Recording decorator: how the runner learns what the bot did (M1)

`Orchestrator.run` returns only the kept `best` (`orchestrator.py:577-588`). Surplus bookings,
`_cancel_extras` failures (`:680-688`) and swallowed blind-POST errors (`:525-537`) never reach the
caller. The runner therefore wraps **each account's adapter** in `tenant.recording.RecordingAdapter`.
It is in-memory and does no I/O, so it adds no calls at T0 (it appends to a list). It records:

- every `book()` that returned BOOKED: raw id (the `TTB:` prefix stripped), slot, and send instant;
- every `book()` that raised anything other than `SlotGoneError` (UNCERTAIN: the POST may have
  landed), including a Captcha/OTP error the blind burst swallowed;
- every `cancel_reservation()` with its outcome (ok / exception class);
- every `refresh_reservations()` / `authenticate()` call, for diagnostics.

**Capability fidelity (SF1).** On Python ≥ 3.12, `runtime_checkable` `isinstance` uses
`inspect.getattr_static`, so a `__getattr__`-forwarding proxy **fails**
`isinstance(proxy, ReservationCacheRefreshable)`. `_reguard_before_fallback` (`:713`) would then
silently fall back to the idempotent `authenticate()`, read the stale pre-burst cache, and could
**double-book**. So `make_recording_adapter(inner)` picks a **concrete class per capability set**.
The variant for ForeUP defines `refresh_reservations`, `is_authenticated`, and `snapshot_trusted`
explicitly; the variant for FakeAdapter/TeeItUp defines none of them. `capabilities` is copied.
Pinned by `test_recording_adapter_isinstance_matches_inner` for every capability combination.

**Blind-POST members (round-2 SF1).** `capabilities` is copied, so a Mangrove Bay recorder reports
`blind_post=True`. The orchestrator then calls `cast(BlindPostCapable, adapter)` and invokes
`synthesize_blind_slots` (`orchestrator.py:421, 890`) and `captcha_pool_size` (`:428`) **on the
recorder**. Those methods are plain casts, not `isinstance` checks, so a missing method fails
**silently and late**: `:890` sits outside the try in `_prefetch_captcha_for`, its AttributeError
is swallowed by `_prewarm_primary`'s gather (`:769-773`), no pool fill starts, and `:421` then
raises at T0, leaving the account UNCERTAIN with no booking. Dev dry-run never reaches either call
site. Therefore:
- `make_recording_adapter` has a **blind-capable variant**, selected iff
  `inner.capabilities.blind_post`, that defines `synthesize_blind_slots` and `captcha_pool_size`
  as pure pass-throughs (not recorded, no I/O);
- the runner **asserts before T0** (at ~05:51, pre-busy-wait) that every adapter it hands to an
  `Orchestrator` with `capabilities.blind_post=True` has both methods via
  `inspect.getattr_static`, and exits systemic non-zero otherwise, which fails loud and early;
- **named test** `test_recording_adapter_blind_capable_end_to_end`: a blind-capable `FakeAdapter`
  wrapped by the recorder runs a full race-path `Orchestrator.run` (`prefetch_book=True`,
  `VirtualClock`) and must fire its staggered blind burst, with the recorder logging the BOOKED
  id. Plus `test_runner_refuses_blind_adapter_missing_blind_methods`.

**Ownership derived at write time:**

| Observation | Ledger / row effect |
|-------------|---------------------|
| recorded BOOKED id, later cancelled OK by `_cancel_extras` | `cancelled_extra` |
| recorded BOOKED id, cancel failed (429 / captcha / transport) | **`held_extra`, owned**, so the watcher's owned-only reconcile collapses it next run. This preserves today's "PR4 watch net will reconcile" promise, which a naive ownership filter would have broken |
| returned `best` (BOOKED) | `held`, row → booked |
| result ALREADY_BOOKED with **no** recorded book and **no** recorded UNCERTAIN (pre-T0 guard or reguard found an existing reservation) | row → booked, **unowned** unless its raw id is already in the ledger. A manual booking is never upgradable or cancellable by the bot |
| result ALREADY_BOOKED **after** a recorded UNCERTAIN blind POST (the reguard found the landed POST) | row → booked, `needs_reconcile`. The watcher adopts it **owned** if the tee time is one of the recorded UNCERTAIN slots (exact slot match, stricter than "in window") |
| BOOKED with `confirmation_code=None` (defensive; MB extraction was fixed in PR0) | row → booked from the slot, `needs_reconcile`; the watcher adopts by exact tee time |

The watcher uses the same decorator (inside the snapshot proxy), which is how it observes an
upgrade's cancel (M2, §3.4).

### 4.7 CPU headroom

N concurrent 1 ms fine busy-wait loops plus N TLS sessions at T0 on 0.25 vCPU can jitter the stagger.
The tenant booking job runs at **0.5 vCPU / 1 GiB**. Cost: ~9 drop runs/mo × 660 s × 0.5 ≈ 3 k
vCPU-s. The per-POST `sent …ms (planned …)` diagnostic already measures send offsets, so any jitter
is self-reporting.

---

## 5. Shared per-course CAPTCHA token pool

### 5.1 Why sharing is safe

ForeUP's token is a reCAPTCHA v2 invisible token for the **site key + page URL**. The 2captcha solve
has never seen a ForeUP session: `make_2captcha_provider(api_key, page_url, site_key)` takes no
cookie or account. Every live booking to date has therefore used a session-independent token.
Sharing tokens across accounts of the same course is the existing mechanism, not a new assumption.
Tokens from different courses are **not** shared (different page URL or site key), so there is one
pool per course.

### 5.2 Semantics (`courses/foreup/token_pool.py::SharedCaptchaPool`)

- **Two modes.**
  *Uncoordinated* (no demand registered for the key): `prefetch(key, count)` does exactly today's
  `prepare_book` (solve `count` concurrently, append to the key's lease, NI10 raise contract:
  count==1 re-raises, count>1 never raises). A `ForeUpAdapter` constructed without a pool gets a
  private uncoordinated pool, so the single-user CLI path is byte-identical (MU-2's gate is that
  `tests/test_captcha_pool.py` passes unmodified).
  *Coordinated* (the runner called `register(key, k)` for every account plus `set_reserve(R)`
  before any orchestrator started): the first `prefetch` starts the single fill task and every
  `prefetch` awaits its completion.
- **Leases prevent starvation.** Each adapter pops only from **its own lease** first. Account B
  cannot consume account A's rank-0 token. After its lease: the **shared reserve**, then an inline
  solve.
- **Round-robin grant.** Tokens are granted in arrival order: round 0 gives one token to each
  account in draft order, round 1 gives the second to each, and so on until each lease reaches k_i.
  Remaining arrivals go to the reserve. If the fill falls short, every account's **rank-0** POST
  still has a token before anyone's surplus POST does.
- **Freshness order.** The earliest arrivals go to burst leases (fired at T0−0.5…T0). The latest go
  to the reserve (used by fallbacks at T0+5…T0+30 s). That matches today's FIFO intent.
- **FIFO single-use pop** within a lease (`popleft`, never returned).
- **MF1 is preserved.** A token from a lease or the reserve counts as `from_pool=True`, so a
  captcha-challenge on it triggers exactly one inline re-solve and re-POST.
- **`_captcha_solve_sem` moves into the pool**, so the inline-solve bound (default 6) is **per
  course, across all accounts**. That is a stricter guard than today's per-adapter bound.
- **`captcha_pool_size()`** returns the account's **lease** size, so the orchestrator's burst size
  `n = min(len(blind_slots), captcha_pool_size())` is unchanged in meaning.
- **`release(key)`** (called by the runner's `finally`) moves unused lease tokens into the reserve.
  An account that short-circuited ALREADY_BOOKED before T0, or booked on its first POST, donates
  its leftovers to other accounts' fallbacks.
- Tokens carry `solved_at` for diagnostics only. There is **no age-based discard**: the discard
  decision stays with ForeUP's response plus MF1, exactly as today.

### 5.3 Fill schedule and the account cap

Facts: token validity V ≈ 120 s from issuance. Prefetch lead L = 120 s. Observed live solve
latency is **~75–78 s** (the 2026-06-07 and 2026-06-14 post-mortems, `captcha.py` docstring
"typically 15–30 s" is optimistic). The 2captcha polling cadence is 5 s. **Concurrency C ≥ 12 has
run live**: `blind_post_max_count=12` was deployed 2026-06-22 → 06-29, which prefetched ~12
concurrent solves per drop.

- **Wave 1** (burst tokens): all submitted at T0−120 s with concurrency ≤ C. With S ≈ 75 s they
  land around T0−45 s, aged ≤ ~105 s at T0 (valid). With S_p90 ≈ 80 s, a second round per worker
  cannot finish before T0, so **burst capacity per drop = C**.
- **Wave 2** (reserve R): submitted as wave-1 workers free up. They land around T0−40…T0+30 s,
  which is ideal for the fallback, since it fires after T0 and wants the freshest tokens. The fill
  task does not block prefetch return on wave 2. Prefetch returns once wave 1 is resolved or at the
  T0−10 s deadline, whichever comes first. Wave-2 tokens join the reserve as they land.
  **Accepted residual:** a fallback that finds the reserve empty starts an inline solve. A wave-2
  token landing a moment later cannot be handed to that in-flight solve, so it is wasted (~$0.003).
  `pop()` always checks the reserve before starting an inline solve, which keeps this rare.
- **Cap:** `N_blind = floor(C / burst_size)`. With **C = 12** (the live-proven default) and
  `burst_size = blind_post_max_count = 3`, that gives **4 accounts per release event per course**
  that get a blind burst. After Spike S-M1 confirms C = 18, it becomes 6.
- **Cost:** (3N + R) × ~$0.003. N = 4 costs ~$0.04 per drop and ~$0.40/mo.
- **N = 1 is today's number exactly:** 3 burst + 2 reserve = 5.

**Account N_blind+1 onward** (for that drop, per the rotating draft order, §5.4) runs the **search
race path**: `blind_post_max_count=0` in its `Orchestrator`, so the five-part gate fails. It is
**still registered with the pool, with k = 0**. Otherwise `_captcha_prefetch_count_for` would return
the default 3 (`orchestrator.py:879-880`), its `prepare_book` would take the uncoordinated branch,
and it would solve 3 tokens outside the C bound. Registered with k = 0, its prefetch joins the
coordinated fill and receives nothing. It
searches at T0 (with `skip_initial_spacing`) and books with a reserve token or an inline solve.
That is slower (likely to lose prime slots at a contested drop), and it is honest about it. The
dashboard labels such a row "search-only this drop". **Hard cap:** at most `max_accounts_per_course
= 8` connected accounts per course, enforced at connect time (§8.4). This bounds both the token
math and the anti-bot footprint (§9.6). v1 has 2 users, so neither cap binds.

### 5.4 Cross-account slot allocation

Two accounts wanting overlapping windows must not blind-POST the same slot. If A's surplus POST
books B's rank-0, A cancels it seconds later but B's POST has already been rejected.

- **Input:** for each claimed row, the full ranked in-window candidate list from that account's
  `synthesize_blind_slots` (unfiltered, `max_count=grid size`).
- **Draft order:** rows sorted by `row.id`, **rotated** by `target_date.toordinal() mod N`. Over a
  season every account holds first pick equally often. The operator may prefer a fixed priority
  instead (§13 Q4).
- **Snake draft:** in each round, walk the order (reversed on odd rounds). Each account takes its
  highest-ranked slot not yet taken, until it holds `burst_size` or its list is exhausted. The
  output is `allowlist[row] = frozenset[SlotId]`.
- **Guarantees:** (a) disjoint across accounts; (b) each account's first pick is its best
  **available** slot. Two accounts get distinct rank-0 slots whenever the in-window grid has ≥ N
  slots; if not, later accounts in the order get fewer or zero slots and run search-only;
  (c) accounts with **disjoint windows are unaffected** (each gets its own top-3). Operator
  08:45–10:00 versus Turk's earlier window means both get exactly today's burst.
- **What each adapter receives:** `MangroveBayAdapter.set_blind_allowlist(allowlist[row])` before
  T0. Its `synthesize_blind_slots` then returns the ranked candidates ∩ allowlist, truncated to
  `max_count`. `_captcha_prefetch_count_for` and `_blind_post_course` both call it, so the token
  count and the burst agree (the RESEARCH_FALLBACK §2 Q3 static-grid invariant still holds).
- **Fallback is not allocated.** After 0-booked, accounts search and book first-come. Two accounts
  may collide; the loser gets `SlotGoneError` and moves to its next candidate. That is the correct
  and cheap behaviour.
- **daily_limit tagging still means what it means.** Each account's POSTs go through its own
  session, so ForeUP's per-account 1/day counter, and therefore `SlotGoneError.reason ==
  "daily_limit"`, is scoped to that account's burst. `_rejection_summary` is untouched and reads
  per account (the log line includes the course; the runner prefixes the row id via a logging
  `extra`/adapter).

---

## 6. Release policy, job derivation, and DST

### 6.1 `core/release_policy.py::ReleasePolicy`

`ReleasePolicy(advance_days: int, release_time: time, timezone: str)` is a frozen dataclass,
attached as a `ClassVar` on the adapter class (E4). It lives in `core/` because adapters may not
import `tenant/` (layering, §2.2). Pure helpers (stubs):

- `release_instant_for(policy, now_utc)`: today's release instant in the course tz.
- `target_date_for(policy, now_utc)`: `local_today + advance_days`, computed in the **course
  timezone** and never the runner's.
- `cron_pair(policy, lead_minutes=10)`: `(dst_cron, std_cron)` UTC crons for `release − lead`.
  Examples: MB 06:00 America/New_York gives `50 9 * * *` / `50 10 * * *` (identical to today's
  `compute.bicep`). A hypothetical 06:00 America/Chicago release gives `50 10 * * *` / `50 11 * * *`.
- `validate_release_policy`: **v1 requires `4 <= release_time.hour <= 22`**. The existing DST gate
  compares `hour == fire_time.hour − 1`, and `Orchestrator._compute_t0` uses `local_now.date()` +
  `fire_time`. A **midnight** release (common on TeeItUp) would fire the cron at 23:50 on D−1,
  compute T0 as 00:00 on D−1 (in the past), and fire ~24 h late on the wrong target date. Hours
  01–03 also collide with DST transition gaps. Midnight support needs an engine change to "next
  occurrence" T0 and is a follow-up (§14).

| Course | ReleasePolicy | hosted_booking | Notes |
|--------|---------------|----------------|-------|
| Mangrove Bay (`foreup:mangrove_bay`) | (7, 06:00, America/New_York) | **True** | matches prod since v0 |
| Sydney R. Marovitz (`teeitup:sydney_marovitz`) | (15, **UNCONFIRMED**, America/Chicago) | **False** | 15 days is CPD policy (module docstring and the [cpdgolf FAQ](https://www.cpdgolf.com/frequently-asked-questions)); the daily release **time is not documented**, see Spike S-M4. PAN path out of hosted scope |

### 6.2 Release events and jobs

A **release event** is a distinct `(timezone, release_time)`. Each event gets **one EDT/EST job
pair**. Several courses may share an event, each with its own `advance_days`, so the read in §4.2
selects per course. The single source of truth is `infra/bicep/release_events.json` (added in
MU-15a), which `compute.bicep` and `killswitch.bicep` both read with `loadJsonContent`:

```
[{ "key": "mb0600et", "timezone": "America/New_York", "releaseTime": "06:00",
   "courses": ["foreup:mangrove_bay"], "cronDst": "50 9 * * *", "cronStd": "50 10 * * *",
   "jobNamePrefix": "teetime-job" }]
```

- **Parity test** (`tests/test_release_events_parity.py`, MU-15a): every event's crons equal
  `cron_pair(policy)` for each listed course's `ReleasePolicy`, and all listed courses share
  (tz, release_time). Each `hosted_booking=True` course appears in exactly one event.
- **Job names:** the MB event keeps the **legacy names** `teetime-job-<env>-edt/-est`
  (`jobNamePrefix`), so the cutover (§11) flips arguments on the existing resources instead of
  creating a parallel job. New events use `teetime-rel-<key>-<env>-<half>`, which is ≤ 32 chars
  (the ACA job-name limit) when key ≤ 10 chars. The parity test asserts the length.
- **Arguments:** a per-env `bookingMode` param (`toml` | `tenant`). In toml mode the args are today's
  `run --config /app/config/container.toml --wait --dry-run <x>`. In tenant mode they are
  `tenant-run --event <key> --wait --dry-run <x>`. One resource per half means **one mode per env
  at a time**, which structurally rules out two paths booking for the same account.

### 6.3 DST

Each event's job pair runs the existing `should_proceed` with `timezone=event.timezone` and
`fire_time=event.release_time`. The runner computes today and the target in the course tz
(`target_date_for`). A Chicago event's wrong-season cron lands at 04:50 or 06:50 CT and exits 0.
No new DST logic is needed; the gate is parameterized per event instead of read from TOML.

### 6.4 TeeItUp

The Marovitz policy exists as data so a second event can be exercised in the parity and allocation
tests. The event table carries only `hosted_booking=True` courses, so **no Marovitz job is
deployed** in v1. Its TeeItUp `book()` needs a PAN, which is out of hosted scope.

### 6.5 The MB grid, and why Turk needs one code change first

`BLIND_POST_MORNING_GRID` is a **hard-coded list covering 08:45–10:00**. A user whose window is
earlier (Turk) gets **zero** blind slots and falls to the slower search race path. So "Turk is a
data change" becomes true only **after MU-3** widens the grid once to the full morning, using the
proven `:00,:07,:15,:22,:30,:37,:45,:52` cadence from the first tee to ~12:00. The first tee time
is unverified, see Spike S-M7. Grid points outside every requested window are never POSTed
(synthesize intersects with the window). This does **not** confound the STAGGER diagnostic: the
BACKLOG concern is about widening the **window**, which changes which slots get emitted for the
operator; widening the **grid** leaves the operator's emitted slots byte-identical. Pinned by
`test_widened_grid_emits_identical_slots_for_0845_1000_window` (MU-3).

---

## 7. Watcher redesign

### 7.1 Per-run flow (`teetime tenant-watch`, `runner.run_tenant_watch`)

1. **One DB read phase** (one rows query + one accounts query): rows with `status ∈ {pending, booked}` and `target_date` within each hosted
   course's horizon `[local_today, local_today + advance_days]` in that course's tz, plus
   `cutoff_at > now` for pending rows, joined to active accounts, plus those accounts' latest
   snapshots. Also, **finalizer**: pending rows now frozen become `lost` (one batch per affected partition; a `lost`
   email is queued once). **Materializer tick** (§7.7).
2. **Shared search** per `(course_id, target_date, party_size)` group (§7.2): **one `search()` per
   group**, a single unauthenticated client per course, spaced ≥ 250 ms (PLAN §12).
3. **Per-row decision** (pure, `watcher.needs_login`): login-and-act only if one of these holds:
   - **pending** and `rank_slots_for_request(group_slots, row_request)` is non-empty (a bookable
     in-window slot);
   - **booked, owned**, and a slot exists that is strictly better than `booked_tee_time` under the
     same tier/midpoint rule `UpgradeOrchestrator` uses (an upgrade candidate);
   - `needs_reconcile` is true, or the row carries a stale booker lease;
   - **reconcile cadence**: `(account_id.int + run_index) % 6 == 0` (the **UUID integer**, which is
     stable across processes; never Python's `hash()` of a str, which is salted per process), where `run_index =
     floor(epoch_minutes / 10)`. This means ~hourly per account, spread across runs, with no state;
   - the snapshot is older than 90 min for an account with a booked row (a backstop).
4. For each account needing login (sequentially, ≥ 250 ms apart): `authenticate`, check
   `is_authenticated` **and** snapshot health (§7.5), `list_reservations`, **persist snapshot**,
   **adopt** (§7.6). For a **booked** row with the upgrade policy on, set `upgrade_started_at` under
   the row lease (one IfMatch replace that also revalidates the fingerprint, M5). Then run the
   engine for that row: an **unmodified** `WatchOrchestrator` with that account's adapter wrapped
   as `make_search_snapshot_adapter(make_recording_adapter(inner), slots)`. Search is served from
   the step-2 result; everything else is delegated live. **Both proxies are per-capability
   concrete classes** (SF1, §4.6). Also passed: a `LeasedBookingStore` (M5 revalidation),
   `reconcile_eligible` = ownership (§7.6), and the row's `BOOKED` terminal **pre-seeded** into the
   in-run store for booked rows, so Gate 3 plus reconcile plus upgrade run exactly as today.
5. **Outcome writes** per row (under its lease), derived from the **recording decorator**, not the
   store terminal (M2):
   - a new BOOKED with the old id cancelled → upgraded (ledger: old `cancelled_upgrade`, new `held`);
   - old id cancelled and no new BOOKED → pending + `needs_reconcile` (a bot-caused loss; re-book allowed);
   - otherwise unchanged.

   `upgrade_started_at` is cleared in the same transaction. If the process dies first, the marker
   survives, and the next run treats a missing reservation as bot-caused, not external.
6. Emails; exit contract per §7.9.

### 7.2 Why group by party size

ForeUP `/times` is called with `players = party_size`, and MB hides slots with fewer open spots
(the `available_spots >= len(players)` filter, plus MB returns `[]` for players=1, per courses
CLAUDE.md). Searching once with the minimum party and filtering locally would **overstate**
availability for larger parties and trigger useless logins. So group by `(course, date,
party_size)`. The search request uses the **union** of the group's windows (ForeUP filters locally
anyway), and each row re-ranks with its own window. The 0-match diagnostic fires per group.

### 7.3 Call budget (per 10-min run, 2 users, Sat+Sun rows)

~2–4 searches (dates × party sizes), plus ~0.33 logins per account per run from the cadence, plus
logins only on real opportunities. Today's single-user watcher does 2 searches plus 2 logins every
run. The tenant watcher with N accounts does **fewer logins per account** than today, and searches
scale with distinct (date, party_size), not with N.

### 7.4 Snapshot persistence (dashboard truth)

Every watcher login upserts the account's `snapshot` doc (`observed_at`, `trusted`, entries) in its `tenant` partition. The
dashboard shows DB row status **plus** the snapshot with its age ("as of 7 min ago"). The site
therefore stays at most ~one cadence stale with **zero extra ForeUP calls**. Mismatch badges: row
booked but not in a trusted snapshot means "not seen at course"; snapshot shows a reservation with
no owned booking means "manual reservation".

### 7.5 Snapshot trust (a failure mode the engine had not surfaced)

ForeUP's `authenticate()` has three quiet degradations. (a) A **soft login failure**
(400/401/rejected body) returns without raising and **clears** the reservation cache. (b) A
**200 with a non-JSON body** sets `_logged_in=True` and does **not** rebuild the cache (`base.py`). (c) A **JSON
success body whose `reservations` is missing or not a list** also sets `_logged_in=True` and leaves the
cache untouched (`base.py:494-496`) (SF3). On a first login the cache is therefore **empty**; on a
refresh (`refresh_reservations`) it is **STALE** — the previous login's list (PR #226 review). In
every case `list_reservations()` returns `[]` or an out-of-date list, and `[]` to a naive tenant watcher looks like "the booking
vanished", leading to cancelled(external) or a re-book (**double booking**). Rules:

- (a) is detected by `is_authenticated` (`AuthStateReportable`, existing). The snapshot is **not**
  persisted and `consecutive_soft_auth_failures` is incremented. At 3, the account becomes
  `auth_failed` (stop logging in; notify the user).
- (b) and (c) are detected by the new `ReservationSnapshotHealth.snapshot_trusted` (E6). ForeUP sets it
  false on the non-JSON branch **and** when `reservations` is absent or not a list. The snapshot is persisted with `trusted=false` and never used for
  vanish inference or adoption.
- **Vanish** needs the reservation absent from **two consecutive trusted** snapshots taken ≥ 10 min
  apart, **and** all of the following:
  - `upgrade_started_at` is NULL (no bot upgrade in flight or crashed; otherwise pending + `needs_reconcile`);
  - the id is not ledgered `cancelled_upgrade` / `cancelled_extra` / `cancelled_user`;
  - there is **no replacement** reservation for the same (date, party_size). If there is one, it is
    **adopted** as the row's booking: owned iff ledgered or it matches a recorded upgrade book;
    otherwise unowned. It is never read as an external cancel.

  Only then does the row go booked → cancelled(`external`) with a user email ("we noticed you
  cancelled your <date> tee time at <course>; we won't re-book it"). **Operator decision
  (2026-09-25): an external cancel is the EXPECTED common case, not an anomaly.** It is detected,
  marked, and notified, and **the bot does not re-book**. The M2 exclusions above guarantee that an
  upgrade in flight (or one that crashed) is never misread as external.
- **Re-requesting after an external (or user) cancel.** A cancelled row leaves the active set, so
  its **date-slot doc is deleted in the same batch** (§3.2) and the (account, date) becomes free.
  The dashboard shows a "Re-request this date" action on cancelled rows. It creates a **new
  explicit row** (`row|x|<uuid>`, pending) under the normal create guard (not frozen, slot free).
  The booker then tries it if it is exactly today+advance; otherwise the watcher tries it. The
  cancelled row is a **user-terminal** entry in the (account, date) history, so **no** rule (not
  even a brand-new one) ever re-materializes that date (§7.7 step 1). Re-booking is always an
  explicit user act. Pinned by `test_external_cancel_frees_slot_for_explicit_rerequest`,
  `test_materializer_does_not_resurrect_cancelled_rule_row` and
  `test_new_rule_does_not_resurrect_external_cancel`.

### 7.6 Ownership, adoption, and the reconcile crash-net

- The `booking` docs (in each account's `tenant` partition) are the ownership ledger. Every reservation the booker or watcher creates is recorded
  with its **raw id** at outcome-write time. Blind extras that `_cancel_extras` cancelled are
  recorded as `cancelled_extra`.
- **Adoption** (before the engine runs for a row): if a pending row's fresh trusted snapshot shows
  a reservation matching (date, party_size), the row becomes `booked`. It is **owned** only if the
  raw id is in the ledger, or `needs_reconcile` is true and the tee time falls inside the row
  window (the crash-between-POST-and-write case, where the bot most likely made it). Otherwise it
  is **unowned** (manual). The seeded terminal carries `TTB:<raw>` only when owned, so
  `maybe_upgrade`'s managed guard **refuses to upgrade or cancel a manual reservation**. This
  closes an engine gap: `_check_course`'s `_synthesize_managed_booking` would otherwise treat any
  live match as managed.
- **Duplicate reconcile** (`_reconcile_duplicate_reservations`, unchanged logic) runs per account.
  `reconcile_eligible` (E5) restricts keep-best-cancel-rest to **owned** reservations (plus the
  `needs_reconcile` in-window rule above). A deliberate manual second booking is therefore never
  cancelled. This resolves the PLAN §12 "single-user residual" for the hosted path.
  **Documented residual (nit):** when a bot booking and a manual booking coexist for the same
  (date, party), only the owned one is eligible, so **both stay held**. ForeUP's 1/day rule normally
  prevents the second one from being created. If it happens anyway, the dashboard flags "manual
  reservation" and the user decides.
  **E5 does not guard the upgrade (MU-4 review):** with zero eligible matches, a contended
  `request_lock` (the reconcile defers and returns `matching` unchanged), or a single manual
  match) `_check_course` still synthesizes a `TTB:` booking from `matching[0]` and calls
  `_try_upgrade`, which can cancel a manual reservation (pinned by
  `test_unadopted_manual_match_reaches_try_upgrade_unguarded`). **MU-10 MUST gate `_try_upgrade` on
  ownership** — adoption + the pre-seeded non-`TTB:` terminal above, so an unowned match never
  reaches the engine's live-reservation upgrade.
- **Documented residual (nit):** the `needs_reconcile` adoption rule claims ownership only on an
  **exact tee-time match** with a recorded UNCERTAIN slot (§4.6), not merely "in window". A manual
  booking made by the user for the *very same slot* during the reconcile gap would still be
  adopted as owned. That is negligible because ForeUP's 1/day rule would reject one of the two.
- The booker's `_cancel_extras` is unchanged; it only ever cancels ids its own `book()` returned.
- **Ledger-only bookings are orphans** (MU-5 review SF4): when an outcome's row was refused and
  the date has no active row, the booking survives only as a ledger entry (and in
  `record_outcomes`' `ExceptionGroup`, which the runner/watcher treat as a non-zero exit). The
  ownership report lists owned ledger entries with no BOOKED row for their (account, date) as
  orphans for the operator.
- **`needs_reconcile` rows stay watched (round-6).** A rule row set pending + `needs_reconcile`
  (upgrade cancelled the old slot, rebook fate unknown) stays in `load_watch_rows` and out of the
  `rows_no_longer_covered` sweep EVEN IF its rule no longer covers it, so a rebook that landed is
  still adopted here instead of becoming an untracked reservation. The reconcile either adopts it
  (→ booked, normal rules apply) or clears the flag (the next tick then sweeps the row). It is never
  offered to the booker (`load_event_rows` still filters it). Pinned by
  `test_needs_reconcile_row_stays_watched_after_rule_moves`.

### 7.7 Materializer

- `materialize_rule` inserts rows for `d ∈ [local_today, local_today + horizon]` with
  `d.weekday() == rule.weekday`, where `horizon = max(21, advance_days + 7)`. It skips dates where
  `frozen_reason(now, d, ...)` is not None (**no rows already past cutoff**) and dates in the past.
  For each date, the materializer does **not** trust an id collision to mean "already
  handled" (round-2 M1). It does one single-partition query of the account's rows for that date,
  then applies `classify_date_history` (pure, `tenant/materialize.py`):
  1. a **user-terminal** row exists (cancelled `user`/`external`/`already_gone`; a withdrawn
     one-off `user_withdrawn` is **not** terminal, round-3 M1) → **skip the date**. This holds for
     *any* rule, including a new rule_id created after an external cancel, so operator decision (c)
     is never violated;
  2. this rule's own row (`row|rule|<rule_id>|<date>`) exists:
     - pending/booked/skipped → nothing to do;
     - withdrawn for a **system** reason (`rule_weekday_changed`/`rule_deactivated`/`rule_deleted`)
       and the slot is free → **reactivate** (batch: IfMatch replace to pending or `superseded_from`, with window/party
       refreshed from the current rule + create the slot);
     - withdrawn for a system reason and the slot is HELD (e.g. by a one-off) → leave it: **round-6 decision**, the one-off's withdraw batch restores it (to `superseded_from` or pending, refreshed from the rule) when the rule is ACTIVE, still covers the row (`rule.weekday == target_date.weekday()` and same account), the date is not frozen, and there is **no user-terminal row for (account, date)**, so deactivate → reactivate (refused: slot held) → withdraw one-off no longer strands the date, and a rule moved to another weekday never gets a row back on the old one. Pinned by `test_withdraw_explicit_restores_system_withdrawn_row_of_active_rule`,
       `test_withdraw_explicit_does_not_restore_row_of_weekday_changed_rule` and
       `test_withdraw_explicit_restores_row_when_weekday_flipped_back_while_slot_held`;
     - superseded and the slot is still held → leave it (round-4 D1: a rule deactivate/delete/
       weekday change has already WITHDRAWN superseded rows, so a still-superseded row always
       belongs to an active rule; the materializer never writes superseded → pending);
  3. no own row: if the slot is free, create the row + slot (batch). If an active row from
     elsewhere holds the slot, create this row as `superseded`.

  Pinned by `test_weekday_flip_back_rematerializes`,
  `test_new_rule_does_not_resurrect_external_cancel`,
  `test_deactivate_then_reactivate_rematerializes`,
  `test_withdrawn_explicit_restores_superseded_rule_row`,
  `test_withdrawn_explicit_does_not_block_rule_rematerialization` (round-3 M1 scenario A: rule
  row superseded by a one-off, one-off withdrawn, rule deactivated + reactivated → the date must
  get a pending row again), and `test_new_rule_materializes_date_of_withdrawn_explicit` (round-3
  M1 scenario B: one-off created then withdrawn, then a rule for that weekday → the date gets a
  row). **Round-4 decisions (MU-5 review):** (D1) `apply_rule_edit` withdraws superseded rows on
  deactivate/delete/weekday change, so scenario A resolves through the system-withdrawn REACTIVATE
  path; (D2) withdrawing a one-off restores the rule row to its pre-supersede status (pending or
  skipped); (round-5) reactivation restores it too. See §3.4.
- **Deactivation / deletion order (MU-5 review round 2).** Deactivating or deleting a rule is
  NOT atomic across rows: the rule write and each row withdrawal are separate batches (different
  documents, and the rows can be many). The web deactivation flow is therefore, in order:
  (1) `reset_materialized_through(rule)` (the rule becomes due for the tick); (2) withdraw the
  unleased PENDING / SUPERSEDED rows; (3) `reset_materialized_through(rule)` AGAIN (a concurrent
  tick may have re-advanced the watermark between (1) and (2); round-4 SF-B); (4) write the rule
  inactive. A crash after (1), (2) or (3) leaves an ACTIVE rule with a cleared watermark, so the
  next tick re-materializes (reactivating the withdrawn rows) instead of skipping it (MU-5 review
  round 3 SF2; pinned by
  `test_reset_materialized_through_puts_rule_back_on_the_tick`). A stored None watermark wins over
  the caller's copy in `upsert_rule` (a reset is never undone by a stale object; round-4 SF-A,
  `test_upsert_rule_honours_a_reset_watermark`). **Residual (accepted):** a tick that re-advances
  the watermark between (3) and a crash before (4) leaves an active rule with a current watermark
  and some withdrawn rows. **The DAILY TICK repairs it:** the horizon end (`local_today + horizon`)
  moves forward every day, so the rule is due again within a day, and `materialize_rule` walks the
  FULL `[today, today + horizon]` range (never only the dates after the watermark), reactivating the
  withdrawn rows. It needs a crash AND a race inside one web request.
- **Reactivation and weekday change (round-5 SF-2, coordinator decision).** `upsert_rule` itself CLEARS
  the watermark when the weekday changes or `active` flips to True, in the same write, so there is
  no separate reset step in those flows: a crash before the web's synchronous materialize still
  leaves the tick work to do (pinned by `test_reactivation_clears_watermark_in_upsert` and
  `test_upsert_rule_clears_watermark_on_weekday_change`). **A weekday MOVE is therefore: (1)
  `upsert_rule` with the new weekday (clears the watermark); (2) withdraw the old-weekday unleased
  PENDING / SUPERSEDED rows (`rule_weekday_changed`); (3) materialize the new weekday.** The
  reset → withdraw → reset → inactive order applies to deactivation and deletion ONLY.
- **Rows a deactivation or weekday move had to skip (MU-5 review round 3 MF1, widened in round 5).**
  "Rule edits never touch leased rows", so a row the booker or watcher holds at deactivation (or
  when the rule moves to another weekday) stays PENDING under a rule that no longer covers it. Three guards make sure it is never booked and never reported
  lost: (i) `load_event_rows` AND `load_watch_rows` exclude PENDING rows their stored rule no longer
  covers (held bookings are still watched); (ii) `finalize_lost` WITHDRAWS such a row instead of
  marking it LOST, so no email; (iii) the materializer tick queries `rows_no_longer_covered(now)`
  (unleased PENDING / SUPERSEDED rows whose rule is missing, inactive, or on another weekday)
  and withdraws them. The reason matches the cause: `rule_deleted`, `rule_deactivated` or
  `rule_weekday_changed`. SKIPPED rows are left alone: they are never booked, and keeping them
  preserves the skip across a reactivation (round-5). Pinned by
  `test_load_event_rows_excludes_rows_of_inactive_rules`,
  `test_load_watch_rows_excludes_pending_rows_of_inactive_rules`,
  `test_finalize_withdraws_pending_rows_of_inactive_rules`,
  `test_rows_no_longer_covered_lists_unleased_stragglers`,
  `test_weekday_change_straggler_never_offered_and_swept` and
  `test_finalize_withdraws_weekday_mismatch_as_rule_weekday_changed`.
- **Owners:** the web runs it synchronously on rule create/edit/reactivate. The watcher runs a cheap
  tick every run: only rules with `materialized_through < local_today + horizon` are touched, so
  it is a single indexed query when there is nothing to do. **The booker never materializes** (it
  stays read + claim only). With a 21-day horizon, a watcher outage has ~14 days of slack before
  a drop finds no row.
- A rule created Wednesday for Saturday creates a pending row inside the open window. The watcher
  will try to book it (recovery-style); the booker will not (its date is today+7). That is correct.

### 7.8 Dry-run environments never mutate reservations (SF2)

Dev logs in with (possibly) the same real MB account as prod (§13 Q11). In any environment with
`dryRun=true`:
- the tenant watcher passes `reconcile_eligible=lambda _: False` (E5), so no duplicate-reconcile
  cancel;
- it runs the engine with the upgrade policy **disabled**, and logs "would upgrade" from the pure
  §7.1 decision;
- it never writes `booked → cancelled(external)` (a dry-run vanish is logged only);
- **the web refuses cancel** (`TEETIME_DRY_RUN=true` → `CancelRefusedError("dry-run environment")`).
  Refresh (a read-only login) and every DB-only action stay enabled.

Pinned by `test_dry_run_watcher_never_cancels` and `test_dry_run_web_refuses_cancel`.

### 7.9 Watcher exit contract

`RateLimitError` aborts the run with exit 0 (unchanged). `AuthError` or a soft-auth threshold
marks the account `auth_failed`, notifies the user, and **exits 0** (it is per-account; today's
non-zero was single-user). `CaptchaError`/`OtpChallengeError` exits non-zero (systemic). A DB
failure exits non-zero. A transient per-account error is logged, and the run continues to the next
account.

---

## 8. Web app

### 8.1 Stack

- **FastAPI + Starlette sessions + Jinja2 (autoescape) + HTMX** (vendored static, no build step),
  served by **uvicorn**, entry `teetime web`. The same image as the jobs, with a different command.
- **Container App:** external HTTPS ingress on the free `*.azurecontainerapps.io` host,
  `minReplicas=0`, `maxReplicas=1`, 0.25 vCPU / 0.5 GiB. With one replica, the in-process TTL cache
  is coherent; rate limits are DB-backed anyway (§8.4).
- **Logic lives in `web/services.py`** (framework-free, unit-testable with the in-memory store and
  FakeAdapter). Routes are thin.

### 8.2 Routes (`web/routes.py::ROUTES` is the contract table)

| Method | Path | Auth | CSRF | Purpose |
|--------|------|------|------|---------|
| GET | `/healthz` | none | – | liveness (no DB call) |
| GET | `/login` / `/auth/{provider}/callback` | none | OAuth `state` + PKCE | sign-in; allowlist check |
| POST | `/logout` | user | yes | clear session |
| GET | `/` | user | – | dashboard: rows (next 21 days), status, booked tee time + confirmation, snapshot age, mismatch badges |
| GET/POST | `/accounts` / `/accounts/connect` | user | yes | connect a CourseAccount (live login probe, §8.4) |
| POST | `/accounts/{id}/reverify` | user | yes | re-probe after `auth_failed` |
| POST | `/accounts/{id}/refresh` | user | yes | live refresh (TTL + rate limit, §8.6) |
| GET/POST | `/rules`, `/rules/{id}` | user | yes | create/edit/deactivate standing rules (materializes synchronously) |
| POST | `/rows` | user | yes | create an explicit dated row |
| POST | `/rows/{id}/skip`, `/rows/{id}/unskip` | user | yes | state-machine transitions |
| POST | `/rows/{id}/withdraw` | user | yes | delete an explicit pending row |
| POST | `/rows/{id}/cancel` | user | yes | managed cancel (§8.5) |
| GET/POST | `/admin/users` | operator | yes | invite/disable users (allowlist) |

Every data query is scoped by the session's `user_id`. There is an IDOR test per route
(`test_route_rejects_other_users_row`).
Row transitions map store errors to responses: `RowLeaseError` → 409 "booking in progress"; `RuleNoLongerCoversError` → 409 "This rule no longer covers <date>; add it as a one-off instead" (with an "add as one-off" action, round-6); any other `TransitionRefusedError` → 409 with its message; `TenantNotFoundError` → 404.

### 8.3 Auth and session

- **OAuth**: GitHub and/or Google (§13 Q2) via `authlib`. Identity is the immutable
  `(provider, subject)`, **never** email alone. First sign-in binds the subject to an **invited**
  `users` row. The invite is matched **only against a verified email**: GitHub via `GET /user/emails`
  entries with `verified: true` (never the profile's public `email` field); Google via the
  `email_verified` claim. Non-invited subjects get a 403 page and an `audit` doc (SF10).
- **Session**: signed cookie (Starlette `SessionMiddleware`, itsdangerous) with `Secure`,
  `HttpOnly`, `SameSite=Lax`, `Path=/`, a 12 h absolute lifetime (issued-at inside the payload,
  checked server-side), and rotation on login. The key is the `WEB-SESSION-SECRET` KV secret.
  **Revocation:** a signed cookie cannot be revoked, so **every request re-reads `users.status`**
  (one indexed PK lookup). `disabled` means the session is cleared and the response is 403 (SF10).
- **CSRF**: per-session random token, rendered into forms and sent by HTMX as `X-CSRF-Token`.
  Every non-GET route verifies it in constant time. `SameSite=Lax` is defence in depth, not the
  control.
- **Headers**: strict CSP (`default-src 'self'`, no inline script; HTMX served from `self`), HSTS,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.

### 8.4 Connect-account login probe (it hits ForeUP)

- Flow: user submits username + password. The service **rate-checks first**, then performs a live
  `authenticate()` on a throwaway adapter. On `is_authenticated`, it encrypts and stores the
  account with `verified_at`. On soft failure it stores nothing and shows a generic "login failed".
- **Limits (DB-backed `probe` docs in `global`, per-item TTL 2 h)**: ≤ 5 probes per user per hour; ≤ 3 per `username_hash` per
  hour; ≤ 30 site-wide per hour; after 2 consecutive failures for a username, a 15 min lockout.
  **Never** retries a failed login automatically (PLAN §8.1/§12). The ForeUP lockout threshold is
  unknown (Spike S-M6), so these limits are deliberately conservative.
- `max_accounts_per_course = 8` is enforced here (§5.3), as is UNIQUE(course, username).

### 8.5 Cancel (the managed-cancel path, FRONTEND_PLAN §2/§3, adapted)

0. In a dry-run environment, refuse (§7.8).
1. Acquire the row lease (60 s), revalidating the row fingerprint (M5). If the booker holds it,
   respond "booking in progress; try after 06:20".
2. Decrypt the credentials and build the adapter. Run `authenticate` and require `is_authenticated`
   plus a trusted snapshot. Otherwise abort with "couldn't verify with the course; nothing was
   cancelled".
3. Confirm `row.booked_raw_id` is present in `list_reservations()`. If it is absent, mark
   cancelled(`already_gone`).
4. `cancel_reservation(booked_raw_id)`. A 404 or ForeUP's 400 "can't find that teetime" counts as
   success (existing adapter contract).
5. **One transactional batch in the account partition:** row → cancelled(`user`) + slot doc
   deleted + ledger `cancelled_user` + snapshot refreshed from the post-cancel list. **The `audit`
   doc lives in `global` (a different partition), so it cannot join the batch.** It is written
   **best-effort after the batch commits**; a failure is logged at ERROR and never un-does or
   blocks the cancel (round-2 SF7). This is the tenant equivalent of
   "cancel + `delete_terminal` under `request_lock`": the durable terminal is the row, and the
   lease is the lock.
6. Release the lease and email the user.

Only rows whose booking is **owned** show the button by default. For an unowned (manual)
reservation, the button is behind an explicit confirm ("this booking wasn't made by TeeTimeBooker").

### 8.6 Refresh and TTL cache

`POST /accounts/{id}/refresh` does a live login and list, persists the snapshot, and caches it in
process for `refresh_ttl_s = 120`. Repeat clicks inside the TTL are served from the cache. Hard
limit: 6 live refreshes per account per hour, DB-backed. The dashboard **never** logs in on page
load; it reads the snapshot (FRONTEND_PLAN §4.3 etiquette).

### 8.7 Notifications

- **Protocol choice:** the engine `Notifier` Protocol is **unchanged**. The recipient is bound at
  construction. The runner gives each account's `Orchestrator` a `BufferingNotifier` (collect only,
  no I/O in the race). After the DB write, it maps results to `UserEvent`s and sends them through
  `tenant.notify.UserNotifier`. `lost` and `cancelled(external)` come from the watcher, not the
  engine, which is why `UserEvent` rather than `BookingResult` is the tenant contract.
- **Backend: Azure Communication Services Email** with an **Azure-managed domain**
  (`*.azurecomm.net`). It needs **no purchased domain** or DNS work (the operator has no custom
  domain today, and the site uses the free ACA hostname). It is IaC-deployable in Bicep, costs
  ~$0.00025 per email, and its key can be written into KV by the Bicep deploy. It is called over
  REST with HMAC-signed requests via httpx (no SDK). Trade-offs: Azure-managed domains have low
  send caps (fine at this volume) and weaker deliverability (spam folder risk, so users should add
  the sender to contacts). **Resend** is the alternative if the operator owns a domain (simpler API,
  better deliverability, but it needs domain DNS verification for non-self recipients) (§13 Q3).
- **Operator summary**: after every booking-runner execution with rows, and on any non-zero exit,
  one email goes to `OPERATOR-NOTIFY-EMAIL`.
- **Content:** course, date, tee time, `TTB:` confirmation, and the reason. No credentials. Rendered
  from a template with autoescape.

---

## 9. Security model

### 9.1 Assets and threat model

The site **holds third-party credentials and can cancel real bookings**. Top threats and controls:

| Threat | Control |
|--------|---------|
| Account takeover of the site | OAuth only; immutable subject id; invite allowlist; 12 h sessions; no password auth of our own |
| CSRF-driven cancel | CSRF token on every mutation + `SameSite=Lax` + POST-only mutations |
| IDOR (seeing or cancelling another user's rows) | every query scoped by `user_id`; per-route IDOR tests |
| XSS stealing a session | Jinja autoescape; CSP with no inline script; HttpOnly cookie |
| Credential exfiltration from the DB | AES-256-GCM ciphertexts; the key is **not** in the DB; AAD binds each blob to its account |
| Web container compromise | it holds the keyring (needed for connect, refresh, cancel), so this is the accepted crown-jewel risk. Mitigations: minimal dependencies, pinned image digest, no shell endpoints, max 1 replica, audit log |
| Using the site to hammer ForeUP logins | DB-backed probe limits (§8.4); `auth_failed` stops automatic logins |
| Log leakage of new secrets | §9.4 |
| DB exposure | Cosmos `disableLocalAuth` (no keys exist to leak); Entra tokens only; per-database data-plane RBAC, so a dev MI cannot touch `/dbs/prod`; public endpoint limited to Azure datacenters (ACA consumption has no static egress; VNet integration needs a new ACA environment, which is out of scope, §14) |

### 9.2 Password encryption

- **AES-256-GCM** (`cryptography`'s `AESGCM`). Fernet was rejected because it has no associated
  data. The blob format is `v1:<kid>:<b64 nonce(12)>:<b64 ciphertext+tag>`. **AAD =
  `course_account_id|course_id|username`**, so a ciphertext copied onto another row fails to
  decrypt.
- **Keyring** in one KV secret, `TENANT-CREDS-KEYRING` = JSON `{"active": "<kid>", "keys":
  {"<kid>": "<b64 32 bytes>"}}`, injected as an env var by ACA's KV secretRef (same pattern as today). The keyring is **never
  fetched through an SDK call**, and it is deliberately kept separate from the Cosmos data plane:
  a Cosmos-only compromise yields ciphertexts, not keys.
- **Rotation**: add a new kid and set `active` (KV edit + redeploy, since secrets resolve at
  container start). Then run `teetime tenant-rekey` (idempotent: re-encrypts every blob not on the
  active kid). Then remove the old kid. Readers accept any kid in the ring. Writers always use
  `active`.
- Plaintext exists only in process memory, per run. It is registered with the log filter (§9.4) and
  never persisted or logged.

### 9.3 PII inventory

| Data | Where | Why | Handling |
|------|-------|-----|----------|
| User email, display name | `user` doc in `global` | OAuth identity, notifications | shown only to the user and the operator |
| ForeUP username (usually an email) | `account` doc `.username` (`tenant`) | login | plaintext field (needed to log in); uniqueness uses a SHA-256 claim doc (§3.2); redacted in logs (the email regex) |
| ForeUP password | `account` doc `.password_ciphertext` | login | AES-GCM (§9.2) |
| OTP mailbox | `account` doc `.otp_mailbox` | future only | nullable, unused v1 |
| Tee times, confirmations | rows, bookings, snapshots | product | not sensitive beyond the user |
| Phone, member number, guest names | **not collected** | ForeUP sends only the count | the `PLAYER1-*` secrets retire with the TOML path (§11 step 9) |
| Card data | **not collected** | TeeItUp out of hosted scope | `redact_payload` remains at every store boundary regardless |

### 9.4 Log redaction for the new secrets

`RedactingLogFilter` masks only what its patterns and knowledge cover (root CLAUDE.md). E7 adds
`register_secret_literals(values)`: an exact-literal registry (minimum length 8, to avoid masking
common words). At startup it is loaded with every
keyring key, `WEB-SESSION-SECRET`, the OAuth client secret, and the ACS key. At runtime it gains
each decrypted ForeUP password. There is no DB password to register: Cosmos auth is an MI token, and the existing `_BEARER_RE`/`_JWT_RE` patterns already mask a token if one is ever logged.
Red test: `test_registered_literal_masked_in_args_and_traceback`. Existing ordering rules
(`install_log_redaction()` right after `basicConfig`) apply to the three new entrypoints and are
pinned by the existing source-position test pattern.

### 9.5 Web → engine boundary

The web never calls `book()`. It calls only `authenticate`, `list_reservations`, and
`cancel_reservation`. Booking is job-only, which keeps the race path's single owner.

### 9.6 Anti-bot / ToS posture change (PLAN §12 must be updated)

Today: one user, one account. Hosted: **N real users' own accounts** from **one egress IP**, with up
to 3N POSTs at each drop. We still do not create accounts (BYO; the shadow flow is out of v1),
rotate IPs, or scalp (one booking per row; extras cancelled within seconds). The footprint is
nevertheless larger and looks like a booking service. It is bounded by `max_accounts_per_course=8`
and `N_blind` (§5.3). This is a **ToS escalation the operator must accept explicitly** (§13 Q8).
PLAN §12 gets a "hosted multi-account" paragraph in MU-18's docs PR.

---

## 10. Infrastructure and cost

### 10.1 Bicep changes (MU-15a, MU-15b, MU-16)

| Module | Change |
|--------|--------|
| `release_events.json` (new) | the event table (§6.2) |
| `compute.bicep` | booking jobs loop over `loadJsonContent('../release_events.json')` × {dst, std}; `bookingMode` / `watchMode` params (default **`toml`**, so the first deploy changes nothing); tenant-mode env adds `TENANT_COSMOS_ENDPOINT`, `TENANT_COSMOS_DATABASE` (`prod`/`dev`), `AZURE_CLIENT_ID` (the MI's client id; plain values, not secrets), plus secretRefs `TENANT_CREDS_KEYRING`, `ACS_EMAIL_CONNECTION`, `OPERATOR_NOTIFY_EMAIL`; `watchCron` param (prod `*/10 * * * *`, dev **hourly** `0 * * * *` per [D6]); tenant booking container 0.5 vCPU / 1 GiB; a new **Manual**-trigger `teetime-migrate-<env>` job (`tenant-migrate`) |
| `cosmos.bicep` (new, **shared**, deployed standalone to `rg-teetime-shared` like `registry.bicep`) | free-tier account, `disableLocalAuth`, Strong consistency, databases `prod`/`dev` (400 RU/s shared each), containers + index policies + TTLs (§10.2) |
| Cosmos data-plane role assignments | **NOT in Bicep. Created ONCE BY HAND by the operator** (operator decision 2026-09-25, round-2 SF3), like the subscription-scoped budgets. Reason: `sqlRoleAssignments/write` on the shared account would let the **dev** pipeline grant itself `/dbs/prod`, which breaks dev/prod isolation. **No CI principal ever holds that right.** Runbook: §10.5 |
| `webapp.bicep` (new) | Container App (`teetime-web-<env>`), same MI, KV secret refs, ingress, scale 0..1 |
| `email.bicep` (new) | ACS Email service + Azure-managed domain + Communication Service; `listKeys()` written into KV secret `ACS-EMAIL-CONNECTION` by the deploy |
| `keyvault.bicep` | no structural change; new secrets are listed in AZURE_PLAN §7.1 |
| `killswitch.bicep` | job arrays from `release_events.json` + the watch job; **new lever (c)**: `POST .../Microsoft.App/containerApps/teetime-web-<env>/stop` for dev and prod |
| `killswitchFired` latch extended (SF8) | `webapp.bicep` takes `effectiveEnableSchedules` (= `enableSchedules && !killswitchFired`, `main.bicep`). When false, the app deploys with **ingress disabled** and `minReplicas=0`, so no traffic and no replica: a CI redeploy after the killswitch fires cannot bring the stopped site back. Pinned by `test_webapp_ingress_disabled_when_killswitch_fired` |
| custom role "ACA Job Schedule Manager" | **operator-manual** update (subscription-scoped; the CI SP cannot): add `Microsoft.App/containerApps/read` + `Microsoft.App/containerApps/stop/action`. Same GUID; `az role definition update` |
| resource providers | **operator must `az provider register`** `Microsoft.DocumentDB` (currently NotRegistered) and `Microsoft.Communication` before the first deploy (the CI SP is RG-scoped) |
| env CI SP rights on the shared RG | **none needed for Cosmos data-plane RBAC** (hand-created, §10.5). The shared `cosmos.bicep` is deployed standalone (by the operator or a shared-RG pipeline), like `registry.bicep` |

New KV secrets (operator pre-creates, because ACA validates KV refs at create time):
`TENANT-CREDS-KEYRING`, `WEB-SESSION-SECRET`,
`OAUTH-CLIENT-ID`, `OAUTH-CLIENT-SECRET`, `OPERATOR-NOTIFY-EMAIL`. Bicep writes
`ACS-EMAIL-CONNECTION`. A parity test is extended
(`test_tenant_env_refs_wired_in_compute_bicep`): every env var name the tenant settings loader
reads is wired (as a secretRef, or as a plain value for the endpoint/database/client id).
**There is no DB secret at all**: Cosmos auth is MI + RBAC (§10.2).

**CI:** what-if already runs on `infra/**` PRs. New validations are **steps inside the existing
`test / lint / typecheck` job**: the conformance suite on `InMemoryTenantStore`, the Bicep and
parity tests. The Cosmos leg is `integration`-marked and skipped in CI. **No new CI job, so no
branch-protection change.** `docker build` / `docker smoke` gain a `teetime web --help` and
`tenant-run --help` smoke. New runtime deps are pure Python (`azure-cosmos`, `azure-identity`, which
pull `aiohttp`): **no system packages, no ODBC driver**. The image stays well under the 300 MB target
in AZURE_PLAN §5.2.

**Migrations:** document `schemaVersion` with expand/contract readers; data backfills via the
Manual `teetime-migrate-<env>` job, which CI starts and awaits **before** deploying jobs and web.
Image rollback is safe because readers accept N−1 (§10.2).

### 10.2 The database: Cosmos DB for NoSQL, free tier (decided 2026-09-25)

**Why not the Azure SQL free offer (kept for the record).** It gives 100,000 vCore-s/month per
database; serverless bills ≥ 0.5 vCore for every online second; and the minimum auto-pause delay is
15 min. A 10-min watcher therefore never lets it pause: 0.5 × 2,592,000 ≈ **1.30 M vCore-s/month,
13× the allowance**, so prod would either pause itself around day 2–3 or bill $100+/month
([free offer](https://learn.microsoft.com/en-us/azure/azure-sql/database/free-offer?view=azuresql),
[FAQ](https://learn.microsoft.com/en-us/azure/azure-sql/database/free-offer-faq?view=azuresql)).

| Option | Monthly cost | Holds the §3.2 invariants? | Status |
|--------|--------------|----------------------------|--------|
| **D. Cosmos DB for NoSQL, free tier** (1000 RU/s + 25 GB free per subscription; the subscription has **no** Cosmos account, so the single free-tier slot is available, verified read-only by the operator) | **$0** | yes: id-uniqueness per partition + transactional batch + ETag (§3.2) | **CHOSEN**, for prod and dev |
| A. Azure SQL Basic (5 DTU), always-on | ~$5 per database (verify in the calculator) | yes (filtered unique indexes) | **fallback** if Cosmos proves unworkable (e.g. Spike S-M9 fails); `TenantStore` isolates the choice |
| B. Azure SQL free offer | $0 only if ≲ 7 wakes/day | yes | rejected (arithmetic above) |
| C. Table Storage | < $0.10 | only with hand-rolled pointer docs (like D, but no batch across entity groups beyond the partition, and no typed SDK) | rejected: D gives the same model for $0 with better tooling |

**Account layout.** One account `cosmos-teetime-shared` in `rg-teetime-shared`, deployed
standalone like the shared ACR (AZURE_PLAN §2.1): `enableFreeTier: true` (this can **only** be set
at account creation), single region East US 2, default consistency **Strong**, `disableLocalAuth:
true` (no account keys at all), public network access with the "accept connections from within
public Azure datacenters" rule. ACA consumption has no static egress IP, and because local auth is
off, only an Entra token holding a data-plane role on that database is accepted. Databases
**`prod`** and **`dev`**, each with **400 RU/s manual shared throughput** (the minimum for a shared
database; S-M9 confirms the minimum stays 400 with 2 (prod) and 4 (dev) containers), 800 RU/s
total ≤ 1000 free. **`capacity.totalThroughputLimit: 1000`** is set on the account (round-2 SF6).
The killswitch cannot stop Cosmos, and provisioned RU/s bills hourly, so the account-level cap
makes a mis-edit above the free tier **impossible to provision** rather than merely alarmed.
Pinned in `test_cosmos_free_tier_and_local_auth_disabled`. Containers per §3.1 (per-item TTLs on
probe/audit docs in `global`).

**Auth: managed identity + data-plane RBAC, which retires "no Azure SDK calls at runtime" for the
tenant path.** Each env's existing user-assigned MI gets the built-in **Cosmos DB Built-in Data
Contributor** role (`00000000-0000-0000-0000-000000000002`) via a `sqlRoleAssignments` child
resource **scoped to its own database only** (`/dbs/prod` or `/dbs/dev`). The assignments are
**created once by hand by the operator** (§10.5), never by CI. At runtime,
`azure.identity.aio.ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)` gets a token from the ACA
identity endpoint and `azure.cosmos.aio.CosmosClient` uses it. **That is an authenticated Azure
SDK call at runtime, which the single-user bot never made.** Why this is acceptable:
- **Better isolation than a key.** The alternative is an account key in Key Vault: a long-lived
  credential for **both** databases. Per-database RBAC means a dev compromise cannot read or
  write prod data. Local auth is disabled outright.
- **It stays off the race path.** The token is acquired at ~05:51 during READ #1. The credential
  object caches it (~24 h lifetime) and is reused for the post-race writes, so there is **no IMDS
  or Cosmos call in the race window** (proof 1, §4.4, spies on the credential too).
- **It fails early and loudly.** A missing role assignment surfaces as a 403 at 05:51 → a systemic
  non-zero exit before T0 (§4.5).

The single-user TOML path keeps the no-SDK property until it is retired (§11 step 9). CLAUDE.md
and AZURE_PLAN §7.2 must state the change (§11.1).

**Client.** Async `azure-cosmos` (≥ 4.7; ships `py.typed`, so it works under mypy strict) plus
`azure-identity`. The booking runner **closes the client before the busy-wait** and re-creates it
for the streamed writes. That avoids an idle connection across the wait; the cached token is
reused. Transactional batch is `container.execute_item_batch(ops, partition_key=accountId)`, with
per-operation `if_match_etag`. Spike S-M9 verifies those exact async semantics before MU-8.

**RU math (1000 RU/s free; 400 RU/s per database).** Assumptions: point read ≈ 1 RU (2 under
Strong); a ~1–3 KB write with the trimmed index policy ≈ 6–10 RU; a small cross-partition query ≈
3–10 RU (×2 under Strong).

| Run | Operations | RU per run |
|-----|-----------|-----------|
| prod watcher (2 accounts, Sat+Sun rows) | rows query + accounts query + finalize query + materializer query (~4 × ~10) + 2 snapshot point reads (~4) + ~0.7 snapshot writes (~7) + occasional lease/outcome batches | **≈ 50 RU**, spread over a ~70 s run → **< 1 RU/s average**, peaks of a few dozen RU in one second; **~7 % of one second's 400 RU budget** |
| booking runner (2 accounts) | rows query + accounts query + 2 claim replaces + 2 outcome batches (~4 ops each) | ≈ 120 RU in total, spread over 05:51 and T0+10 s |
| web page load | 1 rows query + 1 snapshot read + 1 user status read | ≈ 15 RU |
| month total (prod) | 4,320 watcher runs × 50 + trivial | ≈ 220 k RU/month, against a free **2.6 billion** RU/month (400 RU/s × 2.6 M s) |

429s are impossible at this scale. The SDK's built-in retry handles one anyway, and none can occur
in the race window because no calls are made there. Storage: KBs against 25 GB free.

**Testing (round-2 SF2).** Unit and CI tests use `InMemoryTenantStore` + the conformance suite.
The same suite runs **`integration`-marked** (skipped in CI) against the real free-tier account,
via `DefaultAzureCredential` from a developer's `az login`. Data-plane RBAC **cannot create or
delete containers**, so there is no per-run container. Instead, the `dev` database has two
**Bicep-owned CI containers, `tenant-ci` and `global-ci`**, with the same partition keys and index
policies as `tenant`/`global`:
- **Isolation:** each test run uses random `accountId` / `pk` values (a `ci-<uuid>` prefix), so
  runs never collide. Teardown deletes those items (the data plane can), and a 7-day container
  default TTL on the CI containers sweeps anything a crashed run left behind.
- **Why separate containers, not `tenant`:** the dev booker's cross-partition READ #1 and the dev
  watcher's query scan `tenant`, so test rows there could be claimed or acted on. The store's
  container names are configuration (`TENANT_COSMOS_CONTAINER_SUFFIX` = `""` or `"-ci"`); the
  jobs and the web never set the suffix. Pinned by `test_store_uses_ci_containers_only_with_suffix`.
- **Developer access:** a role assignment scoped to **`/dbs/dev/colls/tenant-ci`** and
  **`/dbs/dev/colls/global-ci`** only (Data Contributor), created by hand (§10.5). A developer
  principal can never touch `tenant`/`global` in dev, or anything in prod.

There is no emulator in CI. The Linux emulator is optional for local use.

**Schema evolution.** Documents carry `schemaVersion`. Readers accept version N and N−1, and
writers write N (**expand/contract**). Data backfills run as the Manual-trigger
`teetime-migrate-<env>` job (`tenant-migrate`), started by CI before jobs and web deploy (never by
an agent, per the deploy guard). Containers and index policies are Bicep-owned.

### 10.3 Killswitch

Budget tiers ($20 email / $50 killswitch) are **unchanged**. The Logic App gains PATCH + stop for
each new release-event job (none in v1 beyond MB, which keeps its names) and lever (c), the
Container App stop, for dev + prod. That brings the action count to 12 + 2 = 14 per firing, far
below the 4k free actions. `tests/test_killswitch_job_parity.py` is extended: the killswitch must
reference every job name `compute.bicep` derives from `release_events.json`, plus both
`teetime-web-<env>` apps. The migrate job is Manual-trigger and never auto-fires, so it is
excluded (asserted).

### 10.4 Free-grant compute and cost delta

The shared grant is 180,000 vCPU-s and 360,000 GiB-s per month per subscription. Today it is
~61% used (AZURE_PLAN §9.1).

| Consumer | Today (vCPU-s/mo) | Tenant v1 (vCPU-s/mo) | Arithmetic |
|----------|------------------|-----------------------|------------|
| prod watcher | ~54 k | ~76 k | 4,320 runs × ~70 s (DB query + searches + occasional login) × 0.25 |
| dev watcher | ~54 k | ~12.6 k | **hourly** per [D6]: 720 runs × 70 s × 0.25; **−41 k** |
| booking jobs (both envs) | ~3 k | ~6 k | tenant 0.5 vCPU on ~9 drop days/env; fast exits unchanged |
| web app | 0 | ~2 k | ~20 sessions/mo × ~6 min alive × 0.25 |
| migrate job | 0 | < 0.5 k | a few runs/mo |
| **Total** | **~110 k (61%)** | **~97 k (54%)** | the hourly dev watcher pays for everything else |

GiB-s scales the same way (0.5 GiB for jobs; the booker at 1 GiB adds ~6 k), ending around 200 k
of 360 k (≈ 56%).

| Item | Added $/month |
|------|---------------|
| Container App (web) | $0 (inside the grant) |
| Cosmos DB free tier (prod + dev databases, 800 of 1000 free RU/s, KBs of 25 GB) | **$0** |
| Key Vault operations (+7 secrets, reads at container start) | < $0.05 |
| ACS email (≤ 200/mo) | < $0.10 |
| 2captcha (3N + 2 tokens × ~9 drops) | < $0.50 |
| **Total** | **≈ $0.60/month** (target: under $2). With the SQL Basic fallback: ≈ $5.60 |

### 10.5 Operator runbook: Cosmos data-plane role assignments (one-time, by hand)

These are created by the operator, never by CI (round-2 SF3), after `cosmos.bicep` is deployed and
`Microsoft.DocumentDB` is registered. Built-in role ids: Data **Contributor**
`00000000-0000-0000-0000-000000000002`; Data **Reader** `00000000-0000-0000-0000-000000000001`.

| # | Principal | Role | Scope | Why |
|---|-----------|------|-------|-----|
| 1 | prod user-assigned MI (`id-teetime-prod`) | Contributor | `/dbs/prod` | prod jobs + web |
| 2 | dev user-assigned MI (`id-teetime-dev`) | Contributor | `/dbs/dev` | dev jobs + web |
| 3 | developer principal(s) running integration tests | Contributor | `/dbs/dev/colls/tenant-ci` and `/dbs/dev/colls/global-ci` | conformance suite (§10.2) |
| 4 | operator's own user | **Reader** | `/dbs/prod` and `/dbs/dev` | Portal **Data Explorer** works only through data-plane RBAC once `disableLocalAuth` is on (nit); read-only, so a Portal slip cannot mutate rows |

Command shape (the operator runs it; agents must not, per the deploy guard spirit):
`az cosmosdb sql role assignment create --account-name cosmos-teetime-shared --resource-group
rg-teetime-shared --role-definition-id <id> --principal-id <objectId> --scope <scope>`.
Verification (read-only, agent-safe): `az cosmosdb sql role assignment list …`, and a dev
`tenant-watch --dry-run true` run must not log a 403 at startup. AZURE_PLAN §7.2 and
`infra/CLAUDE.md` gain this runbook (§11.1). `test_no_bicep_creates_cosmos_sql_role_assignments`
pins that no Bicep module declares `sqlRoleAssignments`.

---

## 11. Migration and cutover (zero missed drops, zero double bookings)

**The two dangers are:** (1) the TOML path and the tenant path both acting for the operator's one
ForeUP account, which risks a double booking; (2) neither acting on a drop morning. **Structural
answer:** MB's booking jobs and the watch job are the **same ACA resources** in both modes. The mode
is an argument chosen by the per-env `bookingMode` / `watchMode` params (§6.2), so in any env
exactly one path exists. Dev is `dryRun=true` in both modes, so it never POSTs. MB drops occur only
on **Saturday and Sunday** mornings (target = today+7 must be a wanted weekday, and there are no
other rules in v1). **Every flip deploys Monday–Thursday, 09:00–20:00 ET.**

| Step | What | Gate to proceed | Rollback |
|------|------|-----------------|----------|
| 1 | MU-1…MU-14 merged (code only; `bookingMode=toml` everywhere) | CI green + reviewer APPROVE per PR | revert the PR; no runtime effect |
| 2 | MU-15a/15b/16 infra to **dev** (DB, web, ACS, migrate job; modes still `toml`) | dev deploy green; web reachable; OAuth sign-in works for the operator | redeploy the previous tag; the new resources are inert |
| 3 | Operator connects the MB account **in dev web** (live login probe) and creates the Sat+Sun 08:45–10:00 party-4 rule; the materializer creates 21 days of rows | the dashboard shows the rows | delete rows |
| 4 | **Dev** `bookingMode=tenant`, `watchMode=tenant` (still dry-run) | **≥ 2 consecutive weekends (4 drops)** in parallel with prod's live TOML booking. Each dev run logs: claim; allocation (the pure allocator runs regardless of dry-run); `race: busy-wait complete`; a DRY_RUN outcome from the search path; a streamed per-row write after T0+10 s; the `no store/credential call inside race window` self-check. The dev watcher logs "would adopt" for the real prod bookings and never cancels (§7.8). **This rehearsal cannot exercise the blind burst, pool fill or lease pops (§11.2)** | flip dev back to `toml` |
| 5 | Infra to **prod** with modes still `toml` (DB, web, ACS) | prod deploy green; parity tests green | previous tag |
| 6 | Prod seeding: connect the operator's MB account in **prod web**; create the rule; run `teetime tenant-seed --adopt` (one login: current live reservations matching the rule window/party are recorded as `bookings(source=adopted_owned)` **after operator confirmation on screen**, because the TOML bot made them) | the dashboard shows the next 3 weeks correct; `teetime tenant-plan --event mb0600et --date <next Sat>` (read-only: prints what the runner would claim, allocate, and fire) matches expectations. **Re-run `tenant-seed --adopt` in the flip window immediately before step 7** (the TOML watcher may have upgraded since, which changes raw ids) | delete prod tenant data |
| 7 | **Prod cutover deploy** on a Mon–Thu: `bookingMode=tenant`, `watchMode=tenant`, `infra/v3.0.0` tag | **immediately after the deploy completes** (no TOML actor remains): run `tenant-seed --adopt` once more, so any upgrade the TOML watcher made during the deploy window is recorded as owned (otherwise the §7.5 replacement rule adopts it *unowned*, which is safe but disables upgrades for that row) (SF7); post-deploy the watcher cycle runs clean; the first tenant drop (**operator's account only**) passes the §11.2 log-line checklist | **flip both params to `toml` + tag deploy** (minutes). TOML still has MB creds + `TEETIME-SKIP-DATES`; bookings the tenant path made are visible to the TOML pre-book guard (ALREADY_BOOKED), so there is **no double booking on rollback**. **Rollback runbook (SF7):** diff the web's rules **and explicit rows** against `container.toml` (TOML can express only Sat/Sun 08:45–10:00 × 4). Copy skips into `TEETIME-SKIP-DATES`, and list every explicit date or rule TOML cannot express; those are dropped on rollback and the operator must acknowledge that. **After step 11 a rollback silently drops Turk entirely** (TOML is single-user), so a post-step-11 rollback needs Turk notified first |
| 8 | Soak: **4 weekends** in tenant mode | no rollback used; ≥ 6/8 drops handled with expected outcomes | as step 7 |
| 9 | **Retire the TOML job wiring** (MU-19): the `toml` mode branch removed from `compute.bicep`; `TEETIME-SKIP-DATES`, `PLAYER1-*`, and `MB-USERNAME/PASSWORD` secret refs removed from jobs (KV secrets deleted by the operator later); `booking_day_gate.py` and `skip_dates.py` are no longer called by any job | operator APPROVE | revert the PR (the TOML path is still in the image until step 10) |
| 10 | **Remove the CLI `run`/`watch --config` commands** and `config/container.toml` (MU-20), **≥ 8 weekends after step 7**, unless the operator keeps them for local dev (§13 Q6) | operator decision | git revert |
| 11 | Turk: invite in `/admin/users` **only after ≥ 2 prod tenant drops passed the §11.2 log-line checklist**; Turk connects an account and creates a rule. **Data only** (MU-3 widened the grid) | – | disable the user |

**Why no prod shadow-run of the tenant path:** a prod tenant dry-run alongside prod TOML live would
add a **second concurrent login of the same account at T0−120 s** in prod. Dev already does exactly
that every drop (dev `--wait` pre-warm login in parallel with prod), and prod still books. That is
evidence, though not proof, that concurrent logins do not invalidate each other's sessions
(Spike S-M3). Step 4 is therefore the rehearsal: the same code, the same timing, the same account,
with no POST, within the limits in §11.2.

### 11.1 Doc sites per the change→docs map

| Change | Doc sites |
|--------|-----------|
| New subsystems (tenant, web), Protocols (`TenantStore`, `UserNotifier`), invariants (leases, ownership, pool leases, exit contract) | CLAUDE.md invariant bullets + Package layout + Status; PLAN.md §1/§5/§9/§9.2/§12/§13.1/§16; README architecture + roadmap |
| New CLI commands (`tenant-run`, `tenant-watch`, `web`, `tenant-seed`, `tenant-plan`, `tenant-migrate`, `tenant-rekey`) + env vars | README; AZURE_PLAN §7.3 env inventory; `compute.bicep`/`webapp.bicep`; CLAUDE.md common commands |
| New KV secrets | AZURE_PLAN §7.1; `keyvault.bicep` comments; parity test |
| Cosmos account in `rg-teetime-shared` + MI data-plane RBAC; **the "no Azure SDK calls at runtime" property is retired for the tenant path** | AZURE_PLAN §2.1 (shared resources), §6 (state persistence: no longer in-process only), §7.2 (MI now used at runtime for Cosmos, plus the §10.5 hand-run role-assignment runbook); CLAUDE.md "no authenticated Azure SDK calls" claims in Status + the Azure v1 paragraph; `infra/CLAUDE.md` module tree; README architecture |
| New Bicep modules / job loop / cron param / CPU change | `compute.bicep` comments; AZURE_PLAN §3/§5/§9; `infra/CLAUDE.md` module tree; CLAUDE.md + README schedule claims |
| Killswitch coupling | `killswitch.bicep` header (12 → 14 actions); `tests/test_killswitch_job_parity.py`; COST_KILLSWITCH_PLAN §2; AZURE_PLAN §9.2 |
| Adapter capability (E1, E2, E6) + MB grid | `src/teetime/courses/CLAUDE.md` MB section; CLAUDE.md capability bullets |
| Prod tag bump at step 7 | README / CLAUDE.md / PLAN.md "latest infra tag" (all three, enforced by `tests/test_docs_consistency.py`) |
| New config keys (`captcha_max_concurrent_solves`, `max_accounts_per_course`, `refresh_ttl_s`, …) | `core/config.py` or tenant settings comments; README config walkthrough; parity tests |
| FRONTEND_PLAN superseded; BACKLOG frontend section | supersession banner on FRONTEND_PLAN.md; BACKLOG.md index |
| TOML retirement (steps 9–10) | CLAUDE.md Status + skip-days bullets; PLAN.md §4.2; LEADTIME_SKIP_PLAN status header; AZURE_PLAN §7.5 |

This plan **does not name a current infra tag** in the root docs, so `tests/test_docs_consistency.py`
is unaffected until step 7.

### 11.2 What the dev rehearsal proves, what it cannot, and the first prod drop (M3, decided)

**Operator decision (2026-09-25): no dev harness.** The first coordinated-pool burst runs live in
prod. A plain dev dry-run **cannot** exercise the blind path: `_should_blind_post`
(`orchestrator.py:390`) and `_captcha_prefetch_count_for` (`:879`) short-circuit on
`request.dry_run`, and dry-run builds no CAPTCHA provider.

| Exercised by the dev dry-run rehearsal (§11 step 4) | NOT exercised (first seen live in prod) |
|-----------------------------------------------------|-----------------------------------------|
| DST gate, READ #1, claim, Cosmos auth via MI, client close/reopen, streamed per-row writes | the **coordinated pool fill** with real solves (dev dry-run has no provider) |
| allocation + `set_blind_allowlist` (logged; the pure function runs regardless of dry-run) | **lease pops** in `book()` |
| per-account pre-T0 login pre-warm (real logins), busy-wait timing | the **staggered blind POSTs** and their measured offsets |
| the search path at T0 (dry-run's search → rank → DRY_RUN) | keep-best / cancel-extras / reguard under the recording decorator |
| the whole tenant watcher (shared search, login triggers, snapshots, adoption as "would adopt", finalizer, materializer) and the whole web app | the ownership ledger written from **real** bookings |

**The first prod tenant drops contain exactly one account: the operator's.** Turk is not invited
(§11 step 11) until **≥ 2 prod tenant drops** have completed as expected. With one account, the
tenant run is the single-user race plus exactly one new element: one lease (k = 3) in the shared
pool. It uses the same grid slots (allocation of one account = its own top 3), the same stagger
`(-500, -250, 0)`, the same reserve of 2, and the same unmodified `Orchestrator`.

**Log lines that verify the first drop** (all must appear; grep them in Log Analytics):
1. `tenant-run: claimed 1/1 row(s) for mb0600et target=<Sat>`
2. `tenant-run: allocation order=[<row>] allowlist[<row>]=[<t0>,<t1>,<t2>] search_only=[]`. The
   three times must equal the adapter's own `MB blind-POST: … firing 3 … times=[…]` line, which
   is unchanged: allocation of one account is the identity.
3. `pool: coordinated fill demanded=5 (burst=3, reserve=2) solved=5 granted={<row>: 3} reserve=2`
4. `ForeUP: using pooled CAPTCHA token (lease <row>: 2 left) …`, then `… 1 left`, then `… 0 left`,
   one per blind POST, so the lease served every burst POST.
5. Three unchanged lines `course foreup:mangrove_bay: blind-POST sent <m>ms (planned -500/-250/0ms)
   slot … → …`, with measured offsets within ±50 ms of planned.
6. `tenant-run: outcome row=<row> outcome=BOOKED held=1 cancelled_extra=<n> held_extra=0` (or the
   expected miss shape), then `tenant-run: wrote 1/1 outcome(s)` after T0+10 s.
7. `tenant-run: no store/credential call inside race window [T0-121s, T0+10s]` (the runtime
   self-check that mirrors proof 1).

**Rollback if any line is missing or wrong:** flip `bookingMode` and `watchMode` back to `toml` and
deploy a tag, **Monday–Thursday** (§11 step 7). The TOML path sees any tenant-made booking via its
pre-book guard, so there is no double booking. If a misbehaving Saturday drop threatens Sunday's,
the operator may choose to flip the same day, outside 05:00–06:30 ET. The flip is atomic per job
resource.

---

## 12. PR-by-PR milestone table

Rules for every PR: strict red-green TDD (root CLAUDE.md), an adversarial reviewer per PR, the
operator merges, ruff + ruff format + mypy strict + the full suite green, and the docs checklist.
Size: about half a day to one day of agent work each. **∥** marks tasks that can run in parallel
once their dependencies land.

| PR | Title | Inputs → Outputs (files owned) | Deps | Red tests first (names) | Doc sites |
|----|-------|--------------------------------|------|-------------------------|-----------|
| **MU-0** | Plan + stubs (this PR) | → MULTIUSER_PLAN.md, stubs, CLAUDE.md/BACKLOG pointer | – | none (stubs are not exercised) | CLAUDE.md Status, BACKLOG |
| **MU-1** ∥ | `ReleasePolicy` + helpers + adapter ClassVars | `core/release_policy.py`, MB + Marovitz `release_policy` | MU-0 | `test_target_date_uses_course_tz_not_utc`, `test_cron_pair_mb_matches_compute_bicep`, `test_cron_pair_chicago`, `test_validate_rejects_midnight_release`, `test_release_instant_dst_spring_forward` | courses CLAUDE.md |
| **MU-2** ∥ | `SharedCaptchaPool` + ForeUP injection (E1) | `courses/foreup/token_pool.py`, `foreup/base.py` | MU-0 | `test_captcha_pool.py` **unmodified and green** (the gate); `test_pool_round_robin_grants_rank0_first`, `test_pool_lease_isolated_from_other_key`, `test_pool_reserve_gets_latest_arrivals`, `test_pool_release_moves_lease_to_reserve`, `test_pool_inline_solve_bound_is_per_course`, `test_pool_fill_deadline_t0_minus_10`, `test_pool_uncoordinated_count1_reraises` | CLAUDE.md prepare_book bullet |
| **MU-3** ∥ | MB grid widening + allowlist (E2, E3) + allocator | `mangrove_bay.py`, `tenant/allocation.py` | MU-0 | `test_widened_grid_emits_identical_slots_for_0845_1000_window`, `test_allowlist_filters_before_truncation`, `test_allocation_disjoint`, `test_allocation_distinct_rank0_when_grid_ge_n`, `test_allocation_disjoint_windows_unaffected`, `test_allocation_rotates_first_pick_by_date` | courses CLAUDE.md MB section |
| **MU-4** ∥ | Engine hooks E5/E6/E7 | `watch_orchestrator.py` (kwarg only), `foreup/base.py` (`snapshot_trusted`), `core/redaction.py` | MU-0 | `test_reconcile_eligible_none_is_todays_behavior`, `test_reconcile_skips_ineligible_manual_reservation`, `test_foreup_non_json_login_marks_snapshot_untrusted`, `test_foreup_json_login_without_reservations_list_marks_untrusted` (SF3), `test_registered_literal_masked_in_args_and_traceback` | CLAUDE.md reconcile + redaction bullets |
| **MU-5** ∥ | Tenant models + `TenantStore` Protocol + `InMemoryTenantStore` + transitions + conformance suite | `tenant/models.py`, `tenant/store.py`, `tenant/in_memory_store.py`, `tests/tenant/conformance.py` | MU-0 | `test_one_active_row_per_account_date`, `test_skip_booked_refused`, `test_explicit_supersedes_pending_rule_row`, `test_withdraw_explicit_restores_superseded`, `test_lease_conditional_acquire`, `test_lease_expiry_allows_takeover`, `test_slot_and_row_never_diverge`, `test_supersede_refused_while_leased` (M4), `test_web_transitions_refused_while_leased` (M4), `test_lease_acquire_fails_on_fingerprint_mismatch` (M5), `test_record_outcomes_isolates_rows` (M4), one `test_transition_<from>_<to>` per table row in §3.4 | – |
| **MU-6** | Materializer (**materialize the FULL horizon `[today, today + horizon]`, never only dates after the watermark**; sweep `rows_no_longer_covered`; window/party edits via `rewrite_pending_rule_row`) | `tenant/materialize.py` | MU-1, MU-5 | `test_materialize_idempotent_on_rule_date`, `test_tick_reactivates_withdrawn_rows_before_watermark` (round-6: the full-horizon walk), `test_materialize_skips_frozen_dates`, `test_rule_window_edit_updates_pending_only`, `test_rule_deactivate_withdraws_pending_keeps_booked`, `test_reactivate_restores_withdrawn`, `test_horizon_covers_advance_plus_7`, `test_materializer_does_not_resurrect_cancelled_rule_row` (Q7), `test_weekday_flip_back_rematerializes` (r2 M1), `test_new_rule_does_not_resurrect_external_cancel` (r2 M1), `test_deactivate_then_reactivate_rematerializes`, `test_withdrawn_explicit_restores_superseded_rule_row`, `test_withdrawn_explicit_does_not_block_rule_rematerialization` (r3 M1), `test_new_rule_materializes_date_of_withdrawn_explicit` (r3 M1), `test_second_active_rule_same_weekday_refused` (r3 SF1) | – |
| **MU-7** ∥ | Crypto (adds `cryptography`) | `tenant/crypto.py` | MU-0 | `test_roundtrip`, `test_aad_mismatch_fails`, `test_unknown_kid_fails`, `test_rekey_idempotent`, `test_keyring_missing_active_fails_loud`, `test_plaintext_never_in_repr` | – |
| **MU-8a** | Cosmos document mapping: `to_doc`/`from_doc` per type, deterministic ids, `schemaVersion` readers (N, N−1); pure, no SDK calls | `tenant/cosmos/docs.py` | MU-5 | `test_doc_roundtrip_every_type`, `test_deterministic_ids`, `test_reader_accepts_previous_schema_version`, `test_slot_doc_id_encodes_date` | – |
| **MU-8b** | `CosmosTenantStore` (async `azure-cosmos` + `azure-identity`): transactional batches, IfMatch leases, claim docs; passes the conformance suite `integration`-marked against the real `dev` database | `tenant/cosmos/store.py` | MU-8a, S-M9 | the conformance suite parametrized over `CosmosTenantStore` (integration), plus `test_batch_create_slot_conflict_aborts_row_create`, `test_ifmatch_lease_412_is_not_acquired`, `test_orphan_username_claim_reclaimed`, `test_claim_reclaim_requires_age_and_missing_account` (r2 SF5), `test_claimant_rolls_back_account_on_lost_claim` (r2 SF5), `test_store_uses_ci_containers_only_with_suffix` (r2 SF2) | README deps; AZURE_PLAN §7.2 (MI data-plane auth) |
| **MU-9a0** ∥ | Test/runtime primitives: `VirtualClock` + `RecordingAdapter` (all capability variants incl. blind) | `dev/virtual_clock.py`, `tenant/recording.py` | MU-0 (MU-3 for the blind-capable fake) | `test_virtual_clock_wakes_earliest_deadline_only`, `test_recording_adapter_isinstance_matches_inner`, `test_recording_adapter_blind_capable_end_to_end` (r2 SF1), `test_recorder_records_uncertain_and_reraises`, `test_recorder_records_cancel_outcomes` | – |
| **MU-9a** | Runner core: `run_release_event` over `InMemoryTenantStore` + FakeAdapter; streamed per-row writes; self-deadline | `tenant/runner.py` | MU-1, MU-2, MU-3, MU-5, MU-7, MU-9a0 | `test_runner_no_store_calls_inside_race_window`, `test_runner_uses_unmodified_orchestrator_per_account`, `test_runner_two_accounts_each_get_stagger_and_rank0_first`, `test_runner_records_cancel_extras_failure_as_held_extra`, `test_runner_uncertain_blind_post_sets_needs_reconcile`, `test_runner_prewarm_already_booked_is_unowned`, `test_runner_reguard_already_booked_after_uncertain_is_owned`, `test_runner_overcap_account_registered_k0_solves_nothing`, `test_runner_refuses_blind_adapter_missing_blind_methods` (r2 SF1), `test_runner_streams_outcomes_after_quiet_window`, `test_runner_self_deadline_marks_unfinished_needs_reconcile` | – |
| **MU-9b** | Exit contract + `tenant-run` / `tenant-plan` CLI + the §11.2 verification log lines | `tenant/runner.py` (exit), `__main__.py` (new commands only) | MU-9a | `test_runner_exit_contract_table` (one case per §4.5 row), `test_runner_swallowed_blind_captcha_error_exits_nonzero`, `test_summary_email_failure_exits_nonzero`, `test_runner_one_account_auth_error_does_not_affect_other`, `test_runner_one_account_uncertain_does_not_cancel_siblings`, `test_runner_dst_gate_before_db_read`, `test_runner_claim_skips_leased_row`, `test_runner_emits_first_drop_checklist_lines` (§11.2) | README, CLAUDE.md commands |
| **MU-9c** | `LeasedBookingStore` (row lease + fingerprint revalidation) | `tenant/store.py` | MU-5 | `test_leased_store_maps_request_lock_to_row_lease`, `test_leased_store_raises_concurrent_run_on_fingerprint_change`, `test_leased_store_release_only_by_owner` | CLAUDE.md lease bullet |
| **MU-10a** ∥ | Watcher pure decisions: grouping, `needs_login`, `is_owned`, `classify_missing_booking` (vanish / adopt / bot-caused), snapshot proxy factory | `tenant/watcher.py` | MU-4, MU-5, MU-9a0 | `test_watch_groups_by_course_date_party`, `test_watch_no_login_without_opportunity`, `test_watch_reconcile_cadence_spreads_accounts`, `test_watch_cadence_uses_uuid_int_not_hash`, `test_watch_vanish_needs_two_trusted_snapshots`, `test_watch_vanish_excluded_when_upgrade_marker_set` (M2), `test_watch_vanish_excluded_for_ledgered_cancel` (M2), `test_watch_replacement_reservation_adopted_not_external` (M2), `test_watch_adopt_manual_is_unowned_no_upgrade`, `test_snapshot_proxy_capabilities_mirror_inner` (SF1). **MUST gate `_try_upgrade` on ownership** (E5 does not; §7.6) | – |
| **MU-10b** | Watcher runner wiring + `tenant-watch` CLI | `tenant/runner.py` | MU-6, MU-9a, MU-9c, MU-10a | `test_watch_soft_auth_not_persisted`, `test_watch_upgrade_failed_rebook_sets_pending_reconcile` (M2, via recorder), `test_watch_skipped_between_read_and_lock_not_booked` (M5), `test_watch_finalizes_lost_once`, `test_external_cancel_marked_notified_not_rebooked` (Q7), `test_external_cancel_frees_slot_for_explicit_rerequest` (Q7), `test_watch_respects_booker_lease`, `test_dry_run_watcher_never_cancels` (SF2) | CLAUDE.md watcher bullets, PLAN §9.1 |
| **MU-11** ∥ | Notifications (ACS REST + buffering) | `tenant/notify.py`, `tenant/acs_email.py` | MU-5 | `test_buffering_notifier_no_io`, `test_acs_request_signed`, `test_email_has_no_secret`, `test_operator_summary_on_nonzero` | AZURE_PLAN §7 |
| **MU-12** | Web skeleton: app, settings, OAuth, sessions, CSRF, headers (adds fastapi, uvicorn, jinja2, authlib, itsdangerous) | `web/app.py`, `web/security.py` | MU-5 | `test_non_invited_subject_403`, `test_session_cookie_flags`, `test_session_absolute_expiry`, `test_post_without_csrf_403`, `test_csp_header_present`, `test_healthz_no_db`, `test_disabled_user_session_rejected_next_request` (SF10), `test_github_invite_matches_only_verified_email` (SF10) | README |
| **MU-13** | Dashboard + rules/rows pages | `web/routes.py`, `web/services.py`, templates | MU-6, MU-12 | `test_route_rejects_other_users_row` (per route), `test_dashboard_shows_snapshot_age`, `test_skip_booked_shows_cancel_hint`, `test_rule_create_materializes` | – |
| **MU-14** | Connect/verify, refresh (TTL), cancel | `web/services.py` | MU-7, MU-9c, MU-13 | `test_probe_rate_limits`, `test_probe_never_auto_retries`, `test_connect_encrypts_with_aad`, `test_refresh_ttl_serves_cache`, `test_cancel_requires_trusted_snapshot`, `test_cancel_refused_while_booker_lease`, `test_cancel_writes_row_slot_ledger_in_one_batch`, `test_cancel_audit_failure_does_not_undo_cancel` (r2 SF7), `test_unowned_cancel_requires_confirm`, `test_dry_run_web_refuses_cancel` (SF2) | – |
| **MU-15a** | Infra without the DB: event loop, modes (default toml), webapp, ACS, killswitch lever (c) + latch | `infra/bicep/**` | MU-1, MU-9b, MU-10b, MU-12 (the container runs `tenant-run` / `tenant-watch` / `teetime web`, and `test_tenant_env_refs_wired_in_compute_bicep` reads the settings loaders) | `test_release_events_parity`, `test_compute_default_mode_is_toml`, `test_killswitch_targets_all_derived_jobs_and_web`, `test_webapp_ingress_disabled_when_killswitch_fired` (SF8), `test_tenant_env_refs_wired_in_compute_bicep`, `test_job_names_le_32_chars` | AZURE_PLAN §3/§5/§7/§9, infra/CLAUDE.md, COST_KILLSWITCH_PLAN |
| **MU-15b** | Shared `cosmos.bicep` (standalone in `rg-teetime-shared`; `prod`/`dev` databases, `tenant`/`global` containers + dev-only `tenant-ci`/`global-ci`, `totalThroughputLimit: 1000`) + the §10.5 hand-run runbook | `infra/bicep/modules/cosmos.bicep`, AZURE_PLAN §7.2 | MU-15a, S-M9 | `test_cosmos_free_tier_and_local_auth_disabled` (incl. `totalThroughputLimit == 1000`, r2 SF6), `test_cosmos_two_databases_400ru_each_le_1000`, `test_cosmos_containers_partition_keys`, `test_cosmos_ci_containers_dev_only`, `test_no_bicep_creates_cosmos_sql_role_assignments` (r2 SF3) | AZURE_PLAN §2.1/§7.2/§9, infra/CLAUDE.md module tree + runbook |
| **MU-16** | Migrate job + deploy-workflow ordering + `tenant-seed --adopt` | `.github/workflows/azure-iac.yml`, `__main__` | MU-8b, MU-15b | `test_seed_adopt_requires_confirmation`, `test_workflow_runs_migrate_before_jobs` (static YAML) | AZURE_PLAN §8/§10 |
| **MU-17** | Dev cutover (params only) | `main.bicepparam.dev` | all above | – (verification per §11 step 4) | AZURE_PLAN §10 runbook |
| **MU-18** | Prod cutover (params + tag) + docs (PLAN §12 hosted posture) | `main.bicepparam.prod` | MU-17 soak | – | all "latest infra tag" sites, PLAN §12 |
| **MU-19** | Retire the TOML job wiring | `compute.bicep`, parity tests | MU-18 + 4 weekends | `test_no_toml_mode_remaining` | per §11.1 |
| **MU-20** | Remove the TOML CLI (if Q6 = remove) | `__main__`, `config/` | MU-19 + 4 weekends | – | README, CLAUDE.md |

Critical path: MU-5 → MU-9a → MU-9b → MU-10b → MU-15a → MU-15b → MU-16 → MU-17 → MU-18. **Only
MU-8b and MU-15b depend on Spike S-M9** (async batch + IfMatch semantics). Everything else
proceeds in parallel on `InMemoryTenantStore`. Five parallel lanes can start
immediately after MU-0: MU-1, MU-2, MU-3, MU-4, MU-5, MU-7.

---

## 13. Open questions for the operator

1. ~~Database~~. **RESOLVED 2026-09-25:** Cosmos DB free tier for prod and dev (§10.2). SQL Basic is
   kept as the documented fallback. Operator action: `az provider register -n Microsoft.DocumentDB`.
2. **OAuth provider:** GitHub, Google, or both? Does Turk have a GitHub account?
3. **Email:** ACS Azure-managed domain (no domain needed, spam-folder risk), or Resend + a domain
   you own?
4. **Fairness** for overlapping windows: rotating first pick (default), or operator-first priority?
5. **Sydney Marovitz:** confirm hosted TeeItUp stays out (PAN). If it ever comes in, what is the
   daily release time (Spike S-M4)?
6. **TOML CLI**: remove `run`/`watch --config` entirely (MU-20), or keep them for local
   experiments?
7. ~~External cancel~~. **RESOLVED 2026-09-25:** it is the expected common case. Detect it, mark
   the row cancelled(external), notify, do not re-book; the user may re-request (§7.5).
8. **ToS escalation** (§9.6): accept a hosted multi-account footprint (≤ 8 accounts/course, ≤ 4
   blind bursts per drop from one IP)?
9. ~~Dev watcher cadence~~. **RESOLVED:** hourly per [D6]. Cosmos has no pause budget, so the
   earlier 8-hour proposal (a SQL free-offer artifact) is withdrawn.
10. **Operator-manual prerequisites** (needed before MU-15a/15b deploy): update the custom role
    (containerApps read/stop); register `Microsoft.DocumentDB` and `Microsoft.Communication`; deploy
    the shared `cosmos.bicep`; then **create the Cosmos data-plane role assignments by hand per the
    §10.5 runbook** (decided 2026-09-25: CI never holds `sqlRoleAssignments/write`).
11. Does dev's KV hold the **same** MB account as prod? (The concurrent-login evidence in §11
    depends on it.)
12. Operator notify address for summaries (a new KV secret).
13. Is the 16:00-day-before cutoff global for all users, or will per-user cutoffs be wanted? v1
    assumes global. Changing it later requires recomputing `cutoff_at`.
14. ~~Rehearsal~~. **RESOLVED 2026-09-25:** no dev harness. The first coordinated-pool burst runs
    live in prod, with only the operator's account (§11.2).

### Spikes (question → exit criterion)

| Spike | Question | Exit criterion |
|-------|----------|----------------|
| S-M1 | 2captcha latency and concurrency at C = 12/18/24 concurrent submits around 06:00 ET | p50/p90 solve time and any `ERROR_NO_SLOT_AVAILABLE`/throttle measured over 3 runs (~$0.20). Sets C and N_blind |
| S-M3 | Do concurrent logins of the same ForeUP account from two processes invalidate each other's sessions/JWT? | Dev creds confirmed identical to prod (Q11) + one dev run of "login A, login B, list with A, cancel-dry with A". Alternatively accept the evidence from existing dual-env drops |
| S-M4 | Sydney Marovitz daily release time | Observe the 15-days-out inventory appearing (two probes an hour apart), or a pro-shop answer |
| ~~S-M5~~ | ~~Azure SQL serverless auto-pause~~ | **Withdrawn**: SQL is not used (Cosmos chosen) |
| S-M6 | ForeUP login lockout threshold | Documented or observed; otherwise keep the conservative limits in §8.4 |
| S-M7 | MB first tee time + morning cadence before 08:45 | A dev search of a weekday 7 days out at 06:01 (mornings less contested) shows times < 08:45 |
| ~~S-M8~~ | ~~`msodbcsql18` on Debian 13~~ | **Withdrawn**: no ODBC driver (Cosmos SDK is pure Python) |
| S-M9 | Does async `azure-cosmos` `execute_item_batch` honour per-op `if_match_etag`, (and: confirm the shared-database minimum RU/s is 400 for 2 (prod) and 4 (dev) containers, ≤ 500 either way), and abort the whole batch on a create-conflict (409) of the slot doc? Does `ManagedIdentityCredential(client_id=…)` work in an ACA **job** (not only apps)? | A scratch script against the real `dev` database shows (a) batch with a stale etag → 412 and nothing written; (b) batch with a conflicting create → 409 and nothing written; (c) a dev ACA job execution obtains a token and reads. Blocks MU-8b/MU-15b only |

---

## 14. What this plan deliberately does NOT do

- Does **not** modify `core/orchestrator.py`, the blind-burst logic, stagger offsets, burst size,
  or any T0 decision (§4.4).
- Does **not** change `BookingStore` or `Notifier` Protocols. `InMemoryStore` stays the engine's
  in-run store and the test store.
- Does **not** host TeeItUp booking, handle card data, or add PAN storage.
- Does **not** build shadow accounts, cross-course upgrade, midnight releases, per-user cutoffs, or
  OTP wiring (hooks only).
- Does **not** add VNet/private endpoints (they need a new ACA environment; revisit if the user
  count grows).
- Does **not** background-poll ForeUP from the web (page loads read snapshots only).
- Does **not** run the tenant path in prod in parallel with the TOML path (§11: one mode per env,
  enforced by resource identity).
- Does **not** introduce a sweeper job: `lost` finalization and materialization piggyback on the
  watcher, and `frozen` is derived.
- Does **not** reopen ratified plans (BLIND_POST, RACE_PREWARM, RESEARCH_FALLBACK, STAGGER,
  LEADTIME_SKIP, MULTIDAY, PERDAY). It replaces only the single-user **shell**, and retires the
  skip secret + weekday gate + per-weekday TOML windows for the tenant path after cutover (§11
  steps 9–10).

---

## 15. Review ledgers

### 15.1 Round 1 (verdict BLOCK → revised) and operator decisions (2026-09-25)

| Item | Resolution | Where |
|------|-----------|-------|
| **M1** runner blind to extras, cancel failures, uncertain POSTs; ALREADY_BOOKED ambiguity | fixed: in-memory `RecordingAdapter` (no I/O, no `orchestrator.py` change) with per-capability concrete classes; ownership table (held_extra stays owned, so the crash-net still collapses it; pre-T0 ALREADY_BOOKED is unowned; post-UNCERTAIN reguard match is owned by exact slot) | §4.6, §3.1 ledger states, §4.4 proof 5, `tenant/recording.py` |
| **M2** cancel-OK/rebook-failed undetectable | fixed: observed via the recorder, not the store terminal; `upgrade_started_at` intent marker set under the lease before the engine runs; vanish inference excludes the marker, ledgered cancels, and same-date replacements (adopted) | §3.4, §7.1 steps 4–5, §7.5, `models.RequestRow.upgrade_started_at`, `store.set_upgrade_marker` |
| **M3** dev rehearsal can't exercise the blind path | fixed per operator decision: no harness; the first prod tenant drops contain only the operator's account (one lease, k=3, same slots and stagger); 7 verification log lines + rollback; the table of what dev can and cannot exercise | §11.2, §11 steps 4/7/11 |
| **M4** supersede ignores lease; all-or-nothing WRITE #2 | fixed: every web transition requires the row unleased; WRITE #2 is streamed, one batch per row, ledger keyed by (account, date) too, a refused row never rolls back others | §3.4, §4.2, `store.RowOutcome`, `store.record_outcomes` |
| **M5** lease acquire doesn't re-check row state | fixed: `RowFingerprint` (status, version, booked_raw_id) compared in the same ETag-conditional acquire; a mismatch → `ConcurrentRunError` → the engine defers | §3.5, `models.RowFingerprint`, `store.acquire_row_lease(expected=…)`, `LeasedBookingStore` |
| SF1 `__getattr__` breaks `runtime_checkable` on ≥ 3.12 | fixed: per-capability concrete proxy classes (snapshot proxy and recorder) | §4.6, §7.1, `watcher.make_search_snapshot_adapter`, `recording.make_recording_adapter` |
| SF2 dev can mutate shared real reservations | fixed: dry-run envs never reconcile-cancel, never upgrade, never mark external; the web refuses cancel | §7.8, §8.5 step 0, `WebSettings.dry_run` |
| SF3 JSON success without a `reservations` list | fixed: E6 marks it untrusted | §2.3 E6, §7.5 (c), MU-4 test |
| SF4 FakeClock scrambles concurrent timing | fixed: `VirtualClock` discrete-event scheduler for multi-account tests | §4.4 proof 3, `dev/virtual_clock.py` |
| SF5 replicaTimeout loses all outcomes | fixed: streamed per-row writes from T0+10 s + a self-deadline; the proof window is the race, not the whole run | §4.2, §4.4 proof 1 |
| SF6 summary email failure hides misses | fixed: non-zero exit | §4.5, §4.2, `RunReport.summary_email_failed` |
| SF7 TOML upgrades between seed and flip; rollback runbook | fixed: re-run `tenant-seed --adopt` before and right after the flip; the rollback diffs rules + explicit rows; Turk is dropped on a post-step-11 rollback | §11 steps 6/7 |
| SF8 killswitch latch doesn't cover the web app | fixed: `effectiveEnableSchedules` → ingress disabled + minReplicas 0 | §10.1 |
| SF9 milestone deps / MU-9 too big | fixed: MU-9 split into 9a/9b/9c; MU-15a depends on MU-9b, MU-10, MU-12; MU-14 depends on MU-9c (not MU-8b); MU-15 split into a/b | §12 |
| SF10 revocation; GitHub unverified emails | fixed: `users.status` re-read on every request; invites match verified emails only | §8.3 |
| Nit: "NTP offset doesn't exist" | **disagree**: `core/clock.py:53 measure_ntp_offset`, called at `__main__.py:211` (`RealClock(offset=…)`). The §4.2 row now cites both | §4.2 |
| Nit: stable cadence hash | fixed: `account_id.int` (UUID integer) | §7.1 |
| Nit: needs_reconcile adoption can claim a manual booking | fixed (tightened to an exact recorded-slot match) + documented residual | §4.6, §7.6 |
| Nit: msodbcsql18 on Debian 13 | superseded: no ODBC driver (Cosmos); S-M8 withdrawn; S-M9 added for Cosmos SDK semantics | §13 spikes |
| Nit: owned-only reconcile leaves bot + manual both held | fixed (documented residual) | §7.6 |
| Review verification 3: over-cap keys must register k=0; wave-2 waste | fixed / documented residual | §4.2, §5.3 |
| Review verification 8: blind-POST Captcha/OTP swallowed | fixed: visible via the recorder → non-zero exit | §4.5 |
| **Operator 1: Cosmos DB free tier (option D), prod + dev** | done: document model with a date-slot pointer for "one active row", deterministic ids, transactional batch, ETag leases, MI + per-database RBAC (retires "no SDK at runtime" for the tenant path, with reasons), RU math, integration-marked conformance; SQL Basic kept as the fallback; SQLAlchemy/Alembic/ODBC removed; cost ≈ $0.60/mo. **No correctness reason Cosmos can't hold the invariants** (§3.2 verdict) | §3.1, §3.2, §3.5, §10.1, §10.2, §10.4, §12 MU-8a/8b/15b, §13 Q1 |
| **Operator 2: first real burst live in prod** | done: no harness; one-account first drops; log checklist; rollback | §11.2 |
| **Operator 3: external cancels are expected** | done: detect (two trusted snapshots + M2 exclusions), cancelled(external), notify, no re-book; the slot doc is freed, so the user may create a new explicit row for the same date; the materializer never resurrects the cancelled rule row | §7.5, §3.2, §12 MU-6/MU-10 tests |

### 15.2 Round 2 (verdict BLOCK; all round-1 items verified fixed) → revised

| Item | Resolution | Where |
|------|-----------|-------|
| **M1** deterministic rule-row ids: a weekday flip-back never re-materializes (silent missed drop), while a new rule re-books an external cancel | fixed: resurrection is decided by the (account, date) **history**, not by id collision. `classify_date_history` (pure): frozen → skip; any **user-terminal** row (cancelled user/external/already_gone, withdrawn user_withdrawn) → skip for **every** rule; own row system-withdrawn + slot free → **reactivate** (IfMatch replace + slot create); else create / create-superseded. A 409 now means "a row exists", not "handled" | §3.1 id table, §3.2, §3.4 (withdrawn(system) → pending row, rule-edit paragraph), §7.5, §7.7; `models.SYSTEM_WITHDRAW_REASONS`/`USER_TERMINAL`, `materialize.classify_date_history`/`DateAction`, `store.rows_for_account_date`/`reactivate_rule_row`; tests `test_weekday_flip_back_rematerializes`, `test_new_rule_does_not_resurrect_external_cancel` (MU-6) |
| SF1 recorder doesn't forward `synthesize_blind_slots`/`captcha_pool_size` | fixed: `BlindCapableRecordingAdapter` variant (selected iff `capabilities.blind_post`); a pre-T0 `assert_blind_methods_present` guard (systemic exit at 05:51); end-to-end test through a race-path `Orchestrator.run` | §4.6; `recording.BlindCapableRecordingAdapter`, `runner.assert_blind_methods_present`; tests in MU-9a0/MU-9a |
| SF2 CI integration plan infeasible under data-plane RBAC | fixed: Bicep-owned `tenant-ci`/`global-ci` containers in `dev` only; random `ci-<uuid>` partition keys; teardown + 7-day TTL; separate from `tenant` so the dev booker/watcher can't claim test rows; container suffix is configuration the jobs never set; developer role scoped to the two CI containers | §10.2 Testing, §10.5 row 3; MU-8b/MU-15b tests |
| SF3 CI SP holding `sqlRoleAssignments/write` breaks dev/prod isolation | fixed per operator decision: the operator creates all data-plane role assignments once by hand; no Bicep declares them (pinned); runbook with scopes and command shape | §10.1, §10.2 Auth, **§10.5 (new runbook)**, §13 Q10 |
| SF4 MU-9a / MU-10 oversized | fixed: MU-9a0 ∥ (VirtualClock + RecordingAdapter) split from MU-9a (runner); MU-10a ∥ (pure decisions incl. new `classify_missing_booking`) split from MU-10b (wiring + CLI); deps and critical path updated | §12; `watcher.classify_missing_booking`/`MissingBookingVerdict` |
| SF5 orphan-claim reclaim race | fixed: pending→bound claim protocol; reclaim only if a pending claim is older than 10 min **and** its account doc is missing (IfMatch); a claimant that loses the bind (412) deletes its own account doc | §3.2; MU-8b tests |
| SF6 no hard throughput cap | fixed: `capacity.totalThroughputLimit: 1000` on the account, pinned in `test_cosmos_free_tier_and_local_auth_disabled` | §10.2 account layout, MU-15b |
| SF7 web-cancel "one transaction" can't include the cross-partition audit doc | fixed: the batch is row + slot + ledger + snapshot; the audit doc is best-effort after commit and never un-does the cancel; tests renamed/added | §8.5 step 5, MU-14 |
| Nit: stale SQL wording (UPDATE, `request_rows`, `audit_log`) | fixed | §2.1 diagram, §3.3, §3.6, §7.1, §8.3 |
| Nit: S-M9 confirms shared-DB minimum RU | fixed. Containers were also consolidated from 5 to 2 (`tenant` + `global`, prefixed partition key, per-item TTL), so the count per database is 2 (prod) / 4 (dev) | §3.1, §10.2, §13 S-M9 |
| Nit: Data Explorer needs a data-plane Reader role | fixed: runbook row 4 (operator Reader on both databases) | §10.5 |
| Nit: `store.py` call-budget docstring contradicts streaming | fixed | `tenant/store.py` module docstring |
| Retracted nit (NTP) | acknowledged; no change | – |

### 15.3 Round 3 (verdict BLOCK on one item; all round-2 items verified fixed) → fixed by the coordinator

Round 3 was the last round under the plan-with-review cap. The reviewer verified every round-2
item in the files and blocked on a single one-line regression that round 2's M1 fix introduced;
it explicitly noted the coordinator could verify the fix directly, so it was applied here rather
than in a fourth round. The two residual should-fixes were resolved by decision, not deferred.

| Item | Resolution | Where |
|------|------------|-------|
| **M1** `(WITHDRAWN, "user_withdrawn")` was in `USER_TERMINAL`, so withdrawing a one-off silently poisoned that date for every rule (scenario A: rule row superseded by a one-off, one-off withdrawn, rule later deactivated + reactivated → no pending row, exit 0, missed drop; scenario B: one-off created + withdrawn, then a rule for that weekday → date skipped forever) | fixed: removed from `USER_TERMINAL`; withdrawing an explicit row means "undo my one-off", and **skipped** remains the only "don't book this date" action | `models.USER_TERMINAL`, §3.4 (pending → withdrawn row + rule-edit paragraph), §7.7 step 1, MU-6 tests `test_withdrawn_explicit_does_not_block_rule_rematerialization`, `test_new_rule_materializes_date_of_withdrawn_explicit` |
| **SF1** two active rules on the same weekday: the second rule's rows are created `superseded` and nothing re-classifies them when the first rule is removed → missed drop | resolved by constraint: **one active rule per (account, weekday)** in v1, refused at create/edit (`RuleConflictError`); window-preference lists per weekday are a follow-up | §3.4 rule-edit paragraph, `materialize.RuleConflictError`, MU-6 test `test_second_active_rule_same_weekday_refused` |
| **SF2** a rule edit at ~05:55 does not reach the row the booker already claimed (that morning books the old window/weekday) | documented; UI states "edits apply from the next drop" | §3.4 rule-edit paragraph |
| **SF3** (informational) the first coordinated-pool burst runs live in prod; the §11.2 seven-line checklist is the only verification | acknowledged: the operator greps those lines after each of the first two prod drops before Turk is invited | §11.2 |
| Nit: `recording.py` docstring says MU-9a0 while the constant is `_MU9A` | cosmetic; left | – |

### 15.4 Round-4 decisions (MU-5 review of PR #223; coordinator, 2026-09-25)

| Item | Resolution | Where |
|------|------------|-------|
| **MU-5 review** superseded rule row stranded (deactivate → withdraw one-off → reactivate left it superseded forever); restore ignored a prior skip; reactivation bypassable via the generic transition; booked → pending without `needs_reconcile`; stale lease holders; scan-based rule uniqueness Cosmos cannot make atomic | **D1**: rule deactivate/delete/weekday change withdraws superseded rows too (system reason); **D2**: un-supersede restores the stored pre-supersede status (`superseded_from`). Plus: withdrawn → pending only via `reactivate_rule_row`; booked → pending requires `needs_reconcile`; expired leases cleared on unleased writes and required unexpired for status changes; `ruleday|<weekday>` pointer doc + IfMatch rule versions; cancel reasons tied to actor; ledger-only bookings reported as orphans | §3.1, §3.2, §3.4, §7.6, §7.7; `tenant/models.py`, `tenant/in_memory_store.py`, `tests/tenant/conformance.py` |
| **MU-5 review round 2**: `record_outcomes` still wrote any §3.4 edge for a lease holder (reactivation, withdraw, un-supersede around their guards); D1 cleared `superseded_from`, so reactivation turned a hidden skip into a booking; deactivation not atomic across rows | **Round-5 decision**: `superseded_from` survives superseded → withdrawn and `reactivate_rule_row` restores it (withdrawn → skipped edge). Leased-edge allowlist for `record_outcomes`. §7.7 withdraw-rows-first order, and `load_event_rows` filters inactive rules. Ledger entries must match the row's date and course; `set_materialized_through` never moves backwards | §3.4, §7.7; `tenant/in_memory_store.py`, `tenant/models.py`, `tenant/materialize.py` docstrings |
| **MU-5 review round 3**: pending rows of a rule deactivated while leased stayed bookable by the watcher (and could be reported lost); deactivate → reactivate (slot held) → withdraw one-off stranded the date; web cancel through the unleased path; the §7.7 crash claim was false | **Round-6 decision**: the one-off withdraw batch also restores a system-withdrawn rule row of an active rule (to `superseded_from` or pending, refreshed). Inactive-rule guards in `load_watch_rows` / `finalize_lost` (withdraw, no email) / the tick (`rows_of_inactive_rules`). `transition_row` refuses booked → cancelled. Deactivation resets `materialized_through` first | §3.4, §7.7; `tenant/in_memory_store.py`, `tenant/store.py` |
| **MU-5 review round 4**: the one-off withdraw could restore a row onto a weekday its rule no longer covered; a stale rule object undid a watermark reset; a tick could re-advance the watermark mid-deactivation; the D2 branch ignored user-terminal dates | **Round-7 decision**: a user-terminal date stays blocked for the rule regardless of later re-requests (no restore in either branch). Restore keyed on the rule's current weekday + account. A stored None watermark wins in `upsert_rule`; deactivation is reset → withdraw → reset → inactive, reactivation is upsert → reset. A missing rule's rows are withdrawn `rule_deleted` | §3.4, §7.7; `tenant/in_memory_store.py`, `tenant/models.py` |
| **MU-5 review round 5** (systemic form of rounds 2–4): the generic un-supersede bypassed the round-7 block; a rule row stayed bookable on a weekday its rule no longer covered (leased straggler of a weekday move, stale rule passed to reactivate / insert); unskip revived uncovered rows; the §7.7 residual misnamed its repair | ONE stored-rule coverage predicate (`_rule_covers_row`) for reads, the finalizer and the sweep (`rows_no_longer_covered`), and ONE may-become-active guard enforced in the store's batch for every writer; insert/reactivate IfMatch the stored rule; `upsert_rule` clears the watermark on a weekday change or re-activation; only PENDING/BOOKED rows are leasable; MU-6 must materialize the full horizon (the daily tick is the repair) | §3.4, §7.7, §12; `tenant/in_memory_store.py`, `tenant/store.py`, `tenant/materialize.py` |
| **MU-5 review round 6** (APPROVE; final batch) | Sweep SUPERSEDED leg tested; `needs_reconcile` rows exempt from the coverage filter in `load_watch_rows` and from the sweep (§7.6); `rewrite_pending_rule_row` implemented as the rule-edit writer; **round-6 decision**: unskipping an uncovered row is refused with `RuleNoLongerCoversError`, rendered by the web as "add it as a one-off instead"; a weekday move needs no separate reset; MU-6 must pin `test_tick_reactivates_withdrawn_rows_before_watermark` | §3.4, §7.6, §7.7, §8.2, §12; `tenant/in_memory_store.py`, `tenant/store.py`, `tenant/models.py` |

**Status after round 3: RATIFIED** (2026-09-25). Operator decisions folded in: BYO accounts;
Cosmos DB free tier for prod + dev (SQL Basic is the documented fallback); the first coordinated
burst runs live in prod with only the operator's account; external cancels are the expected common
case (detect, mark `cancelled(external)`, notify, never re-book, re-request allowed); all Cosmos
data-plane role assignments are created by hand by the operator.
