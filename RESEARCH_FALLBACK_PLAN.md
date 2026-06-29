# RESEARCH_FALLBACK_PLAN.md

> **Status:** RATIFIED (plan-with-review, 1 round → adversarial-reviewer **APPROVE**, no
> must-fixes; the 3 should-fixes + 2 nits are folded in below, tagged inline). Course-dependent
> "re-search after blind-fail" change to the booking `Orchestrator`. Extends
> `BLIND_POST_PLAN.md` §6/§11. Race-critical 6:00 AM-drop code; correctness > cleverness.
>
> **Settled before this draft (do NOT re-litigate — see the task brief):**
> 1. On the blind path, when **0** blind POSTs book, fire a **FRESH** search AFTER the
>    blind POSTs return and book from that fresh result (added round-trip latency accepted).
> 2. The new behavior is **ONLY** the blind path (blind-capable primary, gated by
>    `_should_blind_post`). Non-blind courses are byte-for-byte unchanged.
> 3. Add a **configurable token RESERVE** so the fallback book fires with a fresh *pooled*
>    token, not a ~75 s inline solve.
> 4. `blind_post_max_count = 3` is already merged; unchanged.

---

## 1. Problem (precise, with file:line)

`src/teetime/core/orchestrator.py::_blind_post_course` (lines **343–459**) fires `N`
blind book POSTs at T0 **concurrently** with a hedge search:

- `blind_tasks` launched at line **368**, `search_task = create_task(_poll_for_slots…)`
  at line **369**.
- On the **happy path** (≥1 booked, lines **415–427**) the hedge `search_task` is
  **cancelled** (line **419**, `_cancel_task`) — a GET fired at T0 for nothing.
- On the **0-booked path** (lines **429–459**): re-guard (line **433**), then on no match
  the fallback `await search_task` (line **453**) consumes the **hedge snapshot taken at
  ~T0+0.7 s**, ranks it (line **456**), and books at ~T0+3.5 s (after the gather ~1.1 s +
  `refresh_reservations` re-auth ~2 s + `list_reservations`). The snapshot is ~2.8 s
  **stale** at book time → it lists slots other players already claimed.
  `_book_from_candidates` (lines **282–312**) walks past each dead candidate
  (400 → `SlotGoneError` → next), and **each dead candidate burns a scarce pooled CAPTCHA
  token + a round-trip**.

Compounding it: with `blind_post_max_count = 3`, the pre-T0 prefetch
(`_captcha_prefetch_count_for`, lines **703–722**) pre-solves exactly
`min(3, grid) = 3` tokens — **all consumed by the 3 blind POSTs** — so the pool is **DRY**
by fallback time and `ForeUpAdapter.book()` (base.py lines **624–636**) hits a ~75 s
**inline** solve anyway. Two compounding failures on the path that exists precisely to
recover a missed drop.

---

## 2. Resolved design questions

### Q1 — Drop the concurrent hedge entirely. **RECOMMEND: DROP.**
The hedge is net-negative on **both** paths:
- **Happy path:** today the hedge is always cancelled (wasted T0 GET, lines **369/419**).
  Dropping it means the happy path issues **zero** search GETs — strictly better load and
  one fewer task competing for the event loop at the most latency-critical instant.
- **0-booked path:** the hedge snapshot is the *stale* one we are trying to avoid. We
  replace it with a single fresh search fired **after** the burst+reguard.
- **Etiquette (PLAN §12):** GET count goes **down**, never up. Happy path: 0 search GETs.
  0-booked path: exactly **1** search GET (plus the reguard's warm-up GET on a forced
  re-login). No new hammering.
- **Code simplicity:** removes the `search_task` plumbing, **both** `_cancel_task`
  call sites (lines **419**, **435**), the `await search_task` (line **453**), and the
  `_cancel_task` helper itself (lines **542–572**) — including its subtle
  "already-DONE hedge stored a non-cancel error (429/Captcha)" arm, which only ever
  mattered *because* we cancelled a hedge. No hedge → no leaked task, no swallowed
  `CancelledError` concern (see §7 Cancellation).

### Q2 — Ordering: **gather blind → re-guard (match → ALREADY_BOOKED, stop) → fresh search → rank → book.** **CONFIRMED.**
- The re-guard `ALREADY_BOOKED` short-circuit (a landed-but-uncertain blind POST) **MUST**
  gate every fallback book — double-booking defense. So the fresh search runs **strictly
  after** `_reguard_before_fallback` returns `None`. (Tested: §6 PR3
  `test_reguard_match_short_circuits_before_fresh_search` asserts `search_call_count == 0`
  when the reguard matches.)
- This *also* makes the search **maximally fresh**: it runs post-re-auth (~T0+2.5 s), the
  latest possible snapshot before book.
- **RECOMMEND AGAINST overlapping the fresh search with the re-guard re-auth on the shared
  client.** `_reguard_before_fallback` (lines **508–540**) calls
  `refresh_reservations(creds)` which (base.py lines **372–384**) resets `_logged_in` and
  re-runs the warm-up GET + login POST — **mutating PHPSESSID / cookies on the shared
  `self._client`**. An `httpx.AsyncClient` is not safe for a search GET issued concurrently
  with a request that rotates the session cookie mid-flight: the GET could send a
  half-rotated cookie jar or read a session mid-mutation. **Serialize**: reguard completes
  the re-auth, *then* the fresh search GET starts on a settled session. The freshness we
  gain from "after re-auth" is the point; overlap would buy ~2 s of latency for a session
  race we cannot accept on the booking path.

### Q3 — Token reserve formula + config. **RECOMMEND: new field `blind_post_fallback_token_reserve` (default 2); prefetch = `min(blind_post_max_count, len(grid)) + reserve` for a blind-capable primary.**
- **New field, not reuse `captcha_prefetch_count`.** `captcha_prefetch_count` is the
  *single-POST (non-blind) race* depth; the reserve is a *blind 0-booked fallback* concern.
  Coupling them would make two unrelated knobs move together. A dedicated field is explicit,
  independently tunable, and matches the repo's one-field-per-concern scheduler convention.
- **Burst size is unchanged.** `synthesize_blind_slots(max_count=blind_post_max_count)`
  already truncates the grid to ≤ `blind_post_max_count`, so `len(blind_slots) ≤ 3`. The
  burst `n = min(len(blind_slots), captcha_pool_size())`; with the pool inflated to
  `len(blind_slots) + reserve` we get `n = min(len, len+reserve) = len(blind_slots)` —
  the reserve tokens are **never fired**, they sit in the pool for the fallback. (Tested:
  PR2 `test_reserve_does_not_increase_blind_burst`.)
- **FIFO popleft leaves tokens IN THE POOL for the fallback (verified, base.py).** Pooled
  tokens are appended in solve order (`prepare_book`, line **570** `extend`); `book()` pops
  the **oldest** (`popleft`, line **627**). **Correction (reviewer should-fix #1):**
  `prepare_book` solves all `count` tokens in ONE concurrent `gather` (base.py line **565**),
  so the burst and reserve tokens are all solved in the same ~75 s window and are **the same
  age** (~T0−45 s) at fire time — the reserve tokens are NOT "fresher." The value of the
  reserve is simply that `reserve` tokens **REMAIN pooled** after the 3 blind POSTs pop their
  3. FIFO popleft only decides WHICH tokens the burst consumes (the first-appended) vs which
  stay for the fallback; all are equally fresh.
- **Prefetch is best-effort / NI10.** `count = len + reserve ≥ 2 > 1`, so `prepare_book`
  is on the `count > 1` branch (base.py lines **580–586**) — **never raises**; every
  failure is swallowed and `book()` falls back to an inline solve. A prefetch miss can
  never crash the run.
- **`_captcha_solve_sem` does not bound the prefetch.** The semaphore (base.py line
  **176**, used only in `_solve_captcha_inline`, line **498**) bounds *inline* solves. The
  prefetch calls `self._captcha_provider()` directly (line **565**), unbounded — intended,
  off the critical path. With `blind_post_max_count=3` + `reserve=2` the prefetch fires **5**
  concurrent solves pre-T0; the old code already tolerated up to **12** (former
  `blind_post_max_count` default), so 5 is well inside provider tolerance.
- **Staleness vs reCAPTCHA ~120 s freshness — quantified, no risk.** Lead = 120 s; a solve
  is ~75 s, so ALL prefetched tokens finish at ~T0−45 s (one concurrent `gather`). The
  fallback book fires at ~T0+3.5 s → reserve-token age ≈ **48.5 s**, well under the ~120 s
  validity. (All pooled tokens share that age — the reserve is not fresher, just *present*.)
  And if a reserve token *is* stale, `book()`'s **MF1** pooled-token re-solve (base.py lines
  **650–655**) does exactly one inline re-solve — a bounded safety net, not a wall.
- **0-grid degenerate case:** `min(cap, 0) = 0` → return the single-POST `default`
  (unchanged from today, lines **714/722**); the reserve is added only when there is a real
  burst. The fallback still gets `default` tokens.

### Q4 — `skip_initial_spacing` on the fresh search. **RECOMMEND: YES — inherited automatically.**
The fresh search reuses `_poll_for_slots` (lines **742–786**), which already passes
`skip_initial_spacing=self._prefetch_book` (line **757**). On the race path that is `True`,
so the fresh search skips the 250 ms leading courtesy sleep — correct: it is race-critical,
and it is the **first** `/times` GET of the run (the hedge is gone), so there is nothing to
space from. No new plumbing; the existing `_poll_for_slots` behavior is exactly right. The
watcher never sets `_prefetch_book`, so its etiquette is untouched.

---

## 3. New control flow (annotated)

```
_blind_post_course(adapter, course_id, request):
    capable      = cast(BlindPostCapable, adapter)
    blind_slots  = capable.synthesize_blind_slots(request, target_date,
                                                  max_count=blind_post_max_count)
    n            = min(len(blind_slots), capable.captcha_pool_size())
    fire         = blind_slots[:n]

    # CHANGED: launch ONLY the blind burst. NO concurrent hedge search.
    blind_tasks  = [create_task(adapter.book(s, request)) for s in fire]
    blind_results = await gather(*blind_tasks, return_exceptions=True)
    # partition into booked / SlotGone / uncertain  (UNCHANGED, lines 377–413)

    if booked:
        best, extras = self._keep_best(booked, request)
        await self._cancel_extras(adapter, extras)
        return best                       # CHANGED: no _cancel_task(search_task)

    # 0 BLIND BOOKED.
    match = await self._reguard_before_fallback(adapter, course_id, request)
    if match is not None:
        return ALREADY_BOOKED(match)      # CHANGED: no _cancel_task; double-book gate

    # CHANGED: FRESH search, AFTER the reguard re-auth (settled session, max-fresh snapshot).
    slots = await self._poll_for_slots(adapter, request)   # skip_initial_spacing inherited
    if not slots:                          raise _CourseSkippedError()
    candidates = self._rank_slots(slots, request)
    if not candidates:                     raise _CourseSkippedError()
    return await self._book_from_candidates(adapter, course_id, candidates, request)
```

`_cancel_task` (lines **542–572**) is **deleted** (no remaining caller).

---

## 4. Timing diagrams (T0 offsets)

### Happy path (≥1 blind booked)
```
T0−120s  busy-wait reaches prefetch point
T0−120s  _prewarm_primary: login pre-warm  ||  prefetch min(3,grid)+reserve = 5 tokens
T0−45s   ~5 tokens in FIFO pool (all ~same age — one concurrent gather)
T0       fire 3 blind book POSTs (popleft 3 OLDEST tokens).   *** NO hedge search GET ***
T0+1.1s  gather → ≥1 BOOKED
T0+1.1s  _keep_best (rank) + _cancel_extras (cancel the others by their own id)
T0+1.3s  return BOOKED        (2 reserve tokens unused → discarded at run end)
```
Search GETs: **0**.  vs today: 1 GET fired then cancelled.

### 0-booked path (all SlotGone, nothing landed)
```
T0       fire 3 blind POSTs (popleft 3 oldest).   *** NO hedge ***
T0+1.1s  gather → 0 booked (all SlotGone)
T0+1.1s  reguard: refresh_reservations (reset _logged_in + warm-up GET + login POST ~2s)
                  + list_reservations (login cache)
T0+3.1s  reguard → no match
T0+3.1s  FRESH search via _poll_for_slots (skip_initial_spacing=True) → snapshot @ ~T0+3.1s
T0+3.8s  rank → book first candidate with a RESERVE pooled token (age ~48s, fresh) → BOOKED
T0+4.0s  return BOOKED
```
vs today: the fallback used the **stale ~T0+0.7 s hedge snapshot** and **inline-solved
~75 s** on a dry pool. New: maximally-fresh snapshot + a fresh pooled token, ≈ same wall
time, far higher book success.

### 0-booked-but-landed path (uncertain blind landed)
```
T0+1.1s  gather → 0 booked
T0+1.1s  reguard: refresh_reservations + list_reservations → MATCH (landed reservation)
T0+3.1s  return ALREADY_BOOKED.   *** NO fresh search, NO fallback book ***  (double-book gate)
```

---

## 5. Stub signatures / diff shapes (no bodies — implementation is for follow-up agents via TDD)

### 5.1 `SchedulerConfig` — new field (`core/config.py`, after `blind_post_max_count`, line 194)
```python
# Blind-POST 0-booked fallback (RESEARCH_FALLBACK_PLAN §2 Q3). EXTRA CAPTCHA tokens to
# pre-solve beyond the blind burst so the post-reguard FRESH search's book() pops a fresh
# POOLED token instead of a ~75 s inline solve. FIFO popleft leaves these (the freshest)
# for the late fallback. Burst size is unchanged (synthesize truncates to
# blind_post_max_count). Race-critical depth → parity-checked across the configs. 0 = off.
blind_post_fallback_token_reserve: int = Field(default=2, ge=0)
```

### 5.2 `Orchestrator._captcha_prefetch_count_for` — add the reserve (lines 703–722)
```python
count = min(self._scheduler.blind_post_max_count, len(blind_slots))
if count <= 0:
    return default                                      # 0-grid: single-POST depth, unchanged
return count + self._scheduler.blind_post_fallback_token_reserve   # ADDED reserve
```

### 5.3 `Orchestrator._blind_post_course` — drop the hedge, fresh post-reguard search (lines 343–459)
- DELETE line 369 (`search_task = asyncio.create_task(...)`).
- Update the firing log (line 370–374) to drop "+ hedge search".
- DELETE line 419 (`await self._cancel_task(search_task)`) on the booked branch.
- DELETE line 435 (`await self._cancel_task(search_task)`) on the reguard-match branch.
- REPLACE line 453 (`slots = await search_task`) with
  `slots = await self._poll_for_slots(adapter, request)`.
- Add a one-line comment that this is the FRESH post-reguard search (serialized after the
  re-auth to avoid a shared-client cookie race; see §2 Q2).

### 5.4 `Orchestrator._cancel_task` — DELETE (lines 542–572; no remaining caller)

### 5.5 `FakeAdapter` test scaffolding (`dev/fake_adapter.py`) — additive, for PR3 tests
```python
# In __init__:
self.search_book_counts: list[int] = []   # book_call_count observed at each search() entry
# In search(), first line:
self.search_book_counts.append(self.book_call_count)
```
This lets a test prove the (single) fresh search fired **after** the whole blind burst
(`search_book_counts == [n]`). A response *queue* (`set_search_responses`) is deliberately
**not** required — dropping the hedge removes the "stale vs fresh snapshot" distinction;
there is now exactly one search, and the recorder pins that it is post-burst.

---

## 6. PR-by-PR breakdown (small, independently mergeable, TDD red-first)

### PR1 — `SchedulerConfig.blind_post_fallback_token_reserve` field + config wiring + parity
**No behavior change** (field unused) — safe first.
- **Red tests:**
  - `tests/test_config.py::test_scheduler_default_fallback_token_reserve` —
    `SchedulerConfig().blind_post_fallback_token_reserve == 2`.
  - `tests/test_config.py::test_fallback_token_reserve_rejects_negative` —
    `SchedulerConfig(blind_post_fallback_token_reserve=-1)` raises `ValidationError` (`ge=0`).
  - `tests/test_container_config_parity.py::test_container_and_example_blind_fallback_reserve_match`
    — example.toml == container.toml for the new key (extend the existing
    `test_container_and_example_captcha_prefetch_match`, or a sibling assert).
- **Green:** add the field (§5.1); add `blind_post_fallback_token_reserve = 2` to the
  `[scheduler]` block of `config/example.toml` **and** `config/container.toml` with a
  matching comment.
- **Docs:** config comments; this plan; root `CLAUDE.md` scheduler/blind bullet;
  `BLIND_POST_PLAN.md` cross-ref note.

### PR2 — Prefetch reserve in `_captcha_prefetch_count_for`
Deepens the pool; the fallback book stops inline-solving even on today's hedge path.
Depends on PR1.
- **Red tests (`tests/test_orchestrator_blind_post.py`; add a `reserve` kwarg to `_scheduler`):**
  - UPDATE `test_prefetch_scales_to_blind_fanout` → `…_plus_reserve`: 5 in-window slots,
    `blind_max=12`, `reserve=2` → `fa.last_prepare_count == min(12,5)+2 == 7`.
  - `test_prefetch_reserve_respects_blind_max` — `blind_max=3`, 8-slot grid →
    `min(3,8)+2 == 5`.
  - `test_non_blind_primary_prefetch_uses_fixed_count` — UNCHANGED: non-capable primary
    still `== captcha_prefetch_count` (3); reserve not applied.
  - `test_prefetch_reserve_zero_grid_uses_default` — capable primary, empty grid →
    `last_prepare_count == captcha_prefetch_count` (no reserve added).
  - `test_reserve_does_not_increase_blind_burst` — large pool, 3-slot grid → blind
    `book_call_count == 3` (burst bounded by `len(blind_slots)`, not the inflated pool).
- **Green:** §5.2.
- **Docs:** this plan §2 Q3; `BLIND_POST_PLAN.md` §5 token-budget note.

### PR3 — Drop the hedge + fresh post-reguard search (core control-flow change)
Depends on PR1+PR2 (so the fresh fallback already has a deep pool when it lands).
- **FakeAdapter:** add `search_book_counts` recorder (§5.5).
- **Red tests (`tests/test_orchestrator_blind_post.py`):**
  - `test_happy_path_issues_no_search` — REPLACES
    `test_blind_post_wins_even_if_hedge_search_errors`: ≥1 blind booked →
    `fa.search_call_count == 0` (no hedge GET). Happy-path no-regression + hedge dropped.
  - `test_zero_booked_fires_fresh_search_after_blind_burst` — 2 blind `SlotGone`, search
    returns a bookable slot → `fa.search_book_counts == [2]` (search saw all blind books)
    and result booked from the search slot.
  - `test_fresh_search_runs_after_reguard` — order-recording adapter (extend the existing
    `_OrderAdapter` to also append `"search"`). **Call `_blind_post_course` DIRECTLY, not full
    `orch.run()`** (reviewer should-fix #2): on the race path the pre-T0 `_prewarm_login`
    (orchestrator.py:657) and the T0 layer-2 guard (orchestrator.py:241) each call
    `list_reservations`, so a full run would record `["list","list","refresh","list","search"]`
    and the test would fail for the WRONG reason. Drive `_blind_post_course` directly with
    all-SlotGone blind side-effects — as the existing reguard order tests do (test file:523/551)
    — to get the clean `order == ["refresh", "list", "search"]`.
  - `test_reguard_match_short_circuits_before_fresh_search` — EXTEND
    `test_zero_booked_but_landed_uncertain_reguards_to_already_booked`: also assert
    `fa.search_call_count == 0` (fresh search NEVER fires when the reguard matches).
  - `test_zero_booked_empty_fresh_search_skips_course` — all blind gone, fresh search
    empty → `NO_INVENTORY` (keep `test_blind_post_logs_slot_gone_count`'s assertion).
  - `test_blind_post_all_gone_falls_back_to_search` — KEEP (still green); add
    `assert fa.search_call_count == 1`.
  - DELETE `test_cancel_task_swallows_cancellederror_on_pending_hedge` (`_cancel_task` gone).
  - `test_non_capable_course_issues_exactly_one_search` — non-blind primary →
    `fa.search_call_count == 1` (non-blind no-regression, explicit).
- **Green:** §5.3 + delete `_cancel_task` (§5.4).
- **Docs:** §7 below; root `CLAUDE.md` blind-path bullets (remove the "+ hedge search",
  "search=grid-drift fallback", "abandoned hedge" language; describe the fresh
  post-reguard search); `BLIND_POST_PLAN.md` §6/§11 addendum + diagram;
  `src/teetime/courses/CLAUDE.md` blind-POST bullet (the "real T0 search is the
  correctness fallback" line — now post-reguard, not a concurrent hedge).

---

## 7. Reviewer pre-emption

- **Double-booking:** the fresh search runs **strictly after** `_reguard_before_fallback`
  returns `None` (§3, §4). A landed-but-uncertain blind POST short-circuits `ALREADY_BOOKED`
  before any fallback book. Pinned by `test_reguard_match_short_circuits_before_fresh_search`
  (`search_call_count == 0`).
- **Happy-path no-regression:** ≥1 booked returns immediately and issues **no** search
  (the hedge is gone). Pinned by `test_happy_path_issues_no_search`.
- **Non-blind no-regression:** the `_should_blind_post` gate is untouched; a non-blind
  course takes the existing `_run_course` search path and issues **exactly one** search.
  Pinned by `test_non_capable_course_issues_exactly_one_search`.
- **Token-pool exhaustion:** if reserve tokens run out across multiple dead fallback
  candidates, `book()` inline-solves, bounded by `_captcha_solve_sem` (default 6). The
  fallback book loop is single-threaded (`_book_from_candidates` is sequential), so it
  cannot herd; worst case is serial inline solves, each ≤ ~75 s, far under
  `replicaTimeout = 1200 s`.
- **Prefetch failure is non-fatal:** `count = len + reserve > 1` → `prepare_book` is on the
  `count > 1` never-raise branch (NI10); failures are swallowed and `book()` inline-solves.
  No prefetch miss can crash the run.
- **Search etiquette (PLAN §12):** GET count goes **down** — happy path 0 search GETs,
  0-booked path exactly 1 search GET. `_MIN_BETWEEN_S` spacing is honored exactly as today
  via `_poll_for_slots` (`skip_initial_spacing` only trims the *leading* sleep on the race
  path, as it already does).
- **Test isolation / fresh-vs-stale:** dropping the hedge **removes** the stale-snapshot
  surface — there is one search and it is provably post-burst (`search_book_counts`).
  No response-queue scaffolding is needed (called out as a simplification).
- **Cancellation / `CancelledError`:** deleting the hedge removes both `_cancel_task` call
  sites and the helper. There is **no remaining hedge task** to leak or to swallow a
  `CancelledError` from. The blind `book()` tasks are all `await`ed via `gather(...,
  return_exceptions=True)` (no orphan tasks). `BaseException` partition handling for the
  blind tasks (lines 400–403) is unchanged.
- **Code-default exposure (reviewer nit):** `blind_post_max_count` defaults to **12** in code
  (config.py:194); the deployed TOMLs pin 3. A deployment that OMITS the override would
  prefetch `min(12, grid) + reserve` (~14 concurrent solves) — a pre-existing exposure the
  reserve slightly widens. The PR1 parity test guards example↔container, not the code default.
  Acceptable (every real config sets the value explicitly); noted for awareness, no action.

---

## 8. Open questions for the user (surface, do not decide)
1. **Reserve default = 2.** At a competitive Mangrove Bay drop the fresh snapshot is the
   *latest* one, so the first candidate is usually live and 1 reserve token suffices; 2 is a
   cheap hedge against one dead candidate. 3 would cover two dead candidates at +1 pre-T0
   solve (cost + provider rate). Confirm 2, or prefer 1 or 3?
2. **No live-system change here** — this is code/config only. The deployed
   `blind_post_max_count = 3` is unchanged; with `reserve = 2` the pre-T0 prefetch rises
   from 3 to **5** concurrent 2captcha solves. **Cost attribution (reviewer should-fix #3):
   these extra `reserve` solves are paid pre-T0 UNCONDITIONALLY — on EVERY drop, including the
   common HAPPY path where a blind POST wins and the reserve tokens go unused (discarded at run
   end).** So the steady-state cost is ~`reserve` wasted solves per drop (≈ $0.006/drop at
   reserve=2), not an occasional fallback-only cost. Confirm that is acceptable against your
   2captcha plan's concurrency/rate limits (the old `blind_post_max_count = 12` default already
   implied up to 12 concurrent, so this is well within budget; verified live — the 2026-06-28
   Sunday drop fired 11 concurrent solves cleanly).
