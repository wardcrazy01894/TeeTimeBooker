# Multi-user (tenant) path invariants (`src/teetime/tenant/`)

Moved here from the root `CLAUDE.md` on 2026-10-25 so they load only when you work under this
tree (the root keeps the pointer and the non-negotiables). Per-milestone build notes:
[docs/MULTIUSER_AS_BUILT.md](../../../docs/MULTIUSER_AS_BUILT.md); the design is
[MULTIUSER_PLAN.md](../../../MULTIUSER_PLAN.md); the engine this path runs unmodified is
[`../core/CLAUDE.md`](../core/CLAUDE.md); the web app's own rules are
[`../web/CLAUDE.md`](../web/CLAUDE.md). **`tests/tenant/` does not load this file: read it
first when working there.**

<!-- toc -->
## Contents

- [Tenant store vs engine store](#tenant-store-vs-engine-store)
- [Multi-user (tenant) path](#multi-user-tenant-path)

<!-- /toc -->

## Tenant store vs engine store

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
  gate on `/admin/users`; `delete_invited_user` (Uninvite) removes only a still-INVITED,
  never-bound user (Cosmos: IfMatch, so a racing first sign-in wins). A `rejected_signin` (a `global` doc, one per `(provider, subject)`,
  partition `rejected_signin:all`) KEEPS the provider-verified emails, unlike the audit log, which
  redacts them; so it has a per-item TTL of 90 days re-set on every attempt (it relies on the
  `global` container's `defaultTtl: -1`, pinned by `tests/test_cosmos_bicep.py`), and both stores
  also apply the cut-off when listing. It is written on the UNAUTHENTICATED 403 path, so the write
  is best-effort, bounded (`REJECTED_SIGNIN_WRITE_TIMEOUT_S`), and capped (5 emails, 200-char
  name); a failure never changes the 403. The rejection's `audit` doc is bounded the same way and
  written at most once per `(provider, subject)` per `REJECTED_AUDIT_COOLDOWN` (1 h, in-process),
  because anyone can script the OAuth round trip.

## Multi-user (tenant) path


Details per milestone: [docs/MULTIUSER_AS_BUILT.md](../../../docs/MULTIUSER_AS_BUILT.md).

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
  `cancelled(external)` write, and the web refuses cancel. **And send NO email unless the BOOKER
  fails** (operator, 2026-10-04; the day before, dev's watcher had emailed a real inbox "No tee
  time ... before the booking cutoff" at the Oct 4 cutoff, from the Azure-managed sender, and the
  first fix re-addressed such mail to the operator, who then wanted none). A dry run cannot book,
  so every outcome it would report is fiction: `StoreUserNotifier(dry_run=True)` logs each user
  event (kind + row id, never an address) and sends nothing, to the person or the operator, and
  no operator booking notice; `deliver_operator_summary` skips a dry-run summary whose exit code
  is 0 and still sends it on a non-zero exit (the systemic-failure channel, §4.5 SF6; the
  watcher has no summary, so a failing dev watcher is a non-zero exit in Azure only); the web's
  feedback form on a dry-run site files the GitHub issue but logs the email. Every command passes
  its own dry-run flag through `user_notifier_from_env(dry_run=…)` /
  `booker_user_notifier(dry_run=…)` and the booker's `RunSummary.dry_run`; pinned by
  `test_dry_run_store_user_notifier_*`, `test_dry_run_operator_summary_goes_out_only_on_a_systemic_failure`,
  `test_a_dry_run_site_logs_a_report_instead_of_emailing_it`, the watch + web CLI wiring tests
  and the booker's AST test. **Deliberate exception: the invitation email**
  (`web/app.py::_send_invitation`) goes to the invitee in every environment, since it is how a
  person signs in to dev at all. Azure's own budget/killswitch alerts are outside this rule.
- **The operator hears about EVERY booking** (2026-10-01). The booker's run summary covers the
  release run; for a tee time the watcher books or upgrades between runs, `StoreUserNotifier`
  (with `operator_to` from `OPERATOR_NOTIFY_EMAIL`, tag from `TEETIME_ENV`) sends one
  `render_operator_booking_notice` email per `OPERATOR_COPY_KINDS` event after the user's own,
  even when the user cannot be mailed. The notice carries the whole request (operator,
  2026-10-06): the tee time and which ranked choice it landed in, every window asked for, the
  party and price cap, how, the course's reservation id, the cutoff until which the watcher keeps
  upgrading, and the send time on the course's clock; an upgrade names the old tee time. Context
  the event lacks is left out, never printed empty. **The watcher's BOOKED / UPGRADED events take
  the tee time and id from the outcome's `booking`, never from the row as read** (before
  2026-10-06 both the user's and the operator's email for a watcher booking named only the date;
  pinned by `test_watch_booked_event_carries_the_tee_time_and_the_request`). The booker
  constructs its notifier WITHOUT it (no duplicate of the summary). Web emails never hold a
  response: the cancel email is a `BackgroundJobs` job like invites and reports
  (`cancel_row(jobs=…)`); our side hands mail to ACS in ~1 s, and `AcsEmailClient` logs the
  send-to-Succeeded duration so a slow delivery can be placed (ours vs ACS's).
- **Every email goes out as plain text PLUS an HTML part derived from it** (2026-10-23,
  `tenant/email_html.py::html_from_text`, applied in `AcsEmailClient._send`). The renderers
  write plain text only; the cards align values with spaces, which Gmail's proportional font
  put off by a space or two. In the HTML a `  Label:  value` block (two leading spaces, a label
  ending in a colon, TWO or more spaces) is a two-column table, a run of six-plus-space indented
  lines with cells two or more spaces apart (the summary's attempts) a table, everything else a
  paragraph with its line breaks and indentation. A colon followed by one space is never a card
  row. The words are identical in both parts (`test_the_html_part_says_exactly_what_the_text_says`
  round-trips every renderer listed in that test's `_samples()`, the feedback form's included),
  text is escaped, and the markup sets no colour, background or font, so clients theme it. A new
  renderer goes into `_samples()`, and a new card keeps that shape, or it ships misaligned again:
  nothing else checks it.
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
- **The booking cutoff resolves per BOOKING, then per person, and the ROW's `cutoff_at` is the
  single source of truth** (2026-10-08, both the same day). `semantics.resolve_cutoff(own, owner,
  default)` is the ONE resolution order: the booking's own (`StandingRule.booking_cutoff`, or a
  one-off's `RequestRow.booking_cutoff`) → `User.booking_cutoff` → the site default
  `BookingCutoffConfig()` every tenant command and the web pass. Both stores compute a new row's
  `cutoff_at` by it (`insert_rule_row_if_absent`, `create_explicit_row`, refreshed on
  `reactivate_rule_row`, whose frozen guard runs against the FRESH instant, since a withdrawn
  row's stored one is stale). **A rule row carries NO copy of its rule's cutoff** (its
  `booking_cutoff` stays None; it follows the STORED rule through `rule_id`), so there is one
  state to keep right; only a one-off stores its own as the marker. Two rewriters, the same
  write (`unleased_write` + version bump, no transition check): `TenantStore.set_booking_cutoff`
  (Your account, `/me/cutoff`) saves the person's and rewrites their live rows that FOLLOW it
  (PENDING/BOOKED/SKIPPED/SUPERSEDED from today on; a row whose booking has its own is reported
  in `kept_own`, untouched); `TenantStore.rewrite_rule_cutoff(stored_rule)` moves a rule's live
  rows onto the rule's resolved cutoff and is the FIRST step of every `materialize_rule` walk,
  which is how a rule cutoff edit reaches its rows (`apply_rule_edit` ends in a materialize)
  and how a row skipped then (LEASED, or written under the rewrite's IfMatch) converges on the
  rule's next walk (daily, by the tick; reported as `MaterializeReport.cutoff_rewritten`). A
  BOOKED row is rewritten on purpose, so a later cutoff re-opens upgrades and an earlier one
  freezes the date at once (`finalize_lost` then marks a PENDING row LOST); the version bump can
  refuse a booker claim racing it, like any web write. The materializer tick materializes each
  rule under its OWNER's cutoff (`_context_for`; the `cutoff` argument is the site default
  fallback) and `materialize_rule` lets the rule's own win, so its frozen check matches the
  store's `check_create`; the watcher hands the engine `semantics.cutoff_config_of(row)` (the
  inverse of `cutoff_at`), never the site default. Pages word the viewer's own cutoff
  (`base_context["booking_cutoff_text"]`, the `pass_context` `release_cycle` filter), emails
  word the row's. Pinned by the conformance suite ("a cutoff per booking" block),
  `test_tick_materializes_each_rule_under_its_owners_cutoff`,
  `test_materialize_freezes_by_the_rules_own_cutoff`, `test_rule_cutoff_edit_moves_its_live_rows`,
  `test_watch_keeps_upgrading_until_the_rows_own_cutoff` and `tests/web/test_web_booking_cutoff.py`.
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
