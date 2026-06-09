# LEADTIME_SKIP_PLAN.md — Hard booking cutoff + no-redeploy "skip this day"

Status: **IMPLEMENTED + DEPLOYED** (ratified via plan-with-review; shipped as PRs
#107–111). The 4PM-day-before hard booking cutoff (`core/booking_cutoff.py`) and the
no-redeploy `TEETIME_SKIP_DATES` skip-days lever (`core/skip_dates.py`, fail-open,
resolved at `load()`) are both live, wired into the watcher + booking-day gate + the
`watch --date` guard, with the `TEETIME-SKIP-DATES` Key Vault secret in place. This
doc is retained as the architect output for the plan-with-review loop; line numbers
are against the files as they existed at the tag `infra/v2.0.0`.

---

## 1. Architecture summary (one paragraph)

Two independent guards bolt onto the existing per-date acting surface without touching
the RequestId fingerprint or the durable-store decision. **Feature 1 (hard cutoff)** is a
pure tz-aware predicate `is_past_booking_cutoff(now, target_date, *, timezone, cutoff)` that
returns True once wall-clock `now >= datetime(target_date - days_before, time_of_day, ET)`;
it composes (OR) with the existing `_is_past_watch_deadline` into one
`_should_stop_acting_on_date()` predicate evaluated at the very top of `check_once`, so a
past-cutoff date is frozen for BOTH new bookings AND upgrades, and is added as
defense-in-depth (always-pass for a legit today+7) to the booking-day gate. **Feature 2
(skip dates)** is a fail-open parser `parse_skip_dates(raw) -> frozenset[date]` whose source
is the env var `TEETIME_SKIP_DATES` (injected into the ACA Jobs from a Key Vault secret of
the same name, editable in the Portal with no redeploy), referenced from TOML by NAME only
(`skip_dates_env`, following the `*_env` convention). The watcher drops skipped dates before
polling AND before any upgrade; the booking-day gate refuses a skipped today+7. Both guards
take the injected `Clock` and `cfg.scheduler.timezone` — never `datetime.now`. The critical
invariant ("no authenticated Azure SDK calls at runtime") is preserved: skip dates flow ONLY
through ACA's secret→env injection, never a runtime SDK read of Key Vault.

---

## 2. Settled decisions baked in (do not re-litigate)

- After cutoff: **freeze everything** for date D — no new book, no upgrade. Whatever is held
  is final. We never auto-cancel a held booking (Edge case E5).
- Cutoff default: `days_before = 1`, `time_of_day = 16:00` ET. Ship that default.
- Cutoff is **absolute wall-clock relative to the reservation date**, tee-time-independent.
- Skip control: a Key Vault secret `TEETIME_SKIP_DATES`, env-injected per ACA execution,
  editable in the Portal with no redeploy. Plain env var supported for local/dev.
- Format: ISO dates, comma/space separated, e.g. `"2026-06-14, 2026-06-21"`.
- No durable store, no email — InMemoryStore + ConsoleNotifier stay final.

---

## 3. PR-by-PR plan (dependency order)

Five PRs. Each is independently shippable, red-green TDD, with its own adversarial review
and branch-delete (pr-pipeline). PR1→PR2, PR3→PR4, AND **PR2→PR4** are hard-ordered; PR5
(infra) depends on PR3 (it wires the env var PR3 introduced) but is otherwise standalone.

| PR | Title | Depends on |
|----|-------|-----------|
| PR1 | Cutoff core predicate + config model | — |
| PR2 | Wire cutoff into watcher + booking-day gate | PR1 |
| PR3 | Skip-dates parser + config (`skip_dates_env`) + env resolution | — |
| PR4 | Wire skip-dates into watcher + booking-day gate + `--date` guard | **PR2 AND PR3** |
| PR5 | Infra: Key Vault secret + bicep wiring + TOMLs + parity + runbook | PR3 |

**PR4 has a HARD dependency on PR2, not just PR3.** PR4 folds the skip check INTO the shared
`_should_stop_acting_on_date` composite predicate that PR2 introduces in `watch_orchestrator.py`
(§5.3); if PR4 runs before PR2 that predicate does not exist and PR4 strands. PR4 also extends
the booking-day-gate signature PR2 first widens (§5.4). Do NOT attempt PR2 and PR4 in parallel.

**Enforce SERIAL order: PR1 → PR2 → PR3 → PR4 → PR5.** The only safe parallelism is PR1 ∥ PR3
(disjoint files, no shared wiring until PR2/PR4). Everything that touches the wiring is serial.

---

### PR1 — Cutoff core predicate + config model

**Scope.** Add the pure cutoff predicate and the `BookingCutoffConfig` model. No wiring yet
(keeps the diff small and the predicate independently reviewable/tested).

**Files touched**
- NEW `src/teetime/core/booking_cutoff.py` — the predicate + cutoff-instant helper.
- `src/teetime/core/config.py` — add `BookingCutoffConfig`; add `booking_cutoff` field to
  `RequestConfig` with the shipped default.
- NEW `tests/test_booking_cutoff.py`.
- `tests/test_config.py` — assert the default cutoff round-trips.

**Failing tests to write FIRST** (name → intent):
- `test_cutoff_instant_is_4pm_day_before_in_et` — `cutoff_instant(date(2026,6,14), tz, cfg)`
  equals `2026-06-13 16:00 America/New_York` as a tz-aware datetime.
- `test_block_when_now_at_or_after_cutoff` — `now == cutoff` → True (inclusive, `>=`); and
  `now = cutoff + 1s` → True.
- `test_allow_when_now_before_cutoff` — `now = cutoff - 1s` → False.
- `test_cutoff_uses_injected_clock_not_wallclock` — FakeClock at a fixed instant drives the
  decision; no real `datetime.now` reachable (construct FakeClock both sides of the boundary).
- `test_cutoff_spring_forward_day_before` — target `2026-03-09` (Mon after spring-forward);
  D-1 = `2026-03-08` (the 02:00→03:00 skip day); `16:00` is unaffected (after the gap) and the
  instant localizes correctly via zoneinfo (offset -04:00).
- `test_cutoff_fall_back_day_before` — target `2026-11-02`; D-1 = `2026-11-01` (fall-back day,
  01:00 repeats); `16:00` is unambiguous (after the repeated hour), offset -04:00 then math is
  fine; a `now` of `2026-11-01 15:59 ET` is before, `16:00 ET` is at/after.
- `test_seven_day_out_date_never_cut_at_dawn` — defense for PR2/Edge E10: with the default
  cutoff, a `now` at the 05:50 ET booking-cron instant and `target_date = now_date + 7` is
  ALWAYS before cutoff (cutoff is target-6days, far in the future) → False. Parametrize over a
  full week + both DST seasons.
- `test_configurable_cutoff_two_days_before` — `days_before=2, time_of_day=18:00` shifts the
  instant to `D-2 18:00 ET`.
- `test_config_default_cutoff` (in test_config.py) — loading a TOML with no `booking_cutoff`
  yields `days_before==1`, `time_of_day==time(16,0)`.

**Minimal impl.** `cutoff_instant()` does calendar subtraction (`target_date -
timedelta(days=days_before)`) then `datetime.combine(d, time_of_day).replace(tzinfo=ZoneInfo(tz))`
(NOT `astimezone` — we localize a wall-clock time in that zone; zoneinfo resolves the DST
offset for that local datetime). `is_past_booking_cutoff()` compares `clock.now_utc()` against
that instant with `>=`. Pydantic `BookingCutoffConfig(days_before: int = 1, time_of_day: time
= time(16,0))` with a validator: `days_before >= 0`.

**Docs to update.** PLAN.md (new "Booking cutoff" subsection + config schema row); CLAUDE.md
(new architectural-invariant bullet); README.md (config/env table if it lists request fields).

---

### PR2 — Wire cutoff into watcher + booking-day gate

**Scope.** Make the cutoff actually freeze acting. Introduce ONE composite predicate
`_should_stop_acting_on_date` in `watch_orchestrator.py` and call it at the top of
`check_once`. Add defense-in-depth to `booking_day_gate.should_book_today`.

**Files touched**
- `src/teetime/core/watch_orchestrator.py` — add `_should_stop_acting_on_date`; call it at
  `check_once` line ~204 (replacing the bare `_is_past_watch_deadline` call); keep
  `_is_past_watch_deadline` as a component. Thread `booking_cutoff` from config into the ctor.
- `src/teetime/core/booking_day_gate.py` — add optional `skip`/`cutoff` defense params (cutoff
  half here; skip half in PR4). Accept `request_config`-derived cutoff and apply
  `is_past_booking_cutoff` as a belt-and-suspenders refusal.
- `src/teetime/__main__.py` — pass `cfg.request.booking_cutoff` into `WatchOrchestrator` ctor
  (and into the booking-day gate call at line ~239).
- `src/teetime/core/upgrade_orchestrator.py` — NO change needed: because the cutoff is checked
  at the TOP of `check_once` (before BOTH the Gate-3 upgrade at line ~214 AND the
  `_check_course` live-reservation `_try_upgrade` at line ~314), a past-cutoff date never
  reaches `maybe_upgrade`. (Belt-and-suspenders: optionally also short-circuit at the top of
  `maybe_upgrade`; see Edge E4 — recommended for defense-in-depth but the watcher gate is the
  primary guard.)
- `tests/test_watch_orchestrator.py` — new cases.
- `tests/test_booking_day_gate.py` — new defense case.

**Failing tests to write FIRST**:
- `test_check_once_returns_none_when_past_cutoff_no_book` — store empty, slots available;
  FakeClock past cutoff for `target_date` → `check_once` returns None, `adapter.book` never
  called.
- `test_check_once_no_upgrade_when_past_cutoff_gate3` — store has BOOKED terminal,
  `one_booking_policy.enabled=True`, a higher-priority slot available; FakeClock past cutoff →
  `check_once` returns the existing `prior` (frozen), `maybe_upgrade` / `cancel_reservation`
  NEVER called. (Covers Gate-3 path, line ~211-216.)
- `test_check_once_no_upgrade_when_past_cutoff_live_reservation` — store empty BUT
  `list_reservations` returns a matching reservation, policy enabled; past cutoff →
  `_try_upgrade` / `_synthesize_managed_booking` NEVER reached. (Covers `_check_course` path,
  line ~307-316.)
- `test_check_once_books_when_before_cutoff` — same as the first but FakeClock 1s before cutoff
  → books normally (regression guard that the gate is not over-broad).
- `test_stop_acting_composes_cutoff_or_deadline` — unit test of `_should_stop_acting_on_date`:
  True if past cutoff OR past deadline; False if before both. (Deadline is LATER than cutoff,
  so prove the cutoff bites first.)
- `test_booking_gate_defense_blocks_past_cutoff` — `should_book_today` returns False if the
  candidate target is somehow past cutoff (synthetic: offset small enough that today+offset is
  tomorrow and now is already past 16:00 today). Proves defense-in-depth.
- `test_booking_gate_never_blocks_normal_seven_day_out` — with the default cutoff and the real
  05:50 cron instant + offset 7, `should_book_today` is unaffected by the cutoff param (still
  governed purely by weekday). Pin Edge E10.

**Minimal impl.** `_should_stop_acting_on_date(now, target_date) -> bool` returns
`self._is_past_watch_deadline(now, target_date) or is_past_booking_cutoff(now, target_date,
timezone=self._scheduler.timezone, cutoff=self._cutoff)`. In `check_once`, replace the
`_is_past_watch_deadline` branch (line ~204) with a call to the composite. Booking-day gate
gains an OPTIONAL `cutoff` param defaulting to None (None = skip the check, preserving existing
callers/tests); when provided, also require `not is_past_booking_cutoff(...)`.

**THREE distinct reasons → THREE distinct log lines (operator clarity).** The composite predicate
freezes a date for one of three reasons; each MUST emit a distinct INFO log line so an operator
reading the run is never confused by silence and can tell WHY a date was frozen:
- past watch deadline → the EXISTING deadline log line (unchanged).
- past hard cutoff → a NEW distinct line, e.g. `"%s frozen by 4PM-day-before cutoff"`.
- in skip set (PR4) → a NEW distinct line, e.g. `"%s skipped (TEETIME_SKIP_DATES)"`.
This applies to the `--date` cutoff no-op too (E12): the cutoff `--date` freeze logs the cutoff
line, NOT silence. So `_should_stop_acting_on_date` returns a reason (or the caller branches on
the three component checks) to emit the right message — do NOT collapse all three into one
generic "stop acting" line. Add a test asserting the cutoff freeze emits the cutoff-distinct
message (`caplog`), separate from the skip and deadline messages.

**Docs.** PLAN.md (mark cutoff wired; note compose-vs-replace decision = compose/OR);
CLAUDE.md (watcher acting-surface bullet: add cutoff to the stop-acting predicate).

---

### PR3 — Skip-dates parser + config + env resolution

**Scope.** Add the fail-open parser and the config plumbing. The TOML references the env var
by NAME (`skip_dates_env`), resolved at load time like other `*_env` fields. No watcher/gate
wiring yet.

**Files touched**
- NEW `src/teetime/core/skip_dates.py` — `parse_skip_dates(raw: str | None) -> frozenset[date]`.
- `src/teetime/core/config.py` — add `skip_dates_env: str | None = None` and a resolved
  `skip_dates: frozenset[date]` to `RequestConfig`; resolve in `load()` (new `_hydrate_skip`
  helper) so a malformed/unset env is handled at load, fail-open. Add `skip_dates_env` to the
  redaction allow-list reasoning (it is an env-var NAME — safe to show; the resolved
  `skip_dates` is non-secret dates — also safe, but redact for consistency? NO: dates are not
  PII/secret; leave visible so `show-config` reveals the active skip set, which is an operator
  affordance).
- NEW `tests/test_skip_dates.py`.
- `tests/test_config.py` — `skip_dates_env` resolves; unset env → empty frozenset (no raise).

**Failing tests to write FIRST**:
- `test_parse_empty_and_none_is_empty` — `parse_skip_dates(None)` and `parse_skip_dates("")`
  and whitespace-only → `frozenset()`.
- `test_parse_comma_separated` — `"2026-06-14,2026-06-21"` → both dates.
- `test_parse_space_separated` — `"2026-06-14 2026-06-21"` → both.
- `test_parse_mixed_comma_and_space` — `"2026-06-14, 2026-06-21,  2026-07-05"` → three.
- `test_parse_ignores_malformed_token_keeps_valid` — `"2026-06-14, garbage, 2026-06-21"` →
  `{2026-06-14, 2026-06-21}` and logs a warning for `garbage` (fail-open per Edge E6).
- `test_parse_all_malformed_is_empty_not_raise` — `"x, y"` → `frozenset()`, no exception
  (job must never crash on a fat-fingered Portal edit).
- `test_parse_dedupes` — `"2026-06-14, 2026-06-14"` → one date.
- `test_config_resolves_skip_dates_env` (config test) — env `TEETIME_SKIP_DATES="2026-06-14"`,
  TOML `skip_dates_env = "TEETIME_SKIP_DATES"` → `cfg.request.skip_dates == {date(2026,6,14)}`.
- `test_config_skip_dates_env_unset_is_empty` — `skip_dates_env` set but env var ABSENT →
  empty frozenset, NO `MissingEnvVarError` (skip dates are optional; absence = no skips, NOT
  an error — DIFFERENT from credential `*_env`, which DO raise). Justify in docstring.
- `test_config_no_skip_dates_env_is_empty` — `skip_dates_env` omitted entirely → empty.

**Minimal impl.** Parser: split on `,` then whitespace, strip, drop empties, `date.fromisoformat`
each token inside try/except — on `ValueError` log `warning` and skip that token. Return a
frozenset. Config: `_hydrate_skip(cfg)` reads `os.environ.get(cfg.request.skip_dates_env)` (if
the field is set) WITHOUT raising on absence, passes to `parse_skip_dates`, assigns
`cfg.request.skip_dates`. Note the asymmetry vs `_resolve_env`: credentials raise on absence;
skip dates fail-open to empty.

**Docs.** PLAN.md (config schema: `skip_dates_env`); CLAUDE.md (env-var inventory note);
README.md (env table).

---

### PR4 — Wire skip-dates into watcher + booking-day gate + `--date` guard

**Scope.** Honor skip dates in BOTH gates and the `--date` override.

**Files touched**
- `src/teetime/__main__.py`:
  - `_watch` (line ~404-410): after computing `target_dates`, drop any date in
    `cfg.request.skip_dates` BEFORE the `check_once` loop (skip pre-poll). For the `--date`
    branch (line ~399-403): if the explicit date is skipped, refuse with a clear
    `ClickException` (Edge E12) rather than silently booking.
  - `_run` (line ~239): pass `cfg.request.skip_dates` into the booking-day gate call.
- `src/teetime/core/booking_day_gate.py`: add optional `skip_dates: frozenset[date] = frozenset()`
  param; return False if `today + target_offset` is in `skip_dates` (don't book a skipped
  today+7).
- `src/teetime/core/watch_orchestrator.py`: thread `skip_dates` into the ctor and fold into
  `_should_stop_acting_on_date` (defense-in-depth: even if the CLI forgot to filter, the
  orchestrator refuses a skipped date for BOTH book AND upgrade). This is the load-bearing
  guard for "skipped held date must not be upgraded" (Edge E5).
- `tests/test_skip_dates.py` / `tests/test_watch_orchestrator.py` / `tests/test_booking_day_gate.py`
  / `tests/test_watch_cli.py` — cases below.

**Failing tests to write FIRST**:
- `test_booking_gate_skips_skipped_date` — `today+7` is a wanted weekday BUT in `skip_dates`
  → `should_book_today` returns False.
- `test_booking_gate_books_unskipped_wanted_date` — same but date not skipped → True
  (regression: skip set doesn't break normal booking).
- `test_check_once_no_book_when_date_skipped` — slots available, store empty, `target_date`
  in `skip_dates` → `check_once` returns None, `book` never called.
- `test_check_once_no_upgrade_when_date_skipped` — store has BOOKED + higher slot available,
  policy enabled, `target_date` skipped → returns `prior` frozen, `maybe_upgrade` never called.
  (Edge E5 — the held-but-skipped date is NOT upgraded.)
- `test_watch_cli_drops_skipped_dates_before_poll` — CLI-level: with `skip_dates` covering the
  upcoming Saturday, `_watch` calls `check_once` only for the unskipped Sunday.
- `test_watch_cli_date_override_skipped_refuses` — `watch --date <skipped>` → ClickException
  (does not silently book). Edge E12.
- `test_watch_cli_date_override_unskipped_ok` — `watch --date <unskipped wanted>` proceeds.
- `test_skip_execution_day_not_target_does_not_block` — off-by-one pin: a skip date equal to the
  EXECUTION day (today) but NOT the reservation/target date must NOT block a different target.
  Construct a run whose execution day (today / today+0) is in `skip_dates` while `target_date`
  (today+offset, or the watcher's derived target) is NOT — `should_book_today` returns True and
  `check_once` proceeds. Proves skip is compared against the RESERVATION date (today+offset /
  `target_date`), NEVER the day the job happens to execute.
- `test_skipped_date_with_stale_store_terminal_does_not_rebook` — Edge E5 stale-terminal pin:
  a held BOOKED date is skipped, then the user manually cancels on ForeUP (store still has the
  BOOKED terminal — stale). The next watch run must NOT re-book it: the skip branch of
  `_should_stop_acting_on_date` at the TOP of `check_once` short-circuits and returns BEFORE the
  Gate-3 stale-terminal / `_check_course` recovery path is reached. `book` / `maybe_upgrade`
  NEVER called. (Distinct from `test_check_once_no_upgrade_when_date_skipped`, which keeps the
  terminal; here the terminal is stale and the live reservation is gone — the skip gate must
  still win.)

**Minimal impl.** Booking gate: `if (today + timedelta(days=target_offset)) in skip_dates:
return False`. Watcher: `_should_stop_acting_on_date` also returns True if `target_date in
self._skip_dates`. CLI `_watch`: `target_dates = tuple(d for d in target_dates if d not in
cfg.request.skip_dates)` for the derived path; for `--date`, raise if the parsed date is in the
skip set.

**Docs.** PLAN.md (skip wired into both gates); CLAUDE.md (acting-surface + booking-gate
bullets); README.md (operator note: how to skip a day).

---

### PR5 — Infra: Key Vault secret + bicep wiring + TOMLs + parity + runbook

**Scope.** Wire the `TEETIME_SKIP_DATES` env var end-to-end through ACA so the Portal-edit
control works with no redeploy. This is the only PR that touches `infra/`. It must keep the
CI-enforced parity (container.toml ↔ compute.bicep) green.

**Files touched**
- `infra/bicep/modules/compute.bicep`: add to `jobSecrets` a `teetime-skip-dates` entry
  (keyVaultUrl `${keyVaultUri}secrets/TEETIME-SKIP-DATES`); add to `commonEnv` a
  `{ name: 'TEETIME_SKIP_DATES', secretRef: 'teetime-skip-dates' }`. Both booking jobs AND the
  watch job inherit `commonEnv`, so all three jobs get the value automatically.
- `infra/bicep/modules/keyvault.bicep`: add `TEETIME-SKIP-DATES` to the secret-names header
  comment (the vault does not declare secret resources; operator populates).
- `config/container.toml`: add `skip_dates_env = "TEETIME_SKIP_DATES"` under `[request]`.
- `config/local.toml`: add `skip_dates_env = "TEETIME_SKIP_DATES"` under `[request]` (so local
  dev can `export TEETIME_SKIP_DATES=...`).
- `config/example.toml`: add the same with an explanatory comment.
- `tests/test_container_config_parity.py`: the existing
  `test_every_container_env_ref_is_wired_in_compute_bicep` already covers `*_env` references —
  adding `skip_dates_env` to container.toml means the parser `_referenced_env_vars` will pick up
  `TEETIME_SKIP_DATES` (it ends in `_env`? NO — the VALUE is the env var name, the KEY is
  `skip_dates_env`). Verify: `_referenced_env_vars` scans `request` PLAYERS only, NOT top-level
  `request` keys. **So the parity test will NOT auto-discover `skip_dates_env`** — it must be
  added to `_REQUIRED_RUNTIME_ENV_VARS` (like `TWOCAPTCHA_API_KEY`) OR `_referenced_env_vars`
  extended to scan top-level `request.*_env`. RECOMMEND: extend `_referenced_env_vars` to also
  scan top-level `request` keys ending in `_env` (more general, future-proof) AND add a
  dedicated `test_skip_dates_env_wired_in_bicep`.
- Docs: `infra/AZURE_PLAN.md` (§7 secrets inventory + new "Skip dates" runbook subsection);
  `README.md`; `CLAUDE.md`; `config/CLAUDE.md` parity note if present.

**Failing tests to write FIRST**:
- `test_skip_dates_env_wired_in_bicep` — `TEETIME_SKIP_DATES` appears in compute.bicep
  `commonEnv` and `teetime-skip-dates` in `jobSecrets`.
- `test_container_toml_declares_skip_dates_env` — container.toml `[request].skip_dates_env ==
  "TEETIME_SKIP_DATES"`.
- (If extending `_referenced_env_vars`) `test_referenced_env_vars_scans_request_top_level` —
  parser discovers a top-level `request.*_env`.

**Minimal impl.** Two small bicep array additions + two TOML lines + the parity-test extension.

**Operator steps (NOT run by the agent; documented for approval):**
1. `az keyvault secret set --vault-name <kv> --name TEETIME-SKIP-DATES --value "2026-06-14"`
   (or `--value ""` to seed empty). BLOCKED by az-deploy-guard — operator runs it.
2. Deploy the bicep change (dev auto-deploys on merge; prod via `infra/v*` tag).
3. Editing the value later: Portal → Key Vault → Secrets → TEETIME-SKIP-DATES → New Version.

**Runbook latency wording (ship this, NOT "~30 min" as fact):** the PR5 runbook MUST state the
KV-reference refresh latency for ACA *Jobs* as **"pending Spike S1 — conservatively edit the
secret the night before"**, NOT assert a verified "~30 min". The ~30-min figure is the
documented App-level interval (see §7); it is NOT confirmed for Jobs and must not be committed
as fact until S1 lands. The runbook ships with the conservative "night before" guidance, which
is correct regardless of the true interval.

**Docs.** AZURE_PLAN.md §7 + runbook (see §7 of this plan); README; CLAUDE.md.

---

## 4. Config schema additions (exact TOML + model stubs)

### 4.1 TOML — add to `[request]` in BOTH `config/local.toml` and `config/container.toml`

```toml
[request]
target_offsets       = [7]
# ... existing fields ...

# Hard booking cutoff (LEADTIME_SKIP_PLAN F1): never create OR modify a booking for a
# target date once wall-clock time has passed `time_of_day` on the day `days_before` days
# before it (default: 16:00 ET the day before). Freezes whatever is held at the cutoff.
booking_cutoff = { days_before = 1, time_of_day = 16:00:00 }

# No-redeploy "skip this day" (LEADTIME_SKIP_PLAN F2): env-var NAME (never a literal date
# list). The VALUE is a comma/space-separated ISO date list, e.g. "2026-06-14, 2026-06-21".
# In prod it is a Key Vault secret injected by ACA; edit it in the Portal — no redeploy.
# Unset/empty/malformed = no skips (fail-open; the job never crashes).
skip_dates_env = "TEETIME_SKIP_DATES"
```

`config/example.toml` gets the same two lines with the explanatory comments.

### 4.2 `core/config.py` model changes (stubs)

```python
class BookingCutoffConfig(BaseModel):
    """Hard cutoff after which a target date is frozen — no new book, no upgrade.

    Absolute wall-clock relative to the reservation date (tee-time-independent):
    cutoff = datetime(target_date - days_before, time_of_day, tz=scheduler.timezone).
    Default (shipped): 16:00 ET the day before. See LEADTIME_SKIP_PLAN §F1.
    """

    days_before: int = 1
    time_of_day: time = time(16, 0, 0)

    @model_validator(mode="after")
    def _validate(self) -> BookingCutoffConfig:
        raise NotImplementedError("LEADTIME_SKIP_PLAN PR1: days_before >= 0 validation")
```

Add to `RequestConfig` (after `course_preferences`, before the migration sentinels):

```python
    # Hard booking cutoff (LEADTIME_SKIP_PLAN F1). Defaulted so existing configs load.
    booking_cutoff: BookingCutoffConfig = BookingCutoffConfig()

    # No-redeploy skip control (LEADTIME_SKIP_PLAN F2). `skip_dates_env` is an env-var NAME
    # (never a literal date list in TOML). Resolved at load() time (fail-open) into
    # `skip_dates`. NOTE: unlike credential *_env fields, an UNSET skip env is NOT an error
    # (absence = no skips), so it is resolved in load(), not via the raising _resolve_env.
    skip_dates_env: str | None = None
    skip_dates: frozenset[date] = Field(default_factory=frozenset)
```

`load()` gains a new line AFTER the player-hydration line (config.py:242) and BEFORE the
`return cfg` (config.py:243) — i.e. it becomes the new line 243, pushing `return cfg` to 244.
(The cited line 242 IS the player-hydration line; `load()` currently ends at line 243 with
`return cfg`. The skip hydration must run before the return so `cfg.request.skip_dates` is
populated on the returned config.)

```python
    cfg.request.skip_dates = _hydrate_skip(cfg.request)
```

and the helper:

```python
def _hydrate_skip(req: RequestConfig) -> frozenset[date]:
    """Resolve `skip_dates_env` → a frozenset[date], FAIL-OPEN.

    Unlike _resolve_env (credentials), an unset/empty/malformed value is NOT an error:
    it yields an empty set (no skips). The job must never crash on a fat-fingered Portal
    edit of TEETIME_SKIP_DATES. See LEADTIME_SKIP_PLAN §F2 / Edge E6.
    """
    raise NotImplementedError("LEADTIME_SKIP_PLAN PR3: resolve skip_dates_env fail-open")
```

(`date` must be imported in config.py — currently only `time` is imported from `datetime`.)

---

## 5. Stub signatures + exact insertion points

### 5.1 NEW `src/teetime/core/booking_cutoff.py` (PR1)

```python
"""Hard booking cutoff (LEADTIME_SKIP_PLAN F1).

Pure tz-aware predicate: a target date D is FROZEN (no new booking, no upgrade) once
wall-clock now has reached `cfg.booking_cutoff.time_of_day` on the day `days_before`
days before D. Absolute wall-clock relative to the reservation date — tee-time-
independent. Computed via zoneinfo so spring-forward / fall-back on D-1 resolve
correctly. Takes the injected Clock (FakeClock in tests), never datetime.now.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .clock import Clock
from .config import BookingCutoffConfig


def cutoff_instant(
    target_date: date, *, timezone: str, cutoff: BookingCutoffConfig
) -> datetime:
    """The tz-aware instant at/after which `target_date` is frozen.

    = datetime.combine(target_date - days_before, time_of_day) localized in `timezone`.
    Calendar subtraction THEN localize (zoneinfo resolves the D-1 DST offset). Reviewer
    item 2 (DST on D-1).
    """
    raise NotImplementedError("LEADTIME_SKIP_PLAN PR1: cutoff_instant")


def is_past_booking_cutoff(
    clock: Clock, target_date: date, *, timezone: str, cutoff: BookingCutoffConfig
) -> bool:
    """True iff `clock.now_utc() >= cutoff_instant(...)` (INCLUSIVE — Edge E8).

    True  -> freeze: caller must not book or upgrade `target_date`.
    False -> still actionable.
    Pure function of (clock, target_date, timezone, cutoff). FakeClock-deterministic.
    """
    raise NotImplementedError("LEADTIME_SKIP_PLAN PR1: is_past_booking_cutoff")
```

### 5.2 NEW `src/teetime/core/skip_dates.py` (PR3)

```python
"""No-redeploy 'skip this day' parser (LEADTIME_SKIP_PLAN F2).

Parses the TEETIME_SKIP_DATES value (comma/space-separated ISO dates) into a
frozenset[date]. FAIL-OPEN: empty/unset/malformed input yields the dates it CAN parse
(or an empty set), never raises — a fat-fingered Portal edit must not crash the job.
The env var is injected into the ACA Jobs from a Key Vault secret (per-execution); see
LEADTIME_SKIP_PLAN §7 for the ACA secret-refresh guarantee.
"""

from __future__ import annotations

from datetime import date


def parse_skip_dates(raw: str | None) -> frozenset[date]:
    """Parse `raw` into a frozenset of ISO dates. Fail-open.

    - None / "" / whitespace-only -> frozenset().
    - Tokens split on commas AND whitespace; each parsed via date.fromisoformat.
    - An UNPARSEABLE token is logged (warning) and SKIPPED; other valid tokens still
      apply (Edge E6 — partial-parse, not fail-closed). Dedupes.
    """
    raise NotImplementedError("LEADTIME_SKIP_PLAN PR3: parse_skip_dates")
```

### 5.3 `core/watch_orchestrator.py` wiring (PR2 + PR4)

Ctor (line ~157-175) gains two params (defaulted so existing tests/constructors keep working):

```python
        booking_cutoff: BookingCutoffConfig | None = None,   # PR2
        skip_dates: frozenset[date] = frozenset(),           # PR4
```
stored as `self._cutoff` / `self._skip_dates`.

New method (insert near `_is_past_watch_deadline`, line ~470):

```python
    def _should_stop_acting_on_date(self, now: datetime, target_date: date) -> bool:
        """Single 'stop acting on this date' predicate (Reviewer item 3).

        Returns True (freeze: no book, no upgrade) if ANY hold: past the watch deadline
        OR past the hard booking cutoff (LEADTIME_SKIP_PLAN F1) OR the date is skipped
        (F2). Composes (OR) — the cutoff is strictly EARLIER than the deadline, so it
        bites first. Evaluated at the TOP of check_once, before BOTH the book path and
        BOTH upgrade entry points (Gate-3 line ~211 and _check_course line ~308).

        Each of the THREE reasons emits its OWN distinct INFO log line (deadline / cutoff /
        skip) so an operator can tell WHY a date froze — do not collapse to one message.
        The caller (check_once) branches on the three component checks to log the right one;
        this method may return the reason or the caller may re-check the components.
        """
        raise NotImplementedError("LEADTIME_SKIP_PLAN PR2/PR4: compose deadline+cutoff+skip")
```

`check_once` change: replace the line-204 `if self._is_past_watch_deadline(now, target_date):`
guard with `if self._should_stop_acting_on_date(now, target_date):` (same log+`return None`).
Because this sits ABOVE the Gate-3 `prior`/upgrade block (line ~209-222) and the search loop,
it gates new bookings AND both upgrade paths in one place (Reviewer item 4).

### 5.4 `core/booking_day_gate.py` wiring (PR2 cutoff half, PR4 skip half)

`should_book_today` gains two optional params (default = no-op so existing 13 parametrized
tests pass unchanged):

```python
def should_book_today(
    clock: Clock,
    *,
    timezone: str,
    target_offset: int,
    wanted_weekdays: frozenset[int],
    skip_dates: frozenset[date] = frozenset(),          # PR4
    cutoff: BookingCutoffConfig | None = None,          # PR2 defense-in-depth
) -> bool:
    """... existing docstring ...

    Defense-in-depth (LEADTIME_SKIP_PLAN): also return False if `today + target_offset`
    is in `skip_dates`, or (when `cutoff` is provided) is already past its hard cutoff.
    With the default 16:00-day-before cutoff and a 7-day offset the cutoff CANNOT bite a
    normal booking (target is 6 days out), so this never blocks a legit run (Edge E10).
    """
```

Body adds, after computing `target` (line 67): `if target in skip_dates: return False`; and if
`cutoff is not None and is_past_booking_cutoff(clock, target, timezone=timezone,
cutoff=cutoff): return False`. (Import `is_past_booking_cutoff` lazily inside the function or at
module top — watch for an import cycle: `booking_cutoff` imports `config`, `booking_day_gate`
imports `booking_cutoff` and `config`; no cycle since `config` imports neither gate.)

### 5.5 `__main__.py` wiring (PR2 + PR4)

- `_run` line ~239-244: extend the `should_book_today(...)` call with
  `skip_dates=cfg.request.skip_dates, cutoff=cfg.request.booking_cutoff`.
- `_watch` line ~404-410 (derived path): `target_dates = tuple(d for d in target_dates if d not
  in cfg.request.skip_dates)`. Line ~399-403 (`--date` path): after parsing, `if parsed in
  cfg.request.skip_dates: raise click.ClickException(...)`.
- `_watch` WatchOrchestrator ctor (line ~432-441): pass
  `booking_cutoff=cfg.request.booking_cutoff, skip_dates=cfg.request.skip_dates`.

---

## 6. Bicep / Key Vault wiring snippet (PR5)

`infra/bicep/modules/compute.bicep` — add to `jobSecrets` (after the twocaptcha entry, line ~153):

```bicep
  { name: 'teetime-skip-dates', keyVaultUrl: '${keyVaultUri}secrets/TEETIME-SKIP-DATES', identity: userAssignedIdentityResourceId }
```

and to `commonEnv` (after TWOCAPTCHA_API_KEY, line ~181):

```bicep
  { name: 'TEETIME_SKIP_DATES', secretRef: 'teetime-skip-dates' }
```

All three jobs (`-edt`, `-est`, watch) consume `commonEnv`, so one addition covers them all.
`keyvault.bicep` header comment gains `TEETIME-SKIP-DATES` in the secret-names list.

---

## 7. RESOLVED: ACA Key Vault secret-refresh semantics (the load-bearing answer)

**Question:** when an ACA *Job* execution starts, does it re-resolve the Key-Vault-referenced
secret to the latest value, or is there caching?

**Answer (with the honest caveat):** Azure Container Apps resolves Key Vault secret references
through the app/job's managed identity and **caches the resolved value with a refresh interval
of 30 minutes** (the platform periodically re-reads KV-referenced secrets; it does NOT
necessarily fetch a brand-new value on every single job execution). A brand-new revision/job
re-resolves immediately at creation; thereafter the cached value is refreshed on the platform's
interval (documented as ~30 min for Container Apps KV secret references). For a **Job**, each
scheduled execution pulls the *currently cached* secret value for the job resource; it is not
guaranteed to be a fresh KV read per execution.

**Operational consequence / guarantee we ship:** a Portal edit of `TEETIME-SKIP-DATES` takes
effect after the ACA KV-reference refresh latency, with NO redeploy and NO new revision
required. That latency is the documented ~30 min for *Apps* but is **NOT yet confirmed for
*Jobs*** (Spike S1) — so we ship the runbook with the **conservative "edit the night before"**
guidance rather than asserting ~30 min as fact. Given the watch cron fires every 10 min and the
booking cron fires at 05:50 ET, editing the evening before is reliably in effect well before the
next run regardless of the true interval. **Operator guidance: make the skip edit the night
before the relevant booking/watch run** (trivially satisfied for "I'm out of town this Sunday").

**Honest note — the cutoff does NOT cover the stale-skip race.** These two features address
DIFFERENT risks and one does not backstop the other:
- The hard cutoff (F1) only ever FREEZES a date as it nears T0; it never *unbooks*. If a skip
  edit lands too late (stale cached secret), the bot will already have BOOKED a date the user
  wanted skipped, and the 16:00-day-before cutoff does NOT undo that — it only prevents
  *further* action on an already-near date. The cutoff is not a safety net for skip staleness.
- A stale skip secret therefore means: **the bot books a day the user meant to skip.** This is
  a real failure mode, not hand-waved away. We accept it because (a) the use case is plan-ahead
  ("I'll be away next Sunday"), edited days in advance — far outside any refresh window; and (b)
  the alternative (runtime KV SDK read for per-execution freshness) is forbidden by the
  no-runtime-SDK invariant. Risk is REAL but operationally LOW for the intended plan-ahead use;
  do NOT oversell the cutoff as covering it.

**Why not force immediacy?** Immediate per-execution freshness would require either (a) a
runtime Azure SDK read of Key Vault — **FORBIDDEN by the invariant "the bot makes no
authenticated Azure SDK calls at runtime"** — or (b) bumping a new job revision on every edit
(a redeploy, which the feature explicitly avoids). So we accept the ~30-min ACA refresh latency
and document it. This is the honest tension surfaced per reviewer item 1: env-injection gives us
no-redeploy editing at the cost of up-to-~30-min staleness; that latency is operationally
irrelevant for a "skip a day I planned days ahead" use case.

**CONFIDENCE / verification note:** The 30-minute figure is the widely-documented Container Apps
KV-secret-reference refresh interval for *Apps*. Microsoft's docs are less explicit about
*Jobs* specifically. **Spike S1 (below) must confirm the exact Job behavior on live Azure
before PR5 merges** — the runbook's "takes effect by ~30 min" guarantee depends on it. If the
live test shows Jobs re-resolve per-execution (better) or on a different interval, update the
runbook's stated guarantee accordingly. The feature design is unaffected either way (it only
changes the documented latency number).

### 7.1 Portal edit + verify runbook (for AZURE_PLAN.md §7)

1. **Edit:** Portal → `kv-teetime-<env>` → Secrets → `TEETIME-SKIP-DATES` → "+ New Version" →
   set value to the comma/space ISO list (e.g. `2026-06-14, 2026-06-21`) → Create. (Empty value
   = no skips.) No redeploy, no new job revision.
2. **Latency:** pending Spike S1 (the ~30-min figure is App-level, unconfirmed for Jobs).
   Conservatively **edit the night before** — correct regardless of the true interval. NOTE:
   a too-late edit will NOT un-book an already-booked date; the cutoff does not cover this.
3. **Verify the value is live (read-only, agent-safe):**
   - `az keyvault secret show --vault-name <kv> --name TEETIME-SKIP-DATES --query value -o tsv`
     confirms the stored value (this is the SOURCE, not what the job has cached).
   - To confirm the JOB sees it: trigger a manual watch execution AFTER the refresh window and
     read its logs — the watch run logs `Watch check: targets=[...]` (line ~412); a skipped date
     will be ABSENT from that list. (`az containerapp job start` is operator-only — az-deploy-guard.)
4. **Un-skip:** add a new version with the date removed (or empty). Same ~30-min latency.

---

## 8. Edge cases & decisions (every pre-emption item)

- **E1 ACA refresh** — §7. ~30-min KV-reference cache; no-redeploy edit; runtime SDK read
  forbidden so we accept the latency. Spike S1 confirms Job specifics.
- **E2 DST on D-1** — `cutoff_instant` does calendar `- timedelta(days=days_before)` THEN
  localizes `16:00` in ET via `ZoneInfo` (NOT `astimezone` of a UTC value). 16:00 is never in
  the spring-forward gap (02:00-03:00) or the fall-back repeat (01:00-02:00), so the local
  16:00 is always unambiguous; zoneinfo picks the correct UTC offset for that calendar day.
  Comparison is tz-aware (`clock.now_utc()` is UTC; the cutoff instant is ET-aware; Python
  compares correctly). Tests `test_cutoff_spring_forward_day_before` /
  `_fall_back_day_before`.
- **E3 Compose with `_is_past_watch_deadline`** — ONE predicate `_should_stop_acting_on_date`
  = deadline OR cutoff OR skip, evaluated at `check_once` line ~204 (top), gating the book
  path AND both upgrade paths. Decision: **compose (OR), do not replace** — cutoff is stricter
  (earlier) and the deadline still matters for the after-the-round case.
- **E4 Upgrade-freeze coverage** — both upgrade entry points are downstream of the top-level
  gate: (a) Gate-3 store-BOOKED `_try_upgrade` (line ~211-216) and (b) `_check_course`
  live-reservation `_try_upgrade` (line ~307-316). The cutoff/skip check at line ~204 runs
  BEFORE both, so neither is reached when frozen. Optional belt-and-suspenders short-circuit at
  the top of `maybe_upgrade` (upgrade_orchestrator.py line ~199) — recommended but secondary.
  Tests `test_check_once_no_upgrade_when_past_cutoff_gate3` /
  `_live_reservation` / `_when_date_skipped`.
- **E5 Held booking enters skip / passes cutoff** — we do NOT auto-cancel. Skip/cutoff only
  prevent NEW bookings + upgrades. The Gate-3 path returns the existing `prior` (frozen) so the
  user keeps the booking. The user cancels manually on ForeUP if they want it gone; with the
  date skipped the watcher will NOT re-book it (the whole point). The skip check precedes BOTH
  the book and the upgrade paths (E3/E4), so a skipped held date is never upgraded. Documented;
  test `test_check_once_no_upgrade_when_date_skipped`.
- **E6 Malformed/empty TEETIME_SKIP_DATES** — **fail-open, partial-parse**: parse the tokens we
  can, log+skip the ones we can't, never raise. Empty/unset = no skips. Justification: a Portal
  typo must NEVER crash the booking job (which would be a worse failure than missing a skip —
  it could take down the 06:00 booker). Fail-closed (treat malformed as "skip everything") would
  silently stop ALL bookings on a typo — strictly worse. Tests
  `test_parse_ignores_malformed_token_keeps_valid` / `_all_malformed_is_empty_not_raise`.
- **E7 RequestId stability** — NEITHER cutoff NOR skip dates feed the RequestId fingerprint.
  The fingerprint encodes windows (`<wd>:HH:MM-HH:MM`), course prefs, players, offset — NOT
  cutoff/skip. `booking_cutoff` and `skip_dates`/`skip_dates_env` are added to `RequestConfig`
  but `_build_request` (line ~626-665) does not include them in the request identity. STATED
  EXPLICITLY so in-process idempotency keys are unchanged. (Verify in PR1/PR3: a test asserting
  RequestId is identical with/without a cutoff/skip config — add
  `test_request_id_unaffected_by_cutoff_and_skip`.)
- **E8 Cutoff boundary** — INCLUSIVE: freeze when `now >= cutoff_instant` (a booking attempt
  landing exactly at 16:00:00 is blocked). Stated in `is_past_booking_cutoff` docstring; test
  `test_block_when_now_at_or_after_cutoff`.
- **E9 Config schema** — cutoff lives in `RequestConfig.booking_cutoff`
  (`{days_before, time_of_day}`); skip resolves from `RequestConfig.skip_dates_env` (env-var
  NAME) → `skip_dates` at load. Exact TOML in §4.1, model stubs in §4.2, bicep in §6.
- **E10 Booking-job cutoff defense cannot block a normal today+7** — with the default
  (`16:00` day-before), the cutoff instant for `today+7` is `today+6 @ 16:00 ET`, which is
  ~6 days in the FUTURE at the 05:50 cron — `is_past_booking_cutoff` is always False. Test
  `test_seven_day_out_date_never_cut_at_dawn` (PR1) + `test_booking_gate_never_blocks_normal_
  seven_day_out` (PR2), parametrized across the week and both DST seasons.
- **E11 Timezone + clock source** — ALL "today"/cutoff math uses `cfg.scheduler.timezone` for
  the ZONE (one source: the cutoff helper takes `timezone=` explicitly; the watcher passes
  `self._scheduler.timezone`; the booking gate takes `timezone=cfg.scheduler.timezone`; the CLI
  reads `cfg.scheduler.timezone`). For the CLOCK, the load-bearing guard is clock-driven:
  `_should_stop_acting_on_date` / `is_past_booking_cutoff` / `should_book_today` all take the
  injected `Clock` (FakeClock-deterministic) — these are what actually freeze/skip a date.
  **EXEMPTION (pre-existing, out of scope):** the CLI `_watch` date-DERIVATION
  (`__main__.py:405`, `datetime.now(tz=ZoneInfo(...)).date()`) reads wall-clock, NOT the injected
  clock. That only chooses WHICH upcoming dates to check; every load-bearing freeze/skip decision
  on each derived date still runs through the clock-driven predicate. We do NOT claim "ALL
  today/cutoff math uses the injected Clock" — the cutoff/skip GUARD is clock-driven; the CLI
  target-date derivation is wall-clock and is explicitly exempt (fixing it is a separate change).
- **E12 `--date` watch override interaction** — if `watch --date D` names a SKIPPED date →
  explicit `ClickException` ("date D is in TEETIME_SKIP_DATES; remove it or pick another").
  If `--date D` names a PAST-CUTOFF date → the orchestrator's `_should_stop_acting_on_date`
  returns None (frozen) and logs the DISTINCT cutoff line (`"%s frozen by 4PM-day-before
  cutoff"`, item 5) — NOT silence; we do NOT hard-error there because cutoff is a time-based
  natural no-op (the existing deadline `--date` behavior is also a logged no-op). Decision:
  REFUSE on skip (operator intent conflict), LOG+no-op on cutoff (time has simply passed), and in
  BOTH cases emit a reason so the operator is never left guessing why nothing happened. Tests
  `test_watch_cli_date_override_skipped_refuses` / `_unskipped_ok` + the cutoff-log-line test.

---

## 9. Open questions / spike tasks

- **S1 (spike, gates PR5 runbook).** Confirm on LIVE Azure whether an ACA *Job* scheduled
  execution re-resolves a Key-Vault-referenced secret per-execution or honors the ~30-min
  App-level cache. **Question:** does a `TEETIME-SKIP-DATES` value edited at T appear in a job
  execution started at T+1min, or only after ~30min? **Exit criterion:** a logged watch run
  whose `targets=[...]` reflects (or does not reflect) a just-edited skip secret, timestamped,
  so the runbook's "effective within ~30 min" guarantee is either confirmed or corrected. This
  is operator-run (needs `az keyvault secret set` + `az containerapp job start`, both
  az-deploy-guard-blocked). The feature ships either way; only the documented latency number
  depends on it.
- **Q1 (user).** Should the cutoff also apply to the 06:00 booking job as a HARD refusal, or
  stay defense-in-depth only? Current plan: defense-in-depth (it can never bite a legit today+7,
  E10), and the real enforcement is the watcher. Confirm that's the intended posture.
- **Q2 (user).** Should `show-config` display the resolved `skip_dates`? Plan: YES (operator
  affordance; dates are not secret/PII). Confirm.

---

## 10. File-by-file summary (create / touch)

| File | PR | Change |
|------|----|--------|
| `src/teetime/core/booking_cutoff.py` | PR1 | NEW — `cutoff_instant`, `is_past_booking_cutoff` |
| `src/teetime/core/config.py` | PR1,PR3 | `BookingCutoffConfig`; `RequestConfig.booking_cutoff`, `skip_dates_env`, `skip_dates`; `_hydrate_skip`; import `date` |
| `tests/test_booking_cutoff.py` | PR1 | NEW — cutoff predicate tests |
| `tests/test_config.py` | PR1,PR3 | default cutoff + skip-env resolution tests |
| `src/teetime/core/watch_orchestrator.py` | PR2,PR4 | ctor params `booking_cutoff`/`skip_dates`; `_should_stop_acting_on_date`; gate at `check_once` line ~204 |
| `src/teetime/core/booking_day_gate.py` | PR2,PR4 | optional `skip_dates`/`cutoff` defense params |
| `src/teetime/core/upgrade_orchestrator.py` | PR2 | (optional) belt-and-suspenders cutoff/skip short-circuit in `maybe_upgrade` |
| `src/teetime/__main__.py` | PR2,PR4 | pass cutoff+skip into gate + watcher; `_watch` drop/refuse skipped dates |
| `tests/test_watch_orchestrator.py` | PR2,PR4 | cutoff + skip freeze/upgrade-freeze cases |
| `tests/test_booking_day_gate.py` | PR2,PR4 | defense + never-block-7-day-out + skip cases |
| `src/teetime/core/skip_dates.py` | PR3 | NEW — `parse_skip_dates` |
| `tests/test_skip_dates.py` | PR3,PR4 | parser + wiring cases |
| `tests/test_watch_cli.py` | PR4 | `--date` skip refuse + drop-before-poll cases |
| `infra/bicep/modules/compute.bicep` | PR5 | `teetime-skip-dates` secret + `TEETIME_SKIP_DATES` env |
| `infra/bicep/modules/keyvault.bicep` | PR5 | secret-name header comment |
| `config/container.toml` | PR5 | `booking_cutoff` (PR1 docs note) + `skip_dates_env` |
| `config/local.toml` | PR5 | same |
| `config/example.toml` | PR5 | same + comments |
| `tests/test_container_config_parity.py` | PR5 | discover/assert `TEETIME_SKIP_DATES` wiring |
| `PLAN.md` | all | cutoff + skip sections, config schema |
| `CLAUDE.md` | all | invariant bullets (cutoff freeze, skip, ACA refresh) |
| `README.md` | all | config/env tables, operator skip note |
| `infra/AZURE_PLAN.md` | PR5 | §7 secret + Portal runbook + refresh latency |

Note: the `booking_cutoff` TOML line is shipped in PR5 with the other config edits, but the
MODEL/default lands in PR1 — existing configs without the line load fine (defaulted), so PR1 is
independently mergeable without touching the TOMLs.

---

## 11. Summary

**PR list:** PR1 cutoff predicate + config model → PR2 wire cutoff into watcher + booking gate
→ PR3 skip-dates parser + config (`skip_dates_env`) → PR4 wire skip into watcher + booking gate
+ `--date` guard → PR5 infra (KV secret + bicep + TOMLs + parity + runbook).

**ACA-refresh answer:** ACA caches Key-Vault-referenced secrets (no per-execution guaranteed
re-read, no runtime SDK read allowed), so a Portal edit of `TEETIME-SKIP-DATES` takes effect with
NO redeploy after the platform refresh latency. That latency is ~30 min for *Apps* but is
UNCONFIRMED for *Jobs* (Spike S1) — PR5's runbook ships the conservative "edit the night before"
guidance, NOT a verified "~30 min". Operationally fine for "skip a day planned in advance".
Honest caveat: a too-late skip edit means the bot books a day the user wanted skipped, and the
cutoff does NOT un-book it (§7) — real but low-probability for plan-ahead use.

**Open questions:** S1 (live ACA Job KV-refresh timing — gates the runbook number); Q1 (cutoff
on the 06:00 job: hard-refuse vs defense-in-depth — plan says defense-in-depth); Q2 (show
resolved `skip_dates` in `show-config` — plan says yes).
