# Multi-user build notes (as built)

> **Status:** living record of how each MULTIUSER_PLAN milestone was actually built, including
> deviations from the plan. The plan itself is [MULTIUSER_PLAN.md](../MULTIUSER_PLAN.md); the
> current-state summary is in [CLAUDE.md](../CLAUDE.md#current-status).

Moved out of the root CLAUDE.md Status section on 2026-09-26 so every agent session stops loading
it. Phrases such as "unwired" or "nothing calls it yet" record the state when that milestone
merged; since MU-17 the tenant path runs in **dev** (dry-run) and prod still runs the TOML path
until MU-18. MU-2 (the shared CAPTCHA pool) and MU-5 (the tenant store) are described in the
CLAUDE.md invariant bullets on `prepare_book` and on the two stores; MU-7 is `tenant/crypto.py`
(AES-GCM with account-bound AAD).

<!-- toc -->
## Contents

- [MU-1: Release policy (engine hook E4)](#mu-1-release-policy-engine-hook-e4)
- [MU-3: Mangrove Bay grid widening, blind allowlist, slot allocator (E2, E3)](#mu-3-mangrove-bay-grid-widening-blind-allowlist-slot-allocator-e2-e3)
- [MU-4: Engine hooks E5, E6, E7](#mu-4-engine-hooks-e5-e6-e7)
- [MU-6: Materializer](#mu-6-materializer)
- [MU-8a: Cosmos document mapping](#mu-8a-cosmos-document-mapping)
- [MU-8b: `CosmosTenantStore`](#mu-8b-cosmostenantstore)
- [MU-9a0: `VirtualClock`, recording adapter, blind fake adapter](#mu-9a0-virtualclock-recording-adapter-blind-fake-adapter)
- [MU-9a: Booking runner core](#mu-9a-booking-runner-core)
- [MU-9b: Exit contract, `tenant-run` / `tenant-plan`, notifications wiring](#mu-9b-exit-contract-tenant-run--tenant-plan-notifications-wiring)
- [MU-9c: `LeasedBookingStore`](#mu-9c-leasedbookingstore)
- [MU-10a: Tenant watcher: pure decisions](#mu-10a-tenant-watcher-pure-decisions)
- [MU-10b: Tenant watcher runner and `tenant-watch`](#mu-10b-tenant-watcher-runner-and-tenant-watch)
- [MU-11: Notifications (buffering, rendering, ACS Email REST)](#mu-11-notifications-buffering-rendering-acs-email-rest)
- [MU-12: Web skeleton (FastAPI, invite-only OAuth, sessions, CSRF)](#mu-12-web-skeleton-fastapi-invite-only-oauth-sessions-csrf)
- [MU-13: Dashboard, rules and dates pages](#mu-13-dashboard-rules-and-dates-pages)
- [MU-14: Connect / refresh / cancel](#mu-14-connect--refresh--cancel)
- [MU-15a: Infra without the database](#mu-15a-infra-without-the-database)
- [MU-16a: Tenant commands on real collaborators + migrate job](#mu-16a-tenant-commands-on-real-collaborators--migrate-job)
- [MU-16b: Adopt the TOML bot's reservations (prod-cutover seed)](#mu-16b-adopt-the-toml-bots-reservations-prod-cutover-seed)
- [MU-17: Dev cutover](#mu-17-dev-cutover)
- [MU-R1: Ranked options + per-course price (model, store, Cosmos)](#mu-r1-ranked-options--per-course-price-model-store-cosmos)
- [MU-R2: Price cap, group floor, group collapse, cross-course upgrade](#mu-r2-price-cap-group-floor-group-collapse-cross-course-upgrade)
- [MU-R3: Ranked booking form](#mu-r3-ranked-booking-form)
- [MU-15b: Cosmos DB account](#mu-15b-cosmos-db-account)
- [Website UI polish](#website-ui-polish)

<!-- /toc -->

## MU-1: Release policy (engine hook E4)

`core/release_policy.py` is real — `ReleasePolicy(advance_days, release_time, timezone,
hosted_booking)` + pure helpers `target_date_for` (course-local today + advance, NEVER the UTC
date), `release_instant_for` (zoneinfo, DST-correct), `fire_time_for`, `cron_pair` (a
`CronPair(daylight, standard)` of UTC crons for release − 10 min; halves classified by `utcoffset()`
on Jan 1/Jul 1 of `probe_year`, default the CURRENT year since tzdata changes only future rules;
**`.deduped`/`.jobs`** flag a no-DST zone whose two halves are one instant — MU-15a must derive ONE
job from `.jobs`, or two runners race one event), `validate_release_policy` (v1 hour band 04–22: a
midnight release would fire on D-1 and book a day late; lead may not cross midnight; IANA zone must
resolve; **and the fire time must land in hour `release.hour − 1`** — `minute < lead <= minute + 60`
— because that is the reading `dst_gate.should_proceed` makes, so e.g. 06:30 with the default 10-min
lead would never book in summer and pass the WRONG cron in winter) and `release_key` (`(timezone,
release_time)` — the release-EVENT identity that groups courses into one job pair). A (release,
lead) × transition-day sweep pins "validates ⇒ exactly one gate-passing cron per UTC day".
`MangroveBayAdapter.release_policy` = (7, 06:00, America/New_York) and its derived pair is pinned
EQUAL to `compute.bicep`'s `50 9 * * *`/`50 10 * * *` by reading the bicep in
`tests/test_release_policy.py`; today's `dst_gate.should_proceed` semantics are reproduced from the
policy. `SydneyMarovitzAdapter.release_policy` = (15, **06:00 PLACEHOLDER — S-M4 unconfirmed**,
America/Chicago, `hosted_booking=False`). Nothing on the production path reads any of it; the ACA
crons stay hand-written until MU-15a.

## MU-3: Mangrove Bay grid widening, blind allowlist, slot allocator (E2, E3)

(engine hooks E2 + E3 + the allocator; `tenant/allocation.py` is real, not a stub): the Mangrove Bay
`BLIND_POST_MORNING_GRID` spans the full morning 07:00–12:00 and `MangroveBayAdapter.
set_blind_allowlist` filters `synthesize_blind_slots` before truncation (default `None` = no
filter). The operator's 08:45–10:00 burst is pinned byte-identical to the pre-widening grid
(`test_widened_grid_emits_identical_slots_for_0845_1000_window`); nothing in the TOML path calls the
hook, so this is NOT a booking-behavior change. Details in `src/teetime/courses/CLAUDE.md`.

## MU-4: Engine hooks E5, E6, E7

(engine hooks E5 + E6 + E7, each defaulting to today's behaviour and called by nothing in the TOML
path): `WatchOrchestrator(reconcile_eligible=…)` restricts the duplicate reconcile to eligible
(owned) reservations, `ForeUpAdapter. snapshot_trusted` (`ReservationSnapshotHealth`) says whether
the last login's reservation cache can be believed, and `core.redaction.register_secret_literals`
masks exact secret values in logs. See the reconcile, `list_reservations` and log-redaction bullets
below.

## MU-6: Materializer

(`tenant/materialize.py` is real; its owners are the web (MU-13, which now calls `materialize_rule`
/ `apply_rule_edit` on rule create / edit) and the tenant watcher tick, which `run_tenant_watch` now
calls first on every run (MU-10b, itself unwired)): `classify_date_history` (pure),
`materialize_rule` (walks the FULL `[local_today, local_today + max(21, advance_days + 7)]` horizon
every call), `apply_rule_edit` (window/party rewrite, weekday move, the
reset→withdraw→reset→inactive deactivation, reactivation) and `materialize_tick` (due rules + the
`rows_no_longer_covered` sweep). It needed two read-only `TenantStore` additions —
`get_rule_unscoped` / `get_account_unscoped`, system reads with no `user_id` (the web never calls
them) — conformance-pinned. See the materializer bullet below.

## MU-8a: Cosmos document mapping

(`tenant/cosmos/documents.py`, pure, no Azure SDK import; MU-8b's store sits on top):
`to_*_doc`/`from_*_doc` per persisted type plus `to_doc`/`from_doc` dispatchers, returning
`Stored(item, etag)` with Cosmos `_etag` read through (never written) for IfMatch. Deterministic ids
per §3.1 (`account`, `rule|<id>`, `row|rule|<rule_id>|<date>` — a rule row whose `RowId` is not
`rule_row_id(...)` is refused — `row|x|<uuid>`, `slot|<date>`, `ruleday|<weekday>`,
`booking|<course_id>|<raw_id>`, `snapshot`) partitioned by `accountId`; `global` docs carry a
prefixed `pk` (`user:<id>` = id, `claim:<sha256 of kind|key>` = id, `probe:<UTC hour>`,
`audit:<userId|system>`), and only `probe` (2 h) / `audit` (400 d) carry a per-item `ttl`. camelCase
keys, UTC ISO instants (a naive datetime is refused), `type` + `schemaVersion` on every doc (readers
accept N and N−1, refuse anything else; a missing defaulted field reads as its default, a missing
required one is refused); `row_fingerprint_of` / `event_row_from_docs` are the read projections.
Tests: `tests/tenant/cosmos/test_documents.py`. Tenant store (decided 2026-09-25): a Cosmos DB
free-tier account in `rg-teetime-shared` (`prod` + `dev` databases, MI data-plane auth). That
retires "no Azure SDK calls at runtime" for the tenant path only (MULTIUSER_PLAN §10.2); the current
TOML path is unaffected.

## MU-8b: `CosmosTenantStore`

(`tenant/cosmos/store.py::CosmosTenantStore`; no command constructs it until MU-16, and the account
itself is MU-15b): the full `TenantStore` on the async `azure-cosmos` SDK (+ `azure-identity`,
`aiohttp` as the async transport), with the in-memory store's semantics reached through the SAME
pure functions (`tenant/semantics.py`, extracted from `InMemoryTenantStore` behaviour-preserving).
Every multi-doc write is ONE single-partition transactional batch with per-op IfMatch: row
creates/replaces, the `slot|<date>` ops derived from the status change, the `ruleday|<weekday>`
pointer asserted by an IfMatch self-replace whenever a rule row becomes bookable (the §3.2 rule
IfMatch — a concurrent deactivation or weekday move aborts the row write), and ledger upserts;
`_Batch` refuses an op for another partition. Mapping: 409 row create → exists
(`insert_rule_row_if_absent` returns None), 409 slot create → date taken, 412/404 →
`TransitionRefusedError`, a lost lease race → not acquired, a rule 412 → re-read, then
`VersionConflictError` only if the version really moved. Cross-partition uniqueness is the §3.2
claim protocol (pending → write → IfMatch bind; a lost bind deletes/restores what it wrote; a
PENDING claim is reclaimable only if older than 10 min AND its owner does not hold the key, a BOUND
one only as a rename orphan) plus an IfMatch `max_accounts_per_course` counter. Auth is an Entra
token only (`DefaultAzureCredential`; a key-shaped credential is refused), and the `tenant-ci` /
`global-ci` containers are selected ONLY by `TENANT_COSMOS_CONTAINER_SUFFIX=-ci`. `QUERIED_PATHS`
lists every path the queries filter on — **MU-15b's index policy must include all of them** (Cosmos
rejects a filter on an unindexed path; the §3.2 list is too short). Tests: the whole conformance
suite in CI over `tests/tenant/cosmos/fake_container.py` (a fake of the SDK container API, never the
store) and `integration`-marked against the real `dev` CI containers (README), plus unit tests in
`tests/tenant/cosmos/test_cosmos_store.py`. Known residual (documented in the module): the
user-terminal half of the may-become-active guard is a partition query the batch cannot assert. The
integration leg has NOT run yet (no Cosmos account exists).

## MU-9a0: `VirtualClock`, recording adapter, blind fake adapter

(the test/runtime primitives MU-9a's runner and MU-10a's watcher build on; nothing in the TOML path
imports either): `dev/virtual_clock.py::VirtualClock` is a discrete-event `Clock` for multi-account
timing tests — a sleeper parks on its deadline and time jumps to the EARLIEST pending deadline only
once every runnable task is blocked, so N concurrent busy-waits and staggered bursts each measure
their OWN offsets exactly (`FakeClock`'s shared `_now` runs N× fast for the stagger's single-read
`sleep(delay)` pattern — pinned as the non-vacuity contrast; `FakeClock` is untouched and
single-account tests keep using it). `tenant/recording.py::make_recording_adapter` wraps an
account's adapter in an in-memory, zero-I/O recorder that captures everything `Orchestrator.run`
does not return — see the recorder bullet under the capability notes below, which also explains why
it is one CONCRETE class per capability set and never a `__getattr__` proxy.
`dev/blind_fake_adapter.py::BlindFakeAdapter` is the blind-capable `FakeAdapter` variant carrying
the MU-3 allowlist hook (FakeAdapter's defaults are unchanged); it drives the recorder's race-path
end-to-end test through the UNMODIFIED `Orchestrator`.

## MU-9a: Booking runner core

(`tenant/runner.py::run_release_event` + `resolve_credentials` + `assert_blind_methods_present`;
nothing on the production path calls it; `exit_code_for`/CLI/emails are MU-9b (below); the booker
never uses `LeasedBookingStore`): DST gate (pure, before ANY store call) → READ #1 `load_event_rows`
(+ a Python freeze re-check) → WRITE #1 `claim_rows` (a row another writer leases is re-claimed
every 15 s until T0−150 s, then skipped) → in-process decrypt with every password registered as an
E7 secret literal (a per-row decrypt failure skips only that row) → one adapter per account from the
`AdapterFactory` (which now also receives the account's pool `lease_key` = row id), each wrapped by
`make_recording_adapter` → the SF1 blind-member guard (a failure is SYSTEMIC pre-T0: nothing races
and the claimed leases are released) → per course, allocation over each blind account's UNFILTERED
candidates (allowlist cleared first) with `C // burst` blind accounts
(`SharedCaptchaPool.max_concurrent_solves`, 12 without a pool) and the rest search-only → EVERY
account of a pooled course registered in draft order (k = its allowlist size, **k = 0 for
over-cap**) + reserve R + `arm(t0)` → one UNMODIFIED `Orchestrator(prefetch_book=True)` per account
on S′ (event fire time/zone; reserve 0 when pooled; burst 0 when search-only), all concurrent with
per-account exception isolation → each outcome built from the returned result + the recorder log
(§4.6 ownership: kept booking `held` only if THIS run booked its raw id; surplus not cancelled OK →
`held_extra`, owned; cancelled OK → `cancelled_extra`; a guard's ALREADY_BOOKED is unowned,
`needs_reconcile` if an UNCERTAIN POST is on record) and STREAMED: one `record_outcomes` call PER
ROW, none before T0 + `post_burst_quiet_s` (10 s), 60 s retry, a refused/failed write → CRITICAL +
the outcome JSON on stdout + `RunReport.outcome_write_failures`. A self-deadline (start +
replicaTimeout − 90 s) cancels still-running accounts and writes their rows `needs_reconcile`.
`tenant.allocation.draft_order` now rotates by the WEEK index (`toordinal() // 7`): the raw ordinal
never rotated at N = 7, because one account's drops recur weekly. Tests:
`tests/tenant/test_runner{,_race}.py` (VirtualClock throughout).

## MU-9b: Exit contract, `tenant-run` / `tenant-plan`, notifications wiring

(`teetime tenant-run --event <key> [--dry-run] [--wait/--no-wait]` and `teetime tenant-plan --event
<key>` over `tenant/booking_job.py`; the store comes from `tenant/wiring.py::open_tenant_store`
since MU-16a — Cosmos when configured, else in memory). **Exit contract** `runner.exit_code_for`
(pure, one test per §4.5 row in `tests/tenant/test_runner_exit.py`): non-zero ONLY for systemic
causes — `systemic_error` (store read/claim incl. a timeout, keyring, missing 2captcha key, a pre-T0
prepare failure), any decrypt failure, a `CaptchaError`/`OtpChallengeError` out of `orch.run`
(`AccountOutcome.captcha_error`) or one the blind burst swallowed (recorder), any UNCERTAIN, the
self-deadline, a failed WRITE #2, a failed operator summary (SF6). A miss and a per-account
`AuthError` exit **0** (a deliberate change from the TOML `ClickException`: one user's miss must not
mark the job Failed).

**Notifications:** each account's `Orchestrator` gets a `BufferingNotifier` (no I/O near T0); after
WRITE #2 `_row_events` maps rows to `UserEvent`s (BOOKED / MISSED_DROP / AUTH_FAILED to the user;
decrypt / dry-run / NEEDS_RECONCILE / swallowed CAPTCHA / held_extra operator-only), `finish_run`
sends the operator summary FIRST via `deliver_operator_summary` (its returned exit code is
authoritative → `summary_email_failed`), then the user events through `StoreUserNotifier`
(`TenantStore.get_user_unscoped`, a new conformance-pinned system read). An unconfigured
`ACS_EMAIL_*`/`OPERATOR_NOTIFY_EMAIL` yields an `UnconfiguredEmailSender` whose every send fails, so
a run with anything to report exits non-zero. The `AuthError` → account `auth_failed` flip has no
store write yet: `RunReport.auth_failed_accounts` carries it (TODO MU-8b).

**Bounds (#233 review):** every READ #1 / WRITE #1 store call is abandoned after
`STORE_CALL_TIMEOUT_S` (20 s) and never runs into the race window (clamped to T0 − lead − 1 s when
the run started before it); the WRITE #2 writer stops at self-deadline + `WRITER_GRACE_S` (30 s) and
dumps anything unwritten to stdout.

**Wiring:** the runner's `pool_factory` may be async — `HostedPoolFactory` runs the site-key
pre-flight once per course AFTER the claim (a day with no rows never touches ForeUP) and builds a
coordinated `SharedCaptchaPool` on the real 2captcha provider; `tenant_scheduler()` is the shipped
`container.toml` scheduler (parity-pinned), and one account through the runner fires the same slots
at the same offsets with the same 3 + 2 token budget as the TOML `run`
(`test_single_account_run_matches_todays_burst`). The §11.2 first-drop lines are emitted verbatim
(`test_first_drop_emits_section_11_2_log_lines`); line 4 required `ForeUpAdapter.book()`'s pooled-
token INFO line to name the lease (`(lease <key>: N left)`, `(shared reserve)` for a reserve token)
— a log-text change on the TOML path too, no behavior change.

## MU-9c: `LeasedBookingStore`

(`tenant.store.LeasedBookingStore`; its first caller is MU-14's web cancel, and the MU-10b watcher
could replace its own lease-around-the-engine with it): an engine `BookingStore` over an inner
`InMemoryStore` + a `TenantStore` whose `request_lock(request_id)` takes the inner in-process lock
FIRST (so a nested acquire raises exactly like `InMemoryStore`) and then `acquire_row_lease` on the
row registered for that RequestId with the `RowFingerprint` read at registration; a foreign live
lease, a moved row (status/version/booked_raw_id) or an unregistered RequestId raise
`ConcurrentRunError`, so the UNMODIFIED engine defers (pinned end-to-end: `maybe_upgrade` on a row
the user skipped after the read cancels and books nothing). The lease is released on normal exit,
exception and cancellation; a failed release is logged, never raised (the lease expires). Every
other method delegates to the inner store. The ctor takes a `clock` (the lease's `now`/`until`),
which the stub signature lacked. Tests: `tests/tenant/test_leased_booking_store.py`.

## MU-10a: Tenant watcher: pure decisions

(`tenant/watcher.py`, the tenant watcher's PURE decision layer — no I/O, no store or adapter calls;
nothing calls it until the MU-10b runner wiring): `group_rows_for_search` (one shared search per
`(course, date, party_size)` — party is part of the key because MB `players=4` returns a SUBSET of
`players=2`), `needs_login` (the §7.1 step-3 reasons first-match-wins: bookable in-window slot for a
PENDING row, a strictly-closer-to-midpoint upgrade candidate for a BOOKED row **only when OWNED**,
`needs_reconcile`, an expired unreleased lease, the reconcile cadence `(account_id.int + run_index)
% 6 == 0` — the UUID integer, never a per-process-salted `hash()` — and a >90-min-stale snapshot
backstop for booked rows), `ownership_of`/`is_owned` (OWNED iff the raw id is ledgered
`held`/`held_extra`; ADOPTED_RECONCILE iff `needs_reconcile` AND an EXACT instant+party match with a
recorded UNCERTAIN slot the caller passes — fail-safe UNOWNED when nothing is passed),
`upgrade_allowed` (**the ownership gate MU-10b MUST apply before `_try_upgrade`**, since E5 does not
guard the upgrade), `classify_missing_booking` (§7.5: only TRUSTED snapshots count, the last two
must both miss the id and be >=10 min apart, then the M2 exclusions in plan order — upgrade marker
or a ledgered `cancelled_upgrade`/`cancelled_extra` -> `BOT_CAUSED`, a same-(date, party)
replacement -> `ADOPT_REPLACEMENT`, else `EXTERNAL_CANCEL`; `cancelled_user` is deliberately NOT an
exclusion, see the inline note), `dry_run_gate` (§7.8: never upgrade / reconcile-cancel / mark
`cancelled(external)` in dry-run) and the `SearchSnapshotAdapter` family +
`make_search_snapshot_adapter` (serves `search()` from the shared group result, delegates the rest;
ONE concrete class per inner capability set — 16 — with NO `__getattr__`, so `runtime_checkable`
`isinstance`, which is `getattr_static`-based on >=3.12, reads the proxy exactly like the inner).
Wall-clock comparisons convert to the row's course timezone first. Tests:
`tests/tenant/test_watcher_{login,ownership,proxy}.py`.

## MU-10b: Tenant watcher runner and `tenant-watch`

(`tenant/watch_runner.py::run_tenant_watch` + `watch_exit_status` + `seeded_terminal`, and the
`teetime tenant-watch` command, which runs it over an EMPTY in-memory store with a WARNING; no ACA
job until MU-15a, no Cosmos until MU-16): materializer tick → `finalize_lost` (one `lost` email) →
the ONE `load_watch_rows` query → ONE `search()` per `(course, date, party)` group on an
unauthenticated client (≥ 250 ms apart) → `needs_login` per row (a row under another writer's live
lease is skipped before any login) → per account, sequentially: decrypt (E7) → `authenticate` →
soft-auth check (counted via `record_soft_auth_failure`, snapshot NEVER persisted, nothing acts) →
`list_reservations` → `snapshot_trusted` (an UNTRUSTED snapshot is neither persisted nor acted on; a
trusted one is saved) → per row, under its OWN `acquire_row_lease` with the fingerprint read at step
1 (M5): a BOOKED row missing from the snapshot goes through `classify_missing_booking` (NOT_YET →
nothing, the engine is NOT run; BOT_CAUSED → PENDING + `needs_reconcile`; ADOPT_REPLACEMENT → adopt;
EXTERNAL_CANCEL → CANCELLED(external) + email, never re-booked); a PENDING row whose snapshot shows
a (date, party) reservation is ADOPTED (owned iff ledgered, else unowned: raw confirmation, no
ledger entry); otherwise the UNMODIFIED `WatchOrchestrator` runs over
`make_search_snapshot_adapter(make_recording_adapter(inner), slots)` and the outcome comes from the
RECORDER (M2: old id cancelled + new booking → upgraded; old id cancelled and nothing new → PENDING
+ `needs_reconcile`, UNLESS the owned-only reconcile kept an owned `held_extra` still live in the
snapshot, which the row then follows). **The ownership gate on `_try_upgrade` (E5 does not guard it)
is two tenant-side layers:** the engine's in-run store is pre-seeded with the row's BOOKED terminal
carrying `TTB:<raw>` ONLY when `upgrade_allowed` says owned (`seeded_terminal`), so
`maybe_upgrade`'s managed guard refuses a manual reservation; and the upgrade policy is handed to
the engine only for an OWNED BOOKED row (never a PENDING row, whose `_check_course` would otherwise
synthesize `TTB:` from any live match). `set_upgrade_marker` is written under the lease before the
engine runs on such a row; every outcome write clears it. Dry-run (§7.8): policy off,
`reconcile_eligible=lambda _: False`, a vanish logged not written, no marker. Exit (§7.9,
`watch_exit_status`): a 429 anywhere aborts the run with exit 0; `AuthError` and the third soft-auth
failure are per-account (notified, exit 0); Captcha/OTP, a DB failure, a decrypt failure and a
refused outcome write are non-zero; an UNCERTAIN book sets `needs_reconcile` and exits 0.
Deviations: it takes the durable row lease itself around the engine instead of `LeasedBookingStore`
(MU-9c is now implemented but the watcher was deliberately NOT refactored onto it); a HARD
`AuthError` does not flip the account `auth_failed` (no store write exists yet, the same gap as
MU-9b); the orphan report covers watched dates only. Tests:
`tests/tenant/test_watch_runner{,_snapshots}.py`, `tests/tenant/test_tenant_watch_cli.py`.

## MU-11: Notifications (buffering, rendering, ACS Email REST)

(`tenant/notify.py` + `tenant/acs_email.py`; nothing calls them until the MU-9a/MU-10b runners):
`BufferingNotifier` is the engine-`Notifier`-shaped in-race collector (no I/O; `flush()` hands the
results over after WRITE #2); `render_user_event` / `render_operator_summary` produce PII-minimal
plain text (first name, course, date, tee time, `TTB:` confirmation, reason) and pass every subject
and body through `redact_text`, so an E7-registered secret or a stray email in a free-text `detail`
never reaches a mailbox; `EmailUserNotifier` is bound to ONE user and refuses another user's event;
`deliver_operator_summary` sends when there are events or the exit is non-zero and returns the FINAL
exit code — a failed summary send turns a clean exit non-zero (§4.5 SF6). `AcsEmailClient` is the
ACS Email REST `EmailSender` over httpx, no SDK: HMAC-SHA256 signing pinned by a known-answer
vector, POST `/emails:send` then poll `Operation-Location` within a bounded timeout, bounded
429/5xx/transport retry honouring a capped `Retry-After` with one `repeatability-request-id` per
send; it RETURNS an `EmailSendResult` and never raises. `load_acs_settings` reads
`ACS_EMAIL_CONNECTION` + `ACS_EMAIL_SENDER` by name and registers the access key as an E7 literal.
`UserEventKind` gained the operator-only `NEEDS_RECONCILE`. `FakeEmailSender` is the test double.
Tests: `tests/tenant/test_{notify,acs_email}.py`.

## MU-12: Web skeleton (FastAPI, invite-only OAuth, sessions, CSRF)

(`src/teetime/web/`: FastAPI app factory, OAuth sign-in for Google and/or GitHub, INVITE-ONLY — the
first sign-in binds the provider subject to a pre-invited user matched only on a VERIFIED email,
`users.status` re-read on every request, 12 h absolute signed sessions, CSRF on every POST,
CSP/HSTS/frame headers, `/healthz` with no store call, operator-only `/admin/users`; `teetime web`
runs it with `basicConfig` then `install_log_redaction()` and registers the session/OAuth secrets as
E7 literals; tenant store in memory until MU-16).

## MU-13: Dashboard, rules and dates pages

(`web/pages.py` + `web/services.py` + templates; no inline script or style — the only script is the same-origin `web/static/app.js`, see Website UI polish): the
dashboard `/` (the user's rows for the next 21 days: status, booked tee time course-local, the
persisted snapshot's age "as of HH:MM (N min ago)", the §7.4 "not seen at course" / "manual
reservation" badges from a TRUSTED snapshot only; it never logs in to ForeUP), `/rules` (create /
edit / deactivate / reactivate a standing rule, materialized SYNCHRONOUSLY via `materialize_rule` /
`apply_rule_edit`; shows "edits apply from the next drop") and `/dates` (add a one-off, skip /
unskip a rule date, withdraw a one-off, "Re-request this date" after a cancel; Cancel on a BOOKED
row is MU-14, below). Every action is a plain form POST through the app-level CSRF guard, then PRG
to a fixed `?notice=` key.

**IDOR:** every row / rule / account id is resolved through user-scoped reads, and a missing,
malformed or foreign id renders ONE byte-identical 404 page (`WebNotFoundError`); pinned per route
by `test_route_rejects_other_users_{row,rule,account}`. Store errors map per §8.2
(`ActionRefusedError` → 409 re-rendering the source page: `RowLeaseError` "booking in progress",
`RuleConflictError`, `VersionConflictError`, `RuleNoLongerCoversError` → "This rule no longer covers
<date>; add it as a one-off instead" plus a prefilled one-off form, any other
`TransitionRefusedError` its message; bad input → 400). `create_app` gained `policies`
(`str(course_id)` → `ReleasePolicy`) and `cutoff` for the materializer. It needed three read-only,
user-scoped `TenantStore` additions — `get_row(row_id, *, user_id)`, `list_accounts_for_user`,
`list_rules_for_user` — conformance-pinned (no write path changed).

## MU-14: Connect / refresh / cancel

(`web/services.py` + `web/pages.py` + `templates/accounts.html`; since MU-16a `teetime web` passes
the keyring (when `TENANT_CREDS_KEYRING` is set), `HostedAdapterFactory`, the hosted policies and an
ACS notifier): **connect / re-verify** (`connect_account` / `reverify_account`, §8.4) E7-register
the plaintext first, rate-check FIRST from the existing probe docs (5 connect probes / user / h —
the user's refresh probes are subtracted —, 3 / username / h across users, 30 site-wide / h, and a
lockout on ANY 2 probes of a username in 15 min: the conservative form of the plan's "2 consecutive
failures", because `count_login_probes` does not read outcomes), then do ONE `authenticate` on a
throwaway adapter (`pool=None`, `dry_run=True`) that is NEVER retried, record the probe, and only on
`is_authenticated` encrypt with the account-bound AAD and `upsert_account` (user_supplied, ACTIVE,
soft failures reset, `verified_at`); the probe `username_hash` is SHA-256 of
`probe|<course>|<casefolded username>`. **Refresh** (`refresh_account`, §8.6): the user-scoped
account read comes BEFORE the in-process `RefreshCache` (120 s TTL, one replica), then
`auth_failed`/disabled accounts are refused, the 6 / account / h cap is DB-backed as probe docs
under `refresh_probe_hash(account_id)` (no new store method), one login (a soft failure is counted
via `record_soft_auth_failure`), and an UNTRUSTED list (§7.5 b/c) is neither persisted nor cached
(the last trusted snapshot stays). **Cancel** (`cancel_row`, §8.5): user-scoped row read → dry-run
refusal (§7.8, before anything else) → BOOKED only → an unowned (unledgered) booking needs
`confirm_unowned` → the web's 60 s row lease through `LeasedBookingStore` with the fingerprint just
read (a booker/watcher lease or a moved row → "booking in progress", BEFORE any login) → decrypt +
E7 → one login that must be authenticated AND trusted (else "nothing was cancelled") → the id absent
from the trusted list = CANCELLED(`already_gone`) with no cancel call, else `cancel_reservation`
(404 / "can't find that teetime" already succeed per the adapter contract; `CancelError` → refused,
nothing written) → ONE `record_outcomes` outcome (row CANCELLED(`user`) + slot release + ledger
`cancelled_user`, or `vanished` for already_gone) → then, best-effort and never undoing the cancel:
the post-cancel snapshot (the pre-cancel list minus the id, since ForeUP's list is a login cache),
the `global` audit doc and the user email.

**Deviation:** the snapshot is a separate write right after the batch, not inside it — `RowOutcome`
has no snapshot field and store semantics were out of scope; a failed snapshot write only leaves the
dashboard one refresh stale. A batch failure after the course cancelled is logged CRITICAL and
reported (the watcher's vanish inference reconciles the row). Pages: `/accounts` (connect form,
per-account Refresh + Re-verify, snapshot age), a real Cancel form on `/dates` (disabled in dry-run;
a confirm checkbox for an unowned booking via `DashboardRow.owned`), `RateLimitedError` → 429,
password inputs never carry a value. `create_app` gained `adapter_factory`; connectable courses are
the `policies` keys; `CancelRefusedError` is now an `ActionRefusedError` (409). IDOR is pinned by
`test_route_rejects_other_users_account_and_row`, and the OAuth exchange by
`test_oauth_exchange_never_logs_secret` (the MU-12 BACKLOG item). Tests:
`tests/web/test_web_{connect,refresh,cancel,accounts_pages}.py`.

## MU-15a: Infra without the database

(MULTIUSER_PLAN §10.1/§12) `infra/bicep/release_events.json` + `compute.bicep`/`killswitch.bicep`
derive their booking-job loop from it (v1: one event, `mb0600et`, keeping the legacy
`teetime-job-<env>-edt/-est` names); `bookingMode`/`watchMode` params (default `toml` in BOTH envs —
this PR changes NOTHING about what either env's ACA jobs run) select `run --config
.../container.toml` vs `tenant-run --event <key>`/`tenant-watch`, with every tenant-only
secretRef/env var (`TENANT_CREDS_KEYRING`, `ACS_EMAIL_CONNECTION`, `OPERATOR_NOTIFY_EMAIL`,
`TENANT_COSMOS_*`, `AZURE_CLIENT_ID`, `ACS_EMAIL_SENDER`) gated behind a `== 'tenant'` branch so the
default toml mode never references a Key Vault secret the operator has not created. Dev's watch cron
moved to hourly (`0 * * * *`, operator directive — prod untouched at `*/10 * * * *`) via the new
per-env `watchCron` param.

**CI note (2026-09-26):** `azure-iac.yml` deploys with INLINE parameters, so every value a
`.bicepparam` file sets must be parsed and passed by the workflow; the first MU-15a merge missed
this and dev stayed at `*/10`. The workflow now parses all nine MU-15a params, and
`tests/test_azure_iac_killswitch_latch.py::test_every_param_file_value_reaches_every_ci_deploy`
fails CI if any declared param is not passed to every deploy. Two new Bicep modules, BOTH gated off
by default (`deployWebApp`/`deployAcsEmail` = `false` in both envs, so this PR cannot break the dev
auto-deploy on a missing secret): `webapp.bicep` (the `teetime-web-<env>` Container App,
scale-to-zero, ingress/max-replicas latched to the SAME `effectiveEnableSchedules` killswitch signal
as the ACA Jobs) and `email.bicep` (ACS Communication Service + Email Service + Azure-managed
domain, writing `ACS-EMAIL-CONNECTION` via `listKeys()` at deploy time — needs the operator to
register the `Microsoft.Communication` RP and grant the CI deploy identity "Key Vault Secrets
Officer" first). The cost killswitch gained **lever (c)**: a `POST .../stop` on each env's web
Container App (14 actions total, up from 12); the "ACA Job Schedule Manager" custom role needs
`Microsoft.App/containerApps/read` + `.../stop/action` added (operator runs `az role definition
update`, not `create` — same GUID). Also fixed (BACKLOG "scope forwarded_allow_ips"): `teetime
web`'s uvicorn now passes an explicit `forwarded_allow_ips` (default `127.0.0.1` — ACA's ingress
sidecar reaches the container over loopback within the same pod; override via
`WEB_FORWARDED_ALLOW_IPS`) instead of leaving it un-set.

## MU-16a: Tenant commands on real collaborators + migrate job

`tenant/wiring.py::open_tenant_store` is the ONE tenant-store builder behind `tenant-run`,
`tenant-plan`, `tenant-watch`, `tenant-migrate` and `web`: `TENANT_COSMOS_ENDPOINT` set ->
`CosmosTenantStore` via `cosmos_tenant_store` (Entra/MI auth through `AZURE_CLIENT_ID`, client
closed on exit); nothing set -> the in-memory store with a loud `IN-MEMORY` WARNING; half-configured
(endpoint without `TENANT_COSMOS_DATABASE`, or a database/suffix without an endpoint, or an invalid
suffix) -> `TenantStoreConfigError` before anything opens. The database is NEVER defaulted
(`CosmosSettings.from_env` would say `dev` — a prod job must not silently use dev data), and because
compute.bicep always sets `TENANT_COSMOS_DATABASE` in tenant mode, a tenant job whose
`tenantCosmosEndpoint` param is empty fails closed instead of exiting 0 over nothing. `tenant-watch`
now gets `hosted_policies()` (the booker's `HOSTED_COURSES`), `HostedAdapterFactory` (a 2captcha
provider when live; `TWOCAPTCHA_API_KEY` is required unless dry-run) and `user_notifier_from_env`
(ACS `StoreUserNotifier`, else `LoggingUserNotifier` + WARNING); the booker keeps its SF6
`operator_sink_from_env`. `web` opens the store INSIDE its event loop (`asyncio.run` ->
`uvicorn.Server.serve()`, since the async Cosmos client is loop-bound) and passes the keyring
(optional: absent -> WARNING and connect/refresh/cancel refused; malformed -> fail closed;
E7-registered), `HostedAdapterFactory(api_key=None)`, the policies keyed `str(course_id)` and the
notifier. `webapp.bicep` wires the web's tenant backend (`TENANT_COSMOS_*`, `AZURE_CLIENT_ID`,
`TENANT_CREDS_KEYRING`, `ACS_EMAIL_*`) iff `tenantCosmosEndpoint` is non-empty. `teetime
tenant-migrate` (`tenant/migrate.py`: `MIGRATIONS = ()`, each step idempotent, no applied ledger)
requires Cosmos, runs `TenantStore.initialize()` (a point read per container) then the list, exits
non-zero on any failure. `compute.bicep` gains the Manual `teetime-migrate-<env>` job (tenant mode
only, no KV secret, 600 s, excluded from the killswitch — asserted), and `azure-iac.yml` starts and
awaits it RIGHT AFTER deploy pass 2 when `BOOKING_MODE`/`WATCH_MODE` is `tenant` (a deviation from
the plan's "before the jobs": pass 1 runs every job on the bootstrap image; readers accept N−1).
The prod-cutover seed is MU-16b (below). Known limit: `initialize()` maps a 404 to "not found", so a MISSING
container is not detected by the migrate job (auth and endpoint failures are).

## MU-16b: Adopt the TOML bot's reservations (prod-cutover seed)

MULTIUSER_PLAN §11 steps 6-7. `tenant/seed.py::plan_adoptions` (pure) matches a TRUSTED persisted
snapshot against the account's rows by course-local date, party size and an in-window option (best
rank, then earliest) and plans `BOOK` (pending -> booked), `LEDGER` (a booked row the watcher
adopted unowned gets its ledger entry) or `REPOINT` (the TOML watcher upgraded: booked -> booked on
the new id, the old one `cancelled_upgrade`). `apply_adoptions` writes each under the row's lease
with the plan-time fingerprint (a moved row is skipped, never overwritten), ledger source
`adopted_owned`, actor `WATCHER`. It is exposed as the operator-only **Adopt existing bookings**
action on `/accounts` (`web/adopt.py`, `POST /accounts/{id}/adopt`): the preview and the write both
re-plan server-side from a snapshot at most 15 minutes old (else "Refresh from course first"), and
nothing is written without the confirm box; a member gets 403. A deliberate deviation from the
plan's `tenant-seed --adopt` CLI: the site already has the managed identity, keyring and adapters,
so the operator needs no local secrets and no write role on the prod database. Opening the Cosmos
store also lowers the `azure` logger to WARNING (the SDK logged every request's headers at INFO).
Prod runbook: AZURE_PLAN §10.8.

## MU-17: Dev cutover

Lives in `main.bicepparam.dev`: dev runs `tenant-run`/`tenant-watch` (still hourly) over the shared
Cosmos `dev` database, with the web app (Google sign-in) and ACS email deployed; dev stays `dryRun =
true` and PROD IS UNCHANGED until MU-18. With `acsEmailSender = ''` main.bicep derives
`DoNotReply@<managed domain>` from the email module output (`effectiveAcsEmailSender`), and with
`operatorEmail = ''` the web reads `OPERATOR-NOTIFY-EMAIL` from Key Vault, because the repo is
PUBLIC and no email address may sit in a param file (pinned by `tests/test_webapp_bicep.py`).
Runbook: AZURE_PLAN §10.7.

## MU-R1: Ranked options + per-course price (model, store, Cosmos)

(MULTIUSER_PLAN §16, ranked preferences): a row or rule now carries `options: tuple[RankedWindow,
...]` — this course's slice of the user's ONE ranked list of (course, window) options, ranks
1-based, unique and ascending (not necessarily contiguous; overlap allowed; `validate_options` in
`__post_init__`) — instead of one `window_earliest/window_latest`; the engine gets them via
`options_time_windows` in rank order (so `rank_slots_for_request`'s window-index preference keeps
the user's order with no engine change), and `achieved_rank` resolves a booked time first-match in
rank order exactly like `core/slot_utils._matching_window`. The watcher's upgrade check is
rank-aware (a better-ranked option always wins; the same option needs strictly closer to ITS
midpoint). Prices: `CourseAccount.default_max_price` (default `DEFAULT_MAX_PRICE` = $100.00/player)
and `max_price` overrides on rows/rules (None = the account default);
`StandingRule.group_id/group_rank`, copied onto rule rows with options/party/price through
`semantics.RowIntent`. The §16.4 `booked → pending (group_downgrade)` edge is in `check_transition`
(reason-scoped: needs_reconcile forbidden on it and required on every other booked → pending; the
runner may write it only as a group_downgrade) and `LEASED_EDGES`, with ledger state
`cancelled_group`. Cosmos: `SCHEMA_VERSION` 2 stores `options` as a list and money as exact decimal
strings; a v1 row/rule document reads back as its single window at rank 1.

## MU-R2: Price cap, group floor, group collapse, cross-course upgrade

(§16.3/§16.4): every tenant engine request (booker, watcher row request, the watcher's shared group
search at the HIGHEST member cap, and the pure login decision) books under `row_max_price` = the
row's override else the account default — before this the tenant path had NO price cap.
`tenant/groups.py` holds the pure `booked_rank` (computed, not stored), `group_floor` and
`plan_collapse`, plus the `collapse_group` EXECUTOR: per worse OWNED booking, fingerprinted lease →
`set_upgrade_marker` BEFORE the course call → `cancel_reservation` → ONE `record_outcomes` (booked →
pending `group_downgrade`, ledger `cancelled_group`, marker cleared); a failed cancel/write leaves
the row BOOKED with the marker (a later vanish is `BOT_CAUSED`, §7.5, which now also excludes
`cancelled_group`) for the next watcher run; a manual worse booking is never cancelled; dry-run
cancels nothing. `TenantStore.rows_in_groups` (system read across account partitions; Cosmos
`/groupId` is in `QUERIED_PATHS`) feeds the **group floor**, applied in the booker and `tenant-plan`
BEFORE the claim and in the watcher's read to PENDING rows only: a grouped row is attempted only for
options ranked better than its group's best existing booking, and skipped entirely when none are;
rows with no group make no extra read; a failed group read is fail-open.

**Collapse points:** the booker's `_collapse_groups` runs strictly AFTER every WRITE #2 (re-reading
the group for fresh fingerprints, cancelling through the run's still-open adapters; an account
outside the run is left to the watcher), and the watcher's end-of-run `collapse` handles the rest —
including the second half of a cross-course upgrade (the better-ranked sibling books through the
normal path, then the worse booking is cancelled: book first, since different accounts have no
1-per-day conflict).

## MU-R3: Ranked booking form

(`web/booking_form.py` + `web/group_services.py` + the `/bookings/date`, `/bookings/weekly`,
`/accounts/{id}/price` routes): ONE ranked form for a date or a weekday (party, up to `MAX_OPTIONS`
= 6 (course, window, rank) rows, a price per course), parsed PURE by `parse_ranked_form` (distinct
ranks, renumbered 1..N; a foreign account id is the uniform 404), saved as one explicit row / rule
PER COURSE sharing a fresh `group_id` with `group_rank` = the best rank and `max_price` = the typed
override. Not one transaction (§16.2): courses are written in rank order and a per-course refusal is
REPORTED (the page re-renders 409 "Saved for N of M courses"), only an all-failed save raises. Skip
/ unskip / withdraw (`_transition`) and deactivate / reactivate (`set_rule_active`) act on the whole
group via user-scoped reads; the single-window rule edit REFUSES a grouped or multi-option rule (it
would flatten it). Price boxes are BLANK with the default as placeholder, not prefilled: a prefilled
value would be saved as a frozen override and stop tracking the account default. The dashboard shows
`booked_rank` ("got option N"). The old single-window `POST /rows` stays for "Re-request this date"
and the "add it as a one-off" follow-up.

## MU-15b: Cosmos DB account

`infra/bicep/modules/cosmos.bicep` (#248): the shared free-tier Cosmos DB account in
`rg-teetime-shared`, deployed standalone by the operator, with `prod` and `dev` databases and the
dev-only `tenant-ci` / `global-ci` containers. Deploy and data-plane role runbook:
[infra/AZURE_PLAN.md §7.2a](../infra/AZURE_PLAN.md). Its index policy must cover
`CosmosTenantStore.QUERIED_PATHS` (see MU-8b).

## Website UI polish

Operator request 2026-09-26 (#254). Courses render by name via the `course_name` Jinja filter over
`courses/names.py::COURSE_DISPLAY_NAMES` (`create_app(course_names=…)` overrides it; an unknown id
falls back to the raw id), never as a raw course id — also in the partial-save/group messages and in
user emails (`StoreUserNotifier`). One stylesheet (`web/static/base.css`: CSS variables, light/dark
via `prefers-color-scheme`, restyled selects, cards, status pills, a cleaner dashboard table). The
ranked booking form shows ONE option row up front; rows 2-6 sit in an "Add another time slot"
`<details>` that works with JavaScript off, and the same-origin `web/static/app.js` (the ONLY script;
CSP `script-src 'self'`) reveals rows one at a time with a Remove link that blanks the row so the
server skips it. The nav's "Rules" is labelled "Weekly". Pinned by `tests/web/test_web_ui_polish.py`.
