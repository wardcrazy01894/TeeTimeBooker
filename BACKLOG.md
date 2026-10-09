# BACKLOG — future wants

A running list of things to add when there's time. **Not ratified, not scheduled** —
this is the "ideas that would otherwise live in phone notes" file. Detailed designs
live in their own `*_PLAN.md` docs; this file is the index plus the not-yet-designed
items. Add freely; promote an item to a real plan/milestone when you decide to build it, and
delete it here when it ships.

---

<!-- toc -->
## Contents

- [Courses to add](#courses-to-add)
- [Observability / reliability](#observability--reliability)
- [Multi-user website](#multi-user-website)
- [Multi-user follow-ups](#multi-user-follow-ups)
- [Frontend (single-user web UI): superseded](#frontend-single-user-web-ui-superseded)

<!-- /toc -->

## Courses to add

- **Moccasin Wallow Golf Course** (Palmetto, FL — near St. Petersburg).
  - First step: identify the booking platform (**ForeUP / TeeItUp / Chronogolf**) —
    that decides which adapter base to reuse (`courses/foreup/`, `courses/teeitup/`,
    or the `courses/chronogolf/` placeholder).
  - Then follow the step-by-step in
    [`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md) ("adding a
    course"): per-course IDs + a config entry. No engine changes expected — adding a
    course is data/config, not new orchestration.

---

## Observability / reliability

- **A *persistent* 429 (or repeated CAPTCHA/auth failure) is currently invisible.**
  The watcher now backs off cleanly on a rate-limit and exits 0 (the 10-min cron is the
  retry — correct, since the notifier is `ConsoleNotifier` and M4 email was cut, so there's
  no alert channel anyway). But that means a platform that throttles us for hours makes every
  watch run show "Succeeded" while the bot silently stops booking. If any "ACA run
  Succeeded-vs-Failed" alerting is ever added, repeated 429-backoff (and the booker's
  `NO_INVENTORY` terminals) should surface somewhere. No action while there's no alert sink.

- **A late-landing booking race (CAPTCHA-prefetch lead not honored) is log-only.** When the
  ACA booking cron lands late in hour 5, the orchestrator now logs a `prefetch lead not fully
  honored` WARNING and the `book()` POST may fire after the 06:00 drop (it still prefetches, so
  it's *less* late — but the slot can still be lost). That WARNING is only grep-able, not
  actively surfaced. If/when an alert sink exists, route this WARNING (and the late-POST drift)
  into the `Notifier` so a missed drop is visible, not buried. Same "no alert channel" caveat
  as above. (Full-repo-scan follow-up, deferred from PR #114.)

- **Widen the Saturday/Sunday time window.** On the 2026-08-15 miss (target Sat 8/22), a
  `07:37` slot was bookable at T0+6 s and was correctly rejected as out-of-window
  (`08:45–10:00`). Every candidate the bot could reach that morning was outside the window
  by 22 minutes. Widening to e.g. `07:30–10:00` costs nothing on days we win — ranking is
  midpoint-distance based, so a 09:22-ish slot still wins whenever one exists — and only
  matters when the prime band is gone. **Deliberately NOT bundled with STAGGER_PLAN**: it
  changes which slots `synthesize_blind_slots` emits, which would confound the stagger's
  offset→outcome diagnostic on its very first drops. Ship after the stagger has produced a
  reading.

- **Retry burst across a WIDER post-T0 window**, conditional on the stagger diagnostic
  confirming the pre-open/flip-jitter hypothesis (STAGGER_PLAN §4). Today's stagger spans
  369 ms before T0 to T0 (`-369/0` since 2026-10-04; was `-400/0` until 2026-10-04, `-400/-250/0` before); if the release flip turns out to jitter by seconds, the answer is repeated POSTs
  at T0+0.5 s / +1 s / +2 s. That needs a bigger CAPTCHA pool — each `book()` pops a
  single-use token — so it carries its own cost and rate-limit analysis. **Measure first:**
  the hypothesis currently rests on two 0/3 drops, one of which (2026-08-01) is fully
  explained by a whole-day tournament block.

- **Pin whether ForeUP's "1 online reservation per day" is scoped to the PLAY date or the
  CALENDAR day the booking is made.** Surfaced by the adversarial review of #201 and currently
  UNPINNED — the multi-day design implies play-date scoping and nothing observed contradicts it,
  but no evidence discriminates the two (every drop books a different play date on a different
  calendar day, so the two hypotheses predict identical outcomes). It matters because
  `_rejection_summary` now reports an all-`daily_limit` burst as "we already hold a reservation
  for **this date**": under calendar-day scoping that wording would be wrong, since a Sunday
  burst could be bounced by Saturday's reservation made the previous morning. Cheap-ish
  experiment: from a dev/manual session, attempt a second booking for a DIFFERENT play date on a
  day we already booked, and read which body comes back.

---

## Multi-user website

- **Multi-user hosted site** (BYO ForeUP accounts, rules/dates/skips, per-user email):
  RATIFIED 2026-09-25 in [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md) and built through MU-18 (live in
  prod since `infra/v3.0.0`, and in dev, dry-run). Open: retiring the TOML job wiring (MU-19; MU-20
  is dropped, the TOML CLI stays). Store: Cosmos DB free tier (MULTIUSER_PLAN §10.2).

---

## Multi-user follow-ups

- **The ranked form forgets its fields on a 400** (review of #328): a refused submit (a bad window,
  say) re-renders the weekly / one-date form empty, the "Stop looking" cutoff included, so a
  resubmit silently drops the chosen cutoff. Echo the submitted form back (the legacy one-off
  form's `OneOffPrefill` is the pattern), cutoff first since it is the safety-relevant field.
- **Killswitch job names are hand-written** (MU-15a review): `killswitch.bicep` hardcodes
  `teetime-job-<env>-edt/-est` instead of deriving them from `infra/bicep/release_events.json`.
  Correct while v1 has one event; derive them (or pin every event's names in the parity test)
  before a second release event is added.
- **`test_booking_jobs_are_daily` now reads a comment** (MU-15a review): the crons moved to
  `release_events.json`, so the `50 9`/`50 10` strings it finds in `compute.bicep` are only its
  header comment. The real invariant is `test_release_events_crons_match_cron_pair`; retarget or
  delete the old test.
- **Collapse residual across events** (MU-R2): the booker collapses only groups whose accounts
  are all in its run; a worse booking at an account of another release event waits for the
  watcher's end-of-run collapse (≤ one watch interval of holding two tee times).
- **Web cancel: test the 60 s lease expiring mid-cancel** (MU-14 review): a slow ForeUP login
  or DELETE could outlive `WEB_LEASE_SECONDS`, and `record_outcomes` should then refuse the write
  (lease no longer held). Pin it with a FakeClock that advances past the lease inside the adapter.
- **Strict probe limits** (MU-14 review): the login-probe caps are count-then-act, so a burst of
  simultaneous requests can overshoot by N-1. A per-bucket counter doc with IfMatch would make it
  exact; not worth it at invite-only scale.
- **Cosmos: the "no user-terminal row" guard is a query outside the batch** (MU-8b review): a user
  cancel landing in the same round-trip as a rule-row reactivate could be missed. Cheap fix: a
  `terminal|<date>` pointer doc upserted on every user-terminal write, whose ETag the guard asserts
  in the batch (like the `ruleday` pointer).
- **Cosmos `_write_account` create→409→blind upsert has no ETag** (MU-8b review): IfMatch-replace
  with retry, like the rest of the module.
- **Runner: release leases claimed before a mid-retry claim failure** (MU-9b review should-fix):
  today they expire at T0+1200 s, blocking the watcher ~20 min; a bounded release (outside the
  race window) would free them on the next watch cycle.
- **`summary_email_failed` unset when the run was already non-zero** (MU-9b review): exit code is
  correct; add a test + set the flag for the double-failure case.
- **`LeasedBookingStore` fingerprint is single-read** (MU-9c review): add a regression test pinning
  that a version-bumping write between two same-row `request_lock`s on one instance makes the
  second defer, before any refactor moves the watcher onto it.
- **Durable uncertain-slot carrier for PENDING rows** (PR #235 review must-fix, taken as a
  documented gap): an UNCERTAIN watcher book on a PENDING row records `needs_reconcile` but no
  slot (`booked_tee_time` is BOOKED-only), so a POST that actually landed is adopted UNOWNED on
  the next run. Fail-safe. Fix: a `RequestRow.uncertain_tee_time` field threaded through
  `RowOutcome`, the Cosmos mapping and `_uncertain_times`, with a round-trip test.
- **`needs_reconcile` on a PENDING row cannot be cleared via `record_outcomes`**, so that account
  logs in on every watch run (6/hour) until the row books or freezes. Add a flag-clear outcome.
- **Run the Cosmos integration conformance leg** (README, Development): the index policy now
  equals `CosmosTenantStore.QUERIED_PATHS` (pinned by `tests/test_cosmos_bicep.py`), but the suite
  had not yet been run against the real account when MU-15b landed. The 2026-09-30 scan found the
  148 integration tests run nowhere automatically, so `delete_invited_user` (IfMatch delete) and
  the `rejected_signin` per-item TTL are verified only against the fake container. Wanted: a
  weekly + `workflow_dispatch` workflow running `pytest -m integration tests/tenant/cosmos` over
  OIDC against the `-ci` containers. Operator prerequisite: a Cosmos data-plane role for the CI
  service principal on the dev database (created by hand, AZURE_PLAN §7.2a). The live ForeUP
  canary (`tests/test_foreup_canary.py`) needs credentials and stays manual.
- **authlib's httpx integration is deprecated** (`AuthlibDeprecationWarning: ... use httpx2`,
  seen at test collection 2026-09-30). The OAuth flow uses it; move to the replacement before an
  authlib release drops it.
- **`teetime tenant-rekey` for keyring rotation** (full-repo scan 2026-09-30). The rotation
  runbook (MULTIUSER_PLAN §9.2) needs a command that re-encrypts every stored course password onto
  the active kid; `tenant/crypto.py::rekey_password` / `needs_rekey` exist and are tested, but no
  command calls them. Until it exists, adding a kid is safe and dropping one is NOT: a blob still
  on the dropped kid can never be decrypted again.
- **A hard `AuthError` should flip the account to `auth_failed`** (MULTIUSER_PLAN §4.5; was
  mis-tagged `TODO(MU-8b)`). `TenantStore` has no system-side account-status write, so the
  booker and the watcher only REPORT `auth_failed_accounts` in the operator summary and the
  account stays ACTIVE. Low impact today: ForeUP `authenticate()` soft-fails (only `book()`
  raises `AuthError`), and the watcher's 3-strike `record_soft_auth_failure` does flip it.
- **"Add it as a one-off instead" prefills only the first-choice window** (scan 2026-09-30): the
  one-off form takes one window, so a ranked row's options 2+ must be re-added by hand.
- **Rule deletion must clear the `ruleday|<weekday>` pointer atomically** (surfaced in the MU-6
  review). The store has no rule delete yet; when MU-8b (Cosmos) or MU-13 (web) adds one, it must
  remove the rule doc and its weekday pointer in ONE batch, or that weekday is blocked to every new
  rule forever. Rows of a vanished rule are already withdrawn `rule_deleted` by the tick sweep.

---

## Frontend (single-user web UI): superseded

The single-user [FRONTEND_PLAN.md](./docs/plans/FRONTEND_PLAN.md) was never built; the multi-user
site delivered its wants instead:

| Want | Where it landed |
|------|-----------------|
| Show all current bookings | Dashboard `/` (MU-13), from the persisted trusted snapshot |
| Cancel a booking | `/dates` Cancel (MU-14), ownership-aware |
| Change time window / day preference, re-rank courses | Ranked booking form (MU-R3) |
| Auth on the frontend | Invite-only OAuth (MU-12) |

Not carried over: a one-click "cancel all bookings".

## Retry follow-up (from the #260 review)

- **WRITE #2 "already applied" false alarm.** If a replayed outcome write follows a first attempt
  that actually landed, the store refuses it (`check_transition` rejects the self-edge), so the row
  is flagged `needs_reconcile`, a CRITICAL is logged and the run exits non-zero, although the row is
  already correct and nothing is written twice. Detect "already applied by me" (compare the re-read
  row to the outcome's intended state) and treat it as success, in both stores + the conformance
  suite.
