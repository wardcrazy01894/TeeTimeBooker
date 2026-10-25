# Engine invariants (`src/teetime/core/`, `persistence/`, the adapters' error contract)

Moved here from the root `CLAUDE.md` on 2026-10-25 so they load only when you work under this
tree (the root keeps the pointer and the non-negotiables). Each bullet is a rule that code,
tests or incidents have made load-bearing. Change one only with a test that proves the new
behaviour, and update this file in the same PR. The design is [PLAN.md](../../../PLAN.md); the
per-course quirks are [`../courses/CLAUDE.md`](../courses/CLAUDE.md); the tenant path that
runs this engine unmodified is [`../tenant/CLAUDE.md`](../tenant/CLAUDE.md). **Tests live in
`tests/`, which does not load this file: read it before touching `tests/test_orchestrator.py`,
`tests/test_watch*.py` or any test named after a section below.**

<!-- toc -->
## Contents

- [Configuration, dates and time](#configuration-dates-and-time)
- [Double booking, idempotency and stores](#double-booking-idempotency-and-stores)
- [Search and book error handling](#search-and-book-error-handling)
- [The 06:00 booking race (race path only)](#the-0600-booking-race-race-path-only)
- [Blind-POST burst (Mangrove Bay)](#blind-post-burst-mangrove-bay)
- [Watcher and upgrade](#watcher-and-upgrade)

<!-- /toc -->

## Configuration, dates and time


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
- **Hard booking cutoff** (LEADTIME_SKIP_PLAN F1; on the tenant path it is PER PERSON since
  2026-10-08, see the Multi-user section). `request.booking_cutoff`
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

## Double booking, idempotency and stores


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

## Search and book error handling


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

## The 06:00 booking race (race path only)


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

## Blind-POST burst (Mangrove Bay)


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
  `scheduler.blind_post_stagger_ms` (default `(-369, 0)` since 2026-10-04 with `blind_post_max_count = 2`
  since 2026-10-02; was `(-400, 0)` until 2026-10-04 and `(-400, -250, 0)` since 2026-09-30; `early_arrival_ms = 369`; see the
  `too_early` reason below) gives each POST its own offset
  from T0, paired positionally with the RANKED slots; `_fire_blind_post` sleeps to `T0 + offset`
  (a non-positive delay fires immediately, so a late cron never waits). Why: every drop in the log
  window came back 3/3 or 0/3, which a real slot race cannot produce; a simultaneous burst
  point-samples ForeUP's release flip, and a pre-open POST gets the same `400 "Time not
  available."` as a claimed slot (the server `Date` header, logged since `infra/v2.11.0`, has only
  1 s resolution). Staggering orders outcomes by offset (clean cutoff = pre-open rejection,
  unordered = real race) and guarantees one POST is SENT no earlier than T0 (tail offset `0`).
  - **`stagger[0] == -early_arrival_ms`**, so the rank-0 slot fires at the busy-wait wake instant
    (T0 − 369 ms since 2026-10-04; T0 − 400 ms from 2026-09-30, the pre-stagger T0 − 500 ms before);
    **nothing is ever scheduled earlier than `stagger[0]`** (operator directive 2026-08-15). Both pinned by `tests/test_container_config_parity.py`.
  - **The burst RE-RANKS with `rank_slots_for_request` before pairing offsets.** Offsets ascend
    with position, so the best slot must POST first or the 1-per-day rule could reject it in
    favour of a worse sibling. A `field_validator` rejects a descending offset list.
  - Offsets earlier than `-early_arrival_ms` are clamped and logged, not rejected. `()` means
    legacy simultaneous firing; `_local_demo_scheduler` uses it.
  - The per-POST INFO line `blind-POST sent %s (planned %+dms) slot %s → %s (answered in %s)`
    reports the MEASURED send offset (a late run fires everything at once, and logging the plan
    would show a ladder that never happened) and the measured ROUND TRIP to the answer, result or
    rejection (2026-10-04: whether the T0 rung could ever be skipped once the early rung has
    booked depends on ForeUP answering inside the stagger gap, and nothing else measures that).
    With `_blind_outcome_label` it is the whole point of the feature: **don't drop it when
    touching the burst loop.**
- **A blind-POST rejection is tagged with WHY (`SlotGoneError.reason`).** ForeUP returns HTTP 400
  for rejections with opposite meaning and no machine-readable discriminator, so
  `ForeUpAdapter._classify_book_rejection` tags by the `msg` prose (first match wins, in the
  `_BOOK_REJECTION_REASONS` order):
  - `unavailable` (`"Time not available."`): claimed first, OR our POST beat the release flip.
    The only reason that bears on the pre-open-vs-race question.
  - `daily_limit` (`"...1 online reservation per day."`): alongside a booked sibling, ForeUP is
    bouncing the surplus of a burst WE WON (no race information). With NOTHING booked it means a
    reservation for that date already existed; `_rejection_summary` then says "we already hold a
    reservation" instead of "TOTAL wipeout, falling back", because the re-guard usually
    short-circuits to `ALREADY_BOOKED` ("usually": it matches date AND party size, so a manual
    booking with another party size still falls through to the harmless fallback search).
  - `too_early` (`"Booking for <date> starts at <date> 6:00am (EDT)"`): our POST landed BEFORE the
    release; the slot was never contested. First seen 2026-09-29/30 on the -500 ms POST (a server
    `Date` of 09:59:59): ForeUP's clock had run ~0.5 s fast until then, so -500 used to arrive at
    06:00:00 by its clock and often booked. That wasted the rank-0 slot two days running, so the
    ladder moved to `-400/-250/0` until 2026-10-02 (operator, 2026-09-30: watch it, and move
    again if -400 is still `too_early`). The -400 POST then won consistently, so on 2026-10-02 the operator
    dropped the middle rung: the ladder was `-400/0` until 2026-10-04 (one POST early, one at T0; burst 2).
    On 2026-10-04 ForeUP's lead had shrunk to ~395 ms: a -396 POST was `too_early` (Date 09:59:59)
    while its sibling 1 ms later booked, so the first rung is `-369` (operator's pick): `-369/0`. The
    operator summary email says "too early (before the booking window opened)".
  - `conflict` (409) and `unknown` (fail-soft default, so other adapters and unobserved wordings
    are never misfiled).
  It is **diagnostic only**: every reason routes identically (`SlotGoneError` → next slot). It
  surfaces as `gone[<reason>]` and `blind-POST N of M slot(s) rejected (<reason>=<count>, …)`.
  The markers (`_BOOK_DAILY_LIMIT_MARKERS` / `_BOOK_UNAVAILABLE_MARKERS` /
  `_BOOK_TOO_EARLY_MARKERS`) match only wordings
  observed live, on the stable prose tail ("make" and "have 1 online reservation per day" both
  seen). Evidential caution: a 1-booked / 2-`daily_limit` shape is NOT evidence of a stagger
  effect; a simultaneous pre-stagger burst produced it on 2026-07-11.

## Watcher and upgrade


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
