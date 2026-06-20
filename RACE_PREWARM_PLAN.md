# RACE_PREWARM_PLAN.md — Post-T0 latency reduction for the 06:00 ForeUP booking race

Status: APPROVED (plan-with-review, 2 rounds, BLOCK→APPROVE). Implementation in progress,
strict red-green TDD. **PR1 (login pre-warm + pre-T0 reservation guard + short-circuit) is
IMPLEMENTED** (`_prewarm_primary`/`_prewarm_login`/`_prefetch_captcha_for`,
`_prewarmed_course_ids`, the SF6 short-circuit, ForeUP `_logged_in` guard, FakeAdapter
`set_authenticate_side_effects`). PR2 (multi-token pool, **default count=3** per user) + PR3
(race-only search-sleep trim) pending. Refines the race path in PLAN.md §9 and the root
CLAUDE.md "booking race" invariants.

## Round-2 reviewer disposition

The round-1 reviewer issued VERDICT: BLOCK. Per-item disposition (full reasoning in the cited
sections):

| Item | Disposition | Where |
|------|-------------|-------|
| MF1 stale pooled token → hard abort | **fixed** — option (a): a CaptchaError consuming a *pooled* token triggers ONE inline re-solve+retry of the SAME candidate before giving up | §4.3, §8.3 stub |
| MF2 outer-gather error isolation under-specified | **fixed** — both conditions now mandated + tested | §3.5, §8.2 stub |
| MF3 unconditional authenticate() / FakeAdapter no guard | **fixed** — orchestrator tracks pre-warmed course_ids and skips the post-T0 authenticate; test asserts orchestrator behavior, not adapter guard | §3.1, §3.6, §7.1, §8.2/§8.4 stubs |
| SF4 multi-course fallback not accelerated | **fixed** — documented explicitly | §4.7 |
| SF5 Change D breaks watcher inter-GET spacing | **fixed** — leading-sleep trim is opt-in via a `skip_initial_spacing` flag the ORCHESTRATOR sets only on the race path; watcher path unchanged; test reflects the real one-date-per-call pattern | §5, §7.3, §8.3 stub |
| SF6 pre-T0 short-circuit skips T0 verification log | **fixed** — emits an equivalent short-circuit verification line | §3.2 |
| NI9 run() call-site rewire + prefetch-test regressions | **fixed** — PR1 rewire specified; regressing tests enumerated with the exact update | §6.PR1, §7.4 |
| NI10 count=1 total-failure must re-raise | **fixed** — N-dependent raise contract specified + tested | §4.3, §8.1 stub |
| NIT7 parity test asserts both fields | **fixed** — assert count AND lead; note lead absent from container/local today | §4.4, §7.2 |
| NIT8 dev --wait token cost | **fixed (note)** — dev ACA jobs do NOT pass --wait; documented | §4.6 |
| OQ1 default N | **resolved → N=5** (recommended; covers the full 2026 5-attempt spread) | §4.4, §9 |
| OQ2 pre-T0 ALREADY_BOOKED short-circuit | **resolved → SHIP IT** (recommended) | §3.2, §9 |

## 0. Problem

At the 06:00:00 ET drop the prime slot is claimed in the first few seconds. Our post-T0
critical path today is:

```
T0 → authenticate (warm GET + login POST ~2s) → list_reservations (cache read, ~0s)
   → search GET (~6s, the unavoidable floor — today+7 inventory is not published until T0)
   → rank → book POST (token already pre-fetched ~0s, OR inline solve 6–90s on fallbacks)
```

Two avoidable costs sit on the post-T0 path:

1. **`authenticate()` (~2s)** runs at T0 inside `_run_course` even though it is T0-independent
   and idempotent (the warm-up GET + login POST do not depend on today+7 inventory).
2. **Fallback CAPTCHA solves.** Only the FIRST book() consumes the single pre-fetched token;
   every fallback candidate re-solves inline (2captcha polls every 5s, 6–90s each). In the
   2026 prod drop, 5 fallback attempts spread over ~82s and every one hit `400 Time not
   available` because each was minutes late. (Documented caveat in CLAUDE.md.)

A third, trivial cost: a 250ms courtesy `sleep` precedes the FIRST post-T0 search GET, with
nothing to space from.

### What we CANNOT fix (set expectations — change B, context only)

`search()` (~6s ForeUP round-trip) is the real floor and CANNOT be pre-warmed: the today+7
teesheet is not published until T0. This plan does NOT attempt to pre-warm search. After all
three changes the post-T0 path is essentially `search (~6s) → book POST (~0s)` for the first
candidate, and fallbacks fire near-instantly (pooled tokens) instead of serially re-solving.
**Expected win: the first book POST moves from ~T0+8s to ~T0+6s, and fallback POSTs from a
6–90s-each serial tail to a near-instant burst.** The biggest practical gain is the fallback
burst — the 2026 failure mode.

## 1. The three changes

| # | Change | Where | PR |
|---|--------|-------|----|
| A | Pre-warm login (warm GET + login POST) + run the layer-2 "0 reservations" guard pre-T0, first-preference adapter only | `core/orchestrator.py`, `courses/foreup/base.py` | PR1 |
| C | Multi-token concurrent CAPTCHA prefetch into a pooled queue; book() pops, falls back to inline solve when empty | `core/adapter.py` (Protocol), `courses/foreup/base.py`, `core/orchestrator.py`, `core/config.py`, configs | PR2 |
| D | Drop the leading 250ms courtesy sleep before the FIRST post-T0 search GET; keep it between subsequent requests | `courses/foreup/base.py` | PR3 |

Sequence: **PR1 → PR2 → PR3.** PR1 establishes the pre-T0 prewarm hook the orchestrator owns;
PR2 extends the same hook with the token pool (depends on the prewarm sequencing landing
first); PR3 is an independent 1-line trim that can land any time but is sequenced last to keep
the diff stack clean.

## 2. The exact pre-T0 sequence (after all three PRs)

On the `--wait` race path (`prefetch_book=True`), inside `Orchestrator.run()`:

```
compute t0_target
prefetch_at = t0_target - captcha_prefetch_lead_s
if now >= prefetch_at:  WARN "prefetch lead not fully honored" (existing degraded path)
else: busy_wait_until(prefetch_at)
# NEW: pre-warm — concurrent, error-isolated, first-preference adapter ONLY
await _prewarm_primary(request)        # does login prewarm + N-token solve concurrently
    └─ asyncio.gather(
           _prewarm_login(adapter, creds),         # returns ExistingReservation match or None
           adapter.prepare_book(None, request, count=N),   # solves N tokens concurrently
           return_exceptions=True )                 # one failing must NOT cancel the other
    └─ if login prewarm found an existing matching reservation → stash ALREADY_BOOKED short-circuit
# If a matching reservation was found pre-T0 (§3.2):
#   log "race: short-circuited pre-T0, already booked ..." (SF6 verification line),
#   record terminal ALREADY_BOOKED, notify, RETURN — do NOT busy-wait to T0.
busy_wait_until(t0_target)
log "race: busy-wait complete ..."   (unchanged verification surface)
# T0 reached:
for course in preferences:
    _run_course(..., prewarmed_course_ids={primary_id} if prewarm succeeded else set())
                       # for the PRIMARY adapter, the orchestrator SKIPS authenticate()
                       # (it tracks the pre-warmed course_id — MF3), so we never rely on an
                       # adapter-side idempotency guard. search → rank → book (book pops tokens).
```

`_run_course` gains one parameter, `prewarmed_course_ids: frozenset[CourseId]` (MF3). For a
course in that set the orchestrator does NOT call `authenticate()` again — the session was
established pre-T0. For every other course (fallbacks, or the primary when prewarm failed)
`authenticate()` runs inline at T0 exactly as today. This makes correctness independent of
whether an adapter implements an idempotency guard (FakeAdapter and TeeItUp do not; the ForeUP
guard is still added per §3.1 as a defensive belt-and-suspenders, but the test asserts the
ORCHESTRATOR skip, not the adapter guard).

## 3. Change A — login pre-warm + pre-T0 reservation guard (PR1)

### 3.1 Design (MF3 — orchestrator owns the skip, not the adapter)

- New orchestrator method `_prewarm_login(adapter, course_id, request) -> ExistingReservation | None`.
  Best-effort. Calls `adapter.authenticate(creds)` (the same call `_run_course` makes), then
  `adapter.list_reservations()` and `_first_matching_reservation(...)`. Returns the match (or
  None). Any exception is logged + swallowed, returns None (degraded → `_run_course` will
  authenticate inline at T0 as today). **On success it adds `course_id` to a
  `self._prewarmed_course_ids: set[CourseId]` the orchestrator owns.**
- **The post-T0 skip is the ORCHESTRATOR's responsibility (MF3 fix).** `run()` passes
  `prewarmed_course_ids=frozenset(self._prewarmed_course_ids)` into `_run_course`. Inside
  `_run_course`:

  ```python
  if creds is not None and course_id not in prewarmed_course_ids:
      await adapter.authenticate(creds)
  ```

  So for the pre-warmed primary we skip the second `authenticate()` entirely. This does NOT
  depend on any adapter implementing an idempotency guard — which is exactly the round-1 bug:
  `FakeAdapter.authenticate()` has no guard and increments `authenticate_call_count` on every
  call, so a test asserting `count == 1` via an *adapter-side* guard would have failed and
  misled. The orchestrator-skip mechanism makes `count == 1` true by construction for ANY
  adapter, including FakeAdapter, with no adapter change required.
- **Why only on prewarm SUCCESS:** `_prewarm_login` only adds the course_id to the set if
  `authenticate()` returned without raising. A prewarm login failure (bad creds / network /
  captcha) leaves the set empty for that course, so `_run_course` authenticates inline at T0 —
  today's degraded behavior, a second real attempt before book().
- **Defensive adapter guard (kept, but NOT load-bearing for the test).** `ForeUpAdapter.
  authenticate()` STILL gets the `if self._logged_in: return` guard — it is correct hygiene
  (a real ForeUP re-login is wasteful) and matches the Protocol's documented idempotency. But
  the PR1 orchestrator test asserts the SKIP via `prewarmed_course_ids` plumbing
  (`fa.authenticate_call_count == 1`), which now passes for FakeAdapter precisely because the
  orchestrator does not call authenticate a second time. The ForeUP adapter-guard test
  (`test_authenticate_is_idempotent_*`) is a separate ForeUP-level unit test (§7.1) and does
  not gate the orchestrator behavior.
  - Guard keys ONLY on `_logged_in`. A soft login failure (400/401 or rejected body) leaves
    `_logged_in=False`, so a later inline authenticate() correctly retries the full login.

### 3.2 Pre-T0 reservation match short-circuit (reviewer pre-empt #6)

If `_prewarm_login` finds a matching existing reservation pre-T0, the orchestrator records the
ALREADY_BOOKED terminal, notifies, and returns **without busy-waiting to T0 or searching**.
Rationale: the booking already exists; there is nothing to race for, and waiting to T0 just to
re-discover it wastes the replica. This is a behavior addition — gated to the prefetch path
only (the watcher/local-demo never prewarm). Staleness is acceptable per the documented
login-cache invariant (a manual booking made during the run window is the only thing this could
miss, and the post-T0 `_run_course` guard would still catch it on the same data anyway).
**OQ2 resolution: SHIP IT** (recommended — the wasted-busy-wait saving is real and the staleness
gap is already covered by the post-T0 guard reading the same cache).

**SF6 — verification log on the short-circuit path.** The normal race emits
`race: busy-wait complete; firing at ...` after the busy-wait — the ONLY prod evidence (under
dry-run) that the replica fired at T0 (AZURE_PLAN §10). The pre-T0 short-circuit returns BEFORE
that line, so it would emit no verification surface at all and the operator could not tell a
correctly-short-circuited run from a dead replica. FIX: on the short-circuit path emit an
equivalent INFO line BEFORE returning:

```
log "race: short-circuited pre-T0 — matching reservation already booked (conf=%s); skipping busy-wait and search"
```

This is the short-circuit's verification surface. PR1 test `test_run_short_circuits_already_booked_found_pre_t0`
asserts this line is present (and that `race: busy-wait complete` is ABSENT, proving we did not
wait to T0).

### 3.3 Best-effort contract (reviewer pre-empt #5)

`_prewarm_login` and the prefetch are both best-effort: every failure is logged at WARNING and
swallowed. The post-T0 path authenticates/solves inline. A prewarm hiccup must NEVER cost the
booking.

### 3.4 Composition with the "started late" degraded path (reviewer pre-empt #7)

The existing `now >= prefetch_at` WARNING branch already prefetches immediately. PR1 places the
`_prewarm_primary` call AFTER that branch resolves (the same single call site), so the
late-landing path also pre-warms login + solves tokens immediately with whatever runway remains.
No new branch.

### 3.5 Outer-gather error isolation (MF2 — BOTH conditions required, not either/or)

`_prewarm_primary` runs TWO gathers:

- INNER gather: the N concurrent CAPTCHA solves inside the adapter prepare-book
  method run with `return_exceptions=True`. Fine as-is.
- OUTER gather: inside the prewarm-primary helper, the login prewarm and the
  adapter prepare-book call (with count set to N) run concurrently.

The danger the reviewer flagged: a default `asyncio.gather` (no `return_exceptions`) cancels all
sibling coroutines the instant one raises. If a future refactor lets `_prewarm_login` raise (e.g.
removes its internal try/except), a default outer gather would **cancel the in-flight CAPTCHA
solves** — silently destroying the token pool the race depends on. The reverse (a `prepare_book`
that escapes its own swallow) would cancel the login prewarm.

**TWO conditions are MANDATORY — both, not either:**

1. **`_prewarm_login` MUST NOT raise.** It catches every exception internally, logs at WARNING,
   and returns `None`. (Plus: it must not add its course_id to `_prewarmed_course_ids` on
   failure — see §3.1.) This is the primary contract; the stub docstring states it.
2. **The outer gather MUST pass `return_exceptions=True`** as defense-in-depth, so that even if
   condition (1) is violated by a future refactor, a `_prewarm_login` exception does NOT cancel
   the concurrent token solve (and vice-versa). The orchestrator then inspects the gathered
   results: a returned Exception from either leg is logged + swallowed (best-effort, §3.3).

`_prewarm_primary` **awaits BOTH legs to completion before busy-waiting to T0** — it does not
fire-and-forget. The token pool and the reservation-match result must both be settled before the
post-T0 critical path begins. Tests (§7.1): `test_prewarm_outer_gather_isolates_login_failure`
(login leg raises → tokens still solved, pool non-empty, run proceeds) and
`test_prewarm_outer_gather_isolates_prefetch_failure` (prefetch leg raises → login prewarm still
records the match / short-circuits).

### 3.6 MF3 mechanism summary

`Orchestrator` gains `self._prewarmed_course_ids: set[CourseId]` (init empty). `_prewarm_login`
adds the course_id on auth success. `run()` passes `frozenset(self._prewarmed_course_ids)` to
`_run_course`, which skips `authenticate()` for any course in the set. No adapter change is
required for correctness; the ForeUP `_logged_in` guard is kept only as hygiene.

## 4. Change C — multi-token concurrent CAPTCHA prefetch (PR2)

### 4.1 Protocol contract change (reviewer pre-empt #3)

`prepare_book` gains a keyword-only `count: int = 1`:

```python
async def prepare_book(
    self,
    slot: TeeTimeSlot | None,
    request: BookingRequest,
    *,
    count: int = 1,
) -> None: ...
```

- Default `count=1` preserves the UpgradeOrchestrator caller (it needs exactly one token before
  cancel — it passes no `count`, gets 1). No upgrade-path change.
- The race-path orchestrator passes `count=N` (`scheduler.captcha_prefetch_count`).
- FakeAdapter + TeeItUpAdapter remain no-ops (they ignore `count`; signature updated for
  Protocol parity). Future Chronogolf likewise.

We choose a `count` PARAM over a new method to keep the Protocol surface minimal and because
the single-token and multi-token cases are the same operation at different N. The upgrade path
is untouched by construction.

### 4.2 Token pool semantics in ForeUpAdapter (reviewer pre-empt #1, #2)

Replace the single `self._captcha_token: str | None` with a pool:

```python
self._captcha_tokens: collections.deque[str]   # FIFO; oldest solved first
```

- `prepare_book(slot, request, *, count)`: if no provider → no-op. Else solve `count` tokens
  **CONCURRENTLY** via `asyncio.gather(*[provider() for _ in range(count)], return_exceptions=True)`.
  Append each successful token to `self._captcha_tokens`. Log how many of `count` succeeded.
  A `TimeoutError` from any solve is swallowed per-token (that token just doesn't join the pool);
  the method does NOT raise if at least... — see 4.3 for the raise contract.
- `book()`: if a provider is configured:
  - `if self._captcha_tokens: token = self._captcha_tokens.popleft()` (consume oldest first,
    so the freshest tokens are saved for the latest-firing fallbacks — minimizes age-at-use).
  - else inline solve (today's behavior), wrapping `TimeoutError → CaptchaError`.

**Concurrent solve is mandatory (reviewer pre-empt #1, #8):** N solves started together at
`T0 − lead` all finish within one solve-time of each other (bounded by the slowest), so every
pooled token's age at T0 ≤ lead ≤ ~120s reCAPTCHA validity. Sequential solving would age the
first-solved token by `(N−1) × solve_time` and is forbidden.

**FIFO pop = oldest-first (reviewer pre-empt #1):** the first book() (fires at ~T0+6s) takes the
oldest token; the Nth fallback (fires later) takes the freshest. This is the correct ordering to
keep late-firing fallbacks within the freshness window.

### 4.3 Stale/used token at pop → book() must still work (MF1 — highest-risk fix)

A pooled token may be stale by the time a slow fallback pops it (fallbacks dragged past ~120s
after solve). When ForeUP rejects a stale/invalid token it returns either:

- **a captcha-challenge `400`** → `_guard_captcha` raises `CaptchaError`. **THIS IS THE MF1
  REGRESSION the round-1 plan introduced.** In today's code a late fallback inline-solves a FRESH
  token and can still book; with a pool, a *stale popped token* hits the captcha-challenge 400,
  `_guard_captcha` raises `CaptchaError`, which is NOT caught by `_run_course`'s
  `except SlotGoneError` loop, so the whole run aborts non-zero. So change C would REGRESS the
  exact fallback failure mode it claims to fix.

  **FIX (reviewer option (a) — adopted): on a CaptchaError while consuming a POOLED token,
  perform ONE inline re-solve + retry of the SAME candidate before giving up.** Precisely, in
  `ForeUpAdapter.book()`:

  1. If the token used for the POST came from the POOL (popleft), and the POST is classified as a
     captcha-challenge by `_guard_captcha`, then instead of raising immediately: log a WARNING
     ("pooled CAPTCHA token rejected as stale — re-solving inline and retrying once"), solve ONE
     fresh token inline (wrapping `TimeoutError → CaptchaError`), set `body["captchaid"]` to it,
     and re-POST the SAME slot ONCE.
  2. Classify the SECOND response normally: 409/400-captcha → `CaptchaError` (now it really is a
     persistent captcha wall — abort, our solver is systematically failing); 400 Time-not-available
     → `SlotGoneError` (slot genuinely gone, candidate loop advances); 2xx → BOOKED.
  3. If the token came from an INLINE solve (pool was empty), there is NO second retry — a
     captcha-challenge on a freshly-solved token is a real captcha wall, exactly as today.

  This restores today's recoverable behavior: a stale pooled token degrades to one inline
  re-solve of that candidate (the freshest possible token), not a hard abort. The inline re-solve
  is single (no loop) so a true captcha wall still terminates promptly.

  **Why not option (c) (pop-time age check)?** Option (a) subsumes it: a stale token is detected
  by ForeUP's own rejection and recovered by the inline re-solve, with no need to track wall-clock
  token age (which the adapter does not currently record and which would add a clock dependency to
  the adapter). We note (c) as an unnecessary complication given (a). **Why not (b) (tag pooled
  CaptchaErrors as try-next-candidate)?** Advancing to the next candidate would burn another
  *equally stale* pooled token and is strictly worse than re-solving fresh for the current
  candidate; (a) re-solves once and only advances on a genuine SlotGone.

  Implementation note (single-attempt rule): the re-POST here is NOT a violation of "book()'s POST
  is single-attempt / never retried on the §9 UNCERTAIN path." A captcha-challenge 400 is an
  *unambiguous 4xx rejection* — ForeUP created NO reservation — so re-POSTing the same slot with a
  fresh token cannot double-book. This is the same safety basis as the existing 400→SlotGone
  try-next-candidate fallback. Only timeout/5xx (the UNCERTAIN case) remains single-attempt.

- **a generic `400 Time not available`** (slot claimed) → `SlotGoneError` → candidate loop tries
  the NEXT candidate, which pops the NEXT pooled token. Correct: the token was accepted, the slot
  was gone — a SLOT problem, not a token problem.

- **pool exhausted** (more fallback candidates than N tokens) → inline solve (the slow path), same
  as today past the first candidate. N is sized so this is rare (see 4.5).

### 4.3.1 `prepare_book` raise contract (NI10 — N-dependent, made explicit)

The contract is **N-dependent**, and both halves are mandatory:

- **`count == 1` AND the (single) solve fails (raises / times out) → `prepare_book` RE-RAISES.**
  This preserves the UpgradeOrchestrator contract: `maybe_upgrade` catches the exception and
  ABORTS the upgrade (`upgrade_orchestrator.py:382`), leaving the original booking untouched.
  Proceeding with an empty pool would defeat prepare_book's entire purpose (the cancel-to-book
  no-booking window) — it would cancel, then block ~60s on an inline solve inside the window. So
  total failure at count=1 MUST surface. (This is today's behavior: the single-token
  `prepare_book` wraps `TimeoutError → CaptchaError` and lets it propagate.)
- **`count > 1` AND SOME tokens succeed (K ≥ 1 of N) → does NOT raise.** A partial pool is a win;
  logs `solved K/N`. The race path's first K candidates get pooled tokens; the rest inline-solve.
- **`count > 1` AND ALL N solves fail (K == 0) → does NOT raise either.** On the race path the
  orchestrator's best-effort wrapper swallows everything anyway (§3.3), and book() inline-solves
  per candidate. Raising here would buy nothing (the wrapper eats it) and risks coupling the race
  path to the count-1 upgrade semantics. The distinction that matters is purely `count == 1`
  (upgrade, must raise) vs `count > 1` (race, never raises). Tests: `test_prepare_book_count_one_total_failure_raises`
  (count=1 + provider raises → `prepare_book` raises; upgrade-abort parity) and
  `test_prepare_book_count_n_total_failure_does_not_raise` (count=3 + all raise → no raise, empty
  pool). The Protocol docstring (adapter.py) is updated to state this N-dependent contract
  explicitly so a future adapter author cannot get it wrong.

### 4.4 New config field + parity (reviewer pre-empt #8)

Add to `SchedulerConfig`:

```python
captcha_prefetch_count: int = Field(default=5, ge=1)
```

- **OQ1 resolution: default N=5** (recommended, up from the round-1 proposal of 3). The 2026
  failure spread 5 fallback attempts over ~82s; N=5 pre-solves a token for the first candidate
  AND all 4 fallbacks, so the WHOLE spread fires near-instantly from the pool instead of
  re-solving. Cost is negligible (§4.6) and 5 concurrent solves fit the SAME 120s lead as 1
  (§4.5). The configs already on disk say `3`; **PR2 bumps all three TOMLs to 5** to match the
  new default.
- **Parity (NIT7):** the scheduler block is baked into the TOMLs, NOT wired as env vars in
  compute.bicep (confirmed: `compute.bicep` exposes no scheduler env vars). `captcha_prefetch_lead_s`
  lives only in `example.toml` today — **container.toml and local.toml do NOT carry
  `captcha_prefetch_lead_s` and rely on the code default of 120** (verified on disk). So:
  - Parity means: `captcha_prefetch_count` present (and equal, = 5) in all three TOMLs.
  - The new parity test asserts BOTH fields agree where present: it asserts
    `captcha_prefetch_count` is equal across example/container/local, AND asserts the lead is
    consistent (either present-and-equal or absent-and-relying-on-the-120 default). To keep this
    honest, PR2 SHOULD also add `captcha_prefetch_lead_s = 120` explicitly to container.toml and
    local.toml so the prod-facing configs do not silently depend on a code default for a
    race-critical timing value. (NEW TEST, see §7.2.)
- Validation: `captcha_prefetch_count >= 1` via `Field(ge=1)` (already on disk).

### 4.5 Lead-time sufficiency for N tokens (reviewer pre-empt #8)

2captcha solve is 15–120s; concurrent N solves are bounded by the SLOWEST, not the sum. The
current `captcha_prefetch_lead_s = 120` already equals the provider's max poll budget (24 polls
× 5s). Therefore N concurrent solves fit in the SAME 120s lead as 1 — **`captcha_prefetch_lead_s`
needs NO change** for N ≤ a handful. Login prewarm (~2s) runs concurrently with the solves
(`asyncio.gather`), so it adds nothing to the lead. We keep `captcha_prefetch_lead_s = 120`.

Default N=5 (configurable; OQ1 resolution). N up to ~5 is safe on the 120s lead. Do NOT set N
so high that 2captcha rate-limits concurrent submissions (out of scope; 5 is within free-tier
norms — see §10 confidence note on concurrent-submission tolerance).

### 4.7 Multi-course fallback is NOT accelerated (SF4 — stated explicitly)

The `_captcha_tokens` deque lives on the PRIMARY adapter INSTANCE. `_prewarm_primary` warms only
the FIRST course preference. If the primary course exhausts all its ranked candidates and the
orchestrator falls through to course #2, **course #2's adapter has an EMPTY deque**: it
inline-authenticates at T0 (it is not in `_prewarmed_course_ids`) and inline-solves its CAPTCHA
in `book()`, exactly as today. There is NO shared token pool across adapters, and no cross-course
prewarm. This is intentional — the primary is where the race is won; a fall-through to a second
course already means the primary's whole inventory was claimed, at which point the few extra
seconds of inline auth/solve on the (lower-preference) backup course are immaterial. Nobody
should assume the pool is shared. A future enhancement could prewarm all preferences
concurrently, but it is out of scope here (it would multiply token cost by the course count for a
path that rarely fires).

### 4.6 Cost (reviewer pre-empt #9 + NIT8)

N tokens = N × ~$0.003 per run. At N=5, ~$0.015/run; weekly Sat+Sun = ~$0.030/week ≈ $1.56/yr.
Negligible. Note: tokens NOT consumed by book() are simply discarded (no refund) — the cost is
paid whether or not fallbacks fire. Acceptable.

**NIT8 — dev `--wait` token cost.** Under `--dry-run true` the orchestrator returns `DRY_RUN`
before `book()`, so a dry-run `--wait` race would pre-solve N tokens and book nothing — N wasted
solves per run. This is NOT a concern in practice because **the dev ACA jobs do NOT pass
`--wait`**: only the prod `--wait` booking job sets `prefetch_book=True`, and prewarm/prefetch is
gated entirely on `prefetch_book` (§2). Dev runs are `--no-wait` (immediate, no prefetch) or the
watcher (never prefetches). The only way to burn dev tokens is an operator manually running
`teetime run --wait --dry-run true`, which is a deliberate verification action, not a scheduled
cost. PR2 docs (root CLAUDE.md race bullet) note this so nobody adds `--wait` to a dev job
expecting free dry-runs.

## 5. Change D — drop the leading courtesy sleep, RACE PATH ONLY (PR3) — SF5 fix

### 5.1 The round-1 bug (SF5)

The round-1 plan said: in `search()`, move the leading `await asyncio.sleep(_MIN_BETWEEN_S)` to
BETWEEN per-date iterations so "the FIRST GET pays 0ms; the 2nd+ GET keeps the 250ms (watch
etiquette)." **This is wrong, because of how the watcher actually calls search().**

The watcher does NOT pass multiple `target_dates` to one `search()` call. `WatchOrchestrator.
_check_course` (watch_orchestrator.py:350) does
`scoped = dc_replace(request, target_dates=(target_date,))` and calls `adapter.search(scoped)`
with EXACTLY ONE date. `__main__._watch` (line 465) then loops `check_once` over the wanted dates
with **no sleep between calls**. So:

- The watcher's `search()` per-date loop ALWAYS has exactly one iteration.
- The ONLY 250ms spacing between the watcher's back-to-back date-check GETs (Saturday-target GET,
  then Sunday-target GET) is the leading `sleep` at the top of `search()`.
- Removing that leading sleep "because it's the first iteration" therefore removes ALL inter-GET
  anti-bot spacing from the watcher — every watch run would fire its 2 (or more) date GETs with
  zero spacing. That is an etiquette regression, not a no-op. The round-1 §7.3 test (two
  `target_dates` in one `search()` call) does NOT reflect production and would have hidden this.

### 5.2 The fix — make the trim OPT-IN and RACE-PATH-OWNED

Add a keyword-only `skip_initial_spacing: bool = False` parameter to `CourseAdapter.search()` (and
ForeUP/TeeItUp/Fake for parity). Default `False` = today's behavior (leading sleep present). Only
the main booking `Orchestrator`, on the race path (`prefetch_book=True`), passes
`skip_initial_spacing=True` — and only for the FIRST search call after T0, where the burst leads
with this GET and there is genuinely nothing to space from. The watcher NEVER passes it, so its
leading sleep stays and its inter-date-check spacing is preserved.

`ForeUpAdapter.search()` implementation: skip the `sleep` ONLY on the first iteration AND ONLY
when `skip_initial_spacing` is True; sleep before the 2nd+ iteration unconditionally; sleep before
the 1st iteration unless `skip_initial_spacing`. (The race path has one date so it pays 0ms; a
multi-date search with the flag set still spaces 2nd+ GETs.)

**Where the orchestrator sets it:** `_poll_for_slots` is the search caller. It threads
`skip_initial_spacing=self._prefetch_book` (the race-path signal) into `adapter.search(request,
skip_initial_spacing=...)`. Off the race path (watcher uses its own search call; local-demo /
`--no-wait` have `prefetch_book=False`) the flag is False. So the leading-sleep trim happens
ONLY on the prod race path, exactly where it is free, and the watcher's etiquette is untouched.

NOTE: do NOT touch the `cancel_reservation` courtesy sleep — cancel is not on the race path.

### 5.3 Alternatives considered

- *"Sleep between check_once calls in the watcher instead"* — adds spacing the watcher relies on
  the leading search-sleep for; would also need a clock injection in `_watch`. The opt-in flag is
  smaller and keeps the etiquette where it already lives.
- *"Orchestrator owns the leading-sleep trim entirely (don't sleep in search at all)"* — would
  require the orchestrator to space multi-date searches itself; the watcher/booker both rely on
  search() owning intra-call spacing. The flag is the minimal cut.

## 6. PR breakdown

### PR1 — login pre-warm + pre-T0 reservation guard
Files: `core/orchestrator.py`, `courses/foreup/base.py` (authenticate idempotency guard),
`dev/fake_adapter.py` (`set_authenticate_side_effects` — already on disk),
`tests/test_orchestrator.py`, `tests/test_foreup_adapter.py` (or wherever authenticate is tested).
Docs: root `CLAUDE.md` (race-path invariant bullet), `PLAN.md` §9, `RACE_PREWARM_PLAN.md` status.

**NI9 — the call-site rewire is load-bearing and MUST be in PR1.** Today `run()` (orchestrator.py
line ~108) calls `await self._prefetch_captcha(request)` on the race path. PR1 REWIRES this to
`match = await self._prewarm_primary(request)` and then, if `match is not None`, takes the SF6
short-circuit (log + record ALREADY_BOOKED + notify + return) BEFORE the `busy_wait_until(t0_target)`.
PR1 introduces `_prewarm_primary` as a concurrent gather of `_prewarm_login` + the EXISTING
single-token `_prefetch_captcha` (count stays 1 in PR1; PR2 changes it to count=N). `run()` also
constructs `prewarmed_course_ids` and threads it into `_run_course`. This rewire is what the new
PR1 orchestrator tests exercise; without it the stubs are dead code.

**NI9 audit — existing prefetch tests that PR1 changes** (test_orchestrator.py ~498–660). PR1
adds a pre-T0 `authenticate()` + `list_reservations()` to the race path, which the old prefetch
tests did not anticipate:

| Existing test | Still passes? | PR1 action |
|---------------|---------------|------------|
| `test_run_prefetches_captcha_before_t0_when_enabled` | prepare_book_call_count==1 stays 1, book==1 OK; BUT its `_recording_prepare(slot, request)` wrapper has NO `count` kwarg | **Update the wrapper signature to `_recording_prepare(slot, request, *, count=1)`** so the count-passing call site (PR2) and the keyword-call still bind. Add `assert fa.authenticate_call_count == 1` (now a pre-T0 auth happens). The prefetch-timing assertion is unchanged. |
| `test_run_does_not_prefetch_when_disabled` | passes (prefetch_book=False → no prewarm) | none; ADD `assert fa.authenticate_call_count == 1` and that it happened at/after T0 to pin the disabled path. |
| `test_run_prefetch_failure_does_not_abort_race` | passes (best-effort swallow unchanged) | none functionally; the prewarm wrapper still swallows. Confirm `set_prepare_book_to_raise` path still BOOKED. |
| `test_run_warns_when_prefetch_lead_cannot_be_honored` | passes (warning branch unchanged; prewarm runs after it) | none; the WARNING still fires, prewarm still runs immediately. |
| `test_run_does_not_warn_when_prefetch_lead_is_honored` | passes | none. |
| `test_run_logs_race_complete_at_t0` | passes (prefetch_book defaults False → no prewarm, no short-circuit, busy-wait+log unchanged) | none. |
| `test_run_persists_authenticate_call` (count==1) | prefetch_book=False so no prewarm → still exactly 1 inline auth | none. |
| `test_run_short_circuits_when_existing_reservation_matches` (post-T0 guard) | prefetch_book=False → no pre-T0 short-circuit; the POST-T0 `_run_course` guard still fires | none — this stays the non-race ALREADY_BOOKED path; the NEW pre-T0 short-circuit test is race-path only. |

The only mechanical edit to an existing test is the `_recording_prepare` signature; the rest get
ADD-ONLY assertions or are untouched. PR1's new tests (§7.1) cover the prewarm + short-circuit +
skip behavior.

### PR2 — multi-token concurrent prefetch pool
Files: `core/adapter.py` (Protocol `prepare_book` `count` param), `courses/foreup/base.py`
(token deque + concurrent solve + FIFO pop), `core/orchestrator.py` (pass `count=N`),
`core/config.py` (`captcha_prefetch_count`), `config/example.toml`, `config/container.toml`,
`config/local.toml`, `dev/fake_adapter.py` (signature parity), `courses/teeitup/base.py`
(signature parity), `tests/test_orchestrator.py`, `tests/test_foreup_adapter.py`,
new `tests/test_captcha_pool.py`, new parity assertion in `tests/test_container_config_parity.py`.
Docs: root `CLAUDE.md`, `courses/CLAUDE.md` if the prepare_book contract note lives there,
`PLAN.md` §9, `RACE_PREWARM_PLAN.md`.

### PR3 — drop leading search courtesy sleep, RACE PATH ONLY (SF5)
Files: `core/adapter.py` (Protocol `search` gains `*, skip_initial_spacing: bool = False`),
`courses/foreup/base.py` (honor the flag), `courses/teeitup/base.py` + `dev/fake_adapter.py`
(signature parity), `core/orchestrator.py` (`_poll_for_slots` passes
`skip_initial_spacing=self._prefetch_book`), `tests/test_foreup_adapter.py` (or
`tests/test_foreup_search.py`), `tests/test_orchestrator.py` (assert the flag is threaded only on
the race path).
Docs: root `CLAUDE.md` (the anti-bot etiquette bullet — clarify the leading sleep is dropped ONLY
on the race path via `skip_initial_spacing`; the watcher's per-date-check spacing is preserved).

## 7. TDD test list (failing tests FIRST, per PR)

### 7.1 PR1 tests (red first)
In `tests/test_orchestrator.py`:
- `test_run_prewarms_login_before_t0_when_prefetch_enabled` — prefetch_book=True; assert
  `fa.authenticate_call_count == 1` AND the authenticate happened BEFORE T0 (record the clock
  instant). **MF3: this asserts the ORCHESTRATOR skipped the post-T0 authenticate via
  `prewarmed_course_ids` — it is NOT testing a FakeAdapter idempotency guard (FakeAdapter has
  none; it increments on every call). The count is 1 because the orchestrator only called
  authenticate ONCE (pre-T0).** A second orchestrator call would make it 2; the test would catch
  a regression in the skip plumbing.
- `test_run_does_not_prewarm_login_when_prefetch_disabled` — prefetch_book=False; authenticate
  happens at/after T0 (count 1, instant >= T0-ish), no pre-T0 auth.
- `test_run_prewarm_login_failure_falls_back_to_inline_auth` — FakeAdapter authenticate raises on
  first call (prewarm) but succeeds on second; assert the run still BOOKED, `authenticate_call_count
  == 2` (prewarm failure → course_id NOT added to `prewarmed_course_ids` → inline retry at T0).
  (Uses `set_authenticate_side_effects([RuntimeError(...), None])` — already on disk.)
- `test_run_short_circuits_already_booked_found_pre_t0` — prewarm finds a matching existing
  reservation; assert outcome ALREADY_BOOKED, `fa.search_call_count == 0`, `fa.book_call_count == 0`,
  the clock did NOT busy-wait all the way to T0 (instant < T0), the SF6 line
  `race: short-circuited pre-T0` is logged, AND `race: busy-wait complete` is ABSENT.
- `test_run_prewarm_does_not_short_circuit_on_nonmatching_reservation` — existing reservation for
  a DIFFERENT date/party size; race proceeds to BOOKED normally.
- `test_prewarm_outer_gather_isolates_login_failure` (MF2) — make `_prewarm_login` raise
  (FakeAdapter authenticate raises on the prewarm call) while prepare_book SUCCEEDS; assert the
  prepare_book token solve was NOT cancelled (`fa.prepare_book_call_count == 1`) and the run still
  proceeds to BOOKED. Proves `return_exceptions=True` on the outer gather.
- `test_prewarm_outer_gather_isolates_prefetch_failure` (MF2) — make prepare_book raise while
  `_prewarm_login` finds a match; assert the match-driven short-circuit still fires (ALREADY_BOOKED).

In `tests/test_foreup_adapter.py` (respx-mocked) — these are SEPARATE adapter-level tests of the
DEFENSIVE `_logged_in` guard (§3.1), not the orchestrator skip:
- `test_authenticate_is_idempotent_skips_relogin_when_already_logged_in` — call authenticate
  twice; assert only ONE warm-up GET + ONE login POST hit the mock (the 2nd call returns early
  because `_logged_in` is True). RED before adding the guard.
- `test_authenticate_retries_after_soft_login_failure` — first login returns 401 (`_logged_in`
  stays False); a second authenticate() DOES re-issue the warm GET + login POST.

### 7.2 PR2 tests (red first)
New `tests/test_captcha_pool.py` (respx + a counting fake provider — see 8.3):
- `test_prepare_book_solves_count_tokens_concurrently` — provider that records call timestamps;
  `prepare_book(None, req, count=3)` → 3 provider calls, all overlapping (started within epsilon),
  pool has 3 tokens. Use an asyncio-event fake provider to PROVE concurrency (all N enter the
  provider before any returns), not just count.
- `test_book_pops_pooled_token_fifo_oldest_first` — pre-seed pool with tokens ["t0","t1","t2"];
  two successive book() POSTs send `captchaid=t0` then `t1`; pool then holds ["t2"].
- `test_book_inline_solves_when_pool_empty` — empty pool, provider configured → book() calls
  provider inline once (today's behavior), POST carries the solved token.
- `test_prepare_book_partial_failure_keeps_successful_tokens` — provider raises on 1 of 3 solves
  (gather return_exceptions); pool ends with 2 tokens; prepare_book does NOT raise.
- `test_prepare_book_count_default_is_one` — `prepare_book(slot, req)` with no count solves
  exactly 1 (upgrade-path parity).
- `test_prepare_book_count_one_total_failure_raises` (NI10) — count=1 (default), provider raises
  → `prepare_book` RE-RAISES (CaptchaError). Proves the upgrade-path abort contract: an empty pool
  is NOT silently accepted at count=1.
- `test_prepare_book_count_n_total_failure_does_not_raise` (NI10) — count=3, ALL 3 solves raise →
  `prepare_book` does NOT raise; pool is empty. Proves the race-path partial/total tolerance.
- `test_book_inline_solves_when_pool_empty` — empty pool, provider configured → book() calls
  provider inline once, POST carries the solved token.
- `test_book_stale_pooled_token_resolves_inline_and_retries_same_slot` (MF1) — pre-seed ONE pooled
  token; FIRST POST returns a captcha-challenge 400; assert book() does NOT raise immediately but
  inline-solves a FRESH token and re-POSTs the SAME slot once; on the 2nd POST 2xx → BOOKED. Proves
  a stale pooled token is RECOVERABLE, not a hard abort (the round-1 regression).
- `test_book_stale_pooled_token_persistent_captcha_wall_raises` (MF1) — pooled token; BOTH the
  first POST and the inline-retry POST return captcha-challenge 400 → CaptchaError (a real wall
  terminates after exactly one inline retry, no infinite loop).
- `test_book_stale_pooled_token_then_slot_gone_advances` (MF1) — pooled token; first POST
  captcha-challenge 400, inline-retry POST returns plain `400 Time not available` → SlotGoneError
  (candidate loop advances). Proves the inline-retry's SECOND response is classified normally.
- `test_book_inline_token_captcha_challenge_raises_without_retry` (MF1) — empty pool → inline
  solve; POST returns captcha-challenge 400 → CaptchaError with NO second attempt (inline tokens
  get no re-solve retry — only POOLED tokens do).
- `test_book_pooled_time_not_available_raises_slot_gone` — pooled token; POST returns plain
  `400 Time not available` (NOT a captcha challenge) → SlotGoneError directly, no inline retry
  (the token was accepted; the slot was gone).

In `tests/test_orchestrator.py`:
- `test_run_prefetches_count_tokens_on_race_path` — scheduler.captcha_prefetch_count=5 (new
  default), prefetch_book=True; assert `fa.prepare_book` was called with `count=5` (FakeAdapter
  records `last_prepare_count` — already on disk).
- `test_run_fallback_candidates_consume_pooled_tokens` — covered at the ForeUP adapter level in
  `test_captcha_pool.py` (FakeAdapter does not model a pool); the orchestrator test asserts only
  the `count=N` plumbing.

In `tests/test_config*.py`:
- `test_scheduler_captcha_prefetch_count_default_is_five` (NEW default per OQ1)
- `test_scheduler_captcha_prefetch_count_rejects_zero` (ge=1 validation; RED first)
In `tests/test_container_config_parity.py` (NIT7 — assert BOTH fields):
- `test_captcha_prefetch_count_matches_across_configs` — `captcha_prefetch_count` equal (= 5)
  across example/container/local.
- `test_captcha_prefetch_lead_matches_across_configs` — `captcha_prefetch_lead_s` equal where
  present; after PR2 adds it explicitly to container/local, assert all three == 120 (the test
  documents that the lead is no longer relying on a silent code default).

### 7.3 PR3 tests (red first) — SF5-corrected
In `tests/test_foreup_adapter.py` (respx, with an `asyncio.sleep` spy or injected clock):
- `test_search_skips_leading_sleep_when_skip_initial_spacing_true` — single target_date,
  `search(req, skip_initial_spacing=True)` issues the GET with NO leading 250ms sleep. RED first.
- `test_search_keeps_leading_sleep_by_default` — single target_date, `search(req)` (default
  `skip_initial_spacing=False`) DOES sleep before the GET. **This is the watcher's real call
  pattern (one date per call) — it proves the watcher's inter-date-check spacing is preserved.**
- `test_search_spaces_subsequent_requests_even_when_skipping_initial` — two target_dates with
  `skip_initial_spacing=True`: the 1st GET has no sleep, the 2nd GET IS preceded by 250ms (the
  flag only drops the LEADING sleep).
In `tests/test_orchestrator.py`:
- `test_run_threads_skip_initial_spacing_on_race_path` — prefetch_book=True → `_poll_for_slots`
  calls `adapter.search(..., skip_initial_spacing=True)`; prefetch_book=False → False. (Assert via
  a recording wrapper on `fa.search`.)

## 8. Stub surface (signatures only — implementation by follow-up agents)

### 8.1 `core/adapter.py` — Protocol changes (PR2 + PR3)
- `prepare_book` gains `*, count: int = 1`. Docstring states the N-dependent raise contract
  (NI10): count==1 + total failure RE-RAISES (upgrade abort); count>1 NEVER raises (partial or
  total). count>1 pre-solves a POOL; default 1 preserves the upgrade-path single-token caller.
- `search` gains `*, skip_initial_spacing: bool = False` (PR3/SF5). Docstring: the race-path
  caller (`Orchestrator._poll_for_slots` with `prefetch_book=True`) passes True to drop the
  leading courtesy sleep before the FIRST GET (nothing to space from at T0); the watcher passes
  the default False so its per-date-check inter-GET spacing is preserved.

### 8.2 `core/orchestrator.py` — new methods + state (PR1 + PR2 + PR3)
- New ctor state: `self._prewarmed_course_ids: set[CourseId] = set()` (MF3).
- `async def _prewarm_login(self, adapter, course_id, request) -> ExistingReservation | None`
  (PR1) — best-effort authenticate + list_reservations + match. MUST NOT raise (MF2 cond 1);
  catches everything, returns None on failure. On auth success adds `course_id` to
  `_prewarmed_course_ids`. Returns the match or None.
- `async def _prewarm_primary(self, request) -> ExistingReservation | None` (PR1, extended PR2) —
  concurrent `asyncio.gather(_prewarm_login(...), prepare_book(None, request, count=N),
  return_exceptions=True)` (MF2 cond 2) for the first-preference adapter; awaits BOTH to
  completion before returning; logs+swallows any gathered Exception. Returns the reservation match.
- `_run_course` gains `prewarmed_course_ids: frozenset[CourseId]` and skips `authenticate()` for a
  course in that set (MF3).
- `_poll_for_slots` passes `skip_initial_spacing=self._prefetch_book` to `adapter.search` (PR3).
- `run()` rewire (NI9): race path calls `match = await self._prewarm_primary(request)`; if match,
  SF6-log + record ALREADY_BOOKED + notify + RETURN before the busy-wait. Then busy-wait to T0 and
  pass `frozenset(self._prewarmed_course_ids)` into each `_run_course`.
- `_prefetch_captcha` stays in PR1 (called by `_prewarm_primary`); PR2 folds it into the gather as
  the `count=N` `prepare_book` call (the standalone `_prefetch_captcha` may be removed in PR2 once
  `_prewarm_primary` calls `prepare_book` directly).

### 8.3 `courses/foreup/base.py` (PR1 + PR2 + PR3)
- `authenticate`: add the defensive `if self._logged_in: return` guard at the top (PR1).
- Field `self._captcha_tokens: deque[str]` (already on disk).
- `prepare_book(self, slot, request, *, count=1)`: concurrent gather of `count` solves into the
  deque; N-dependent raise contract per NI10 (PR2).
- `book`: pop from `self._captcha_tokens` (FIFO) if non-empty else inline solve. **MF1: track
  whether the token came from the POOL; on a captcha-challenge classification of a POOLED token,
  inline-solve ONE fresh token and re-POST the same slot ONCE, then classify normally. An INLINE
  token gets no retry.** (PR2)
- `search(self, request, *, skip_initial_spacing=False)`: sleep before the 1st iteration UNLESS
  `skip_initial_spacing`; always sleep before the 2nd+ iteration (PR3).

### 8.4 `dev/fake_adapter.py` (PR1 + PR2 + PR3)
- `prepare_book(self, slot, request, *, count=1)`: record `self.last_prepare_count = count`;
  no-op (already on disk).
- `search(self, request, *, skip_initial_spacing=False)`: accept + ignore the flag for parity;
  optionally record `self.last_search_skip_initial_spacing` so the orchestrator threading test can
  assert it (PR3).
- `set_authenticate_side_effects(self, effects)` (already on disk) — drives the prewarm-failure
  test.

### 8.5 `courses/teeitup/base.py` (PR2 + PR3)
- `prepare_book(self, slot, request, *, count=1)`: no-op (already on disk).
- `search(self, request, *, skip_initial_spacing=False)`: accept the flag for Protocol parity; it
  MAY honor it (drop its own leading inter-step sleep) or ignore it — TeeItUp is local-dev-only
  and never on the prod race path, so ignoring is acceptable. Signature parity is the requirement.

### 8.6 `core/config.py` (PR2)
- `SchedulerConfig.captcha_prefetch_count: int = Field(default=5, ge=1)` (default bumped to 5 per
  OQ1; already on disk at 3 — PR2 changes the default and the TOMLs).

## 9. Resolved questions (round 2)

1. **Default N → 5.** RESOLVED: recommend `captcha_prefetch_count = 5` (was 3). Covers the full
   2026 5-attempt fallback spread; cost negligible; fits the 120s concurrent lead. PR2 sets the
   default to 5 and updates all three TOMLs.
2. **Pre-T0 ALREADY_BOOKED short-circuit → SHIP IT.** RESOLVED: recommend shipping the
   short-circuit (§3.2) with the SF6 verification line. The wasted-busy-wait saving is real; the
   only staleness gap (a manual booking placed after prewarm but before T0) is already covered by
   the post-T0 `_run_course` guard reading the same login cache. If the user prefers maximal
   conservatism, the fallback is to always busy-wait and let the post-T0 guard handle it — flag
   for explicit sign-off, but the recommendation is to ship.

## 10. Confidence / unverified

- **2captcha concurrent-submission tolerance.** I did not verify 2captcha's policy on N
  simultaneous `in.php` submissions per key. At N=3–5 this is almost certainly fine (their API
  is built for batch solving) but I could not confirm a hard concurrency limit. If a future N is
  raised high, re-check. Mitigated by `return_exceptions=True` (a rate-limited solve just drops
  that one token; the pool still gets the rest).
- **reCAPTCHA token freshness window** is stated as ~2min from the existing code comments; I did
  not independently re-verify. The oldest-first FIFO pop + concurrent solve keep all tokens
  within `lead` (120s) of T0, which is at the edge of that window for the LAST-used token if
  fallbacks drag — hence the documented stale-token → CaptchaError/SlotGone handling in 4.3.
```
