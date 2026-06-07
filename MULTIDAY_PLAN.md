# MULTIDAY_PLAN — Saturday + Sunday booking re-architecture

> **Status: SHIPPED (PRs #70–#75), then partly SUPERSEDED.** The Sat+Sun daily-cron
> re-architecture described here is fully implemented and on `main`. The `target_weekday(s)`
> config scheme this plan introduces was itself subsequently **superseded by per-day time
> windows** (`PERDAY_WINDOWS_PLAN.md`, #76/#77): wanted days are now **derived from the
> `[[request.time_windows]]` weekdays**, not a separate `target_weekdays` list, and the
> `target_weekday`/`target_weekdays` keys were removed. Read the design below as historical —
> the root `CLAUDE.md` is the authoritative description of current behavior.

Architect plan to extend the bot from "one managed reservation on the target Sunday" to
"one managed reservation **per wanted morning day**" — the upcoming Saturday AND the
upcoming Sunday, same morning window for each — by replacing the booking-weekday-specific
cron design with **daily crons that self-gate on whether `today + offset` is a wanted
booking day**.

This re-opens the M6_PLAN.md Sunday-only decision (`50 9/10 * * 0`). M6's `--wait`,
`core/dst_gate.py`, watcher-enable, target-date anchoring, and the merged PRs #67
(book-POST 400 → `SlotGoneError` + multi-slot fallback) and #68 (`Orchestrator.prefetch_book`
two-phase busy-wait, `SchedulerConfig.captcha_prefetch_lead_s`) all **carry over unchanged**.

> Scope guard: this plan does NOT implement M2.T3 reconciliation, per-day DISTINCT time
> windows (same window for all wanted days in v0), durable storage, or email. Those are
> settled out of scope.

---

## 1. Executive summary

### 1.1 What changes

| # | Change | Why |
|---|--------|-----|
| 1 | Booking crons fire **daily** (`50 9 * * *` + `50 10 * * *`), not Sunday-only (`… * * 0`). Still exactly 2 booking jobs. | A daily cron + a self-gate is how we book multiple weekdays without adding a cron per weekday. Adding Sat crons the OLD way → 4 booking jobs → 10 total → breaks the killswitch's hardcoded 6 (§9). |
| 2 | New pure gate `core/booking_day_gate.py::should_book_today` runs on the `--wait` path, AFTER `dst_gate` and BEFORE the busy-wait. Computes `today + offset` (course-local) and exits 0 if its weekday ∉ wanted set. | 5/7 mornings the daily cron lands on a non-booking day and must fast-exit (~2 s, sub-cent) without auth/search/busy-wait. |
| 3 | Config: `request.target_weekday: str` → `request.target_weekdays: list[str]` (default `["saturday", "sunday"]`), with a backward-compat alias accepting the old singular key. | The wanted set drives both the booking gate and the watcher's multi-date list. |
| 4 | `core/target_date.py`: add `next_occurrences_within_horizon(today, wanted, horizon)` returning the next upcoming occurrence of EACH wanted weekday ≤ horizon days out (incl. today if today is wanted). Booking-job target math is now the gate's `today + offset` (single date). | The watcher must check next-Sat AND next-Sun; the booking job books exactly one date per run. |
| 5 | Watcher loops `check_once` over the multi-date list (`__main__._watch`), **passing each iteration a per-date-scoped request** (`dc_replace(request, target_dates=(target_date,))`). | One independent per-date watch + upgrade per wanted day, searching ONLY that date. |
| 6 | `WatchOrchestrator.check_once` scopes its OWN search to `target_date` (`dc_replace` before `adapter.search`) — a structural guarantee, not just a caller convention. | **Must-fix 1**: net-new watcher bookings are restricted to slots whose date == the loop's `target_date`. A Saturday watch must NEVER book a Sunday slot (confirmed user decision). |
| 7 | `WatchOrchestrator` drops the time-of-day **polling-hours gate** (`_is_outside_polling_hours`); it searches on EVERY cron invocation. The `_is_past_watch_deadline` gate is KEPT. | **New user requirement**: the 6:10 AM watch run skipped its search (`polling_start_hour=7`) → zero visibility into the 6 AM drop + early-morning cancellations. The bot must poll every run. |
| 8 | `compute.bicep`: change the 2 booking crons from `* * 0` to `* * *` (in-place cron edit, same job names). `bookingJobs` stays length-2. | Keeps job count at 6 — the killswitch list/names are unchanged (no churn there). |

### 1.2 What does NOT change (verified in current code)

- **`Orchestrator.run` already books exactly one date** — `resolved_date = request.target_dates[0]`
  (`core/orchestrator.py:78`). The booking job will pass a **single-element** `target_dates`
  (the gated `today + offset`). No change to the BOOKING `Orchestrator`. (The separate
  `WatchOrchestrator` gets the must-fix-1 search-scoping change in PR4 — see §1.1 row 6.)
- **RequestId is already weekday-independent.** `build_request_fingerprint`
  (`core/models.py:43-70`) hashes `course_ids | target_offsets | time_windows | party` —
  **weekday is not in the fingerprint**. So `target_weekday` str → `target_weekdays` set
  does NOT change the RequestId. The idempotency key is `(RequestId, resolved_date)`
  (`orchestrator.py:81`, `watch_orchestrator.py:208`, `upgrade_orchestrator.py:408-409`),
  so Sat and Sun get distinct **store rows** from one stable RequestId. **No models.py change.**
  **Caveat (reviewer N3 / must-fix 1):** distinct store ROWS is a property of store-keying
  alone. It does NOT by itself stop the watcher from BOOKING a Sun slot under the Sat row —
  that needs the SEARCH to be date-scoped too. Today `_check_course` calls
  `adapter.search(request)` with the unscoped request (`watch_orchestrator.py:306`); both
  adapters iterate `request.target_dates` (`foreup/base.py:308`, `teeitup/base.py:313`) and
  `rank_slots_for_request` does NOT filter by date (`slot_utils.py:43-58`). **PR4 closes this
  gap** (per-date `dc_replace` before `search`). Distinct rows ≠ correct net-new bookings
  until PR4 lands. This is the must-fix-1 scoping fix, not blanket coverage.
- **One-booking-per-DAY already holds.** Both `WatchOrchestrator.check_once`
  (`watch_orchestrator.py:174,208`) and `UpgradeOrchestrator.maybe_upgrade`
  (`upgrade_orchestrator.py:155,211,408`) are parameterised by `target_date` and key the
  store on `(request_id, target_date)`. There is **no global "already booked" short-circuit**
  — every gate is per-date. A Sat booking cannot block/cancel a Sun booking. **No
  orchestrator/upgrade change** (see §6 for the file:line proof).
- PRs #67 / #68 — untouched.

### 1.3 Headline

Two new pure modules' worth of logic (a booking-day gate + a watcher horizon helper), a
config set-field, a daily-cron edit, and a watcher loop with per-date search scoping. The
upgrade path, the models, and the killswitch are untouched. The ONE behavioural change
inside `WatchOrchestrator` is **must-fix 1**: `check_once` (or its caller) must `dc_replace`
the request to a single-element `target_dates` BEFORE `adapter.search`, so a multi-date watch
loop reusing one RequestId never searches/books the wrong date. The upgrade path already does
this (`upgrade_orchestrator.py:269-274`); the net-new `_check_course` search path did not —
PR4 fixes it. The other `WatchOrchestrator` change is removing the polling-hours gate so the
watcher polls every run (new user requirement). The heavy invariants (per-date store keying,
weekday-independent RequestId, single-date booking) are already in place; this plan wires the
multi-day SHAPE on top of them.

---

## 2. PR-by-PR sequence (ordered)

Six PRs. The strict chain is **PR1 (config + `_build_request`, atomic) → PR2 (booking gate)
→ PR3 (target_date helper) → PR4 (watcher loop + per-date search scoping + poll-every-run)**.
PR5 (bicep) and PR6 (docs/verification) are independent of the code chain but PR5 touches
`compute.bicep` (parity-checked) so it serializes against any other bicep edit.

```
PR1 (config set-field + _build_request migration, ATOMIC) ─► PR2 (booking_day_gate + _run wiring) ─┐
                                                          └► PR3 (target_date horizon helper) ──────┴► PR4 (watcher: multi-date loop + per-date scope + poll-every-run)
PR5 (bicep daily crons) ── independent (own merge slot; touches compute.bicep)
PR6 (docs + verification) ── last
```

- **PR1 is ATOMIC (must-fix 2).** It changes `RequestConfig.target_weekday: str` →
  `target_weekdays: list[str]` AND, in the SAME PR, updates `__main__._build_request`
  (`:540-587`) which today calls `weekday_from_name(cfg.request.target_weekday)` (`:550`) —
  a single str. After the rename `target_weekday` defaults to `None`, so
  `weekday_from_name(None)` would raise at startup of BOTH `run` and `watch`, reddening the
  whole suite. PR1 therefore cannot ship the schema change without the `_build_request` edit.
  See "**must main never be red between PRs**" in PR1 for the exact interim `target_dates`.
- **PR2 depends on PR1** (gate reads `wanted_weekday_indices` from config).
- **PR3 depends on PR1** (helper reads the wanted set) but is independent of PR2.
- **PR4 depends on PR1 + PR3** (loops over the helper output, passing a per-date-scoped
  request) and ALSO removes the polling-hours gate (folded in — same file, same concern:
  "what does each watch invocation actually search").
- **PR5 is independent** but must not collide with a concurrent `compute.bicep` edit; it
  also bumps `tests/test_compute_bicep_schedule.py`, so it owns those test files outright.
- **PR6 is docs + verification tests**, lands last.

Parallel-execution note: PR1 must land first (atomically); then PR2 and PR3 can be implemented
in parallel by two agents (different files: `booking_day_gate.py`+`__main__._run` vs
`target_date.py`). PR5 can be implemented at any time in parallel with the code chain (it
only touches bicep + bicep tests). PR4 is the join point and must land after PR3.

---

### PR1 — Config: `target_weekday` (str) → `target_weekdays` (set), backward-compat alias

**Scope.** Replace the single weekday with a set of wanted booking weekdays. Keep the
old singular key parsing as a deprecated alias so existing configs and any in-flight prod
config don't break. Default to `["saturday", "sunday"]`. **This PR is ATOMIC (must-fix 2):
it also migrates `__main__._build_request` in the SAME PR**, because that function is the
only caller of the field being renamed and would otherwise crash both `run` and `watch` at
startup (`weekday_from_name(None)`), reddening `main`. PR1 must leave a green tree.

**Files touched.**
- `src/teetime/core/config.py` — `RequestConfig`: new `target_weekdays`, alias handling,
  validator.
- `src/teetime/__main__.py` — `_build_request` (`:540-587`): stop calling
  `weekday_from_name(cfg.request.target_weekday)` (`:550`, which no longer exists as a single
  str). See "**must-fix 2: `_build_request` interim contract**" below for the exact target
  computation PR1 ships.
- `config/container.toml`, `config/local.toml`, `config/example.toml` — switch the key.
- `tests/test_config.py`, `tests/test_cli.py` — red tests below.

**Exact schema change (`core/config.py`, `RequestConfig`).**

Current (`config.py:63-83`):
```python
class RequestConfig(BaseModel):
    ...
    course_preferences: list[str]
    target_weekday: str = "sunday"

    @field_validator("target_weekday")
    @classmethod
    def _validate_target_weekday(cls, v: str) -> str:
        weekday_from_name(v)
        return v
```

New:
```python
class RequestConfig(BaseModel):
    ...
    course_preferences: list[str]
    # Wanted booking weekdays. The booking job books `today + offset` ONLY when that
    # date's weekday is in this set (core/booking_day_gate.py); the watcher checks the
    # next upcoming occurrence of EACH of these within the horizon (core/target_date.py).
    # Default Sat+Sun. Stored as the raw list; helpers convert to weekday indices.
    target_weekdays: list[str] = Field(default_factory=lambda: ["saturday", "sunday"])
    # Deprecated alias: the old singular key. If present and target_weekdays was not
    # explicitly given, it seeds the set. Never both.
    target_weekday: str | None = None

    @model_validator(mode="after")
    def _resolve_weekdays(self) -> RequestConfig:
        # Alias migration: old singular `target_weekday` → singleton set.
        # `model_fields_set` holds the keys actually present in the input TOML, so we can
        # distinguish "user gave target_weekdays" from "default applied" (S1 fix — the
        # earlier `if "target_weekdays" was explicitly set:` pseudocode does not compile).
        fields_set = self.model_fields_set
        if "target_weekday" in fields_set and "target_weekdays" in fields_set:
            raise ValueError(
                "set either target_weekday (deprecated) or target_weekdays, not both"
            )
        if self.target_weekday is not None:
            self.target_weekdays = [self.target_weekday]
        if not self.target_weekdays:
            raise ValueError("target_weekdays must be non-empty")
        for name in self.target_weekdays:
            weekday_from_name(name)   # raises ValueError on a bad name
        # Normalise: dedupe + sort by weekday index for a deterministic order.
        seen = sorted({weekday_from_name(n): n.strip().lower() for n in self.target_weekdays}.items())
        self.target_weekdays = [name for _, name in seen]
        return self

    @property
    def wanted_weekday_indices(self) -> frozenset[int]:
        """Python weekday() indices (Mon=0..Sun=6) of the wanted set."""
        return frozenset(weekday_from_name(n) for n in self.target_weekdays)
```

Notes for the implementer:
- The both-keys check uses `self.model_fields_set` (pydantic v2) — the set of keys actually
  present in the input — inside `model_validator(mode="after")`. Raise iff both keys are in
  it. This is the S1-fixed, compiling form shown above.
- Replace `field_validator` with `model_validator` on the existing import line
  (`config.py:15` is `from pydantic import BaseModel, Field, field_validator`) →
  `from pydantic import BaseModel, Field, model_validator` (drop `field_validator` if no
  other field uses it — `_validate_target_weekday` is the only one in this file; confirm by
  grep before removing the import to keep `ruff` clean).
- `wanted_weekday_indices` returns a **frozenset** to match the gate's signature and to
  be order-independent (the validator already sorts the list form for display).

**Determinism / RequestId (reviewer item 5).** The RequestId fingerprint
(`models.py:43-70`) does **not** include the weekday — only `target_offsets`. So this
schema change cannot alter the RequestId; `derive_request_id` keeps producing **one stable
id per run**. The set is sorted by weekday index in the validator purely for legible
logging/config display, not for the fingerprint. Confirm with a test (below).

**must-fix 2: `_build_request` interim contract (the load-bearing atomic edit).** Today
`_build_request` (`__main__.py:540-587`) computes
`target_dates = resolve_target_dates(today, offsets, weekday_from_name(cfg.request.target_weekday))`
(`:547-551`). After the rename, `cfg.request.target_weekday` is `None` by default, so
`weekday_from_name(None)` raises `AttributeError`/`ValueError` at startup of BOTH `run`
(`_run:198`) and `watch` (`_watch:354`) — the whole suite goes red. PR1 MUST replace this in
the same PR. **Exact interim PR1 behaviour** (stable, non-crashing, deterministic; the booking
job and watcher each override it in later PRs):

```python
# __main__._build_request, PR1 interim — anchor on the EARLIEST wanted weekday so the
# existing single-target_dates contract holds and nothing crashes. PR2 overrides this for
# the booking job (single gated date); PR4 overrides it for the watcher (multi-date list).
anchor_weekday = min(cfg.request.wanted_weekday_indices)   # frozenset -> a real index
target_dates = resolve_target_dates(today, cfg.request.target_offsets, anchor_weekday)
```

Rationale for `min(wanted_weekday_indices)` as the PR1 interim anchor:
- It is a single, deterministic weekday index (no `None`), so `resolve_target_dates` works
  unchanged and `target_dates` stays the historical 1-tuple `(anchor + offset,)`.
- It does NOT need to be "correct" for multi-day — PR2 (booking) and PR4 (watch) each compute
  their own dates and stop relying on `_build_request`'s `target_dates`. The interim only has
  to be non-crashing and stable so `main` is green between PRs.
- With the default Sat+Sun set, `min({5,6}) == 5` (Saturday). After PR1-only, both `run` and
  `watch` would target the upcoming Saturday + offset. That is acceptable for an interim
  commit (the gate/loop land in PR2/PR4 before any behavioural reliance); it is NOT the final
  behaviour. State this explicitly in the PR1 description so a reviewer doesn't mistake the
  interim for the spec.
- Do NOT introduce a second weekday literal here — `min(wanted_weekday_indices)` derives from
  config, consistent with the "single source of truth" rule.

**Red test for the interim (`tests/test_cli.py`).**
- `test_build_request_after_rename_does_not_crash` — load a config with default
  `target_weekdays` (Sat+Sun), call `_build_request`; assert it returns a `BookingRequest`
  with a non-empty `target_dates` and does NOT raise. (Guards must-fix 2 — `main` stays green.)
- `test_build_request_interim_anchors_min_weekday` — with `target_weekdays=["sunday"]`,
  `_build_request`'s `target_dates[0].weekday() == 6`; with `["saturday","sunday"]`,
  `target_dates[0].weekday() == 5` (min of the set). Pins the documented interim so PR2/PR4
  changing it is a visible, intentional edit.

**Red tests first (`tests/test_config.py`).**
- `test_target_weekdays_default_is_sat_sun` — load a config omitting both keys; assert
  `cfg.request.target_weekdays == ["saturday", "sunday"]` and
  `cfg.request.wanted_weekday_indices == frozenset({5, 6})`.
- `test_target_weekdays_explicit_list_parses` — `target_weekdays = ["sunday"]` →
  `wanted_weekday_indices == frozenset({6})`.
- `test_target_weekday_singular_alias_migrates` — old `target_weekday = "sunday"`, no
  `target_weekdays` → `target_weekdays == ["sunday"]`, indices `{6}`. (Backward-compat.)
- `test_both_weekday_keys_raises` — both `target_weekday` and `target_weekdays` present →
  `ValueError` mentioning "not both".
- `test_empty_target_weekdays_raises` — `target_weekdays = []` → `ValueError`.
- `test_invalid_weekday_name_raises` — `target_weekdays = ["someday"]` →
  `ValueError(match="invalid weekday")`.
- `test_target_weekdays_dedupe_and_sort` — `["sunday", "saturday", "sunday"]` →
  `["saturday", "sunday"]` (deduped, index-sorted).
- `test_request_id_unchanged_by_weekday_set` — build two `BookingRequest`s via
  `_build_request` from configs identical except `target_weekdays` (`["sunday"]` vs
  `["saturday","sunday"]`); assert `request.request_id` is **equal** (proves the weekday
  set is not in the fingerprint). This is the reviewer-item-5 guard.

**Stub signatures.** None new beyond the `RequestConfig` edit above (shown as a diff, not
edited on disk — `config.py` is an existing real implementation).

**Doc updates.** `config/example.toml` (replace the `target_weekday` block with
`target_weekdays` + an alias note), root `CLAUDE.md` (the "Target date anchors to
`target_weekday`" invariant → "to the wanted `target_weekdays` set"), PLAN.md §13.1
(note the weekday set is excluded from the fingerprint, same as before).

**CI/parity (reviewer item 7).** `target_weekdays` is **not** a `*_env` reference and is
**not** a field `test_container_config_parity.py` inspects (`_referenced_env_vars` only
collects `*_env` keys; `_bicep_env_var_names` only collects UPPER_SNAKE env names). So the
parity test is unaffected by this rename — confirmed by reading the test
(`tests/test_container_config_parity.py:50-78`). It compares container↔bicep env wiring
only; weekday config lives only in TOML, never in `compute.bicep`. No parity test change.

---

### PR2 — Booking-day gate (`core/booking_day_gate.py`) + `_run` wiring

**Scope.** Add the pure gate that decides whether the daily-firing booking cron should
proceed to book `today + offset`. Wire it into `__main__._run` on the `--wait` path,
**after** `dst_gate.should_proceed` and **before** the busy-wait. On skip: log a clear
"not a booking day" line and exit 0. The stub already exists at
`src/teetime/core/booking_day_gate.py` (this PR fills it in test-first).

**Files touched.**
- `src/teetime/core/booking_day_gate.py` — implement `should_book_today` (stub on disk).
- `src/teetime/__main__.py` — `_run`: call the gate; also change `_build_request` to pass
  the booking job the **single** gated target date (see "_build_request interaction" below).
- `tests/test_booking_day_gate.py` — NEW; full FakeClock matrix.
- `tests/test_cli.py` — `_run` ordering + skip-exit tests.

**Predicate (exact).**
```
proceed  ⇔  (clock.now_utc().astimezone(ZoneInfo(timezone)).date()
             + timedelta(days=target_offset)).weekday()  ∈  wanted_weekdays
```
`today` is read in the **course-local timezone** (`America/New_York`) — the same zone the
bot books in — so the weekday tested is the weekday actually booked (reviewer item 1).

**Ordering & combined truth table (reviewer items 1, 8).** In `_run`, on the `--wait`
path only:

```
1. dst_gate.should_proceed(clock, tz, fire_time)   # season check (existing, unchanged)
      False -> log "DST-half gate: wrong-season cron" + return (exit 0)
2. booking_day_gate.should_book_today(clock, tz, offset, wanted)   # NEW
      False -> log "booking-day gate: today+%d is %s, not a wanted booking day — exit 0"
               + return (exit 0)
3. busy_wait + book the single gated date
```

`dst_gate` runs first so the booking-day decision is only evaluated at the correct ET wall
clock (~05:50 ET, hour == fire_time.hour-1). Why the order is safe either way:

| Cron season | DST gate | ET land time | `today+7` weekday | booking-day gate | Net |
|---|---|---|---|---|---|
| Correct (EDT cron, EDT season) | proceed | 05:50 ET, day D | computed from D | evaluated | book iff D+7 wanted |
| Correct (EST cron, EST season) | proceed | 05:50 ET, day D | from D | evaluated | book iff D+7 wanted |
| Wrong (EST cron, EDT season) | **skip** | 06:50 ET | n/a | not reached | exit 0 |
| Wrong (EDT cron, EST season) | **skip** | 04:50 ET | n/a | not reached | exit 0 |

The two wrong-season rows exit at the DST gate before the booking-day gate runs, so we
never make a booking-day decision off a wrong-season clock. Note the EDT-cron-in-EST land
time (04:50 ET) is the SAME calendar day as a 05:50 land — so even if the order were
reversed the date would be identical — but the DST gate's skip is what actually protects
us, and keeping DST first means we never busy-wait the ~70-min wrong-season case.
**Decision: DST gate first.**

**Full FakeClock test matrix (`tests/test_booking_day_gate.py`).** `fire_time` is fixed at
06:00; the gate ignores it (it reads only the date), but tests pin the UTC instant at the
correct-season :50 to mirror the real cron. `target_offset = 7`,
`wanted = frozenset({5, 6})` (Sat+Sun) unless noted. Reference dates: 2026-05-31 is a
Sunday (EDT); 2026-12-06 is a Sunday (EST); 2026-03-08 is spring-forward Sunday;
2026-11-01 is fall-back Sunday.

| Test name | Clock (UTC) | ET date `today` | `today+7` | weekday | Expect |
|---|---|---|---|---|---|
| `test_books_when_target_is_sunday` | 2026-05-31 09:50 | Sun 05-31 | Sun 06-07 | 6 | True |
| `test_books_when_target_is_saturday` | 2026-05-30 09:50 | Sat 05-30 | Sat 06-06 | 5 | True |
| `test_skips_when_target_is_monday` | 2026-05-25 09:50 | Mon 05-25 | Mon 06-01 | 0 | False |
| `test_skips_when_target_is_friday` | 2026-05-29 09:50 | Fri 05-29 | Fri 06-05 | 4 | False |
| `test_sunday_only_set_skips_saturday_target` | 2026-05-30 09:50 | Sat 05-30 | Sat 06-06 | 5 | False (wanted={6}) |
| `test_sunday_only_set_books_sunday_target` | 2026-05-31 09:50 | Sun 05-31 | Sun 06-07 | 6 | True (wanted={6}) |
| `test_est_season_books_sunday_target` | 2026-12-06 10:50 | Sun 12-06 | Sun 12-13 | 6 | True |
| `test_est_season_skips_wednesday_target` | 2026-12-02 10:50 | Wed 12-02 | Wed 12-09 | 2 | False |
| `test_spring_forward_week_books_sunday` | 2026-03-08 09:50 | Sun 03-08 | Sun 03-15 | 6 | True |
| `test_spring_forward_week_skips_tuesday` | 2026-03-10 09:50 | Tue 03-10 | Tue 03-17 | 1 | False |
| `test_fall_back_week_books_saturday` | 2026-10-31 09:50→EDT | Sat 10-31 | Sat 11-07 | 5 | True |
| `test_fall_back_week_skips_thursday` | 2026-11-05 10:50 | Thu 11-05 | Thu 11-12 | 3 | False |
| `test_offset_not_hardcoded` | 2026-05-29 09:50, offset=1 | Fri 05-29 | Sat 05-30 | 5 | True (offset param honoured) |

Cross-product coverage required by the brief: {wanted day, unwanted day} × {correct
season, wrong season} × {spring-forward week, fall-back week}. The wrong-season rows are
covered at the `_run` integration level (the gate itself is season-agnostic — it reads the
date, not the season — so wrong-season behaviour is the DST gate's job, asserted in
`test_dst_gate.py` and in the `_run` ordering test below). State this explicitly in the
test module docstring: **the booking-day gate is weekday-only; season correctness is the
DST gate's concern (reviewer item 8 — the gate is weekday-agnostic w.r.t. DST).**

**`_run` integration tests (`tests/test_cli.py`).**
- `test_run_wait_dst_first_then_booking_day_gate` — spy both gates; on a wrong-season
  clock assert `should_book_today` is NOT called (DST gate short-circuits first).
- `test_run_wait_booking_day_skip_exits_zero_no_book` — correct season, `today+7` is a
  Monday, `--wait --use-fake-adapter`; assert no `Orchestrator.run` / no `book()`
  (`FakeAdapter.book_call_count == 0`) and a clean return; assert the "not a wanted
  booking day" INFO log line is emitted (reviewer item 6 verification surface).
- `test_run_wait_booking_day_proceed_books_single_date` — correct season, `today+7` is
  Sunday; assert the request handed to `Orchestrator` has a **single-element**
  `target_dates` equal to `today+7`.
- `test_run_no_wait_bypasses_booking_day_gate` — `--no-wait` never evaluates either gate
  (spy; assert not called), matching the manual/local always-proceed semantics.

**`_build_request` interaction (the load-bearing `__main__.py` change).** After PR1
`_build_request` produces the documented interim (`min(wanted_weekday_indices)` anchor) and
`Orchestrator.run` uses only `target_dates[0]`. PR2 OVERRIDES that interim for the booking
path: the booking run must target the single gated `today + offset`. Two clean options:

- **Option A (chosen):** `_run` computes the booking target itself on the `--wait` path:
  after the booking-day gate proceeds, build the request with
  `target_dates = (today_local + timedelta(days=offset),)` (a 1-tuple). The watcher uses a
  different path (PR4) and does not share this. This keeps the booking job's single-date
  contract explicit and avoids `resolve_target_dates` (which is now a watcher concern).
- Option B (rejected): keep `resolve_target_dates` and pass all wanted dates; rejected
  because `_first_matching_reservation` (`orchestrator.py:276`) matches `r.tee_time.date()
  in request.target_dates` — a multi-date booking request would let a Saturday reservation
  vacuously satisfy a Sunday booking run's pre-book guard. Single-date target_dates for the
  booking job is REQUIRED for correctness (reviewer item 4 corollary). Call this out.

So PR2's `__main__.py` diff: in `_run`, on the `--wait` (and `--no-wait`) booking path,
replace `request = _build_request(cfg, dry_run=dry_run)` with a variant that pins
`target_dates` to the single resolved booking date. The cleanest factoring is a new
`_build_booking_request(cfg, *, dry_run, target_date)` that wraps `_build_request` and
overrides `target_dates=(target_date,)`. The `--no-wait` path computes `target_date` via
`today_local + offset` directly (no gate). Show the helper signature in the plan; it is a
thin wrapper, implemented test-first.

```python
# __main__.py — NEW thin wrapper (signature for the plan; implemented in PR2)
def _booking_target_date(cfg: AppConfig) -> date:
    """The single date the booking run targets: today (course-local) + target_offsets[0].
    The booking-day gate has already confirmed (on the --wait path) that this date's
    weekday is wanted. The --no-wait path uses it directly (always-proceed)."""
    ...
```

**Stub signatures.** `core/booking_day_gate.py::should_book_today` — already on disk
(stub raising `NotImplementedError("MULTIDAY_PLAN.md PR2 (booking-day gate)")`). Signature:
```python
def should_book_today(
    clock: Clock, *, timezone: str, target_offset: int, wanted_weekdays: frozenset[int]
) -> bool: ...
```

**Doc updates.** root `CLAUDE.md` (new invariant: "Booking-day gate: daily booking cron
self-gates on whether `today+offset`'s weekday ∈ `target_weekdays`; runs AFTER `dst_gate`,
BEFORE busy-wait, `--wait` path only"), PLAN.md §6.x (new subsection mirroring the dst_gate
one), `infra/AZURE_PLAN.md` §5.3 (note the daily cron + booking-day gate pairing).

**CI/parity.** New module must pass `mypy --strict` + `ruff`. No config/bicep change in
PR2. No new CI job.

---

### PR3 — `core/target_date.py`: watcher horizon helper

**Scope.** Add `next_occurrences_within_horizon` returning the next upcoming occurrence of
EACH wanted weekday ≤ `horizon` days out, with "today counts if today is wanted." The
horizon is derived from `max(target_offsets)` (single source of truth — reviewer item 3).
Keep the existing `resolve_target_dates` / `most_recent_weekday` / `weekday_from_name`
(still used by the booking-default path and tests).

**Files touched.**
- `src/teetime/core/target_date.py` — add the new function (existing real module; the new
  function's signature/docstring is shown here and implemented test-first in PR3).
- `tests/test_target_date.py` — extend with the matrix below.

**New function (signature + contract).**
```python
def next_occurrences_within_horizon(
    today: date, wanted_weekdays: frozenset[int], horizon_days: int
) -> tuple[date, ...]:
    """The next upcoming occurrence of EACH wanted weekday within `horizon_days` of today.

    For each weekday w in `wanted_weekdays`, the result includes the smallest date d such
    that d.weekday() == w and 0 <= (d - today).days <= horizon_days. "Today counts": if
    today.weekday() is wanted, today itself is the occurrence (delta 0) — never a past
    date, never next week's same weekday.

    Returned dates are sorted ascending and de-duplicated. A weekday whose next occurrence
    falls strictly beyond the horizon is omitted (cannot happen for a 7-day horizon, which
    always contains every weekday exactly once counting today — stated as an invariant in
    the test).

    Horizon is the booking window length; callers pass `max(target_offsets)` so the 7-day
    window is defined in exactly ONE place (config target_offsets), never hardcoded here.
    """
    ...
```

Implementation note for the agent: for each wanted `w`,
`delta = (w - today.weekday()) % 7; d = today + timedelta(days=delta)`; include iff
`delta <= horizon_days`. `delta == 0` when today is that weekday (today counts). Sort the
results.

**"Today included?" semantics (reviewer item 2).** The watcher's date list **includes
today if today is a wanted day**. With a 7-day horizon and "today counts", the next Saturday
occurrence FROM a Saturday is today itself (delta 0) and the next Sunday is tomorrow (delta
1). So on a Saturday the list is `{this Sat, this Sun}` = the two soonest wanted days, which
is correct: those are the two days with a live managed reservation in the bookable horizon.
The booked-7-days-out dates (next-next Sat/Sun) are not yet watchable because they are >7
days out and not yet booked. Watching THIS Saturday catches last-minute cancellations/
upgrades on the round happening today. This matches the
existing `WatchOrchestrator._is_past_watch_deadline` (`watch_orchestrator.py:462-472`),
which keeps watching through the morning OF the target date (`local_date > target_date`
stops it the day AFTER), so including today is consistent with the existing within-target-
date polling. **Decision: today is included when today's weekday is wanted.**

**Horizon coupling (reviewer item 3).** `_watch` passes
`horizon_days = max(cfg.request.target_offsets)`. With `target_offsets = [7]` the horizon
is 7, the same number the booking job offsets by — derived once, never two literal 7s. If
`target_offsets` ever changes, both the booking offset and the watch horizon move together.

**Red tests (`tests/test_target_date.py`).** Reference: 2026-05-31 is Sunday. Cover each
day-of-week the watcher might run × wanted `{Sat=5, Sun=6}` with horizon 7:

| Test name | `today` | DOW | Expected dates (Sat, Sun ≤7d) |
|---|---|---|---|
| `test_horizon_from_sunday` | 2026-05-31 (Sun) | Sun | (2026-06-06 Sat, 2026-05-31 Sun) → sorted (05-31, 06-06) |
| `test_horizon_from_monday` | 2026-06-01 (Mon) | Mon | (06-06 Sat, 06-07 Sun) |
| `test_horizon_from_friday` | 2026-06-05 (Fri) | Fri | (06-06 Sat, 06-07 Sun) |
| `test_horizon_from_saturday_today_counts` | 2026-06-06 (Sat) | Sat | (06-06 Sat=today, 06-07 Sun) |
| `test_horizon_from_wednesday` | 2026-06-03 (Wed) | Wed | (06-06 Sat, 06-07 Sun) |
| `test_horizon_never_returns_past` | 2026-06-06 (Sat) | Sat | min date == today, none < today |
| `test_horizon_sunday_only_set` | 2026-06-03 (Wed) | Wed, wanted={6} | (06-07 Sun,) — single date |
| `test_horizon_dedupe_sorted` | 2026-05-31 (Sun) | Sun, wanted={5,6,5} | (05-31, 06-06) deduped, ascending |
| `test_horizon_boundary_7_days_inclusive` | 2026-06-01 (Mon), wanted={0} (Mon) | Mon | (06-08 Mon,) delta 7 included |
| `test_horizon_excludes_beyond` | 2026-06-02 (Tue), wanted={0} (Mon), `horizon_days=5` | Tue | () empty — next Mon (06-08) is delta 6 > 5, excluded |
| `test_horizon_includes_at_boundary` | 2026-06-02 (Tue), wanted={0} (Mon), `horizon_days=6` | Tue | (06-08 Mon,) — delta 6 == horizon, inclusive |
| `test_horizon_uses_max_offset_not_literal` | call with `horizon_days=max([7])` | — | same as 7-day cases (guards reviewer item 3 indirectly) |

(`test_horizon_excludes_beyond` and `test_horizon_includes_at_boundary` are the clean
exclusion/inclusion pair: from Tue 06-02 the next Monday is 06-08 = delta 6, so
`horizon_days=5` → `()` and `horizon_days=6` → `(06-08,)`. The boundary is `delta <=
horizon_days` (inclusive).)

**Stub signatures.** `next_occurrences_within_horizon` (above) — added to the existing
`target_date.py`; shown here, implemented test-first (not stubbed on disk because the file
is an existing real implementation).

**Doc updates.** `target_date.py` module docstring (note the new watcher helper + the
horizon-from-offsets rule), root `CLAUDE.md` (the target-date invariant gains the watcher
multi-date sentence).

**CI/parity.** No config/bicep change. `mypy --strict` + `ruff`.

---

### PR4 — Watcher: multi-date loop + per-date search scoping + poll-every-run

**Scope.** Three tightly-coupled changes to "what each watch invocation searches", all in
`watch_orchestrator.py` + `__main__._watch`:
1. **(must-fix 1) Per-date search scoping** — `_check_course` `dc_replace`s the request to a
   single-element `target_dates=(target_date,)` BEFORE `adapter.search`, so a multi-date
   watch never searches/books the wrong date. **Net-new watcher bookings are restricted to
   slots whose date == the loop's `target_date`** (confirmed user decision: a Saturday watch
   must NEVER book a Sunday slot).
2. **Multi-date loop** — `_watch` loops `check_once` over the horizon helper's date list.
   `--date` still overrides to a single date (and that date is what gets searched — must-fix
   1 covers the override path too).
3. **(new user requirement) Poll every run** — remove the `_is_outside_polling_hours` gate so
   the watcher does a real search on EVERY cron invocation. KEEP `_is_past_watch_deadline`.

`UpgradeOrchestrator` is unchanged — it already `dc_replace`s per date
(`upgrade_orchestrator.py:269-274`). The fix is in the net-new `_check_course` path.

**Files touched.**
- `src/teetime/core/watch_orchestrator.py` — (a) `_check_course`: `dc_replace` before
  `search` (must-fix 1); (b) delete `_is_outside_polling_hours` + its call (`:194-200`,
  def `:448-460`); the `now = self._clock.now_utc()` at `:191` is still needed by the
  deadline gate (`:203`).
- `src/teetime/__main__.py` — `_watch` (`:354-401`): compute the date list, loop.
- `src/teetime/core/config.py` + `core/models.py` + `config/{container,local,example}.toml`
  — drop `polling_start_hour`/`polling_end_hour` (decision below).
- `tests/test_watch_cli.py`, `tests/test_watch_orchestrator.py` (or the existing watch
  orchestrator test module) — red tests below.

#### must-fix 1: per-date search scoping (the load-bearing correctness fix)

Today `_check_course` (`watch_orchestrator.py:272-314`) calls `await adapter.search(request)`
(`:306`) with the UNSCOPED request. Both adapters iterate `request.target_dates`
(`foreup/base.py:308`, `teeitup/base.py:313`) and `rank_slots_for_request` filters by
spots/holes/price/time-of-day window only — **NOT by date** (`slot_utils.py:43-58`). So with a
multi-date `request.target_dates`, `search` returns slots for ALL dates, ranking picks the
single closest-to-midpoint slot across dates, and `_book_candidates` records it under the
loop's `target_date` store key (`:360-361`) regardless of the slot's actual date — a Saturday
loop could book a closer Sunday slot under the Saturday row (cross-date contamination / an
extra reservation). The fix mirrors the upgrade path's existing `dc_replace`:

```python
# watch_orchestrator.py _check_course — scope the search to THIS target_date before searching.
# This is the must-fix-1 structural guarantee: a Sat watch can only ever search/book Sat.
scoped = dc_replace(request, target_dates=(target_date,))
try:
    slots = await adapter.search(scoped)
except (NoInventoryError, InventoryNotPublishedError):
    return None
candidates = rank_slots_for_request(slots, scoped)
```
Add `from dataclasses import replace as dc_replace` to `watch_orchestrator.py` (already used
in `upgrade_orchestrator.py:99`). The rest of `_check_course` (the `list_reservations` match
at `:286-289`, which already filters `r.tee_time.date() == target_date`) is correct as-is.

**Why scope inside `_check_course`, not only at the caller.** Scoping at the `_watch` caller
(passing a 1-tuple-request per iteration) ALSO works, but doing it in `_check_course` makes
the per-date guarantee STRUCTURAL — it holds for EVERY caller of `check_once` (the `--date`
override, a future direct caller, the loop) without relying on each caller remembering to
scope. We do BOTH: `_watch` passes the request as-is (multi-date is fine now) and
`_check_course` scopes per its `target_date` parameter. The `target_date` parameter is the
single source of truth for which date this invocation acts on.

**Red tests (must-fix 1) — `tests/test_watch_orchestrator.py`.**
- `test_check_once_books_only_target_date_slot` — FakeAdapter `search` returns BOTH a Saturday
  slot AND a Sunday slot that is strictly closer to the time-window midpoint; call
  `check_once(request, target_date=<Sat>)` with `dry_run=false`. Assert the booked slot's
  `tee_time.date() == Sat` and the Sunday slot is NEVER booked / never recorded under the Sat
  row. **This is the exact reviewer-mandated red test + the user's confirmed contract.**
- `test_watch_date_override_searches_that_date` — drive the `--date` override path with a
  FakeAdapter whose `search` would return slots for multiple dates; assert the search/booking
  is scoped to the overridden date only (covers the latent `--date` mismatch at
  `__main__.py:357-363`). Behavioural (date assertion), not a call-count test.

#### Multi-date loop (`__main__._watch`)

Today (`__main__.py:354-365`) `_watch` derives a single `target_date` (`--date` override or
`request.target_dates[0]`) and calls `check_once` once. New:
```python
# __main__.py _watch — replace the single-date block with:
if target_date_str:
    target_dates = (date.fromisoformat(target_date_str),)   # explicit override: one date
else:
    today = datetime.now(tz=ZoneInfo(cfg.scheduler.timezone)).date()
    target_dates = next_occurrences_within_horizon(
        today,
        cfg.request.wanted_weekday_indices,
        max(cfg.request.target_offsets),
    )
log.info("Watch check: targets=%s dry_run=%s", [str(d) for d in target_dates], dry_run)
for target_date in target_dates:
    result = await watch.check_once(request, target_date)   # _check_course scopes the search
    if result is not None:
        log.info("watch result: date=%s outcome=%s confirmation=%s",
                 target_date, result.outcome, result.confirmation_code)
```
A single `WatchOrchestrator` instance is reused (stateless beyond injected store/adapters;
each `check_once` is independent and per-date). The loop continues even if a date returns a
result (both Sat and Sun must be checked every run) — do NOT `break`.

**Log-line change note (reviewer follow-on to M6 item 6).** The watch log line changes from
`Watch check: target=<date>` (singular, `__main__.py:365`) to `Watch check: targets=['<sat>',
'<sun>']` (plural). `infra/AZURE_PLAN.md:829` greps for the OLD `Watch check: target=`
string — PR4 updates that runbook grep to `Watch check: targets=` (and the PR6 verification
doc). Flag in the PR description.

#### New user requirement: poll every run (remove the polling-hours gate)

**Why.** The polling-hours gate blinds the watcher during the highest-value windows. Confirmed
in prod: the 06:10 ET watch run skipped its search because `polling_start_hour=7`, so we had
zero visibility into the 6 AM drop AND early-morning cancellations. The watcher must search on
EVERY cron invocation.

**Change.** Delete `_is_outside_polling_hours` (`watch_orchestrator.py:448-460`) and its
early-return + debug log (`:194-200`). Keep `_is_past_watch_deadline` (`:203`, def `:462`) —
the reviewer/user explicitly confirmed that gate is CORRECT (don't poll a date that already
passed) and is NOT what the user objected to. `now = self._clock.now_utc()` at `:191` stays
(the deadline gate consumes it).

**Config-field decision: DROP `polling_start_hour`/`polling_end_hour` (cleaner).** They become
dead config the moment the gate is gone. Remove them from:
- `WatcherConfig` (`core/config.py:126-127`) + its `to_watch_config()` pass-through
  (`:140-141`).
- `WatchConfig` dataclass (`core/models.py:254-255`) + the comment block (`:251-253`).
- `config/container.toml:73-74`, `config/local.toml:64-65`, `config/example.toml:105-106`.

**Parity impact (the field the requirement asks about):** `test_container_config_parity.py`
does **NOT** compare these fields. Verified by reading the test (`:50-78`): `_referenced_env_vars`
collects only keys ending in `_env` (course creds, player contact, card fields);
`_bicep_env_var_names` collects only UPPER_SNAKE env names from `compute.bicep`. The polling
hours are plain TOML ints, never `*_env`, never in bicep — so removing them does not touch the
parity surface. (`compute.bicep` does not reference them either.) **No parity-test change.**
The only test fallout is in any test that constructs `WatchConfig(... polling_start_hour=...)`
or asserts the polling defaults — grep `polling_` across `tests/` and update those constructions
in the same PR (they are red-then-green).

**Anti-bot note.** Removing the hours gate does NOT remove the rate limit: the 10-min cron
interval + the `poll_interval_s >= 300` floor (`WatchConfig.__post_init__`,
`core/models.py:257-260`) remain the throttle. Per the project's reversed ToS posture
(PLAN.md §12; CLAUDE.md "Modifying anti-bot etiquette"), polling at the existing 10-min cadence
across the full day is acceptable — it is still "one user making normal bookings" frequency,
just without an arbitrary nighttime blackout. State this in the PR description.

**Interaction with the multi-date loop + per-date scoping (the recovery path).** With the
hours gate gone AND per-date scoping in place, an early-morning watch run (e.g. 06:10 ET) that
finds the just-dropped target window open can now BOOK it — a real recovery path if the 06:00
booking cron failed/raced. This is intended and desirable. It respects one-booking-per-DATE:
the book goes through `_book_candidates`, which re-checks `get_terminal(request_id, target_date)`
under the advisory lock (`watch_orchestrator.py:347-349`) before booking and records the
terminal per `target_date` (`:360-361`). So the watcher cannot double-book the date the 06:00
job already booked, and per-date scoping ensures it only books the loop's `target_date`.
Confirm both in tests below.

#### Per-date independence (reviewer item 4) — confirmed; only the SEARCH needed scoping

The store-keying was already per-date; must-fix 1 adds the matching SEARCH scoping. Proof by
file:line (re-verified against current code):
- `check_once(request, target_date)` keys every store touch on `target_date`:
  `get_terminal(request.request_id, target_date)` (`watch_orchestrator.py:208`), the
  reservation match `r.tee_time.date() == target_date` (`watch_orchestrator.py:289` — N1: it
  is line 289, not 288), and the book path `record_terminal(result, target_date)` /
  `delete_terminal(..., target_date)` (`watch_orchestrator.py:360-361`). The BOOKED
  short-circuit (`:209`) is `prior.outcome == BOOKED` for THAT `(request_id, target_date)`
  row only.
- The ONE place that was NOT date-scoped was `_check_course`'s `adapter.search(request)`
  (`:306`) — fixed by must-fix 1's `dc_replace`.
- `maybe_upgrade(request, target_date, current_booking)` is fully per-date already:
  `request_lock(request.request_id)` (`upgrade_orchestrator.py:227`) serialises by RequestId
  (shared across dates — see lock contention below); the search
  (`dc_replace(request, target_dates=(target_date,), …)`, `upgrade_orchestrator.py:269-274`),
  the priority list (`_build_priority_list(request, target_date)`,
  `upgrade_orchestrator.py:211`, def `:450`), and the writes
  (`delete_terminal/record_terminal(..., target_date)`, `upgrade_orchestrator.py:408-409`)
  are all date-scoped.
- **No global "already booked" short-circuit exists** in either orchestrator. With must-fix 1
  applied, watching Sat then Sun in one loop run cannot let Sat block, cancel, search, or
  satisfy Sun.

**Lock contention nuance (pre-emption, not a blocker).** `request_lock` is keyed by
`request.request_id`, the SAME for Sat and Sun (weekday-independent fingerprint). The `_watch`
loop is sequential (`await check_once` then next date), so Sat releases the lock before Sun
acquires it — no self-deadlock. Across the booking + watch jobs, the existing ACA concurrency
groups + advisory lock serialise; a Sat-watch holding the lock briefly blocks a concurrent
Sun-book only for the ~1-2 s book window, and `ConcurrentRunError` is caught and retried next
cron (`watch_orchestrator.py:371`). Same contention profile as today's single-date watcher —
the loop is sequential, not concurrent. "No new lock semantics."

**Red tests (`tests/test_watch_cli.py`).** Use FakeClock + FakeAdapter (`--use-fake-adapter`),
`set_search_response`.
- `test_watch_checks_both_wanted_days` — FakeClock on a Wednesday, `target_weekdays=Sat+Sun`;
  assert `check_once` is invoked for exactly `{next Sat, next Sun}` (spy on `check_once`
  recording `target_date` args).
- `test_watch_today_counts_on_saturday` — FakeClock on a Saturday; assert the date list is
  `(this Sat, this Sun)` (today included).
- `test_watch_date_override_single` — `--date 2026-06-14` → exactly one `check_once` for that
  date, helper not consulted. (Call-count companion to `test_watch_date_override_searches_that_date`
  above — that one asserts the search is DATE-scoped, this one asserts the loop count.)
- `test_watch_loop_continues_after_result` — script the FakeAdapter so the Sat check returns a
  DRY_RUN result; assert the Sun check STILL runs (no `break`).
- `test_watch_sat_and_sun_independent_store_rows` — pre-seed the store with a BOOKED terminal
  for Sat only; assert the Sun `check_once` proceeds to search (Sat's BOOKED row does not
  short-circuit Sun). Behavioural proof of item 4.
- `test_watch_logs_plural_targets` — assert the new `Watch check: targets=[...]` line.

**Red tests (poll-every-run) — `tests/test_watch_orchestrator.py`.**
- `test_watch_polls_before_7am` — FakeClock at 06:10 ET, a future `target_date` (not past
  deadline); assert `check_once` performs a search (FakeAdapter `search` is called) instead of
  early-returning. Pre-removal this fails because the polling gate short-circuits.
- `test_watch_polls_at_any_hour` — FakeClock at 03:00 ET; assert `check_once` still searches.
- `test_watch_past_deadline_still_short_circuits` — FakeClock whose local date > `target_date`;
  assert `check_once` returns None WITHOUT searching (the deadline gate is retained). Regression
  guard that removing the hours gate did not also remove the deadline gate.
- `test_watch_recovery_books_just_dropped_window` — FakeClock at 06:10 ET, empty store,
  FakeAdapter `search` returns an in-window slot for `target_date`, `dry_run=false`; assert
  `check_once` BOOKS it and records the terminal under `(request_id, target_date)` (the
  recovery path). Then a SECOND `check_once` for the same date returns the existing BOOKED
  terminal without re-booking (one-booking-per-date respected).

**Doc updates.** root `CLAUDE.md` (watcher invariants: "polls every run — no time-of-day gate;
the deadline gate is retained"; "net-new watcher bookings are scoped to the loop's
`target_date` — a Sat watch never books a Sun slot"; "checks the next occurrence of each
wanted weekday within the horizon; per-date independent"; remove the stale
`polling_start_hour`/`polling_end_hour` mention if any), `infra/AZURE_PLAN.md:829` grep string
→ plural, README watch section, PLAN.md M-feature-1 (multi-date + poll-every-run note), the
`WatchConfig` docstring in `core/models.py` (drop the polling-hours paragraph).

**CI/parity.** No bicep change. Config fields dropped (no parity impact — see above).
`mypy --strict` + `ruff`. `grep -rn polling_ tests/ src/` must come back empty after this PR.

---

### PR5 — Bicep: daily booking crons (keep job count 6, killswitch untouched)

**Scope.** Change the two booking crons from Sunday-only (`… * * 0`) to **daily**
(`… * * *`). `bookingJobs` stays length-2; **job names stay `-edt-sun` / `-est-sun`** OR
are renamed (decision below). Update the module header comment and
`tests/test_compute_bicep_schedule.py`.

**Job-name decision (reviewer items 9 + the cutover, the load-bearing call).** The crons
are no longer Sunday-only, so the `-sun` suffix is now a misnomer. Two options:

- **Option A (CHOSEN): keep the names `-edt-sun` / `-est-sun`, change only the cron.** The
  suffix becomes a (now-inaccurate but harmless) DST-half label. **Why:** the killswitch
  (`killswitch.bicep:127-129,137-139`) hardcodes the exact job names
  `teetime-job-${envName}-edt-sun`, `-est-sun`, `teetime-watch-job-${envName}` and the
  prod equivalents (`:137-139`). Renaming would require editing the killswitch's 6 PATCH +
  6 POST URIs (`killswitch.bicep:239-397`) AND would force a **delete+create** of both
  booking job resources on the next deploy (a renamed ACA Job is a new resource), churning
  prod mid-flight. Keeping the names makes the cron change an **in-place property edit**
  (`scheduleTriggerConfig.cronExpression` only) — `what-if` shows a Modify, not a
  Delete+Create — and the killswitch needs **zero** changes. The job COUNT stays 6, so the
  killswitch's "3 jobs × 2 envs × 2 actions = 12 HTTP calls" invariant
  (`killswitch.bicep:4-21`) is preserved exactly.
- Option B (rejected): rename to `-edt` / `-est` for accuracy. Rejected for the churn +
  killswitch-rewrite cost above; cosmetic accuracy is not worth a prod resource recreate
  and a coupled killswitch edit. We instead fix the *comment* to explain the suffix is a
  DST-half label, not a day label.

**Files touched.**
- `infra/bicep/modules/compute.bicep` — `cronEdtSun`/`cronEstSun` values `* * 0` → `* * *`;
  header comment (lines 1-11) and the `cronEdtSun`/`cronEstSun` comment (lines 80-84) updated
  to "daily; booking-day gate selects wanted days, DST gate selects the season"; the
  `output jobName` description (line 346) reworded (no longer "Sunday-only").
- `tests/test_compute_bicep_schedule.py` — update assertions (below).
- `infra/AZURE_PLAN.md` §5.3 cron table → daily.
- `killswitch.bicep` — **NO change** (names unchanged). State this explicitly in the PR.

**Exact bicep edit.**
```bicep
// was:
var cronEdtSun = '50 9 * * 0'    // 09:50 UTC = 05:50 EDT Sunday
var cronEstSun = '50 10 * * 0'   // 10:50 UTC = 05:50 EST Sunday
// now (daily; the booking-day gate picks wanted weekdays, the DST gate picks the season):
var cronEdtDaily = '50 9 * * *'    // 09:50 UTC = 05:50 EDT, EVERY day (gate selects wanted days)
var cronEstDaily = '50 10 * * *'   // 10:50 UTC = 05:50 EST, EVERY day
// bookingJobs names UNCHANGED (-edt-sun/-est-sun kept as DST-half labels so killswitch
// job-name list + count stay fixed; rename would churn prod + require a killswitch edit):
var bookingJobs = [
  { name: '${jobName}-edt-sun', cron: cronEdtDaily }
  { name: '${jobName}-est-sun', cron: cronEstDaily }
]
```
(Variable renames `cronEdtSun`→`cronEdtDaily` are internal; if the implementer prefers to
keep the var names too, that is fine — the test asserts on the cron STRING and the job-name
suffix, not the var name. See test note.)

**What-if churn the operator will see (reviewer item 10).** Because the job names are
unchanged and only `scheduleTriggerConfig.cronExpression` changes, `az deployment group
what-if` shows **Modify** on both `teetime-job-<env>-edt-sun` and `-est-sun` (cron string
`* * 0` → `* * *`), and **no change** to the watch job, the killswitch Logic App, the
Action Group, or any RBAC. No Delete+Create, no resource recreation. The dev auto-deploy
applies it with `dryRun=true` (no real bookings). State in the PR description: "expect two
Modify lines on the booking jobs; everything else NoChange."

**Test updates (`tests/test_compute_bicep_schedule.py`).** The current tests assert
Sunday-only (`50 9 * * 0` present, Saturday absent). Rewrite:
```python
def test_booking_jobs_are_daily(bicep: str) -> None:
    assert "50 9 * * *" in bicep    # daily EDT cron
    assert "50 10 * * *" in bicep   # daily EST cron
    # The old Sunday-only crons are gone.
    assert "50 9 * * 0" not in bicep
    assert "50 10 * * 0" not in bicep
    # Job-name suffixes are UNCHANGED (DST-half labels; killswitch coupling).
    assert "-edt-sun" in bicep
    assert "-est-sun" in bicep

def test_still_exactly_two_booking_crons_for_killswitch_parity(bicep: str) -> None:
    # Killswitch hardcodes 3 jobs/env (edt + est + watch). Job COUNT must stay 2 booking + 1 watch.
    assert bicep.count("'${jobName}-edt-sun'") == 1
    assert bicep.count("'${jobName}-est-sun'") == 1

def test_watch_cron_is_daily_every_10_min(bicep: str) -> None:   # unchanged
    assert "'*/10 * * * *'" in bicep
```
Drop `test_no_orphan_saturday_cron_vars` (there were never Saturday vars in the current
file — the M6 plan already removed them; the current file has only `cronEdtSun`/`cronEstSun`).
Keep `test_jobname_output_index_zero_is_a_real_sunday_job` but rename to
`test_jobname_output_index_zero_is_a_real_job` (index 0 is still `-edt-sun`).
Keep `test_enable_schedules_param_toggles_both_jobs` unchanged.

**New killswitch-parity test (recommended, reviewer item 9).** Add a small static test
asserting the killswitch job-name list still matches `compute.bicep`'s job names, so a
future rename can't silently de-sync the two:
```python
# tests/test_killswitch_job_parity.py (NEW)
def test_killswitch_targets_match_compute_job_names() -> None:
    """Killswitch PATCHes/stops exactly the jobs compute.bicep creates (count + names).
    If compute renames -edt-sun→-edt the killswitch URIs must change in the same PR."""
    # assert 'teetime-job-${envName}-edt-sun', '-est-sun', 'teetime-watch-job-${envName}'
    # appear in BOTH compute.bicep (as job names) and killswitch.bicep (as PATCH/stop targets).
```
**CI required-check note (CLAUDE.md "Required CI checks"):** this new test runs inside the
existing `test / lint / typecheck` job (it is a pytest file), so it does NOT add a new CI
*job* and does NOT require a branch-protection contexts edit. Only a brand-new ci.yml job
would. State this in the PR.

**Doc updates.** `infra/AZURE_PLAN.md` §5.3 (cron table → daily; add a row explaining the
booking-day gate), §5.4 (booking jobs ×2 daily), `src/teetime/courses/CLAUDE.md` ("Schedule
is Sunday only" → "Schedule books wanted morning days (Sat+Sun); daily crons self-gate"),
root `CLAUDE.md` (status + the schedule note), README §intro + roadmap.

**CI/parity.** `az bicep build` + `what-if` in `azure-iac.yml`. The static schedule test +
new parity test run in normal CI. `test_container_config_parity.py` is unaffected (no env
wiring change). Merge auto-deploys to dev (`dryRun=true`).

---

### PR6 — Docs sync + dev verification

**Scope.** Sync every stale doc (list in §10) and add the verification surface for the new
behaviour: the booking-day gate skip log, the daily-cron behaviour, the multi-date watch
log. Mostly docs; one verification test that the booking-day-skip log line is emitted (so
it can't silently drop), plus the M6_PLAN superseded note.

**Files touched.**
- `M6_PLAN.md` — add a top banner: "SUPERSEDED in part by MULTIDAY_PLAN.md: the Sunday-only
  schedule (PR5) is replaced by daily crons + a booking-day gate; `--wait`/`dst_gate`/
  watcher-enable remain in force."
- `infra/AZURE_PLAN.md` §10.4 verification: add "5/7 days the booking cron fast-exits with
  `booking-day gate: today+7 is <weekday>, not a wanted booking day — exit 0`"; update the
  watch grep to the plural `Watch check: targets=`.
- `README.md`, root `CLAUDE.md`, PLAN.md, `src/teetime/courses/CLAUDE.md` — see §10.
- `tests/test_cli.py` — `test_booking_day_skip_log_emitted` (caplog) so the verification
  string is pinned to real output (mirrors M6's reviewer-item-6 fix).

**Cost + log verification (reviewer item 6).** On a non-booking day the booking cron does:
image pull (warm env ~30-90 s billed, dominated by the unavoidable pull either way) +
config load + `dst_gate` (microseconds) + `booking_day_gate` (microseconds) + log + exit.
**No auth, no `_resolve_site_keys`, no NTP probe** (those are after the gate in `_run`), no
busy-wait, no search. Consumption billing is the replica runtime (~a few seconds of compute
after the pull) → **sub-cent per fast-exit**; 5 extra fast-exits/week ≈ 10 extra
fast-exits/week across both DST crons (only the correct-season one proceeds past the DST
gate; the wrong-season one already exited) — still well within the free ACA tier
(`AZURE_PLAN §"180,000 vCPU-s free"`). Document the exact skip log string for grep.

**CI/parity.** Log/doc + caplog test. No config/bicep change.

---

## 3. Config schema change — consolidated reference

| Aspect | Value |
|---|---|
| New field | `request.target_weekdays: list[str]` |
| Default | `["saturday", "sunday"]` |
| Validator | `model_validator(mode="after")`: non-empty, each name valid via `weekday_from_name`, dedupe+index-sort, reject both-keys-present |
| Backward-compat | old `target_weekday: str` accepted as an alias → singleton set; error if both keys present |
| Derived | `wanted_weekday_indices -> frozenset[int]` property |
| RequestId impact | **none** — weekday not in `build_request_fingerprint` (`models.py:43-70`) |
| Parity-test impact | **none** — not a `*_env` ref; `test_container_config_parity.py` ignores it |
| Files | `config/{container,local,example}.toml`, `core/config.py` |

The wanted set drives: (a) the booking gate `should_book_today(..., wanted_weekday_indices)`
and (b) the watcher helper `next_occurrences_within_horizon(today, wanted_weekday_indices,
max(target_offsets))`. Single config source for both.

---

## 4. Pre-emption summary (reviewer checklist → where addressed)

1. **DST × booking-day ordering / timezone** — §PR2 "Ordering & combined truth table";
   `today` read in course-local ET (`booking_day_gate.py` docstring). DST gate first; wrong-
   season exits before the booking-day decision. ✔
2. **"Today included?" for booking vs watch** — booking books `today+offset` (single,
   future); watcher INCLUDES today when today is wanted (§PR3 "today counts" semantics, with
   the `_is_past_watch_deadline` consistency argument). ✔
3. **Horizon vs offset coupling** — `next_occurrences_within_horizon` takes
   `horizon_days = max(target_offsets)`; no second literal 7 (§PR3, `booking_day_gate.py`
   docstring). ✔
4. **One-booking PER-DAY vs global "at most one"** — proven per-date with file:line in §PR4
   "Per-date independence" and §6; no global short-circuit exists; `maybe_upgrade`/`check_once`
   fully date-keyed. ✔
4b. **Must-fix 1 — net-new watcher search/book scoped to `target_date`** — `_check_course`
   `dc_replace`s before `search` (§PR4 "must-fix 1"); red test
   `test_check_once_books_only_target_date_slot` (Sat loop, closer Sun slot present → books Sat,
   never Sun) + `test_watch_date_override_searches_that_date`. Confirmed user contract. ✔
4c. **Must-fix 2 — `main` never red between PRs** — PR1 is atomic (config rename +
   `_build_request` migration); exact interim `target_dates` (`min(wanted_weekday_indices)`
   anchor) + guard test (§PR1 "must-fix 2"). ✔
4d. **New requirement — poll every run** — polling-hours gate removed; `_is_past_watch_deadline`
   kept; config fields dropped (no parity impact); recovery-path + at-any-hour + deadline-still-
   short-circuits tests (§PR4 "poll every run"). ✔
5. **RequestId determinism** — weekday not in fingerprint (`models.py:43-70`); one stable id
   per run; set sorted for display; guard test `test_request_id_unchanged_by_weekday_set`
   (§PR1). ✔
6. **Daily fast-exit cost + log** — sub-cent, free-tier; exact skip log string + caplog test
   (§PR2, §PR6). ✔
7. **Parity test** — weekday lives only in TOML, not bicep; parity test unaffected
   (read `test_container_config_parity.py:50-78`) (§PR1). ✔
8. **Spring-forward / fall-back with a DAILY cron** — `dst_gate` is weekday-agnostic (reads
   ET hour only); the daily cron does not change its matrix — the existing `test_dst_gate.py`
   cases still hold; the booking-day gate is also weekday-only/season-agnostic (§PR2 matrix
   docstring note). ✔
9. **Killswitch job count/names** — names UNCHANGED (`-edt-sun`/`-est-sun` kept as DST-half
   labels), count stays 6; killswitch needs zero edits; new parity test pins the coupling
   (§PR5). ✔
10. **Cutover / what-if churn / in-flight Sunday booking** — in-place cron Modify (not
    Delete+Create) because names unchanged → no resource recreation, no disruption to a
    live/pending Sunday booking; `killswitchFired` guard interaction unchanged (§5 below). ✔

---

## 5. Cutover (prod is live with `-sun` jobs + `one_booking_policy` on)

Current prod: `teetime-job-prod-edt-sun` / `-est-sun` fire Sunday-only with `dryRun=false`,
watcher + `one_booking_policy` enabled. A live or pending Sunday booking may exist.

Cutover via the normal flow (PR5 merges → dev auto-deploy → prod tag deploy):

1. **dev auto-deploy (on merge of PR5).** `what-if` shows two **Modify** lines (cron
   `* * 0` → `* * *`) on the dev booking jobs; everything else NoChange. dev stays
   `dryRun=true` — no real bookings. Verify the daily fast-exit log on a non-booking day and
   a normal Sunday race in dev before tagging prod.
2. **prod deploy (manual `infra/v*` tag, approval-gated).** Same two Modify lines on the
   prod booking jobs. **In-place cron edit does NOT touch any running execution** — ACA
   applies the new schedule to FUTURE fires only; a replica mid-busy-wait at deploy time
   keeps running its current execution to completion. An already-BOOKED Sunday reservation
   lives on ForeUP (cross-run source of truth), is unaffected by a bicep deploy, and is
   re-discovered by the next run's `list_reservations` pre-book check. **No in-flight Sunday
   booking is disrupted.**
3. **First daily-cron effect.** After the prod deploy, the booking cron fires every morning;
   on Mon-Fri it fast-exits at the booking-day gate (with `target_weekdays=Sat+Sun`, those
   are the only days that proceed). The first Saturday booking lands 7 days before the first
   wanted Saturday. State the exact first-Saturday-drop date in the prod PR description
   (compute from the deploy date).
4. **`killswitchFired` guard interaction.** Unchanged. The cron-string edit is a normal
   `enableSchedules=true` deploy; if `killswitchFired=true` is set in the param files (post-
   trip), `enableSchedules` is forced false and the jobs deploy as Manual (no cron) — the
   daily-cron value is then moot until re-armed. PR5 does NOT touch `killswitchFired`,
   `enableSchedules`, the killswitch Logic App, or the budgets. The deploy-clobber guard
   semantics (`killswitch.bicep:63-73`) are preserved verbatim.
5. **Rollback.** Revert PR5 → cron returns to `* * 0` (in-place Modify back to Sunday-only);
   the code (gate + watcher loop) is inert under a Sunday-only cron (the booking-day gate
   just always proceeds on the only day the cron fires), so a partial rollback (bicep only)
   is safe and leaves a working Sunday-only bot.

---

## 6. One-booking-per-day invariant — file:line proof (reviewer item 4 + must-fix 1)

The brief asks whether `UpgradeOrchestrator.maybe_upgrade` / priority logic needs changes,
with file:line evidence. **Answer for the UPGRADE path: no change.** **Answer for the net-new
`_check_course` SEARCH path: ONE change — must-fix 1's `dc_replace` scoping (PR4).** The
store-KEYING was always per-date; the only un-scoped operation was the search. Evidence:

- `WatchOrchestrator.check_once(request, target_date)` — every store touch is per-date:
  `get_terminal(request.request_id, target_date)` (`watch_orchestrator.py:208`); BOOKED
  short-circuit is for that row only (`:209`); reservation match `r.tee_time.date() ==
  target_date` (`:289` — N1 fix: line 289, not 288); writes
  `delete_terminal/record_terminal(..., target_date)` (`:360-361`).
- **The one un-scoped operation:** `_check_course` calls `adapter.search(request)` (`:306`)
  with the full multi-date request; adapters iterate `request.target_dates` and
  `rank_slots_for_request` does not filter by date. PR4 fixes this with
  `dc_replace(request, target_dates=(target_date,))` before `search` (must-fix 1). After PR4
  the SEARCH is per-date too, so a Sat watch only ever searches/books Sat.
- `UpgradeOrchestrator.maybe_upgrade(request, target_date, current_booking)` — date-scoped
  throughout: priority list `_build_priority_list(request, target_date)` (`:211`, def `:450`);
  search request `dc_replace(request, target_dates=(target_date,), …)` (`:269-274`); writes
  `delete_terminal/record_terminal(..., target_date)` (`:408-409`).
- **No global "already booked" branch** in either file — grep confirms every outcome check
  is paired with a `target_date`. There is no "if any BOOKED exists, skip" anywhere.
- The single cross-date coupling is `request_lock(request.request_id)`
  (`watch_orchestrator.py:344`, `upgrade_orchestrator.py:227`) keyed by the shared RequestId.
  The watcher loop (§PR4) is **sequential**, so Sat releases before Sun acquires — no
  self-deadlock; the contention profile is identical to today's single-date watcher.

Conclusion: Sat and Sun are independent `(RequestId, target_date)` rows from one stable
RequestId. With must-fix 1's search scoping, a Saturday watch neither blocks, cancels, searches,
nor books Sunday (and vice-versa). The per-date store-keying already isolated the WRITES; PR4
adds the matching SEARCH scoping. The rest of the multi-day work is the gate + the watcher's
date list + poll-every-run + the cron cadence.

---

## 7. Docs to update (specific stale lines)

| Doc | Stale line(s) | Change |
|---|---|---|
| `config/example.toml` | `:42-44` `target_weekday = "sunday"` + comment | → `target_weekdays = ["saturday", "sunday"]`; document the deprecated singular alias |
| `config/container.toml` | `:15-18` `target_weekday = "sunday"` + comment | → `target_weekdays = ["saturday", "sunday"]` |
| `config/local.toml` | the `target_weekday` line | → `target_weekdays` |
| `config/{container,local,example}.toml` | `polling_start_hour`/`polling_end_hour` (container `:73-74`, local `:64-65`, example `:105-106`) | REMOVE — polling-hours gate deleted (PR4) |
| `src/teetime/core/config.py` | `WatcherConfig.polling_start_hour`/`polling_end_hour` (`:126-127`) + `to_watch_config` pass-through (`:140-141`) | REMOVE (PR4) |
| `src/teetime/core/models.py` | `WatchConfig.polling_start_hour`/`polling_end_hour` (`:254-255`) + the polling-hours docstring paragraph (`:251-253`) | REMOVE (PR4) |
| root `CLAUDE.md` | "Target date anchors to `target_weekday`…" invariant; the watcher "outside polling hours" mention if any | → wanted-set + booking-day gate + watcher multi-date; status line (Sat+Sun); add "watcher polls EVERY run (no hours gate); deadline gate retained; net-new bookings scoped to the loop's `target_date`" |
| `PLAN.md` | §6.3 schedule table (Sunday-only), §13.1 fingerprint note, §16 milestone row | daily cron + booking-day gate; reaffirm weekday excluded from fingerprint; new milestone |
| `infra/AZURE_PLAN.md` | §5.3 table `:230-235` (Sunday crons), §5.4 table `:264`, §10.4 verify `:829` grep | daily crons + booking-day gate row; watch grep → `targets=` plural |
| `src/teetime/courses/CLAUDE.md` | `:45` "Schedule is Sunday only (M6)" | → books wanted morning days (Sat+Sun); daily crons self-gate |
| `README.md` | `:20` "fire … on Sunday", `:27` "upcoming target Sunday", roadmap | Sat+Sun; daily crons; watcher checks both |
| `M6_PLAN.md` | top | SUPERSEDED-in-part banner (PR5 schedule replaced; --wait/dst_gate/watcher remain) |
| `tests/test_compute_bicep_schedule.py` | Sunday-only asserts | → daily asserts (§PR5) |
| `infra/AZURE_PLAN.md` | §10.4 watch grep `Watch check: target=` | → `Watch check: targets=` |

---

## 8. Open questions / spikes

1. **Per-day windows (out of scope, confirm)** — v0 uses the SAME `time_windows` for Sat and
   Sun. The `PrioritySlot` model already carries `target_date` (`models.py:280`) and
   `_build_priority_list` already takes `target_date` (`upgrade_orchestrator.py:450`), so a
   future per-day window is a clean extension (date-keyed priority list). Flagged, not built.
2. **Watcher load with poll-every-run × 2 dates/run** — each 10-min watch run now does up to
   2× the searches (one per wanted date per course), AND now runs across the full 24 h (the
   hours gate is gone). At Mangrove Bay (1 course, 2 dates) that is ~2 GETs every 10 min =
   ~288 GETs/day worst case, up from the old ~144 (15 h) window. Still well under any "one
   normal user" anti-bot ceiling at the 10-min/`poll_interval_s>=300` floor (PLAN.md §12;
   reversed ToS posture). If more courses × more wanted days are added later, revisit the
   per-run search budget. No action for Sat+Sun/1-course v0. **Decided, not open** — the user
   explicitly requires polling every run for drop-window visibility.
3. **Recovery booking from an early watch run (confirmed desirable)** — with the hours gate
   gone + per-date scoping, a 06:10 ET watch run that finds the just-dropped target window
   open will BOOK it (recovery if the 06:00 booking cron failed/raced). This is intended; it
   respects one-booking-per-date via the in-lock `get_terminal` re-check
   (`watch_orchestrator.py:347-349`). Asserted by `test_watch_recovery_books_just_dropped_window`.
   **Confirmed, not open.**
4. **First-Saturday-drop date** — compute from the actual prod deploy date and state it in
   the prod PR (operator-facing). Not a code question.
