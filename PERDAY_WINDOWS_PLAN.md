# PERDAY_WINDOWS_PLAN.md — Per-day (and multiple-per-day) booking windows

Status: PROPOSED (plan + stubs; no implementation). TDD mandatory.

## 1. Executive summary

Today `target_weekdays` (a flat list of wanted weekdays) and `time_windows`
(a flat list of earliest/latest ranges) are **independent** and combined as a
**cross-product**: every window applies to every wanted day. The booker/watcher
search every configured weekday against every configured window.

Evidence (current `main`):
- `core/config.py:67` — `time_windows: list[TimeWindowConfig]` (flat, untagged).
- `core/config.py:77` — `target_weekdays: list[str]` (separate hand-maintained list).
- `core/config.py:100-103` — `wanted_weekday_indices` derives ONLY from `target_weekdays`.
- `core/slot_utils.py:61-67` — `_matching_window` iterates ALL `request.time_windows`;
  a slot matches if it lies in ANY window.
- `__main__.py:600` / `__main__.py:622` — `_build_request` copies ALL `time_windows`
  into every `BookingRequest`, regardless of which date it targets.
- `core/watch_orchestrator.py:305,311` — `_check_course` scopes `target_dates` to the
  loop date but **leaves `time_windows` untouched** (all windows searched on every date).

**Goal:** bind each window to a specific weekday and allow MULTIPLE windows per
day (e.g. Sat 08:30–10:00; Sun 09:00–10:00; Sun 17:00–19:00). Wanted booking
weekdays become **derived** from the distinct weekdays present in the windows
(remove `target_weekdays` as a hand-maintained field). For now the user keeps the
existing values (Sat+Sun both 08:45–10:00), expressed per-day.

**Decided invariant (do not relitigate):** ONE reservation PER DAY (best window).
With Sunday morning + Sunday afternoon both configured, the bot holds AT MOST ONE
Sunday reservation, chosen across BOTH Sunday windows. This keeps the per-date
`(RequestId, resolved_date)` store key, idempotency, and the one-booking/upgrade
model UNCHANGED.

**The mechanism is small and reuses the existing per-date scoping spine.** Each
invocation (booking run OR a single watcher date-loop iteration) already pins
`target_dates` to one date; we ALSO pin `time_windows` to **that date's weekday's
windows**. `rank_slots_for_request` then "just works" — it sees only that day's
windows. The only genuinely new code is: a `weekday` field on the window config,
a `windows_for(weekday)` helper, and changing `wanted_weekday_indices` to read
from windows.

**Chosen schema: (A) flat list, each `[[request.time_windows]]` carries a required
`weekday`.** Rationale in §3.

---

## 2. The `rank_slots_for_request` multi-window analysis (the subtle one)

Read `core/slot_utils.py:24-67` precisely. Current behaviour with multiple windows:

1. For each slot, `_matching_window` (`slot_utils.py:61-67`) returns the **FIRST
   window in `request.time_windows` order whose `[earliest, latest]` contains the
   slot's local time**. List order is the tiebreak ONLY when windows overlap; for
   disjoint windows each slot belongs to exactly one window (or none).
2. The global sort key (`slot_utils.py:57`) is
   `(|slot − that-window's-midpoint|, slot.tee_time)`. **Crucially this is distance
   to the slot's OWN matching window's midpoint — NOT a single global midpoint, and
   NOT window-list priority.** All candidates from all windows are pooled and sorted
   together by their per-window midpoint distance.

### The disjoint-window consequence (OPEN QUESTION — see §13, Q1)

Given Sunday windows **09:00–10:00** (midpoint 09:30) and **17:00–19:00**
(midpoint 18:00), suppose two slots are available:
- 09:50 → distance to 09:30 = 20 min
- 18:00 → distance to 18:00 = 0 min

Under the CURRENT pooled-midpoint-distance sort, **18:00 wins** (distance 0 < 20),
so the bot would book the **afternoon** slot even though a perfectly good morning
slot exists. A user who lists the morning window first likely expects the morning
to be preferred. The current code has **no notion of window priority** — a slot
dead-centre in a less-preferred window beats an off-centre slot in a more-preferred
one.

This was harmless before this feature because the single production window made
every candidate share one midpoint. With genuinely disjoint per-day windows it
becomes a real, user-visible choice. **It must not be silently assumed.**

**Recommended default (pending user confirmation): window list order = priority.**
Prefer ANY slot in an earlier-listed window over ANY slot in a later-listed window;
within the chosen window, keep the existing midpoint-distance sort. This makes
"list your favourite window first" intuitive and matches how `priority_slots`
already encodes preference by order (`upgrade_orchestrator.py:228`, sorted by
`priority`). For the user's CURRENT config (one window per day) the two algorithms
are identical — so this can ship behind the schema change with zero behaviour change
today, and the new ordering only bites once a second same-day window is added.

**Alternative (status quo): pure pooled closest-to-any-window-midpoint** — simpler,
no new sort tier, but picks 18:00 over 09:50 as shown. Surfaced as Q1.

This plan implements the schema + scoping in PR1–PR3 **without changing the ranking
algorithm** (so the current single-window-per-day behaviour is byte-identical), and
isolates the priority-vs-pooled ranking decision into **PR4, gated on Q1**. PR4 is
optional and can be deferred until the user actually adds a second same-day window.

---

## 3. Schema decision: (A) flat tagged list vs (B) nested day_windows

### (A) Flat list, each window carries `weekday` (CHOSEN)

```toml
[[request.time_windows]]
weekday  = "saturday"
earliest = 08:45:00
latest   = 10:00:00

[[request.time_windows]]
weekday  = "sunday"
earliest = 08:45:00
latest   = 10:00:00
```

A Sunday-afternoon window is just another row with `weekday = "sunday"`.

### (B) Nested day_windows

```toml
[[request.day_windows]]
weekday = "sunday"
windows = [
  { earliest = "09:00:00", latest = "10:00:00" },
  { earliest = "17:00:00", latest = "19:00:00" },
]
```

### Decision: **(A)**, for these reasons

1. **Minimal churn / reuses the existing shape.** `TimeWindowConfig` already exists
   and is a flat `[[request.time_windows]]` list (`config.py:30-34`, used at
   `__main__.py:600`). Adding one field is a far smaller diff than introducing a new
   nested table type, a new `RequestConfig` field, and rewriting `_build_request`.
2. **`BookingRequest.time_windows` stays a flat tuple** (`models.py:141`). The whole
   downstream spine (`slot_utils`, `foreup/base.py:350`, `teeitup/base.py:243`,
   `upgrade_orchestrator`) consumes a flat `tuple[TimeWindow, ...]`. (A) keeps the
   domain model unchanged; per-invocation scoping just filters the flat list by
   weekday. (B) would still have to flatten to the same tuple, so the nesting buys
   nothing downstream.
3. **TOML ergonomics.** (A)'s repeated `[[request.time_windows]]` blocks read
   cleanly and match the existing players-list idiom. (B)'s inline-array-of-tables
   inside a table is the ugliest corner of TOML and error-prone to hand-edit.
4. **Validation is simple in (A):** "every window has a valid `weekday`" is a flat
   per-row check. Derivation of wanted-days is `{weekday_from_name(w.weekday) for w}`.

The one ergonomic cost of (A) — a day's windows aren't visually grouped — is
mitigated by sorting/normalising the list by `(weekday, earliest)` in the validator
(mirrors the existing `target_weekdays` normalisation at `config.py:95-97`).

### New helper

`RequestConfig.windows_for(weekday: int) -> tuple[TimeWindowConfig, ...]` — the
windows whose weekday index == `weekday`, in normalised order. Used by the CLI to
scope each invocation's request. Returns `()` for a weekday with no windows (callers
never pass such a weekday — see §5 invariant).

---

## 4. Domain-model impact (`core/models.py`)

`TimeWindow` (`models.py:119-130`) stays **weekday-free**. Weekday lives only in the
**config** layer; by the time a `BookingRequest` is built, `time_windows` has already
been scoped to a single date, so the windows in the request are unambiguously that
date's windows. Keeping `TimeWindow` weekday-free avoids touching `slot_utils`,
`foreup/base.py`, `teeitup/base.py`, `upgrade_orchestrator`, and every test that
constructs a bare `TimeWindow(earliest=…, latest=…)` (dozens; e.g.
`test_watch_orchestrator.py:89`, `test_upgrade_orchestrator.py:82`).

### Fingerprint / RequestId (`models.py:43-70`) — see §6

`build_request_fingerprint` already includes `time_windows` as `HH:MM-HH:MM` tokens
(`models.py:66-68`). The weekday is NOT in the fingerprint today. Decision in §6:
**add weekday to the window token** so two configs that differ only in which day a
window applies to produce different RequestIds.

---

## 5. Wanted-days derivation + the gate/watcher

`wanted_weekday_indices` becomes derived from the windows, eliminating the
hand-maintained `target_weekdays`:

```python
@property
def wanted_weekday_indices(self) -> frozenset[int]:
    return frozenset(weekday_from_name(w.weekday) for w in self.time_windows)
```

Consumers are unchanged — they already take a `frozenset[int]`:
- `should_book_today(..., wanted_weekdays=cfg.request.wanted_weekday_indices)`
  (`__main__.py:257`, gate at `booking_day_gate.py:37-66`).
- `next_occurrences_within_horizon(today, cfg.request.wanted_weekday_indices, …)`
  (`__main__.py:396`, `target_date.py:55-74`).

**Invariant (assert it):** a target date whose weekday has NO windows can never be
produced. It holds structurally — `wanted_weekday_indices` is the set of weekdays
that HAVE windows, and both the gate and the watcher only ever produce dates whose
weekday is in that set. PR3 adds an explicit guard in the CLI/orchestrator: when
scoping an invocation to a date, assert `windows_for(date.weekday())` is non-empty
(defensive against a future caller passing `--date` for a windowless weekday — see
Q2 / watcher `--date` handling in §8).

`target_weekdays` and the deprecated `target_weekday` alias are **removed**
(`config.py:77-98`). See §7 for the migration / hard-cutover decision.

---

## 6. Fingerprint / RequestId impact

`build_request_fingerprint` (`models.py:43-70`) folds `time_windows` into the
RequestId. Two impacts:

1. **Adding `weekday` changes window identity.** A config where the 08:45–10:00
   window is Saturday vs Sunday should be a *different* request identity (it books a
   different day). So the weekday MUST enter the fingerprint. Change the window token
   from `HH:MM-HH:MM` to `<weekday-index>:HH:MM-HH:MM` (e.g. `5:08:45-10:00`).
   Sorting stays lexical; prefixing the index keeps a deterministic, weekday-grouped
   order. (Signature change: `time_windows` param becomes
   `Sequence[tuple[int, TimeWindow]]` — a `(weekday_index, window)` pair — so the
   model layer stays weekday-free in `TimeWindow` while the fingerprint still encodes
   the day. See stub in §11.)

2. **The RequestId WILL change for existing deployments** (the window tokens change
   shape regardless of weekday once the index prefix is added). This is acceptable:
   the store is in-process only (`InMemoryStore`; no durable record across runs — see
   CLAUDE.md "Idempotency key is `(RequestId, resolved_date)` … held in-process
   only"). A changed RequestId only matters within a single run, and within a run it
   is internally consistent. No migration of any persisted key is needed. State this
   in the PR body so the reviewer doesn't flag a phantom idempotency break.

Note: the per-invocation request is **scoped to one weekday's windows** before the
fingerprint is computed (PR3). Two different dates of the SAME weekday share the same
window set → same RequestId, which is correct (the offset, not the date, defines
identity; `derive_request_id` excludes resolved dates, `models.py:34-39`). Two
different weekdays produce different RequestIds — also correct, they are different
goals.

---

## 7. Backward-compat / migration: **HARD CUTOVER**

**Decision: hard cutover. Old-style config (untagged `time_windows` and/or any
`target_weekdays` / `target_weekday` key) is REJECTED with a clear error.** Justified
because:
- Single-user, and WE control all three committed configs (`container.toml`,
  `local.toml`, `example.toml`) — we migrate them in the same PR (PR2).
- A silent auto-migration (e.g. "untagged window applies to all `target_weekdays`")
  would resurrect the cross-product semantics this feature is removing, and would
  mask a stale config. Better to fail loudly once.
- The validator already distinguishes "user supplied" from "default" via
  `model_fields_set` (`config.py:84-88`), so a precise migration error is cheap.

Error behaviour (PR1 validator):
- `time_windows` empty → `ValueError("request.time_windows must be non-empty")`.
- any window missing `weekday` → pydantic "field required" on `TimeWindowConfig.weekday`.
- `target_weekdays` present → `ValueError("target_weekdays has been removed; tag each
  [[request.time_windows]] with a weekday instead (see PERDAY_WINDOWS_PLAN.md §7).")`
- `target_weekday` present → same removal error, naming the new schema.

To detect the removed keys we keep them as **transient, forbidden** fields on
`RequestConfig` (typed `object | None = None` with a `model_validator` that raises if
set) rather than deleting them outright — otherwise pydantic with the default config
silently ignores unknown keys and the operator gets no signal. (Confirm pydantic
`model_config` extra policy in PR1; if `extra="forbid"` is already set globally, an
unknown key already errors and we can drop the sentinel fields — Spike S1.)

### Migrated TOML (all three files, PR2) — SAME current values, per-day

`config/container.toml` and `config/local.toml` and `config/example.toml`:

```toml
# was: target_weekdays = ["saturday", "sunday"] + one untagged window.
# now: one tagged window per wanted day (Sat+Sun, same 08:45–10:00 morning window).
[[request.time_windows]]
weekday  = "saturday"
earliest = 08:45:00
latest   = 10:00:00

[[request.time_windows]]
weekday  = "sunday"
earliest = 08:45:00
latest   = 10:00:00
```

`example.toml` additionally gets a commented illustration of a second same-day
window (the afternoon case) so the per-day-multi feature is discoverable:

```toml
# Multiple windows on one day are allowed — the bot still holds at most ONE
# reservation that day, booked in whichever window yields the best slot.
# Window LIST ORDER is preference (earlier window preferred) — see PERDAY_WINDOWS_PLAN.md Q1.
# [[request.time_windows]]
# weekday  = "sunday"
# earliest = 17:00:00
# latest   = 19:00:00
```

---

## 8. Per-date window scoping (the spine)

### Booking run — `_build_booking_request` (`__main__.py:632-642`)

Already pins `target_dates=(target_date,)` via `dc_replace`. ALSO pin `time_windows`
to that date's weekday's windows:

```python
def _build_booking_request(cfg, *, dry_run, target_date):
    base = _build_request(cfg, dry_run=dry_run)
    windows = _windows_for_date(cfg, target_date)   # tuple[TimeWindow, ...]
    return dc_replace(base, target_dates=(target_date,), time_windows=windows)
```

`_windows_for_date` (new CLI helper, PR3) maps `cfg.request.windows_for(
target_date.weekday())` → `tuple[TimeWindow, ...]` and **asserts non-empty**
(§5 invariant; the booking-day gate guarantees `target_date.weekday()` is wanted, so
this never fires in normal flow — it is a defensive guard).

Confirms reviewer item 3: when `today+offset` is Sunday and Sunday has 2 windows,
the booking request carries BOTH Sunday windows; `rank_slots_for_request` pools both
and books ONE slot (one-per-day). Not one-per-window.

### Watcher — `_check_course` (`core/watch_orchestrator.py:305`)

Currently:
```python
scoped = dc_replace(request, target_dates=(target_date,))
```
Change to ALSO scope windows to the loop date's weekday:
```python
scoped = dc_replace(
    request,
    target_dates=(target_date,),
    time_windows=_windows_for_weekday(request, target_date.weekday()),
)
```
But `_check_course` has only the (already-scoped-per-date) `request` — which under
the current `_watch` flow still carries ALL weekdays' windows (PR3 changes `_watch`
to pass the FULL multi-day request into `check_once`, and `_check_course` does the
per-date narrowing). So the narrowing helper must live where the FULL window set is
available. Two clean options (decide in PR3, lean toward (i)):

- **(i) Narrow in `_check_course`** from `request.time_windows` by reading each
  window's weekday. But `TimeWindow` is weekday-free (§4) — so the watcher's
  `request.time_windows` cannot tell which day a window belongs to. ⇒ This forces the
  weekday onto the request, contradicting §4. **Rejected.**
- **(ii) Pass the per-date scoped request INTO `check_once`.** The CLI `_watch`
  already loops over `target_dates` (`__main__.py:432-433`). Build a per-date scoped
  request there — `dc_replace(request, target_dates=(d,), time_windows=windows_for(d))`
  — and pass THAT to `check_once(scoped_request, d)`. Then `_check_course`'s existing
  `dc_replace(request, target_dates=(target_date,))` is already correct (windows are
  pre-scoped). **CHOSEN — keeps `TimeWindow` weekday-free and the orchestrator
  date-agnostic; all per-day knowledge stays in the CLI config layer.**

So the watcher change is in `__main__._watch`, NOT in `watch_orchestrator.py`:
```python
for target_date in target_dates:
    scoped_request = _scope_request_to_date(request, cfg, target_date)
    result = await watch.check_once(scoped_request, target_date)
```
`_check_course`'s line 305 stays as-is (it re-pins `target_dates`, a harmless no-op
since already pinned, and never needs to touch windows). This is the smallest correct
change and confirms reviewer item 8 (a Saturday check searches only Saturday windows;
a Sunday check only Sunday windows) **by construction at the call site**.

`watch_orchestrator.py:305` gets a one-line comment update noting windows are
pre-scoped by the caller. No code change there.

### Upgrade path — `UpgradeOrchestrator` (reviewer item 9)

`_try_upgrade_slot` (`upgrade_orchestrator.py:269-274`) already `dc_replace`s the
request with `time_windows=(priority_slot.time_window,)` — it uses the priority
slot's OWN window, NOT `request.time_windows`. So the upgrade SEARCH is unaffected
by per-day windows.

The one place it reads `request.time_windows` is the **fallback** priority list
(`_build_priority_list`, `upgrade_orchestrator.py:481-491`) used when
`policy.priority_slots` is empty (the user's CURRENT config — `priority_slots`
omitted). It uses `request.time_windows[0]` as the single fallback window. Because
the watcher now passes a **per-date-scoped request** into `check_once` →
`_try_upgrade` → `maybe_upgrade`, `request.time_windows` here is already narrowed to
THIS date's windows. So `[0]` is this day's first (preferred) window — correct for
the one-per-day upgrade. **No code change needed in `upgrade_orchestrator.py`**,
provided PR3 scopes the request at the watcher call site (it does). Add a regression
test asserting the fallback priority list uses the scoped day's window (PR3).

Caveat to document: with multiple windows on a day, the fallback priority list still
uses only `time_windows[0]` (the preferred window), so the default upgrade policy
only upgrades *within the preferred window's* midpoint ranking. Upgrading across a
day's second window requires explicit `priority_slots`. This is acceptable for v0
(the user runs one window per day today) and is noted in `example.toml` comments.

---

## 9. What does NOT change (per-date independence — reviewer items 6, 7)

- **Store keys / idempotency:** `(RequestId, resolved_date)` unchanged. One-per-day
  means one record per date, exactly as today.
- **`maybe_upgrade` / `check_once` gating:** unchanged (the per-date request is just
  narrower in `time_windows`).
- **Parity test** (`test_container_config_parity.py`): inspects only `*_env` keys
  (`_referenced_env_vars`, lines 50-72). `weekday` is not an `*_env` key →
  **zero parity impact**. Confirmed by reading the test. `compute.bicep` is
  untouched (no env-var or schedule change). PR2 adds one assertion to the parity
  test pinning "every container window has a weekday" so a future un-tagged edit is
  caught (optional hardening, not required for correctness).
- **Killswitch / bicep / crons:** this feature is CONFIG + Python only. No job names,
  no cron expressions, no ACA schedule changes → **killswitch untouched, no bicep
  change.** Confirmed.

---

## 10. PR-by-PR sequence

Five PRs. PR1→PR2→PR3 are the core (sequential — each depends on the prior). PR4 is
gated on Q1 and OPTIONAL/deferrable. PR0 is a tiny spike.

### PR0 (Spike S1): pydantic extra-keys policy

- **Question:** Does the repo's pydantic models use `extra="forbid"`? If yes, an
  unknown `target_weekdays` key already errors and PR1 can skip the sentinel-field
  approach.
- **Exit criterion:** grep `model_config` / `ConfigDict` in `core/config.py` and
  base classes; one-line answer recorded in PR1's description. No code.
- **Files:** none (investigation).

### PR1 — Schema: `weekday` on `TimeWindowConfig`, derive wanted-days, remove `target_weekdays`

- **Scope:** config layer only. Add `weekday` to `TimeWindowConfig`; add
  `windows_for`; rewrite `wanted_weekday_indices` to read from windows; remove
  `target_weekdays`/`target_weekday` with a clear migration error; update the
  fingerprint to encode weekday.
- **Files:**
  - `src/teetime/core/config.py` (edit — see §11 stub signatures)
  - `src/teetime/core/models.py` (edit — `build_request_fingerprint` signature; §11)
  - `src/teetime/__main__.py` (edit — `_build_request` passes `(weekday, window)`
    pairs to the fingerprint; line 600/611-616)
- **Red tests (write FIRST, exact names → file `tests/test_config.py` +
  `tests/test_request_id.py`):**
  - `test_time_window_requires_weekday` — TOML window block lacking `weekday` →
    pydantic ValidationError mentioning `weekday`. Input: `{earliest, latest}` only.
  - `test_time_window_rejects_bad_weekday` — `weekday="funday"` → ValueError from
    `weekday_from_name`. Expected: message lists valid names.
  - `test_wanted_weekday_indices_derived_from_windows` — windows tagged sat+sun →
    `wanted_weekday_indices == frozenset({5, 6})`. A sun-only config → `{6}`.
  - `test_windows_for_returns_only_that_weekday` — config with sat 08:45-10:00,
    sun 09:00-10:00, sun 17:00-19:00 → `windows_for(6)` returns the TWO sunday
    windows in (earliest) order; `windows_for(5)` returns the one saturday window;
    `windows_for(0)` (monday) returns `()`.
  - `test_target_weekdays_key_is_rejected` — TOML with `target_weekdays=[…]` →
    ValueError naming the removal + the new schema. Same for `target_weekday`.
  - `test_empty_time_windows_rejected` — `time_windows=[]` → ValueError.
  - `test_windows_normalised_by_weekday_then_earliest` — out-of-order rows →
    `windows_for`/iteration order is `(weekday, earliest)` ascending.
  - `test_fingerprint_includes_window_weekday` (`test_request_id.py`) — two configs
    identical except one window's weekday (sat vs sun) → DIFFERENT RequestIds.
  - `test_fingerprint_stable_same_weekday_diff_date` — already true (dates excluded),
    re-assert with the new token shape: same windows, two run dates → SAME RequestId.
- **Stub signatures:** §11 (edits shown as exact new signatures, NOT applied —
  follow-up agent writes bodies test-first).
- **Doc updates:** `CLAUDE.md` (idempotency-key note: window token now encodes
  weekday); `src/teetime/courses/CLAUDE.md:45-46` (window-per-day note); this plan
  marked PR1-done.
- **CI/parity impact:** none new. Existing `test_config.py` cases that build configs
  with untagged windows MUST be migrated to tagged windows in this PR (they will go
  red otherwise — that is expected and part of the cutover). Enumerate them: any
  `time_windows` dict literal in `tests/` (e.g. `test_multi_course.py:70,168,198,
  230,275`, `test_watch_cli.py:61`) — add `weekday`.
- **Merge-order:** first. Blocks PR2, PR3.

### PR2 — Migrate the committed TOML configs

- **Scope:** rewrite the three configs to tagged per-day windows (same values).
- **Files:** `config/container.toml`, `config/local.toml`, `config/example.toml`
  (edits per §7), `tests/test_container_config_parity.py` (optional: add
  "every container window has a weekday" assertion).
- **Red tests:**
  - `test_container_config_windows_all_tagged` (in `test_container_config_parity.py`)
    — every `[[request.time_windows]]` in container.toml has a `weekday`. Input:
    parsed container.toml. Expected: assertion passes only after migration.
  - `test_container_and_example_wanted_weekdays_match` (optional) — derived
    `wanted_weekday_indices` for example.toml == container.toml (both {5,6}).
- **Stub signatures:** none (config + test only).
- **Doc updates:** none beyond plan status.
- **CI/parity impact:** the existing parity tests still pass (no `*_env` change). The
  new assertions go green after migration.
- **Merge-order:** after PR1 (configs would fail to load under PR1's validator until
  migrated — so PR1+PR2 could even be ONE PR; keeping them split keeps the schema
  change reviewable separately, but they MUST land together / PR2 immediately after).
  **Note:** because PR1's validator rejects the OLD committed configs, CI on PR1
  alone would fail any test that loads `container.toml`/`example.toml`. Therefore
  **PR1 and PR2 must be merged together (one PR, or PR2 stacked and merged in the
  same batch).** Recommend a single combined PR1+PR2 to keep `main` green. Flagged.

### PR3 — Per-date window scoping (booking run + watcher + upgrade fallback)

- **Scope:** wire the scoped windows into each invocation. No schema change.
- **Files:**
  - `src/teetime/__main__.py` (edit `_build_booking_request` §8; edit `_watch` loop
    to pass a per-date-scoped request; add `_windows_for_date`/`_scope_request_to_date`
    helpers — §11 stubs)
  - `src/teetime/core/watch_orchestrator.py` (comment-only update at line 305 noting
    windows are pre-scoped by the caller; no logic change)
- **Red tests (`tests/test_cli.py`, `tests/test_watch_cli.py`,
  `tests/test_watch_orchestrator.py`):**
  - `test_booking_request_scopes_windows_to_target_weekday` — cfg with sat
    08:00-09:00 and sun 17:00-18:00; `_build_booking_request(target_date=<a Sunday>)`
    → `request.time_windows == (TimeWindow(17:00,18:00),)`. A Saturday target →
    `(TimeWindow(08:00,09:00),)`.
  - `test_booking_request_includes_all_same_day_windows` — sun has TWO windows →
    `_build_booking_request(<Sunday>)` carries BOTH (one-per-day proven downstream).
  - `test_watch_passes_per_date_windows` — `_watch` over [Sat, Sun] calls
    `check_once` with a Saturday request whose windows are only Saturday's, and a
    Sunday request whose windows are only Sunday's. (Spy on `check_once`.)
  - `test_check_course_searches_only_loop_date_windows` (watch_orchestrator) — feed a
    pre-scoped Sunday request + Saturday slots in the adapter → no candidates; Sunday
    slots within the Sunday window → booked. Confirms reviewer item 8.
  - `test_upgrade_fallback_uses_scoped_day_window` (upgrade_orchestrator) — policy
    with empty `priority_slots`, scoped request carrying a single Sunday window →
    `_build_priority_list` fallback uses that window (not some other day's). Confirms
    reviewer item 9.
  - `test_scope_request_to_windowless_date_raises` — defensive: scoping to a Monday
    (no windows) raises AssertionError/ValueError (never reached in normal flow).
- **Stub signatures:** §11.
- **Doc updates:** `CLAUDE.md` (per-date scoping note — extend the existing
  "Target date anchors…" / watcher-scoping bullets to mention window scoping);
  `watch_orchestrator.py:40` docstring (target-date resolution) reference.
- **CI/parity impact:** none.
- **Merge-order:** after PR1+PR2.

### PR4 (OPTIONAL, gated on Q1) — Window-list-order = priority ranking

- **Scope:** change `rank_slots_for_request` so an earlier-listed window is preferred
  over a later-listed window, regardless of midpoint distance; within a window, keep
  the midpoint-distance sort. ONLY if the user picks the "list order = priority"
  answer to Q1. NO-OP for the current single-window-per-day config.
- **Files:** `src/teetime/core/slot_utils.py` (edit `rank_slots_for_request` /
  `_matching_window` to return the window INDEX too; sort key becomes
  `(window_index, midpoint_distance, tee_time)`).
- **Red tests (`tests/test_sort_priority.py`):**
  - `test_earlier_window_preferred_over_closer_late_window` — windows [09:00-10:00,
    17:00-19:00]; slots 09:50 and 18:00 → returns 09:50 FIRST (window-0 beats
    window-1 despite 18:00 being dead-centre). This is the §2 scenario.
  - `test_single_window_ranking_unchanged` — one window, several slots → identical
    order to current (regression guard that PR4 doesn't change today's behaviour).
  - `test_within_window_still_midpoint_sorted` — two slots in the SAME (first) window
    → midpoint-distance order preserved.
- **Stub signatures:** §11.
- **Doc updates:** `slot_utils.py` module docstring (sort key now window-index-first);
  `CLAUDE.md` slot-ranking note; `example.toml` comment ("list order = preference").
- **CI/parity impact:** none.
- **Merge-order:** last, and only after Q1 is answered "list order = priority". If
  the user picks "pooled midpoint", PR4 is dropped entirely and §2's status quo
  stands (document the chosen behaviour either way).

---

## 11. Stub signatures (NOT applied — follow-up agents implement test-first)

All edits to EXISTING files are shown as target signatures, NOT written to disk
(per architect rules: no real implementations, and don't pre-edit existing impls).
A genuinely NEW pure helper module stub is written to disk (see "Files created").

### `core/config.py` (edits)

```python
class TimeWindowConfig(BaseModel):
    """One acceptable tee-off range on a specific weekday.
    Times in 24h HH:MM, course-local. `weekday` binds this window to one day;
    multiple windows may share a weekday (one-per-day still applies — best wins).
    """
    weekday: str          # validated via weekday_from_name; required (hard cutover)
    earliest: time
    latest: time
    # @model_validator: weekday_from_name(weekday) must succeed; earliest <= latest.

class RequestConfig(BaseModel):
    target_offsets: list[int]
    time_windows: list[TimeWindowConfig]   # non-empty; each carries a weekday
    players: list[PlayerConfig]
    holes: int = 18
    max_price_per_player: Decimal | None = None
    cart: CartPreference = CartPreference.EITHER
    course_preferences: list[str]
    # REMOVED: target_weekdays, target_weekday. Migration sentinels (see §7):
    #   detect-and-reject if present in the input.

    @model_validator(mode="after")
    def _validate_windows(self) -> RequestConfig:
        """Reject removed keys with a migration error; require non-empty windows;
        validate each weekday; normalise window order by (weekday, earliest).
        Raises NotImplementedError until implemented (PR1)."""
        raise NotImplementedError("PERDAY_WINDOWS_PLAN.md PR1")

    @property
    def wanted_weekday_indices(self) -> frozenset[int]:
        """Derived from the distinct weekdays present in time_windows."""
        raise NotImplementedError("PERDAY_WINDOWS_PLAN.md PR1")

    def windows_for(self, weekday: int) -> tuple[TimeWindowConfig, ...]:
        """The configured windows whose weekday index == `weekday`, in
        (earliest) order. () if none (callers never pass a windowless weekday)."""
        raise NotImplementedError("PERDAY_WINDOWS_PLAN.md PR1")
```

### `core/models.py` (edit)

```python
def build_request_fingerprint(
    *,
    course_ids: list[CourseId],
    target_offsets: list[int],
    time_windows: Sequence[tuple[int, TimeWindow]],   # (weekday_index, window)
    players: list[Player],
) -> str:
    """...windows segment token: '<weekday>:HH:MM-HH:MM', sorted lexically.
    Weekday in the token so a sat-vs-sun window is a distinct request identity.
    See PERDAY_WINDOWS_PLAN.md §6."""
    raise NotImplementedError("PERDAY_WINDOWS_PLAN.md PR1")
```
(`TimeWindow` itself is UNCHANGED — weekday lives in config + the fingerprint pair,
not in the domain `TimeWindow`. §4.)

### `__main__.py` (edits / new helpers)

```python
def _windows_for_date(cfg: AppConfig, target_date: date) -> tuple[TimeWindow, ...]:
    """Domain TimeWindows for target_date's weekday. ASSERTS non-empty (§5)."""
    raise NotImplementedError("PERDAY_WINDOWS_PLAN.md PR3")

def _scope_request_to_date(
    request: BookingRequest, cfg: AppConfig, target_date: date
) -> BookingRequest:
    """dc_replace(request, target_dates=(d,), time_windows=_windows_for_date(...)).
    Used by _watch to hand check_once a per-date-scoped request."""
    raise NotImplementedError("PERDAY_WINDOWS_PLAN.md PR3")

# _build_booking_request: dc_replace(base, target_dates=(target_date,),
#                                    time_windows=_windows_for_date(cfg, target_date))
# _build_request: build the (weekday_index, TimeWindow) pairs for the fingerprint
#   from cfg.request.time_windows; request.time_windows stays the full flat tuple
#   (PR3 scoping narrows it per invocation).
```

### `core/slot_utils.py` (edit, PR4 only, gated on Q1)

```python
def rank_slots_for_request(slots, request):
    """...sort key (window-list-order = priority): (window_index, midpoint_distance,
    tee_time). Earlier-listed window always preferred. See PERDAY_WINDOWS_PLAN.md
    §2 / Q1 / PR4."""
    ...
def _matching_window(slot, request) -> tuple[int, TimeWindow] | None:
    """Return (index, window) of the first containing window, or None."""
    ...
```

---

## 12. Docs-to-update checklist

| Doc | Update | PR |
|-----|--------|----|
| `CLAUDE.md` | idempotency-key note (window token now encodes weekday); per-date window scoping in booking/watch; remove `target_weekdays` mentions | PR1, PR3 |
| `src/teetime/courses/CLAUDE.md:45-46` | "books wanted morning days" + "time window 08:45–10:00" → per-day tagged windows; multi-window-per-day note | PR1 |
| `config/example.toml` | tagged windows + commented second-same-day-window example | PR2 |
| `MULTIDAY_PLAN.md` | cross-reference: `target_weekdays` superseded by per-window weekday | PR1 |
| `PLAN.md` §13 (fingerprint) | window token shape | PR1 |
| `README.md` | if it documents `target_weekdays` / window config | PR1/PR2 |
| this plan | mark PRs done | each |

No `infra/AZURE_PLAN.md` change (no infra impact).

---

## 13. OPEN QUESTIONS

**Q1 (the disjoint-window ranking — REQUIRES user decision before PR4).**
With multiple windows on one day, how should the single best slot be chosen across
disjoint windows? See §2 for the worked example (morning 09:50 vs afternoon 18:00).
- **(a) Window list order = priority** (RECOMMENDED): prefer ANY slot in an
  earlier-listed window; midpoint-distance within the chosen window. → 09:50 wins.
  Requires PR4. NO-OP for the current single-window-per-day config.
- **(b) Pooled closest-to-any-window-midpoint** (status quo): 18:00 wins. No code
  change. Simpler but arguably surprising.
This only matters once a second same-day window is actually added; the user keeps one
window per day today, so either answer is behaviourally identical NOW. PR4 is
deferrable until the user adds an afternoon window.

**Q2 (watcher `--date` for a windowless weekday).** `teetime watch --date YYYY-MM-DD`
can name a date whose weekday has no windows. Current plan: `_windows_for_date`
asserts non-empty → the watch raises a clear error for that date. Confirm the user
wants a hard error (recommended) vs a silent skip.

**Q3 (combined PR1+PR2 vs split).** PR1's validator rejects the OLD committed
configs, so PR1 alone reddens CI. Recommend merging PR1+PR2 as ONE PR to keep `main`
green. Confirm acceptable (vs a stacked-branch dance).

---

## 14. Parallel-execution note

- **PR0 (S1)** — independent, do first/concurrently (investigation only).
- **PR1+PR2** — the schema + config migration; must land together; single agent.
- **PR3** — depends on PR1 (needs `windows_for`); single agent after PR1+PR2 merge.
- **PR4** — depends on PR1 (request shape) and Q1's answer; independent of PR3's
  call-site wiring (touches only `slot_utils`), so PR3 and PR4 can be written in
  PARALLEL once PR1+PR2 are merged and Q1 is answered. They touch disjoint files
  (`__main__.py`/`watch_orchestrator.py` vs `slot_utils.py`) — no merge conflict.

File ownership: PR1 owns `config.py` + `models.py` fingerprint; PR3 owns
`__main__.py` + the `watch_orchestrator.py` comment; PR4 owns `slot_utils.py`. No
two parallel PRs write the same file.
