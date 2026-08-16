# STAGGER_PLAN.md — stagger the T0 blind-POST burst across the open boundary

**Status:** LIVE IN PROD — `infra/v2.14.0`, deployed 2026-08-15 (`main`@`e6a8abb`,
`dryRun=false`, all three jobs verified on the new image with crons/timeouts unchanged).
Shipped in PR #199: 749 tests green, two adversarial review rounds (BLOCK → APPROVE).
**First exercise — Sun 2026-08-16 05:50 ET (booked 8/23): PASSED, non-regression confirmed.**
Booked the rank-0 nearest-midpoint slot (09:22, `TTB:TTID_081606000041qx5`). The stagger
executed to spec — measured send offsets `-498 / -249 / +1 ms` against planned
`-500 / -250 / 0`, busy-wait drift 0.7 ms, NTP offset 1.2 ms.
Two results beyond non-regression:
1. **The `-500 ms` POST returned HTTP 200**, so inventory was live at 05:59:59.5 — on a
   SUNDAY, the pre-open-rejection hypothesis (A) does not hold. Saturdays remain open;
   all three misses were Saturdays.
2. **First non-uniform burst outcome in the RETENTION WINDOW** (1 booked / 2 rejected,
   where every other drop there was 3/3, 2/3 or 0/3). The two rejects were `daily_limit`,
   NOT lost races — ForeUP's 1/day rule bounced them once the rank-0 booking committed.
   Consequence: `cancelled 0 extra(s)`, no surplus reservation to clean up.
   **Do NOT read this as a stagger effect.** The pre-stagger 2026-07-11 drop produced the
   same 1-booked/2-`daily_limit` shape from a SIMULTANEOUS burst (CLAUDE.md, `infra/v2.9.0`),
   so a simultaneous burst can serialize behind the 1/day counter too. n=1 either way — the
   shape is uninformative about timing until more drops land.
   This also exposed a diagnostic defect fixed in PR #201 — the aggregate line called
   both rejects "claimed pre-book", reporting two lost races that never happened. See
   §3.3 on the `gone[<reason>]` tag.
The first real diagnostic reading is still the Sat 2026-08-22 drop (books 8/29).
**Scope:** booking-behavior change, race path only (`--wait` + blind-capable primary).
**Motivates:** the 2026-08-15 miss (target Sat 2026-08-22) and the still-unexplained
2026-07-18 miss (target Sat 2026-07-25).

---

## 1. The observation

Every blind-POST drop in the Log Analytics retention window, by per-slot HTTP status.
All three POSTs fire CONCURRENTLY at `T0 − early_arrival_ms` (= 05:59:59.500 ET) and all
three responses carry a server `Date` of exactly `10:00:00 GMT`.

| Drop | Target | 09:15 | 09:22 | 09:30 | Outcome |
|---|---|---|---|---|---|
| Sun 2026-07-19 | 07-26 | — | 200 | 200 | booked |
| Sat 2026-07-25 | 08-01 | 200 | 200 | 200 | booked |
| Sun 2026-07-26 | 08-02 | 200 | 400 | 200 | booked |
| Sat 2026-08-01 | 08-08 | 400 | 400 | 400 | **miss** (Anniversary Tournament — whole-day block) |
| Sun 2026-08-02 | 08-09 | 200 | 400 | 200 | booked |
| Sat 2026-08-08 | 08-15 | 200 | 200 | 200 | booked |
| Sun 2026-08-09 | 08-16 | 200 | 200 | 200 | booked |
| Sat 2026-08-15 | 08-22 | 400 | 400 | 400 | **miss** (unexplained) |

The isolated 400s among 200s are ForeUP's "1 online reservation per day" rule rejecting a
sibling after the first lands — not races (see the `foreup-one-per-day-limit` note).

**The result is bimodal: 3/3 or 0/3.** A genuine slot race cannot produce that. Our POSTs
land within ~100 ms of the window opening; no human books three specific tee times in
100 ms. A race would produce mixed, unordered outcomes — which is exactly what we see
*within* a successful drop (the 1/day rejections), and never on a miss.

Two candidate explanations survive for a 0/3 on a day with no known block:

- **(A) Boundary/flip timing.** ForeUP's 7-day release is a state flip. If our burst
  arrives before the flip, every POST gets `400 {"success":false,"msg":"Time not
  available."}` — byte-identical to a slot-gone rejection. All-or-nothing across the burst,
  because they all fire at the same instant. Whether the flip instant jitters or our
  −500 ms arrival is simply marginal, the failure mode is the same.
- **(B) An unpublished whole-day or whole-morning block**, like 2026-08-01.

We currently **cannot tell these apart**, and that is the core problem this plan fixes.
The server `Date` header (added in `infra/v2.11.0` for exactly this question) has 1-second
resolution, so `10:00:00 GMT` cannot separate `10:00:00.05` from `10:00:00.95`.

### 1.1 What was ruled out

- **Not a bot defect.** The 2026-08-15 run was mechanically perfect: NTP offset 0.1 ms,
  busy-wait `drift_ms=0.7`, 5/5 CAPTCHA tokens pooled pre-T0, login + layer-2 guard clean,
  re-guard clean, fresh fallback search fired correctly, clean `no_inventory` terminal.
- **Not a grid drift.** The synthesized grid (`08:45`…`10:00`, 7–8 min cadence) matches the
  live teesheet cadence; `07:37` observed post-drop sits exactly on it.
- **Not "the morning sold out and we were slow."** On 2026-08-08 — a drop we WON — the
  watcher saw `available tee times 10:45–17:45` just **27 s** after T0. A vaporised morning
  within seconds of the drop is the NORMAL Mangrove Bay Saturday, present on winning weeks
  too. It is therefore not diagnostic of a miss.

## 2. The change

Give each blind POST in the burst its **own fire offset relative to T0**, instead of firing
the whole burst at one instant.

```
today:     ─────────┬─────────────────  all 3 POSTs @ T0−500ms
                  T0−500

proposed:  ─────────┬────┬────┬────────  POST0 @ T0−500 (unchanged: 09:22, rank-0)
                  T0−500 │    T0          POST1 @ T0−250 (09:15)
                       T0−250             POST2 @ T0     (09:30)
```

This is simultaneously:

- **A hedge.** At least one POST is guaranteed to be SENT no earlier than T0 — and, given
  ~50-150 ms of network latency, to ARRIVE after it. Under (A) the
  burst can no longer be wiped out as a unit.
- **A diagnostic.** The outcome pattern is now *ordered by offset*. A clean cutoff —
  everything at or before offset X fails, everything after succeeds — is (A). Outcomes
  unordered with respect to offset are (B) or a real race. One drop resolves it.

### 2.1 Non-regression is the binding constraint

The rank-0 (best, nearest-midpoint) slot keeps **today's exact timing**. The default's first
entry is `−500`, identical to the current `early_arrival_ms=500` fire instant. On every drop
we currently win, POST0 fires at the same instant against the same slot and still wins. The
staggering only changes what the *other two* POSTs do — and today those are pure surplus
that get 400'd by the 1/day rule anyway **when rank-0 wins**. When rank-0 loses they are the
fallback, and they now fire 250 ms / 500 ms later than today; see the first row of §6, which
is the honest cost of this change.

Worst case under (A): we lose the rank-0 slot to a pre-open rejection but POST1/POST2 catch
the flip, so we book 09:15 or 09:30 instead of 09:22. That is strictly better than today's
outcome on such a day, which is nothing.

### 2.2 Interaction with the 1-reservation-per-day rule

Staggering means the earliest-firing POST that succeeds may block later ones. Because the
offsets are paired with slots in **rank order** (best slot ← first offset), the earliest
POST is always the best slot, so the 1/day rule can only ever reject a *worse* sibling.
`_keep_best` + `_cancel_extras` are unchanged and still correct.

**This required an orchestrator change.** `_blind_post_course` previously fired
`blind_slots[:n]` in whatever order the adapter returned. Ranked order was only an *adapter
convention* — `synthesize_blind_slots` is contracted to return ranked slots and Mangrove Bay
does, but nothing enforced it, and the simultaneous burst never cared. It matters now, so
the burst re-ranks with `rank_slots_for_request` before pairing offsets. A no-op for the
live adapter; it makes the safety property true by construction rather than by convention.
(Caught by the test asserting POST order — `FakeAdapter.synthesize_blind_slots` returns its
scripted list unranked, which is exactly the divergence worth defending against.)

## 3. Design

### 3.1 Config

New `SchedulerConfig` field:

```python
blind_post_stagger_ms: tuple[int, ...] = (-500, -250, 0)
```

- Per-POST fire offsets in **milliseconds relative to T0** (negative = before T0), paired
  positionally with the ranked blind slots: `blind_slots[i]` ← `blind_post_stagger_ms[i]`.
- More slots than offsets → the surplus slots reuse the **last** offset (so a widened
  `blind_post_max_count` degrades to today's simultaneous behaviour for the tail rather
  than silently dropping POSTs).
- **Empty tuple → legacy behaviour**: every POST fires immediately on busy-wait completion.
  This is the escape hatch; it is exactly today's code path.
- **Offsets must be NON-DECREASING** (`field_validator`). §2.2's safety argument requires the
  best-ranked slot to POST first; `(-500, 0, -250)` would fire rank-2 before rank-1 and let
  the 1/day rule reject the better slot. That shape passes every config-parity assertion
  (`stagger[0] == -early_arrival_ms`, `min == -early_arrival_ms`, `max >= 0`), so only a
  monotonicity check catches it. This is a WITHIN-field check, so it carries none of the
  cross-field coupling that made a validator the wrong tool for the clamp below.
- **Offsets earlier than `-early_arrival_ms` are CLAMPED to it, with a WARNING** naming both
  the raw and effective values. We cannot fire before the busy-wait wakes us.

  *(Revised during implementation. The first draft made this a pydantic cross-field
  validator. That was wrong twice over: it coupled `blind_post_stagger_ms` to
  `early_arrival_ms` in every config and test helper — 137 unrelated test failures, all
  noise — and it bought nothing, because `_fire_blind_post` already self-clamps by
  computing a non-positive, no-sleep delay. The honesty requirement that motivated the
  validator is satisfied by clamping in `_stagger_offsets_for`, since those are the values
  the diagnostic line logs. Behaviour identical; blast radius zero.)*

`early_arrival_ms` keeps its existing meaning: it sets the busy-wait wake instant. It is now
the *earliest possible* burst offset rather than *the* burst offset.

### 3.2 Orchestrator

- Extract `_compute_t0()` (absolute T0); `_compute_t0_minus_early()` becomes
  `_compute_t0() - early_arrival_ms`. Behaviour-preserving refactor.
- `_blind_post_course` wraps each `adapter.book(...)` in `_fire_blind_post(...)`, which
  sleeps to its target instant before POSTing:

  ```python
  async def _fire_blind_post(self, adapter, slot, request, offset_ms, t0):
      delay = ((t0 + timedelta(milliseconds=offset_ms)) - self._clock.now_utc()).total_seconds()
      if delay > 0:
          await self._clock.sleep(delay)
      return await adapter.book(slot, request)
  ```

  A negative/zero delay fires immediately — so a late-landing cron that starts past an
  offset never *waits*, it just goes. Clock-injected, so `FakeClock` drives it in tests.
- The burst stays `asyncio.create_task` + `gather(return_exceptions=True)`. Task creation is
  still simultaneous; only the POST instant differs. All existing capture/keep-best/
  cancel-extras/re-guard semantics are untouched.

### 3.3 Observability (the whole point)

`ForeUpAdapter.book()` already logs the response status + server `Date` per slot. Add the
offset to the orchestrator's per-POST accounting so the log line reads the boundary directly:

```
course foreup:mangrove_bay: blind-POST firing 3 book POST(s), staggered at T0 offsets [-500, -250, 0] ms
course foreup:mangrove_bay: blind-POST sent -500ms (planned -500ms) slot 202607220922 → gone[unavailable]
course foreup:mangrove_bay: blind-POST sent -250ms (planned -250ms) slot 202607220915 → gone[unavailable]
course foreup:mangrove_bay: blind-POST sent +0ms (planned +0ms) slot 202607220930 → BOOKED
```

That third line is the finding. One drop with this shipped resolves (A) vs (B).

**The `[reason]` tag is load-bearing to that reading** (added post-ship, after the
2026-08-16 drop). ForeUP returns HTTP 400 for two rejections with OPPOSITE evidential
weight and no machine-readable discriminator — only the `msg` prose differs:

* `gone[unavailable]` (`"Time not available."`) — the slot was not bookable. This is the
  ONLY reason that bears on (A) vs (B).
* `gone[daily_limit]` (`"...1 online reservation per day."`) — ForeUP bouncing the surplus
  POSTs of a burst WE ALREADY WON. It says nothing about the race. 2026-08-16 came back
  1 booked / 2 daily_limit, plausibly because the 250 ms gaps let the rank-0 booking commit
  before the siblings were processed — but **that causation is NOT established**: every
  other drop in the retention window booked 2–3 and cancelled extras, yet the pre-stagger
  2026-07-11 drop produced the SAME 1/2 shape from a simultaneous burst. A simultaneous
  burst can evidently also serialize behind ForeUP's 1/day counter, so the 1/2 shape is
  uninformative about timing on its own.

Before the split, both logged as `gone` and the aggregate line called all of them
"claimed pre-book", i.e. reported lost races that never happened.

**`sent` is MEASURED at the send instant, not copied from the plan** (adversarial review,
must-fix 1). On a run that STARTS past an offset — a late-landing cron, a mid-deploy fire —
every delay is non-positive and all N POSTs go out simultaneously. Reporting the planned
ladder there would show outcomes spread across three instants that never happened, and an
operator reading a 0/N would conclude "unordered ⇒ not the boundary" and aim the next fix at
the wrong thing. `planned` rides alongside so a late start and any `Clock.sleep` jitter are
both visible. `tests/test_blind_post_stagger.py` pins this with a clock 3 s past T0.

## 4. What this plan does NOT do

- **Does not change `early_arrival_ms`.** Blindly moving it would trade one guess for
  another and destroy the baseline the diagnostic needs.
- **Does not widen the time window.** `07:37` was bookable at T0+6 s on 2026-08-15 and was
  correctly rejected as out-of-window (`08:45–10:00`). Widening the Saturday window is a
  real, separate improvement — tracked in BACKLOG.md, not bundled here, because it would
  confound the diagnostic by changing which slots the grid synthesises.
- **Does not add a repeated/retry burst.** If the shipped diagnostic confirms (A), the
  follow-up is a *retry* burst across a wider post-T0 window (T0+0.5 s, +1 s, +2 s), which
  needs a larger CAPTCHA pool (each `book()` pops a single-use token) and therefore its own
  cost/rate-limit analysis. Measure first.

## 5. Token budget (unchanged)

The burst is still `blind_post_max_count = 3` POSTs, each popping one pooled token, with
`blind_post_fallback_token_reserve = 2` left for the post-reguard fallback — 5 pre-solved
concurrently in the 120 s lead, exactly as today. No new CAPTCHA cost, no new rate-limit
exposure. The staggered POSTs hold their tokens ~500 ms longer (the shipped tail offset is `0`).
That is NOT "far inside" the freshness budget — `config.py` documents token age at T0 as
`lead − solve_time <= 120 s <= reCAPTCHA validity`, i.e. already AT the boundary, not
comfortably within it. +500 ms is still safe, and a genuinely stale pop is covered by the
MF1 inline re-solve, but the margin should not be overstated.

## 6. Risks

| Risk | Assessment |
|---|---|
| **POST1/POST2 fire 250 ms / 500 ms LATER than today** | **The real cost, and it is not zero.** §2.1 calls them "pure surplus that get 400'd by the 1/day rule anyway" — true only when rank-0 WINS. On exactly the drops this change targets (rank-0 lost) they are the fallback, and under hypothesis (B)/genuine-race a delay strictly reduces their chances in a market this plan itself says vaporises within seconds. Accepted: the delay only matters in the branch where the *hedge* is what saves us, and a hedge that fires at the same instant as the thing it is hedging is not a hedge. Chosen tail offset `0` (not `+250`) to minimise this. |
| Rank-0 slot lost to a pre-open rejection | Only on days we currently lose everything, so no drop we win today is affected. Net positive — but see the row above; "strictly better" overstates it for the race branch. |
| A later POST books a worse slot while the best was merely slow | `_keep_best` re-ranks whatever booked; the 1/day rule can only reject worse siblings (§2.2), enforced by the monotonicity validator + the burst re-rank. |
| Stagger delays push the burst past the replica timeout | Max added latency 500 ms against `bookingReplicaTimeout=1200 s`. Negligible. |
| Hypothesis (A) is wrong | Then the diagnostic says so on the first 0/3 drop — the offsets' outcomes will be unordered. Not quite "zero cost": see the first row, and the rank/offset confound in §4. |

## 7. Test plan (TDD, red first)

All in `tests/test_blind_post_stagger.py` unless noted. **Status: implemented, 749 green.**

1. Default `(-500, -250, 0)` keeps the rank-0 POST at `-early_arrival_ms` and puts at
   least one POST after T0. Parity-pinned across the committed configs, plus a mechanical
   pin that `stagger[0] == -early_arrival_ms` (`tests/test_container_config_parity.py`).
2. An offset `< -early_arrival_ms` is clamped to the wakeup and WARNs; offsets within it
   are neither clamped nor warned.
3. Burst applies per-slot delays: a POST for `T0+250 ms` issued at the wakeup (`T0−500 ms`)
   sleeps exactly 750 ms (`RecordingClock` asserts the requested delay). The shipped tail
   offset is `0`, so its real delay is 500 ms.
4. Slots beyond the offset list reuse the last offset; fewer slots take the leading ones.
5. Empty tuple → no sleeps, legacy simultaneous behaviour.
6. A POST whose target instant has already passed fires immediately (no negative sleep);
   likewise an offset exactly at the wakeup adds no latency.
7. Ordering: `book()` is called in rank order, best slot first (guards §2.2).
8. Existing blind-POST suite (keep-best, cancel-extras, re-guard, fallback, BaseException
   capture) stays green unchanged.

**Testing note.** The stagger tests use a `RecordingClock` that records sleeps WITHOUT
advancing time, not `FakeClock`. `FakeClock.sleep` advances time additively, so three
*concurrent* stagger sleeps would interfere and the recorded delays would depend on
coroutine scheduling order. Freezing time makes each task compute its delay from one
`now_utc()` reading, which is exactly the property under test. `FakeClock` is still used for
the burst-integration test, which asserts outcomes and ordering rather than timings.

## 8. Docs to update (change→docs map)

- `CLAUDE.md` — blind-POST invariant bullet (burst is staggered, not simultaneous).
- `PLAN.md` — §6 blind-net description AND **§12 (ToS/etiquette)**, which described the burst
  as "a handful of requests at a single instant". It is a ~500 ms-wide fan-out now; the
  etiquette claim must describe the real request pattern.
- `BLIND_POST_PLAN.md` — supersession banner on its "fire N POSTs CONCURRENTLY" mechanism.
- `config/example.toml`, `config/container.toml`, `config/local.toml` — new key + comment.
- `src/teetime/core/config.py` — field comment.
- `tests/test_container_config_parity.py` — parity/default pin.
- `README.md` — only if the config walkthrough enumerates scheduler keys.
- `BACKLOG.md` — record the deferred window-widening and retry-burst follow-ups.
