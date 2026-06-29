# BLIND_POST_PLAN.md — per-course blind-POST booking at the 06:00 ET race

> **GATE MECHANISM SUPERSEDED IN PART (#147, `infra/v2.6.0`):** this doc's "Mechanism" /
> PR-table sections describe the gate as `runtime_checkable BlindPostCapable` + a
> `supports_blind_post: bool` + `isinstance(adapter, BlindPostCapable) and adapter.supports_blind_post`.
> That double-gate was REPLACED by the explicit frozen `AdapterCapabilities(blind_post=...)` flag —
> the orchestrator now gates on `adapter.capabilities.blind_post` alone, and `BlindPostCapable`
> survives ONLY as a typing cast-target (for `captcha_pool_size()` / `synthesize_blind_slots()`).
> See `core/adapter.py` and the root `CLAUDE.md` blind-POST bullet for the current design. The rest
> of this plan (the T0 burst, keep-best/cancel-extras, reguard, watcher reconcile) is unchanged.

**Status:** LIVE in prod (`infra/v2.5.0`, deployed 2026-06-22 with `dryRun=false`; the #147 gate
refactor rode `infra/v2.6.0`). PR0–PR5
MERGED — the orchestrator blind path is
live in code (gate + hybrid net + keep-best/cancel-extras + reguard + prefetch scaling;
`core/orchestrator.py`, default `blind_post_max_count=12`), the watcher >1-reservation crash-net
reconcile backstop is in place (`core/watch_orchestrator.py`), and the docs (PLAN §12 etiquette
paragraph + README + the opt-in `tests/test_foreup_canary.py` template-drift canary) are landed.
Real effect is prod-only (the gate requires the `--wait` race path + `not dry_run`); it went live
with the `infra/v2.5.0` prod deploy that shipped this code at `dryRun=false`. Authoritative
design for the blind-POST feature. Read `PLAN.md` §6/§9/§12/§13 and `RACE_PREWARM_PLAN.md` first —
this builds directly on the race path they define and does not re-litigate it.

**Scope cut line:** ONE new capability — at T0, for courses that DECLARE support
(Mangrove Bay only initially), fire concurrent "blind" book POSTs for the top-N
ranked in-window tee times built from a frozen payload template + a *computed*
`start_front` slot id, with NO dependency on a live search; keep the best-ranked
reservation that books and cancel the rest; fall back to the existing
search→rank→sequential-POST flow if every blind POST fails. Everything else
(watcher, TeeItUp, Chronogolf, durable store, email) is untouched.

---

## 1. Why

The 2026-06-07 prod failure (documented in `PLAN.md` §9 and the root `CLAUDE.md`
race notes) was a ~100 s gap between the post-T0 search and the book POST: by the
time we searched, ranked, and POSTed, the prime slot was claimed → HTTP 400 → no
tee time. RACE_PREWARM_PLAN already removed the CAPTCHA solve and the login from the
post-T0 critical path. What remains on that path is **search → rank → book**.

Blind-POST removes **search** from the critical path for the fast attempt: at T0
we already know (a) the static booking payload and (b) the deterministic
`start_front` for every morning tee time on the target date, so we can POST the
top-N ranked candidates *immediately and concurrently*, before any search returns.

Empirically (live dev tests, treated as ground truth — see §2):
- `start_front` is deterministic from the tee time (no search needed to learn it).
- A payload hand-built from a frozen template with only `time` + `start_front`
  recomputed books successfully.
- Concurrent book POSTs are NOT serialized by ForeUP's one-reservation guard —
  3 simultaneous POSTs created 3 DISTINCT reservations in ~1.1 s. So firing N
  POSTs WILL create up to N reservations that must be reconciled down to one.

---

## 2. Empirical facts (ground truth, established via dev-account live tests)

1. **`start_front` is deterministic:** `f"{YYYY}{month-1:02d}{DD}{HH}{MM}"` — month
   is **0-indexed** (JS `Date` style), as an int. Verified 15/15 against a real
   search. Computed against the target date in `America/New_York`.
2. **The book POST body is a flat dict (~73 fields). Only 2 are time-dependent:**
   - `time` = `"YYYY-MM-DD HH:MM"` with the **real (1-indexed) calendar month**.
   - `start_front` = the computed int from fact 1.
   The other ~60 static fields (`course_id`, `schedule_id`/teesheet ids, fees,
   flags) are constant for the Mangrove Bay 18-hole schedule and date-independent.
3. A payload built from a frozen template captured from a DIFFERENT tee time, with
   only `time` + `start_front` recomputed, **books successfully** (proven, then
   cancelled).
4. **Concurrent book POSTs are not serialized** — N concurrent POSTs create up to
   N distinct reservations. Reconciliation (cancel all but the kept one) is
   mandatory.
5. **The target date (today+7) is NOT searchable before T0** — a search beyond the
   7-day window returns 0 slots until 06:00 ET. So the template and the valid
   morning tee-time grid CANNOT be harvested from the target date pre-T0; they
   come from a DIFFERENT already-open date (static fields are date-independent) or
   are config-shipped. The real T0 search is the correctness fallback for grid drift.
6. **`ForeUpAdapter.book()` returns `confirmation_code=None` for Mangrove Bay**
   today: the response is a flat dict with the id in `teetime_id`/`TTID`, which the
   current extraction chain (`reservation.pending_reservation_id`/`.id`/top-level
   `id`/`booking_id`/`confirmation_code`) MISSES. For multi-POST cancel-extras we
   need each booking's id from its OWN POST response → fixing this extraction is
   now LOAD-BEARING (PR0). The within-window/tier *upgrade* cancel is unaffected —
   it gets its id from `list_reservations` via `_synthesize_managed_booking`, not
   from `book()` (see the root CLAUDE.md note).

---

## 3. The capability gate (MANDATORY — non-config, adapter-owned)

Blind-POST is gated on an **adapter capability**, never on a config flag. A
non-capable course can NEVER blind-POST even with a fat-fingered config, because
the orchestrator branches on the capability, and a course that lacks it has no
`synthesize_blind_slots` / blind-book machinery to invoke.

**Mechanism:** a new `runtime_checkable` Protocol `BlindPostCapable`, structurally
distinct from `CourseAdapter`:

```python
@runtime_checkable
class BlindPostCapable(Protocol):
    supports_blind_post: bool       # MUST be True to be eligible
    def captcha_pool_size(self) -> int: ...   # FIFO pool length; bounds the burst
    def synthesize_blind_slots(
        self, request: BookingRequest, target_date: date, *, max_count: int
    ) -> list[TeeTimeSlot]: ...
```

> **Note (landed in PR1, ahead of §12's PR3 line):** `captcha_pool_size()` ships
> on the Protocol + `ForeUpAdapter` + `FakeAdapter` in **PR1**, not PR3 — it is part
> of the capability contract and the orchestrator (PR3) reads it to size the blind
> burst at `min(len(blind_slots), captcha_pool_size())`. `runtime_checkable` only
> checks member PRESENCE, so every ForeUP adapter satisfies `isinstance` once the base
> ships these members; the `supports_blind_post` BOOLEAN is the real gate.

- `MangroveBayAdapter` sets `supports_blind_post = True` and implements
  `synthesize_blind_slots`.
- The `ForeUpAdapter` base sets `supports_blind_post = False` (a bare ForeUP
  course is NOT capable until it ships+validates its own template/grid).
- TeeItUp adapter: `supports_blind_post = False` (no attribute → fails the
  `isinstance(adapter, BlindPostCapable)` + truthy check).
- `FakeAdapter`: gains a constructor knob `supports_blind_post: bool = False` and a
  scriptable `synthesize_blind_slots` so tests exercise BOTH capable and
  non-capable paths.

**Enforcement point (single gate):** in `Orchestrator._run_course` (race path
only), the orchestrator calls a new `_blind_post_course(...)` ONLY when ALL of:
1. `self._prefetch_book` is True (race path — `--wait` ACA job), AND
2. `isinstance(adapter, BlindPostCapable) and adapter.supports_blind_post`, AND
3. `not request.dry_run` (dry-run never POSTs), AND
4. this is the PRIMARY (first-preference) course — the only course pre-warmed
   pre-T0 with a CAPTCHA token pool (fallback courses authenticate + solve inline,
   so they keep the existing sequential search flow).

If any condition is false, control falls through to the EXISTING search-based
`_run_course` body, byte-for-byte unchanged. This makes leakage impossible: the
gate is a positive capability check on the concrete adapter object, not a string
in TOML.

> **Reviewer pre-empt — "where exactly is the gate?"** It is the
> `isinstance(adapter, BlindPostCapable) and adapter.supports_blind_post` check in
> `_run_course`. A non-capable adapter does not even possess
> `synthesize_blind_slots`; the branch is unreachable for it. There is no config
> field that can flip this on for an arbitrary course. Mangrove Bay is the only
> class that sets the attribute True in this feature.

---

## 4. Template + grid sourcing (decision)

**Decision: SHIP a static template + grid in the course module (option b), with the
real T0 search as the correctness fallback (the hybrid net of §6).** We reject
"harvest from an already-open date during `_prewarm_primary`" (option a) as the
PRIMARY source for these reasons:

- **Determinism + testability.** A committed template + a pure grid generator is
  unit-testable with `respx`-free pure tests and a `FakeClock`; a pre-T0 harvest
  adds a live GET on the race's critical pre-T0 window and a parse step that can
  fail silently.
- **The template is date-independent (fact 2).** There is nothing the target date
  teaches us pre-T0 that an already-open date or a committed capture does not.
- **Grid drift is bounded and detectable.** The morning tee-time grid (start times)
  is NOT a clean fixed interval — the live 2026-06-24 afternoon capture was mostly
  7-8 min apart WITH gaps (12:15, 12:22, 12:37, 12:45, 12:52, 13:00, 13:07, 13:15,
  13:22, 13:30, 13:37, 13:52, 14:00, 14:15, 14:30). So we model the grid as an
  **EXPLICIT enumerated list of valid HH:MM start times** captured from a real search
  (OQ1 decision), NOT an `interval_min`. We generate candidate `start_front`s by
  intersecting that committed list with the request window. If the real grid drifted
  (a holiday cadence change, a shotgun start), every blind POST 400s and the hybrid
  search fallback books from real inventory. Drift therefore degrades to "as slow as
  today," never to a wrong booking.

**What ships in `mangrove_bay.py` (as module constants, NOT secrets):**
- `BLIND_POST_TEMPLATE: dict[str, object]` — the static fields, **CAPTURED LIVE and
  COMMITTED in PR2** (OQ2 CLOSED — the dev-test book-response IS the template; the full
  raw is in `mangrove_bay.py` and §2 fact 3). It is the SEARCH-slot raw shape; `book()`
  already overlays players/green_fee/total/captchaid onto `slot.raw`, so the template is
  card-free (ForeUP is card-on-file; the POST never carries PAN/CVV — root CLAUDE.md
  invariant). `time`/`start_front` are placeholders overwritten per slot.
- `BLIND_POST_MORNING_GRID: list[str] | None` — the EXPLICIT enumerated morning start
  times (HH:MM ET). **POPULATED in PR2** with the operator-approved DERIVED grid
  `["08:45","08:52","09:00","09:07","09:15","09:22","09:30","09:37","09:45","09:52","10:00"]`.
  OQ1 was closed by DERIVATION, not a direct morning capture: mornings sell out inside the
  7-day window so could not be searched, but a live read-only afternoon search proved the
  Mangrove Bay teesheet cadence is a clean gap-free 8/hour at minutes
  :00,:07,:15,:22,:30,:37,:45,:52, which this grid extrapolates over 08:45-10:00. It is a
  best-effort starting point validated retroactively (next bullet), NOT a guess we trust
  blindly — the real T0 search is still the correctness fallback. Sentinel stays `None`
  (NOT `[]`/`0`) so a future re-blanking fails LOUD instead of enumerating nothing (nit 3);
  `synthesize_blind_slots` asserts it is not None.

**Retroactive grid validation (operator request, PR2 logging):** because the morning grid
is derived rather than captured, PR2 adds logging on BOTH sides of the eventual comparison
so a real 06:00 drop confirms or corrects it: `synthesize_blind_slots` logs the configured
grid + the in-window times it will blind-POST (`MB blind-POST: synthesized …`), and
`ForeUpAdapter.search()` logs each date's matched (in-window) tee times — not just a count
(`ForeUP: matched tee times for …`). Both are times-only (PII-free). After the first live
drop, diff the two log lines: any real matched morning time absent from the grid (or vice
versa) is drift to fold back into `BLIND_POST_MORNING_GRID`. (The §4 canary, PR5, automates
the template side of this.)

`synthesize_blind_slots(request, target_date, max_count)`:
1. Assert `BLIND_POST_MORNING_GRID is not None` (fail loud pre-capture).
2. Enumerate candidate tee times = the grid start times that fall inside the
   request's (already date-scoped) `time_windows`.
3. Build a `TeeTimeSlot` for each: `slot_id` = computed `start_front` (str),
   `tee_time` = tz-aware ET datetime, `holes`/`spots`/`price` from config/template,
   and `raw` = `{**BLIND_POST_TEMPLATE, "time": "...", "start_front": <int>}`. Confirm
   this `.raw` is exactly the template shape with the two fields set, so the existing
   `book()` body (which overlays players/fee/captchaid) works UNCHANGED.
4. Return them **ranked** via the SAME `rank_slots_for_request` (slot_utils) the
   search path uses, truncated to `max_count`. This guarantees "keep best" agrees
   with the search path (§6, reviewer pre-empt).

> The template is verified against live ForeUP by the existing canary pattern
> (`tests/test_foreup_canary.py`, **integration-marked, NOT a required CI check** —
> CLAUDE.md "Required CI checks" rule; it never runs in default `-m "not integration"`
> CI, so no network at merge gate — should-fix 2): a new opt-in canary asserts that a
> freshly-searched real slot's static fields still match `BLIND_POST_TEMPLATE` (drift
> alarm). Not a merge gate; an early-warning surface.

---

## 5. Token budgeting (N) — OQ3 decision

User intent: "fire for all the tee times in the window." So N = ALL ranked in-window
candidates, capped by a config field AND by the pooled-token count. Each blind POST
consumes one single-use CAPTCHA token from the FIFO pool that `prepare_book(count=N)`
fills during the pre-T0 busy-wait (RACE_PREWARM_PLAN). We do NOT inline-solve a fresh
token per POST at T0 — that reintroduces the ~75 s latency the whole feature exists to
remove.

- **New config field `scheduler.blind_post_max_count` (default 12, ge=0).** This is the
  blind fan-out cap — DECOUPLED from `captcha_prefetch_count` (=3), which stays the
  single-POST race path's prefetch depth. Reusing the fixed `=3` would defeat the user's
  "all the tee times" intent. `0`/absent disables blind fan-out (single-POST race path).
- **`N = min(scheduler.blind_post_max_count, len(synthesized_ranked_candidates),
  adapter.captcha_pool_size())`** — the cap, the actual in-window grid count, and the
  tokens actually in hand.
- **The CAPTCHA prefetch SCALES to the blind fan-out** (the key new link, OQ3). On the
  race path, for a blind-capable primary, `_prefetch_captcha_for` solves
  `min(blind_post_max_count, len(synthesize_blind_slots(...)))` tokens CONCURRENTLY in the
  120 s lead (NOT the fixed 3). `synthesize_blind_slots` is pure/sync, safe to call pre-T0.
  So firing N blind POSTs just pops N pooled tokens — no T0 solving.
- **Cost / load (honest, OQ3):** ~$0.003/token => ~$0.03/drop at 12, and 10-12 concurrent
  solves inside the 120 s lead (the provider must handle the parallel solve burst; if it
  cannot, the pool ends up short and the token-exhaustion rule below kicks in).
- **Fewer than N tokens solved (token exhaustion):** fire ONLY `min(N, captcha_pool_size())`
  blind POSTs. Each blind `book()` pops a pooled token; the remaining blind candidates are
  NOT fired blindly — they are left to the hybrid search fallback (which may inline-solve
  off the critical path, as today). The latency win only exists when a token is already in
  hand; an inline solve at T0 is exactly the failure mode we removed.
- **Etiquette cap (ToS, §9):** N is hard-capped by `blind_post_max_count`. We do NOT fan
  out beyond it (and never beyond the in-window grid). Then cancel N−1 within ~1 s.
- **0-booked fallback reserve (extends this — see `RESEARCH_FALLBACK_PLAN.md`).** A follow-up
  ratified plan adds `scheduler.blind_post_fallback_token_reserve` (default 2) so the prefetch
  solves `min(blind_post_max_count, grid) + reserve`: the blind burst pops its N, and the
  reserve tokens REMAIN pooled so the 0-booked **fresh** search fallback books with a pooled
  token instead of a ~75 s inline solve. That plan also drops the concurrent hedge search and
  re-searches AFTER the re-guard. The config field lands first (inert); the wiring follows.

---

## 6. The hybrid fast-path / fallback (how blind + search interleave)

At T0, on the primary capable course, `_blind_post_course` runs blind POSTs AND the
real search **concurrently**, then reconciles. Precise algorithm:

```
_blind_post_course(adapter, course_id, request):     # NO target_date arg — derived (must-fix 2)
    # (pre-T0 prewarm already authenticated the primary + filled the token pool)
    target_date = request.target_dates[0]   # request is single-date-scoped by _build_booking_request
    blind_slots = adapter.synthesize_blind_slots(request, target_date,
                                                 max_count=scheduler.blind_post_max_count)  # (§5/OQ3)
    n = min(len(blind_slots), adapter.captcha_pool_size())   # token budget — int, NOT len() (must-fix 1)
    fire = blind_slots[:n]                                    # already ranked

    # INVARIANT (should-fix 1): each book() pops its pooled token (popleft) SYNCHRONOUSLY,
    # before its first POST await. With n <= captcha_pool_size() every one of the n tasks
    # gets a pooled token — none inline-solves at T0 (the latency failure this feature removes).
    # Launch concurrently:
    #   - one book() task per blind candidate (each pops a pooled token)
    #   - one real search() task (correctness fallback)
    blind_tasks = [asyncio.create_task(adapter.book(s, request)) for s in fire]
    search_task = asyncio.create_task(self._poll_for_slots(adapter, request))

    # gather blind results (return_exceptions=True): BOOKED → booked[],
    #   SlotGoneError → drop, other adapter errors (UNCERTAIN 5xx/timeout) → log+drop
    blind_results = await asyncio.gather(*blind_tasks, return_exceptions=True)
    booked = [r for r in blind_results if isinstance(r, BookingResult)
              and r.outcome == BOOKED]

    if booked:
        # FAST PATH WON. Rank booked by the SAME slot ranking (their .slot), keep
        # best, cancel the rest IN-RUN via each extra's own confirmation_code (§7/OQ5).
        best, extras = self._keep_best(booked, request)   # uses rank_slots_for_request
        await self._cancel_extras(adapter, extras)         # under the lock; §7
        search_task.cancel()                                # we won; stop the GET
        return best

    # 0 BLIND BOOKED. A blind POST may have LANDED-but-UNCERTAIN (timeout/5xx). BEFORE the
    # search-book fallback, re-run layer-2 with a FRESH read so we don't double-book (must-fix 4).
    # NOTE: ForeUP list_reservations reads the LOGIN CACHE, and authenticate() is idempotent —
    # _reguard calls refresh_reservations() (the ReservationCacheRefreshable capability: force
    # a fresh login) first so a THIS-RUN blind reservation is visible (must-fix 1/3). A plain
    # re-authenticate() would no-op and return the stale pre-burst snapshot → double-book.
    match = await self._reguard_before_fallback(adapter, course_id, request)
    if match is not None:
        return ALREADY_BOOKED(match)        # short-circuit; do NOT book a second slot

    # CORRECTNESS FALLBACK: await the real search and use the EXISTING candidate loop.
    # INVARIANT (new-issue 1): this runs ONLY when blind booked zero, so the blind book-set
    # and the search book-set are mutually exclusive by construction — no same-slot dedup needed.
    slots = await search_task
    if not slots:
        raise _CourseSkippedError()
    candidates = self._rank_slots(slots, request)
    # ... identical to today's _run_course sequential book loop ...
```

**Interleave + "keep best" precisely:**
- Blind tasks and the search task start at the same instant. Blind POSTs typically
  resolve first (~1.1 s for the burst); the search GET + parse is slower.
- "Keep best" across the WHOLE feature is decided by `rank_slots_for_request`
  applied to the `.slot` of each `BOOKED` blind result. This is the SAME ranking
  used pre-fire (to order which slots to blind-POST) and the SAME ranking the
  search path uses — so blind and search agree by construction (reviewer pre-empt).
- If ANY blind POST booked, we never consult the search result — the fast path
  already holds the best in-window slot we could fire for. The search task is
  cancelled (it was a hedge).
- If NO blind POST booked, the search result is authoritative and the existing
  sequential loop runs, unchanged.

**Lock discipline:** the blind burst itself does not need the lock for the POSTs
(ForeUP is the serialization point and concurrency is intended), but the
**reconcile (keep-best + cancel-extras) + record_terminal happens under
`request_lock`**, consistent with §9 layer 5. The advisory lock is already held by
`run()` for the whole orchestration; `_run_course` (and thus `_blind_post_course`)
executes inside it. No new lock acquisition; no deadlock risk.

---

## 7. Multi-POST reconciliation + crash safety

**Reconcile (happy, PRIMARY mechanism — OQ5):** the cancel-extras happens IN-RUN, the
same run that fired the burst. Rank the BOOKED results, keep index 0, cancel the rest via
`adapter.cancel_reservation(r.confirmation_code)` for each extra. Cancel uses the
`confirmation_code` (the `teetime_id`) from each blind POST's OWN response — which is
exactly why PR0 (the `teetime_id`/`TTID` extraction fix) is load-bearing: without a real
id per booking, we cannot cancel the extras. **The happy path NEVER depends on a later
watch run distinguishing identical reservations** — every extra is cancelled by the id we
already hold.

**Cancel-extras failure:** if a `cancel_reservation` on an extra raises, log a
CRITICAL warning (the user holds >1 reservation for that date) and continue
cancelling the others. We still RETURN the kept best (the user has a booking). The
stranded extra is recovered by the PR4 watch net (below).

**Crash safety — job dies after booking N, before the in-run cancel completes (BACKSTOP
only, not the happy path):**
- There is no durable store (by design, M3 cut). The backstop is the **next watch run's
  `list_reservations`** — and the plan must be honest about exactly how that sees the
  stranded extras (must-fix 3):
  - `ForeUpAdapter.list_reservations()` reads the **LOGIN-RESPONSE CACHE, not a live GET**
    (base.py:637). A blind reservation created in the crashed run is invisible to any
    `list_reservations()` that did not re-authenticate after it landed.
  - **A FRESH watch PROCESS re-authenticates** at the top of `_check_course`
    (watch_orchestrator.py:321) before `list_reservations()`, so it rebuilds the login
    cache and DOES see the stranded extras. The recovery rests on this re-auth, not on a
    (nonexistent) live read.
  - Matching also relies on `party_size == len(request.players)` (orchestrator.py:499,
    watch_orchestrator.py:329): a blind reservation is created for the request's party
    size, so it matches. (If party size is changed between runs, the orphan won't match —
    the same caveat the existing layer-2 guard already documents in courses/CLAUDE.md.)
- **PR4 watch net (CONSERVATIVE crash-net, generalize 1→N) — ✅ IMPLEMENTED:** when the
  watcher finds >1 reservation matching the request's party_size on the target date and
  policy is enabled, `WatchOrchestrator._reconcile_duplicate_reservations` keeps the
  best-ranked (`_rank_reservations`, the same `rank_slots_for_request` order) and cancels
  the rest UNDER the `request_lock`. This is the BACKSTOP, not the primary
  mechanism. **Residual risk (documented honestly, must-fix 3 / should-fix 3 / PLAN §12):**
  `is_managed` CANNOT distinguish a blind extra from a deliberate manual second booking —
  server-sourced `ExistingReservation.confirmation_code` is a RAW id (no `TTB:` prefix), so
  `is_managed` is always False for them and all N matches look identical. PR4's policy is
  therefore "for THIS request's target date + party_size, keep best, cancel the rest." The
  accepted consequence: a deliberate MANUAL second booking by the same account, same date,
  same party size, would also be cancelled. Acceptable for this single-user bot; documented
  in PLAN §12. PR4 only fires when `one_booking_policy.enabled = true` (already true in prod).
- **Residual window:** between a crash and the next watch fire (≤10 min cron), the user
  holds up to N reservations for one morning. Benign for a municipal course (no scalping,
  the user is eligible for all of them); self-heals within one watch cycle. Same class of
  residual the cancel-before-book upgrade already accepts. No durable record (M3 stays cut).

**Idempotency / double-book interplay (§9):**
- Layer 2 (`list_reservations` pre-book guard) still runs in `_prewarm_primary`
  pre-T0 — if we are ALREADY booked for the date, `run()` short-circuits
  ALREADY_BOOKED BEFORE T0 and never blind-POSTs (existing path, unchanged).
- The blind burst is the intentional, ToS-accepted exception to "single attempt per
  slot": we fire N DISTINCT slots concurrently (not the same slot N times). Each
  slot is still attempted at most once per run. The reconcile collapses to one.
- `_CourseSkippedError` / `NO_INVENTORY`: if every blind POST fails AND the search
  fallback yields no candidates, `_blind_post_course` raises `_CourseSkippedError`
  exactly like today, so `run()` advances to the next course and records
  `NO_INVENTORY` if none book. No crash.

---

## 8. confirmation_code extraction fix (PR0 — load-bearing, TDD)

Add `teetime_id` and `TTID` to `book()`'s fallback extraction chain, preserving the
`TTB:` prefix + `is_managed` semantics and `cancel_reservation`'s prefix-stripping.
New chain (order matters — most-specific first, keep existing entries):

```
conf_raw = (
    reservation.get("pending_reservation_id")  # existing
    or reservation.get("id")                    # existing
    or data.get("pending_reservation_id")       # existing
    or data.get("id")                           # existing
    or data.get("booking_id")                   # existing
    or data.get("confirmation_code")            # existing
    or data.get("teetime_id")                   # NEW (flat MB response)
    or data.get("TTID")                         # NEW (flat MB response)
)
```

- The `MANAGED_BOOKING_TAG` (`TTB:`) prefix stamping is UNCHANGED — `conf` is still
  `TTB:<raw>` when `conf_raw` is found.
- `cancel_reservation` still strips `TTB:` → raw id. The extracted `teetime_id`/
  `TTID` IS the raw id ForeUP's DELETE endpoint expects (confirmed: it is the same
  id `_parse_reservation` already reads — base.py:801-802 reads `TTID`/`teetime_id`
  from the login-cache shape). **Cross-reference (nit 2):** both parsers
  (`book()`'s extraction chain and `_parse_reservation`) read the SAME two flat-id
  fields; keep them in sync if ForeUP renames either, or the book-side id and the
  list-side id would diverge and cancel-extras would target the wrong reservation.
- `ExistingReservation` (server-sourced) semantics unchanged — `is_managed=False`
  for raw server ids (no prefix).

This must NOT regress: a response that already has `reservation.pending_reservation_id`
must still use it (the new keys are LATER in the chain).

---

## 9. Anti-bot / ToS posture (honest)

Blind-POST is more aggressive than holding one reservation: we briefly hold up to N
(default 3) reservations and cancel N−1 within ~1 s. The user has explicitly
accepted this. Documented in `PLAN.md` §12 as an addition:

- **What changes:** at the drop, up to `captcha_prefetch_count` concurrent book
  POSTs (vs sequential single POSTs), then immediate cancel of the extras.
- **What does NOT change:** still one booking per request as the END state; still no
  IP rotation, no multiple accounts, no login hammering; still capped (N ≤
  `captcha_prefetch_count`, default 3 — NOT the full grid); extras cancelled
  promptly (same run, under the lock, within ~1 s). This is the existing
  "bulk-book-and-cancel (scalper pattern)" line being relaxed in a bounded way for a
  single user's single morning — NOT scalping (the user keeps exactly one, is
  eligible for all, holds none beyond ~1 s + the ≤10 min crash-recovery worst case).
- **ForeUP detection risk rises** (a burst of N near-simultaneous POSTs from one
  account is a louder signal than one POST). Accepted for single-user personal use,
  same posture as solving the CAPTCHA. The cap keeps the burst small.

---

## 10. Clock / DST invariants (unchanged)

- `synthesize_blind_slots` computes `start_front` and `time` from `target_date` in
  `America/New_York` via the adapter's `_timezone` + the injected request scoping
  (the request is already date-scoped by `_build_booking_request`). The 0-indexed
  month in `start_front` vs the 1-indexed month in `time` is encoded explicitly and
  unit-tested (a DST-month boundary test: e.g. a March target where month-1 crosses).
- T0 timing is entirely unchanged — blind POSTs fire AFTER `busy_wait_until(t0_target)`
  in `run()`, exactly where `book()` fires today.
- Tests use `FakeClock`; no `datetime.now()` in any new code path except inside the
  ForeUP `book()` `booked_at` stamp (already the case).

---

## 11. State-machine relationship (§9.1)

Blind-POST is a NEW pre-state that FANS OUT the `POSTING` state:

```
   PRE_BOOK (pre-T0 prewarm: layer-2 list_reservations guard) ── already booked ─→ ALREADY_BOOKED (short-circuit, pre-T0)
       │ clear
       v
   BLIND_FANOUT  (capable + race + primary + not-dry-run)
       │  fire N concurrent book() POSTs (each = one POSTING, one token)
       │  + concurrent real search() (hedge)
       ├── ≥1 BOOKED ──→ KEEP_BEST ──→ cancel extras IN-RUN by own id (under lock) ──→ BOOKED (terminal)
       │                                  └ cancel-extra fails → log CRITICAL, still BOOKED;
       │                                    PR4 watch net reconciles via re-auth+list_reservations
       └── 0 BOOKED  ──→ REGUARD (force-refresh snapshot + list_reservations; landed-but-uncertain check, must-fix 1/4)
                            ├── match found ──→ ALREADY_BOOKED (terminal; do NOT search-book)
                            └── no match ──→ SEARCH_FALLBACK ──→ existing sequential candidate loop
                                              ├ books → BOOKED
                                              └ exhausted/empty → _CourseSkippedError → next course / NO_INVENTORY
```

Invariants preserved: each individual `book()` is still called at most once per
slot per run (the fan-out is across DISTINCT slots, not retries of one). UNCERTAIN
(timeout/5xx) on any single blind POST still propagates as today — that POST's task
returns the exception; reconciliation of a possibly-landed POST is the watcher's job,
asynchronously (M2.T3's in-run path was cut — PLAN.md §9.1). A blind POST 4xx maps
to `SlotGoneError` (existing `book()` behavior) and is dropped from `booked`.

> **Reviewer pre-empt — UNCERTAIN inside the fan-out (must-fix 4).** If a blind POST
> times out (5xx/transport), `asyncio.gather(return_exceptions=True)` captures it; we log
> it and treat that candidate as not-booked. The reservation MAY have landed (the §9
> UNCERTAIN case). Two guards prevent a double-book:
> 1. **If ≥1 OTHER blind POST booked:** we keep-best + cancel-extras and NEVER search-book,
>    so the uncertain landed one is simply an extra recovered by the PR4 watch net.
> 2. **If ZERO blind POSTs returned BOOKED but one landed-uncertain:** `_reguard_before_
>    fallback` FORCE-REFRESHES the snapshot via `refresh_reservations()` (the
>    `ReservationCacheRefreshable` capability — reset `_logged_in` + re-login to rebuild
>    ForeUP's login cache; a plain idempotent re-authenticate would NOT rebuild it — must-fix
>    1/3) and `list_reservations()`; the landed reservation is now visible, we short-circuit
>    ALREADY_BOOKED, and the fallback search-book NEVER runs. This is the fix for the
>    production-biting single-run double-book the reviewer flagged. The PR4 watch net
>    remains the backstop for the crash-after-land case (no re-guard ran).

---

## 12. PR-by-PR breakdown (each small, test-first)

Ordered so each PR is independently mergeable and reviewable. TDD: every PR writes
failing tests FIRST. Docs each PR updates are listed.

### PR0 — `book()` confirmation_code extraction fix (load-bearing prerequisite)  ✅ MERGED
- **Code:** add `teetime_id`, `TTID` to `ForeUpAdapter.book()`'s extraction chain.
- **Tests (red first):**
  - `test_book_extracts_teetime_id` — flat response `{"teetime_id": 123}` → conf
    `"TTB:123"`.
  - `test_book_extracts_TTID` — `{"TTID": "abc"}` → `"TTB:abc"`.
  - `test_book_prefers_pending_reservation_id_over_teetime_id` — both present →
    existing field wins (no regression).
  - `test_cancel_strips_ttb_from_teetime_id_conf` — cancel of `"TTB:123"` → DELETE
    `/reservations/123` (respx).
- **Docs:** root `CLAUDE.md` (update the already-staged note — change "cosmetic
  only" to "fixed in PR0; now load-bearing for blind-POST cancel-extras");
  `src/teetime/courses/CLAUDE.md` (Mangrove Bay response shape).

### PR1 — capability Protocol + FakeAdapter knob (no orchestrator wiring yet)  ✅ MERGED
- **Code:** `core/adapter.py` add `BlindPostCapable` Protocol. `ForeUpAdapter`
  base: `supports_blind_post = False`. `FakeAdapter`: `supports_blind_post` ctor
  knob (default False) + scriptable `set_blind_slots(...)` + `synthesize_blind_slots`.
  Stub `synthesize_blind_slots` raises `NotImplementedError("BLIND_POST_PLAN PR2")`
  on `ForeUpAdapter`/`MangroveBayAdapter` until PR2.
- **Tests:** `isinstance(MangroveBayAdapter(...), BlindPostCapable)` is False until
  PR2 sets it True (so PR1 asserts the base is non-capable);
  `isinstance(FakeAdapter(supports_blind_post=True), BlindPostCapable)` True;
  default Fake / TeeItUp non-capable.
- **Docs:** `CLAUDE.md` (new capability-gate invariant); `BLIND_POST_PLAN.md` (this).

### PR2 — Mangrove Bay grid capture + `synthesize_blind_slots`  ✅ MERGED
- **Code:** `mangrove_bay.py` — `BLIND_POST_TEMPLATE` is ALREADY committed (OQ2 closed;
  card-free capture). PR2 captures the MORNING `BLIND_POST_MORNING_GRID` (the `None`
  sentinel → the real HH:MM list), sets `supports_blind_post = True` (already set), and
  implements `synthesize_blind_slots` (pure: assert grid not None, intersect EXPLICIT grid
  ∩ window, compute `start_front`/`time`, build ranked `TeeTimeSlot`s whose `.raw` is the
  template + the two fields).
- **Spike (OQ1):** capture one real MORNING search of an open Mangrove Bay date; exit
  criterion: the enumerated `BLIND_POST_MORNING_GRID` matches the searched start times 1:1.
- **Tests (red first, pure + FakeClock):**
  - `test_start_front_is_zero_indexed_month` — known date → exact int.
  - `test_synthesize_filters_to_window` — only in-window grid times returned.
  - `test_synthesize_returns_ranked_by_midpoint` — order matches
    `rank_slots_for_request`.
  - `test_synthesize_respects_max_count`.
  - `test_synthesize_raw_has_time_and_start_front` — raw payload merges template +
    recomputed fields; `time` is 1-indexed month, `start_front` 0-indexed.
  - `test_synthesize_dst_month_boundary` — March/Nov target.
- **Docs:** `src/teetime/courses/CLAUDE.md` (Mangrove Bay blind-POST specifics).

### PR3 — orchestrator blind path + hybrid fallback + keep-best/cancel-extras  ✅ WIRED
- **Code:** `Orchestrator._run_course` gate (§3); `_blind_post_course`, `_keep_best`,
  `_cancel_extras`, `_reguard_before_fallback` (§6/§7). `captcha_pool_size()` is on the
  `BlindPostCapable` Protocol + `ForeUpAdapter` + `FakeAdapter` so the orchestrator sizes
  the burst without reaching into the deque. The `ReservationCacheRefreshable` capability
  Protocol (`core/adapter.py`) + `ForeUpAdapter.refresh_reservations()` force a fresh login
  snapshot in `_reguard_before_fallback` (must-fix 1: a plain idempotent re-auth would miss
  a landed blind reservation and double-book). New config field
  `scheduler.blind_post_max_count` (default 12, OQ3) + scale `_prefetch_captcha_for` to
  `min(blind_post_max_count, len(synthesize_blind_slots(...)))` for the blind-capable
  primary.
- **Tests (red first, FakeAdapter/FakeClock/InMemoryStore — `_build`):**
  - `test_blind_post_books_best_and_cancels_extras` — 3 blind BOOKED → keep best
    (by rank), 2 cancels issued.
  - `test_blind_post_all_gone_falls_back_to_search` — all blind raise
    `SlotGoneError`; search returns a slot → booked via fallback.
  - `test_blind_post_token_exhaustion_fires_fewer` — pool size 1 → only 1 blind
    POST, rest left to fallback.
  - `test_non_capable_course_never_blind_posts` — capable=False → search path only
    (`synthesize_blind_slots` never called).
  - `test_dry_run_never_blind_posts` — dry-run → DRY_RUN via search path.
  - `test_non_primary_course_uses_search_path` — fallback course is search-only.
  - `test_blind_post_keep_best_agrees_with_search_ranking` — canary: keep-best uses
    `rank_slots_for_request`.
  - `test_cancel_extra_failure_still_returns_best` — one cancel raises → CRITICAL
    log, best still returned.
  - `test_uncertain_blind_post_does_not_crash_run` — one blind task raises a
    transport/5xx-style error; run still keeps a booked one or falls back.
  - `test_zero_booked_but_landed_uncertain_reguards_to_already_booked` (must-fix 1/4) —
    all blind tasks "fail" (e.g. raise/SlotGone) but the re-guard `list_reservations`
    (post FORCE-REFRESH) now returns a matching reservation → ALREADY_BOOKED, and the
    fallback `book()` is NEVER called (assert `search`/`book` call counts). The double-book
    guard. The collaborator faithfully models ForeUP's IDEMPOTENT `authenticate()` (a 2nd
    call is a no-op and does NOT reveal the booking) so only `refresh_reservations()` reveals
    it — the regression guard for must-fix 1.
  - `test_reguard_refreshes_before_listing` — `_reguard_before_fallback` calls
    `refresh_reservations()` before `list_reservations()` on a `ReservationCacheRefreshable`
    adapter; `test_reguard_falls_back_to_authenticate_for_non_refreshable` covers the
    `authenticate()`-before-list fallback for non-refreshable (live-GET) adapters.
  - `test_prefetch_scales_to_blind_fanout` — blind-capable primary → `prepare_book` count
    == `min(blind_post_max_count, len(blind_slots))`, not the fixed `captcha_prefetch_count`.
- **Docs:** `CLAUDE.md` (race-path blind invariant); `PLAN.md` (new §
  "Blind-POST at T0" + state-machine note); `BLIND_POST_PLAN.md` status → wired.

### PR4 — watcher: reconcile >1 reservation on the target date (CRASH-NET backstop only)
- **Code:** `WatchOrchestrator._check_course` `matching` branch: when >1 reservation
  matches the request's `party_size` on the target date and policy enabled, keep
  best-ranked, cancel the rest (generalize the one-booking invariant 1→N). Under the lock.
  This is the BACKSTOP — the happy path's in-run `_cancel_extras` (PR3) is the primary
  mechanism (OQ5). Recovery depends on the FRESH watch process re-authenticating
  (rebuilding ForeUP's login cache) before `list_reservations` (must-fix 3) and on
  `party_size` matching.
- **Policy caveat (must-fix 3 / should-fix 3):** `is_managed` is always False for
  server-sourced reservations (raw id, no `TTB:` prefix), so the N matches are
  indistinguishable. PR4 keeps the best by rank and cancels the rest for THIS request's
  date+party_size — accepting that a deliberate manual second booking matching the same
  date+party_size would also be cancelled (single-user; documented PLAN §12).
- **Tests (red first):**
  - `test_watch_reconciles_multiple_reservations_same_date` — 3 existing same date →
    keep best, 2 cancels.
  - `test_watch_reconcile_keeps_best_by_rank` — the kept one is the highest
    `rank_slots_for_request` slot.
  - `test_watch_single_reservation_unchanged` — N=1 → today's behavior.
- **Docs:** `CLAUDE.md` (watcher reconcile note); `BLIND_POST_PLAN.md` §7.

### PR5 — PLAN.md §12 etiquette + README/config docs + canary
- **Code:** opt-in integration canary (`tests/test_foreup_canary.py`, `@pytest.mark.
  integration`) asserting `BLIND_POST_TEMPLATE` static fields still match a live-searched
  slot (drift alarm). **NOT added to required CI checks; never runs in default
  `-m "not integration"` CI — no network at the merge gate** (should-fix 2).
- **Docs:** `PLAN.md` §12 (blind-POST etiquette paragraph, §9 above; INCLUDE the must-fix-3
  residual: PR4's keep-best-cancel-rest would also cancel a deliberate manual second
  booking on the same date+party_size — accepted for this single-user bot); `README.md`
  (feature + the new `blind_post_max_count` config field, default 12); `BLIND_POST_PLAN.md`
  status banner (now realized: `LIVE in prod`, `infra/v2.5.0`, 2026-06-22).

> **Ordering note:** PR0→PR1→PR2 are prerequisites for PR3. PR4 is independent of
> PR3 (it is the safety net and can land before or after) but should land BEFORE
> blind-POST goes live in prod so the crash-recovery net exists. PR5 is docs+canary,
> last.

---

## 13. Open questions — ALL RESOLVED (round 1)

1. **OQ1 — grid (RESOLVED, PR2 capture spike, NOT a user decision).** The `start_front`
   FORMULA is proven 15/15. The grid is NOT a clean interval — model it as an EXPLICIT
   enumerated HH:MM list (`BLIND_POST_MORNING_GRID`), captured from a real search and
   committed. Afternoon grid captured 2026-06-24; the MORNING (08:45-10:00) grid is the
   one remaining PR2 capture (mornings sell out). Hybrid real-search fallback covers drift.
   Exit criterion: enumerated grid matches the searched start times 1:1.
2. **OQ2 — template (CLOSED).** The live dev book-response IS the template; card-free
   (ForeUP card-on-file). Committed verbatim as `BLIND_POST_TEMPLATE` in `mangrove_bay.py`
   (the search-slot raw shape; `book()` overlays players/fee/captchaid). No spike needed.
3. **OQ3 — N (DECIDED).** N = ALL ranked in-window candidates, capped by the new config
   field `scheduler.blind_post_max_count` (default 12) AND the pooled-token count. The
   CAPTCHA prefetch SCALES to `min(blind_post_max_count, len(grid))` (decoupled from the
   fixed `captcha_prefetch_count=3`). Cost ~$0.03/drop; 10-12 concurrent solves in the lead.
4. **OQ4 — pre-fallback re-guard (DECIDED yes).** Re-run the layer-2 `list_reservations`
   guard (with re-auth) before the fallback search-book (`_reguard_before_fallback`),
   so a landed-but-uncertain blind POST short-circuits ALREADY_BOOKED. ~2 s on the
   already-lost path. Baked in (§6, must-fix 4).
5. **OQ5 — extras reconciliation (DECIDED).** Cancel extras IN-RUN via each blind POST's
   own `teetime_id` (PR0). The happy path NEVER depends on a later watch run distinguishing
   identical reservations. PR4's watch reconcile is a CONSERVATIVE crash-net backstop only,
   with the documented residual that a deliberate manual second booking on the same
   date+party_size would also be cancelled (PLAN §12).

No open questions remain. Residual risks (manual-second-booking cancellation; ≤10 min
crash window holding N reservations; provider concurrent-solve load at N=12) are
documented and accepted in §5/§7/§9.
