# M6 — First Production Cron Run (implementation plan)

> **⚠️ SUPERSEDED IN PART by `MULTIDAY_PLAN.md` (and then `PERDAY_WINDOWS_PLAN.md`).** The
> **Sunday-only schedule** described here (PR5: two `… * * 0` Sunday crons, jobs `-edt-sun`/
> `-est-sun`) was replaced by **DAILY crons + a booking-day gate** (`core/booking_day_gate.py`),
> jobs renamed `-edt`/`-est`, and per-day time windows; the watcher's `target_weekday` anchor +
> polling-hours gate were replaced by the multi-date `next_occurrences_within_horizon` + poll-
> every-run. STILL IN FORCE from this plan: `run --wait` busy-wait, `core/dst_gate.py`, watcher
> enablement, `bookingReplicaTimeout`. Read this doc as historical for the schedule specifics.

Architect plan for the milestone that takes the **booking** and **watch** ACA Jobs from
"dry-run-only, books at the wrong time, watcher disabled" to "books at the exact
06:00:00 ET drop every Sunday and actively watches/upgrades", verified in dev with
`dryRun=true`, then cut over to `dryRun=false` in prod.

---

## Round-2 responses (reviewer BLOCK)

1. **replicaTimeout vs 10-min busy-wait** — *fixed* — §2 PR3 (new "Replica-timeout arithmetic"
   subsection) + §2 PR3 bicep stub + §5.1 + R7. The booking job gets a dedicated
   `bookingReplicaTimeout = 1200` (20 min); arithmetic worked through for the early-jitter worst
   case (~870 s). The watch job keeps `watchReplicaTimeout = 120`. The old shared
   `replicaTimeout = 900` var is removed (no remaining consumer).
2. **Busy-wait timing only verifiable on live Sunday cron** — *fixed (accepted as exit criterion)* —
   §6.1 + new §6.5. I add a test-only `--fire-time HH:MM:SS` override (dev/test escape hatch,
   refused unless `--dry-run true`) so an operator can run an on-demand `--wait --dry-run true`
   job whose DST gate and busy-wait are satisfiable at any wall-clock hour, exercising the real
   busy-wait + race-complete log without waiting for Sunday. The unit-level proof is FakeClock
   (PR6 test). The *production-identical* (fire_time=06:00, real cron jitter) timing is accepted
   as verifiable ONLY on the live Sunday cron — stated plainly as an M6 exit criterion.
3. **NTP offset never exercised before first prod run** — *fixed* — §2 PR1 (re-gate) + R5. The NTP
   measurement is decoupled from `dry_run` and re-gated on `wait and not use_fake_adapter`, so the
   dev `--wait --dry-run true` run DOES probe UDP:123 reachability and logs the offset/warning.
   PR1's `test_run_wait_measures_ntp_offset_only_when_live` is renamed/rewritten accordingly.
4. **FakeAdapter.book_call_count already exists** — *fixed* — confirmed at `fake_adapter.py:51`
   (incremented `:116`). Dropped extension E3; PR4 and §8 now say "use the existing
   `book_call_count`."
5. **Wrong-season-cron rationale is one-directional** — *fixed* — §2 PR2 (jitter bullet 3 rewritten)
   + §3 + dst_gate docstring now state BOTH failure modes the gate prevents: EST-cron-in-EDT books
   ~50 min late (past T0); EDT-cron-in-EST would busy-wait ~70 min into the replicaTimeout and get
   killed. The gate is necessary in both directions.
6. **Asserted log text not verified vs real format** — *fixed* — §6.1/§6.2 grep strings pinned to
   the ACTUAL rendered output: booking line renders `target=['2026-06-07']` (quoted list,
   `__main__.py:140-145`); watch line renders `Watch check: target=2026-06-07` (bare date,
   `__main__.py:270`).
7. **Daily watch + target=today+7 ranks every day** — *fixed (flagged as follow-up)* — §4.3 +
   new Open Question 6. Daily DRY_RUN ranking on non-intended dates is harmless ONLY while
   `one_booking_policy.enabled=false` (M6 keeps it off). Daily-watch + policy-on is now an explicit
   NEW follow-up design question, not a settled M6 decision.
8. **Unfinished test-table row** — *fixed* — §2 PR2 `test_gate_fall_back_wrong_cron_skips` row
   completed.
9. **Remove E3** — *fixed* — §8 E3 deleted (see item 4).
10. **`output jobName = bookingJob[0].name` consumer audit** — *fixed* — §2 PR5 (new "Output +
    consumer audit" bullet). After PR5 index 0 is `-edt-sun`. Consumers audited: `main.bicep:147`
    re-exports it; `AZURE_PLAN.md:407` runbook text is `teetime-job-<envName>` (suffix-less and
    already imprecise — fixed to name the four/two real job names). No consumer assumes `-edt-sat`.
11. **local.toml PLAYER2/3/4 divergence** — *fixed* — §2 PR4 ("Config parity" note). local.toml's
    PLAYER2/3/4_EMAIL refs (`:27,32,37`) are DELIBERATE (local dev uses real per-guest emails);
    container.toml's name-only guests are the foursome invariant. PR4 does NOT mirror PLAYER2/3/4
    into container; the parity test compares container↔bicep, so the divergence is intended and
    left untouched.
12. **`_resolve_wait_mode` call site vs mypy** — *fixed* — §2 PR1 stub now shows `_resolve_wait_mode`
    called in `run_cmd` (resolving `bool | None` → `bool`) BEFORE `asyncio.run(_run(..., wait=bool))`,
    so `_run` takes a plain `bool`.

---

> Scope guard: this plan does NOT implement M2.T3 reconciliation
> (UNCERTAIN→RECONCILING→BOOKED/LOST). That remains separate. M6 wires the *timing*,
> *DST gate*, *watcher enablement*, and the *Sunday-only schedule* — everything needed for
> the first real cron run — on top of the already-shipped orchestrators.

---

## 1. Executive summary + execution-mode decision

### 1.1 The four gaps (verified in code today)

| # | Gap | Evidence |
|---|-----|----------|
| 1 | **Booker never busy-waits to T0.** `_run()` always builds `_local_demo_scheduler(cfg.scheduler)` which sets `fire_time = now`, so `Orchestrator._compute_t0_minus_early()` returns ~now and `busy_wait_until` returns immediately. The cron fires at :50 (≈05:50 ET) and the bot books *before slots release*. | `src/teetime/__main__.py:166` (`scheduler = _local_demo_scheduler(cfg.scheduler)`), `:431-442` (`_local_demo_scheduler` sets `fire_time=time(now…)`), `core/orchestrator.py:76` (`busy_wait_until(self._compute_t0_minus_early(), …)`). |
| 2 | **No DST-half gate anywhere in the bot.** `compute.bicep` registers 4 crons (Sat/Sun × EDT/EST); the wrong-season same-day cron will compute a *past* T0 once the real busy-wait is wired and book ~50 min late. The gate previously lived ONLY in the deleted `book.yml` `dst` step. | `infra/bicep/modules/compute.bicep:78-105` (4 crons, comment "DST gate in bot"), PLAN.md §6.3 ⚠️ status note (line 212), grep for `dst`/`ZoneInfo.*hour` in `__main__.py` → none. |
| 3 | **Watcher disabled.** `watch_cmd` early-returns because `cfg.watcher.enabled` defaults `False` and no config sets a `[watcher]` block. | `core/config.py:105` (`enabled: bool = False`), `__main__.py:228-235` (early return), `config/container.toml` has no `[watcher]`. |
| 4 | **Schedule books Saturdays too.** `bookingJobs` has 4 entries incl. two `-sat`. The user wants **Sunday only**. | `compute.bicep:100-105`. |

### 1.2 Execution-mode decision (prod cron vs local demo) — **EXTENSION, not change**

The ratified architecture already contains the real-timing path (`_compute_t0_minus_early` +
`busy_wait_until` + NTP offset). The only missing thing is a *selector* that lets the ACA cron
take the real path while local demo runs keep the immediate path. I propose a single explicit
flag with a **safe default of immediate (no-wait)**, plus an env override so the container args
stay simple:

- **`teetime run --wait / --no-wait`** click flag.
  - **Default `--no-wait`** (preserves current local-demo behaviour; nobody accidentally hangs a
    laptop until 06:00 ET).
  - `--wait` selects the REAL `cfg.scheduler` (no `_local_demo_scheduler`), enables the NTP
    offset measurement, and runs `busy_wait_until(T0 - early_arrival_ms)`.
- **Env fallback `TEETIME_WAIT=1`** consulted only when neither `--wait` nor `--no-wait` is
  passed explicitly. This lets the ACA job set the mode via `commonEnv` without editing the
  cron `args` per environment, and keeps a single source of truth (the bicep env block) for
  "this deployment is a real run." The CLI flag, when present, always wins.
- **ACA booking job passes `--wait`** (explicit, in `args`), so the behaviour is legible from the
  bicep without chasing env vars. The env fallback exists for on-demand/manual invocations.

Decision rationale: a flag (not "detect if running in ACA") keeps the timing path *testable* and
*explicit*. `--no-wait` default means the dangerous behaviour (multi-hour busy-wait) is never the
default; you must opt in. This is the smallest extension that closes gap #1 without touching the
orchestrator.

`--wait` also governs the **DST gate** (gap #2): the gate only runs on the real-timing path. A
`--no-wait` run is by definition a manual/local/demo run and must **bypass** the gate (this is
the old `workflow_dispatch` "always proceed" behaviour). See §3.

### 1.3 Watcher "look but don't book" steady state (NEW user requirement)

The watch path already does "look but don't book" under `dry_run=true`:
`WatchOrchestrator._book_candidates` returns a `DRY_RUN` `BookingResult` *before* acquiring the
lock or POSTing (`watch_orchestrator.py:328-338`), and `_try_upgrade` →
`UpgradeOrchestrator.maybe_upgrade` is gated on `request.dry_run` inside the upgrade path. So the
required dev steady state — **watcher fully enabled (`enabled=true`) but suppressing only the
final POST** — is `watcher.enabled = true` + `--dry-run true`. M6 turns the watcher on in config
and confirms the dry-run suppression is truthful (§4.2). No orchestrator change needed; this is
pure config + a confirmation test.

### 1.4 Headline

Add a `--wait/--no-wait` selector + a DST-half gate to the entrypoint so the booking ACA job
busy-waits to the exact 06:00:00 ET drop and the wrong-DST-season cron exits cleanly; enable the
watcher in container config (look-but-don't-book under dry-run); narrow the booking schedule to
Sunday-only; keep the watch cron daily. Verify both jobs in dev under `dryRun=true` via explicit
log assertions, then cut prod to `dryRun=false`.

---

## 2. PR-by-PR plan (ordered)

Six PRs. PR1→PR2→PR3 are the booking timing/DST chain (must land in order). PR4 (watcher
config) and PR5 (bicep schedule) are independent of the timing chain and of each other but both
touch parity-checked files, so they serialize on merge to avoid parity-test churn. PR6 is
docs/verification-only and lands last.

```
PR1 (--wait flag) ─► PR2 (DST gate) ─► PR3 (bicep: --wait + lead-time) ─┐
PR4 (watcher config) ───────────────────────────────────────────────────┤
PR5 (bicep: Sunday-only + watch decision) ──────────────────────────────┤
                                                                          └─► PR6 (verification runbook + docs)
```

> PR3 and PR5 both edit `compute.bicep`. To avoid a merge conflict, **PR5 lands first**
> (schedule shape), then PR3 rebases and adds the `--wait` arg + cold-start lead. The ordering
> above shows the logical dependency (PR1→PR2→PR3); the *merge* order is PR1, PR2, PR4, PR5, PR3, PR6.

---

### PR1 — `teetime run --wait/--no-wait` selects the real-timing scheduler

**Scope.** Add the execution-mode selector. `--wait` uses the real `cfg.scheduler` (the
06:00:00 ET busy-wait + NTP offset); `--no-wait` (default) keeps `_local_demo_scheduler`.
Wire the `TEETIME_WAIT` env fallback. Re-gate the NTP offset on `wait` (NOT `dry_run`) per
reviewer item 3. Add the dev/test `--fire-time HH:MM:SS` override (E3; refused unless
`--dry-run true`) that powers the §6.5 on-demand busy-wait verification. No orchestrator changes.

**Files touched.**
- `src/teetime/__main__.py` — add the flag to `run_cmd`, thread `wait` into `_run`, branch the
  scheduler + NTP-offset selection.
- `tests/test_cli.py` — red tests below.

**Red tests first (FakeClock where time matters; click `CliRunner` + FakeAdapter for CLI).**
- `test_run_no_wait_uses_demo_scheduler_fires_immediately` — with `--no-wait` + FakeAdapter,
  `_run` builds a scheduler whose `fire_time` ≈ now and the run completes without a long wait.
  Assert by patching `_local_demo_scheduler` is invoked (spy) OR asserting the resolved
  scheduler's `early_arrival_ms == 0`.
- `test_run_wait_uses_real_scheduler` — with `--wait`, assert `_local_demo_scheduler` is NOT
  called and the orchestrator receives `cfg.scheduler` verbatim (`fire_time == time(6,0,0)`,
  `early_arrival_ms == 500`). Inject a FakeClock anchored 2 s before a synthetic T0 so the
  busy-wait returns deterministically.
- `test_run_wait_default_is_no_wait` — calling `run` with neither flag and `TEETIME_WAIT` unset
  resolves to no-wait.
- `test_run_env_wait_fallback_used_when_flag_absent` — `TEETIME_WAIT=1` and no flag → real
  scheduler; flag present always overrides env (`--no-wait` + `TEETIME_WAIT=1` → no-wait).
- `test_fire_time_override_refused_when_live` — `--fire-time 12:00:00 --dry-run false` raises a
  ClickException (the §6.5 guard); `--fire-time 12:00:00 --dry-run true` is accepted and the
  resolved `cfg.scheduler.fire_time` is `time(12,0,0)`.
- `test_fire_time_override_makes_wait_path_reachable` — `--wait --dry-run true --fire-time <~now>`
  + FakeClock anchored just before that T0 → busy-wait returns and the DST gate proceeds (ET hour
  == fire_time.hour − 1). Proves the §6.5 escape hatch works at a non-06:00 hour.
- `test_run_wait_measures_ntp_offset_on_wait_path` — (REWRITTEN per reviewer item 3) NTP
  measurement is decoupled from `dry_run` and re-gated on `wait and not use_fake_adapter`. So
  `--wait` against the REAL adapter triggers `measure_ntp_offset` regardless of `--dry-run`
  (mock it); `--no-wait` does NOT; `--use-fake-adapter` does NOT (no network in tests). This
  REPLACES the old `__main__.py:169` `not dry_run` guard, so the dev `--wait --dry-run true`
  verification run probes UDP:123 reachability and logs the offset/warning BEFORE the first real
  prod Sunday. Correctness is unchanged (offset 0 on block, ratified R5); the change is that the
  timing path the busy-wait depends on is now actually exercised in dev.

> Reviewer item 3 rationale: the old gate `not use_fake_adapter and not dry_run` (`__main__.py:169`)
> meant `measure_ntp_offset()` first ran in ACA egress on the first real prod Sunday. If ACA blocks
> UDP:123 the offset silently degrades to zero — fine for correctness, but the dev dry-run never
> verified reachability. Re-gating on `wait` (the NTP offset is meaningful ONLY when we busy-wait
> to a real T0) makes the `--wait --dry-run true` dev run probe the path. The offset is harmless
> under dry-run (no POST fires either way).

> NOTE the SUT here is the CLI wiring; mock collaborators (`measure_ntp_offset`,
> `Orchestrator.run`) — never the SUT. Use the existing `FakeAdapter` via `--use-fake-adapter`
> for the happy paths so no network is touched.

**Stub signatures.**

```python
# src/teetime/__main__.py

@cli.command(name="run")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--dry-run", type=bool, default=True, show_default=True,
              help="If true, do everything except the final booking POST.")
@click.option("--wait/--no-wait", "wait", default=None,
              help="Busy-wait until the configured fire_time (real 06:00 ET race path). "
                   "Default (no flag): consult TEETIME_WAIT env, else --no-wait (immediate, "
                   "local-demo timing). The ACA booking job passes --wait.")
@click.option("--fire-time", "fire_time_str", type=str, default="",
              help="DEV/TEST ONLY. Override the scheduler fire_time (HH:MM:SS) so an on-demand "
                   "--wait run's DST gate + busy-wait are satisfiable at any wall-clock hour. "
                   "REFUSED unless --dry-run true (cannot affect a real booking). See §6.5.")
@click.option("--use-fake-adapter", is_flag=True, default=False, help="...")
def run_cmd(config_path: Path, dry_run: bool, wait: bool | None,
            fire_time_str: str, use_fake_adapter: bool) -> None:
    """Run one BookingRequest end-to-end."""
    # Resolve bool|None -> bool HERE (in the command), so _run takes a plain bool and
    # mypy --strict is happy. _resolve_wait_mode is NOT called inside _run.
    resolved_wait = _resolve_wait_mode(wait)
    # --fire-time is a dev/test escape hatch; hard-refuse it on a live run (§6.5).
    if fire_time_str and not dry_run:
        raise click.ClickException("--fire-time is dev/test-only and requires --dry-run true.")
    try:
        cfg = load(config_path)
    except MissingEnvVarError as e:
        raise click.ClickException(str(e)) from e
    if fire_time_str:
        cfg = _with_fire_time_override(cfg, fire_time_str)  # returns a copy with cfg.scheduler.fire_time replaced
    asyncio.run(_run(cfg, dry_run=dry_run, wait=resolved_wait, use_fake_adapter=use_fake_adapter))


def _resolve_wait_mode(flag: bool | None) -> bool:
    """Resolve the execution mode.

    Precedence: explicit --wait/--no-wait flag > TEETIME_WAIT env (truthy: '1'/'true') > False.
    Returns True for the real 06:00 ET busy-wait path, False for immediate local-demo timing.
    Called from run_cmd (NOT _run) so _run receives a concrete bool.
    """
    ...


def _with_fire_time_override(cfg: AppConfig, fire_time_str: str) -> AppConfig:
    """Return a copy of cfg with scheduler.fire_time replaced by HH:MM:SS (dev/test only).
    Raises click.ClickException on a malformed time. Used by --fire-time to make the --wait
    DST gate + busy-wait reachable on-demand under dry-run (§6.5)."""
    ...


async def _run(cfg: AppConfig, *, dry_run: bool, wait: bool, use_fake_adapter: bool) -> None:
    """When wait is True: run the DST gate (PR2), use cfg.scheduler verbatim (real T0 busy-wait),
    and measure the NTP offset (gated on `wait and not use_fake_adapter` — NOT on dry_run, per
    reviewer item 3, so the dev --wait dry-run probes UDP:123). When False: use
    _local_demo_scheduler (immediate); no DST gate; zero NTP offset."""
    ...
```

**NTP gating change (reviewer item 3).** `__main__.py:169` becomes:
```python
# was: measure_ntp_offset() if not use_fake_adapter and not dry_run else timedelta(0)
clock_offset = measure_ntp_offset() if wait and not use_fake_adapter else timedelta(0)
```
The offset is meaningful only on the busy-wait path; gating on `wait` (not `dry_run`) makes the
dev `--wait --dry-run true` run exercise the NTP probe before the first real prod Sunday.

**Doc updates.** `README.md` (the `teetime run` flag table / common commands), root `CLAUDE.md`
(common-commands table: note `--wait` is the production race path), PLAN.md §6.3 ⚠️ note (mark the
"real-scheduler T0 busy-wait re-homed" half resolved).

**CI/parity.** No config/bicep changes → parity test unaffected. `mypy --strict` must accept
`bool | None` flag.

---

### PR2 — DST-half gate in the entrypoint (the wrong-season cron exit)

**Scope.** Re-home the deleted `book.yml` `dst` step into the container entrypoint. Predicate:
**proceed iff the ET wall-clock hour at fire time equals 5** (the cron fires at :50 of the hour
preceding T0=06:00 ET). The gate runs ONLY on the `--wait` path (real cron run); `--no-wait`
(manual/local/on-demand) bypasses it, matching the old `workflow_dispatch` always-proceed
behaviour. The gate is computed against the injected `Clock` so it is FakeClock-testable.

**Files touched.**
- `src/teetime/core/dst_gate.py` — NEW small pure module (one function + a result type).
- `src/teetime/__main__.py` — call the gate early in `_run` when `wait` is True; on "skip" log
  and return exit 0 (a wrong-season cron firing is NOT an error).
- `tests/test_dst_gate.py` — NEW. The full transition matrix below.

**Why a separate module:** the gate is pure (clock + tz → decision) and needs its own dense
test matrix (spring-forward, fall-back, both seasons, jitter). Keeping it out of `__main__`
keeps it unit-testable without `CliRunner`.

**Predicate (exact).**

```
proceed  ⇔  ZoneInfo("America/New_York")  wall-clock hour of clock.now_utc()  == fire_hour - 1
```

where `fire_hour = scheduler.fire_time.hour` (6). The cron lands the runner at :50 of the
preceding hour (05:xx ET), so at gate-evaluation time the ET hour is 5 in the *correct* season
and 4 or 6 in the wrong season (the same UTC instant is 04:50 or 06:50 ET depending on the
offset). Using `fire_hour - 1` rather than a hardcoded `5` keeps the gate correct if `fire_time`
is ever changed. The gate reads the hour only — minutes/seconds are irrelevant because the
busy-wait downstream handles sub-hour precision.

**Interaction with `busy_wait`.** The gate runs *before* `busy_wait_until`. If it returns
`skip`, `_run` logs and returns immediately — the busy-wait (which would otherwise compute a
PAST T0 in the wrong season and return instantly, then book ~50 min late) is never reached. If
it returns `proceed`, control falls through to the normal busy-wait at the correct T0. This is
the single point that prevents the "second cron of the day runs anyway" failure.

**Manual / on-demand bypass.** The gate is invoked only inside the `wait=True` branch.
`--no-wait` runs (the default; all local and `az containerapp job start` ad-hoc dry-runs) never
evaluate it. A reviewer asking "how do I run an on-demand dev booking at 2 PM?" → use
`--no-wait` (immediate, demo timing) which is exactly the old `workflow_dispatch` semantics.

**Jitter handling (reviewer pre-emption).** ACA cron is best-effort. Two jitter risks:
1. *Runner lands late, after 06:00 ET (hour reads 6).* In the **correct** season this means we
   already missed the cron-10-min lead; the gate would now read hour 6 and SKIP — which is the
   right call only if we genuinely can't make the race. To avoid skipping a still-winnable race,
   the gate accepts an **inclusive lower bound**: proceed iff `fire_hour - 1 <= et_hour < fire_hour`,
   i.e. ET hour ∈ {5}. We deliberately do NOT widen to include hour 6, because once it is past
   06:00 ET the slots have already dropped and a late run racing a public window is both useless
   and risks double-attempt churn; the watch job is the recovery path for a missed drop. This is
   called out as an accepted limitation in the docstring and §8.
2. *Runner lands early (05:49 vs 05:50).* ET hour still 5 → proceed. Fine.
3. *Wrong-season cron lands — the gate is necessary in BOTH directions (reviewer item 5).*
   - **EST-tuned cron (10:50 UTC) firing during EDT season** → 06:50 ET → hour 6 → skip. Without
     the gate, the busy-wait would compute a T0 (06:00 ET) that is ~50 min in the PAST →
     `busy_wait_until` returns instantly → the bot books ~50 min LATE.
   - **EDT-tuned cron (09:50 UTC) firing during EST season** → 04:50 ET → hour 4 → skip. Without
     the gate, the busy-wait would compute a T0 (06:00 ET) that is ~70 min in the FUTURE → the
     bot busy-waits ~70 min INSIDE the replica. Combined with cold-start + post-T0 work this
     blows the booking replicaTimeout (see §2 PR3 arithmetic) → replica killed mid-race, no
     booking. So the gate prevents a books-late failure in one direction and a timeout-kill
     failure in the other. A later "optimization" that widens the gate must preserve BOTH.

**Red tests first (FakeClock; no real wall-clock — RTK/CLAUDE rule).** Build each FakeClock at a
specific UTC instant and assert the gate decision. `fire_time = time(6,0,0)`,
`timezone="America/New_York"`.

| Test name | Clock instant (UTC) | ET local | Expect |
|-----------|--------------------|----------|--------|
| `test_gate_proceeds_edt_correct_cron` | 2026-05-31 09:50 | 05:50 EDT (Sun) | proceed |
| `test_gate_skips_edt_cron_in_est_season` | 2026-12-06 09:50 | 04:50 EST | skip |
| `test_gate_proceeds_est_correct_cron` | 2026-12-06 10:50 | 05:50 EST | proceed |
| `test_gate_skips_est_cron_in_edt_season` | 2026-05-31 10:50 | 06:50 EDT | skip |
| `test_gate_spring_forward_morning_proceeds` | 2026-03-08 09:50 | 05:50 EDT (DST starts; 02→03 skipped, 05:00 unaffected) | proceed |
| `test_gate_spring_forward_wrong_cron_skips` | 2026-03-08 10:50 | 06:50 EDT | skip |
| `test_gate_fall_back_morning_proceeds` | 2026-11-01 10:50 | 05:50 EST (fall-back; offset already settled by 05:00, fold irrelevant) | proceed |
| `test_gate_fall_back_wrong_cron_skips` | 2026-11-01 09:50 | 04:50 EST (rollback at 02:00 already settled to EST by gate time; 09:50 UTC − 5h = 04:50) | skip |
| `test_gate_proceeds_when_hour_is_fire_minus_one_generic` | fire_time=time(7,0,0), clock 06:30 ET | 06:xx | proceed (predicate uses fire_hour-1) |

> Fall-back note: on 2026-11-01 the rollback happens at 02:00 EDT→01:00 EST, well before 05:00,
> so by gate time the offset is unambiguously EST. The EST cron (10:50 UTC) reads 05:50 EST →
> proceed; the EDT cron (09:50 UTC) reads 04:50 EST → skip. The "wrong cron skips" fall-back test
> asserts the 09:50-UTC firing on 2026-11-01 reads ET hour 4 → skip. (The ambiguous-hour
> `fold=0` concern from PLAN.md §6.3 affects T0 *resolution* at 06:00, not the 05:xx gate; the
> gate never touches the ambiguous 01:00–02:00 window.)
- `test_gate_bypassed_on_no_wait_path` — CLI-level: `teetime run --no-wait` at any clock never
  invokes the gate (spy on the gate function; assert not called).

**Stub signatures.**

```python
# src/teetime/core/dst_gate.py
"""DST-half gate. Re-homes the deleted book.yml `dst` step into the entrypoint.

Background: compute.bicep registers two same-day crons (EDT + EST) because we do not
re-edit cron schedules twice a year. Only ONE is correct in any given DST season; the
other fires at the wrong UTC offset. With a real busy-wait this misfires in TWO ways:
  - EST cron in EDT season → T0 is ~50 min in the PAST → busy-wait returns instantly →
    books ~50 min late.
  - EDT cron in EST season → T0 is ~70 min in the FUTURE → busy-wait blocks ~70 min →
    exceeds the booking replicaTimeout → replica killed, no booking.
This gate makes the wrong-season cron exit cleanly in BOTH directions.

Predicate: proceed iff the course-local (ET) wall-clock HOUR at fire time equals
fire_time.hour - 1 (the cron lands at :50 of the hour preceding T0=06:00 ET, so the
correct season reads hour 5; the wrong season reads 4 or 6). See PLAN.md §6.3.
Pure function of (clock, timezone, fire_time) so it is FakeClock-deterministic.
"""
from __future__ import annotations
from datetime import time
from .clock import Clock


def should_proceed(clock: Clock, *, timezone: str, fire_time: time) -> bool:
    """Return True iff the current course-local wall-clock hour == fire_time.hour - 1.

    Called ONLY on the real-timing (--wait) cron path, BEFORE busy_wait_until. The
    --no-wait path (manual/local/on-demand) bypasses this entirely, matching the old
    workflow_dispatch always-proceed semantics. Reads the hour only; sub-hour precision
    is the busy-wait's job. A False return means "wrong DST-season cron — exit 0, this is
    not an error." See M6_PLAN.md §2 PR2 for the jitter analysis (does NOT proceed once
    ET hour reaches fire_time.hour; the watch job is the missed-drop recovery path).
    """
    ...
```

**Doc updates.** PLAN.md §6.3 (replace the ⚠️ open-status note: gate re-homed to
`core/dst_gate.py`, predicate documented, book.yml reference is historical), root `CLAUDE.md`
(the ⚠️ DST note → "DST gate lives in `core/dst_gate.py`, evaluated on the `--wait` path"),
`infra/AZURE_PLAN.md` §5.3 (gate location updated from book.yml to entrypoint).

**CI/parity.** No config/bicep change. New module must pass `mypy --strict` + `ruff`.

---

### PR3 — Bicep: booking job passes `--wait`; confirm cold-start lead covers the race

**Scope.** Make the booking ACA job take the real-timing path by adding `--wait` to its `args`,
and confirm/adjust the cron lead so the container is *ready* (image pulled, deps loaded, auth +
captcha pre-fetch done) before T0. Set `TEETIME_WAIT` in `commonEnv` as a belt-and-suspenders
fallback so on-demand `az containerapp job start` of the booking job also waits (operator can
still pass `--no-wait` to override for a manual immediate test — but note the booking job's
*args* already force `--wait`, so the env is mostly informational; see decision note).

> Merge order: lands AFTER PR5 (Sunday-only) to avoid a `bookingJobs`/`args` conflict in the same
> file. PR3 rebases onto PR5.

**Files touched.**
- `infra/bicep/modules/compute.bicep` — add `'--wait'` to the booking job `args`; **add a
  dedicated `bookingReplicaTimeout = 1200` var and use it on the booking jobs** (the watch job
  keeps `watchReplicaTimeout = 120`; the generic `replicaTimeout = 900` is now used by nothing
  and is removed to avoid a misleading dead constant). The watch job must NOT get `--wait` (it has
  no busy-wait; keep args clean).
- `infra/AZURE_PLAN.md` — cold-start + replica-timeout budget note (§4, §5.2).

**Decision: booking-only `--wait`.** `commonEnv` is shared by both jobs. If we add `TEETIME_WAIT`
to `commonEnv` it would also reach the watch job — harmless (watch ignores wait) but misleading.
Cleaner: pass `--wait` explicitly in the booking job's `args` array (legible in bicep) and leave
`TEETIME_WAIT` OUT of `commonEnv`. The env fallback in PR1 then serves only true ad-hoc CLI
invocations outside ACA. **Chosen: explicit `args` `--wait` on the booking job; no `TEETIME_WAIT`
in bicep.** This avoids a watch-job env that does nothing.

**Cold-start budget (reviewer pre-emption — "is 10 min lead enough?").** The cron fires at :50,
T0 is :00 of the next hour → **10-minute nominal lead**. Budget at T0-10:00:
ACA cold pull of the ACR image (~30–90 s for a 0.25 vCPU/0.5 Gi consumption replica on a warm
env) + `uv`/python import (~2–5 s) + `_resolve_site_keys` reCAPTCHA fetch (live only, ~1–3 s) +
ForeUP `authenticate()` (~1–2 s). The captcha *token* pre-fetch is in `prepare_book()`, which the
**booking** Orchestrator currently does NOT call before the race (only `UpgradeOrchestrator`
does). For the booking path the captcha solve happens *inside* `book()` AFTER T0 — see Risk R3
in §8; M6 documents this as an accepted v0 limitation (the captcha solve is part of the post-T0
POST latency, not the pre-T0 lead). Conclusion: 10 minutes comfortably covers cold-start + auth.
ACA jitter up to several minutes still leaves multi-minute slack; the bot's busy-wait nails the
second. **No cron-lead change required**; PR3 only documents the budget. If dev observation (PR6)
shows pulls > 5 min, bump the cron to :48 in a follow-up.

**Replica-timeout arithmetic (reviewer item 1 — the single most likely first-run failure).**
This is the load-bearing change PR3 must make. Pre-M6 the booking job "returned immediately"
(demo scheduler). M6 makes it **busy-wait up to ~10–12 minutes INSIDE the replica**, so the
governing `replicaTimeout` must be re-derived. `replicaTimeout` counts from replica START
(container scheduled), not from T0.

Timeline of a correct-season booking replica:
```
t = replica start (cron fires at :50; ACA may land the runner :48–:52 due to best-effort jitter)
  + cold pull + import + auth + site-key fetch        ~30–100 s   (happens once, early)
  ... busy_wait_until(T0 − early_arrival_ms) ...       fills the rest until 06:00:00 ET
t = T0 (06:00:00 ET)
  + _poll_for_slots until non-empty or max_poll_seconds=30        ≤ 30 s   (config/container.toml:54)
  + book(): captcha solve (~15–30 s, R3) + card/POST round-trips  ~30–45 s
t = replica completes
```
Total replica wall-time = (T0 − replica_start) + post-T0 work.

- **Nominal** (runner lands at :50:00): (T0 − :50:00) = 600 s busy-wait + ~30 s poll + ~45 s
  book = **~675 s**. Fits 900 s — but the margin is only ~225 s and the busy-wait portion grows
  with early jitter.
- **Early jitter** (runner lands at :48:00, which the plan explicitly accepts): (T0 − :48:00) =
  720 s busy-wait + ~30 s poll + ~45 s book = **~795 s**. Against `replicaTimeout = 900` that
  leaves only ~105 s — and any captcha retry, slow ForeUP, or a runner landing at :47 pushes it
  past 900 → **the platform kills the replica mid-race, no booking**. This is the failure the
  reviewer flagged; 900 s is too thin once the job busy-waits.

**Decision: the booking job gets `bookingReplicaTimeout = 1200` (20 min).** Worst tolerated case
(runner at :47:00 = 780 s busy-wait + 30 s poll + 60 s book-with-one-captcha-retry = 870 s) sits
comfortably under 1200 with ~330 s slack. 1200 s is also still well inside ACA's job timeout
ceiling and costs nothing extra (Consumption bills only for actual replica runtime, which is
dominated by the unavoidable busy-wait either way). The DST gate (PR2) independently caps the
busy-wait: the EDT-cron-in-EST case (which would otherwise busy-wait ~70 min and certainly blow
ANY reasonable timeout) is skipped before the busy-wait, so 1200 only needs to cover the
correct-season lead + post-T0 work, not the wrong-season 70-min wait. The watch job is unchanged
(`watchReplicaTimeout = 120`; one HTTP round-trip, no busy-wait).

> Why not just keep 900 and move the cron to :52 (smaller lead)? A smaller lead shrinks the
> busy-wait but also shrinks the cold-start safety margin — a slow ACR pull could then land the
> runner AFTER T0 (hour 6 → DST gate skip → missed drop). Keeping the :50 lead for cold-start
> safety AND raising the timeout to absorb the resulting busy-wait is the safer combination.

**Red tests first.** Bicep is validated, not unit-tested, but add a static-assertion test:
- `tests/test_compute_bicep_booking_args.py::test_booking_job_passes_wait_flag` — read
  `compute.bicep`, assert the booking-job container `args` contains `'--wait'` and the watch-job
  args do NOT. (Regex on the file text, same style as `test_container_config_parity.py`.)
- `test_booking_args_still_pass_dry_run_param` — assert the `dryRun ? 'true' : 'false'` arg
  survives next to `--wait` (don't regress dry-run wiring).
- `test_booking_replica_timeout_covers_busy_wait` — assert `bookingReplicaTimeout` is defined and
  `>= 1200`, and that the booking job's `replicaTimeout` references it (NOT the old `900`). Guards
  reviewer item 1 from silently regressing.
- `test_watch_replica_timeout_unchanged` — assert the watch job still uses `watchReplicaTimeout`
  (120), not the booking timeout.

**Stub signature.** (bicep fragment, illustrative)

```bicep
// compute.bicep — busy-wait-aware timeout. The booking job busy-waits up to ~12 min to T0
// INSIDE the replica, so its timeout must cover lead + busy-wait + post-T0 work (§2 PR3
// arithmetic). The old generic `replicaTimeout = 900` is removed (no remaining consumer).
var bookingReplicaTimeout = 1200   // 20 min: covers :47 early-jitter lead + 30s poll + 60s book
// watchReplicaTimeout = 120 unchanged (one HTTP round-trip, no busy-wait).

// booking job container args — adds --wait for the real 06:00 ET race path.
args: [
  'run'
  '--config'
  '/app/config/container.toml'
  '--wait'                       // M6: select real-timing busy-wait (DST gate + 06:00 ET race)
  '--dry-run'
  dryRun ? 'true' : 'false'
]
// booking job configuration: replicaTimeout: bookingReplicaTimeout  (was: replicaTimeout)
```

**Doc updates.** `infra/AZURE_PLAN.md` §5.2 (cold-start budget), §5.3 (gate now in entrypoint),
root `CLAUDE.md` (note booking job runs `--wait`).

**CI/parity.** `az bicep build` + `what-if` in `azure-iac.yml`. New static test runs in normal CI.
Merge to `main` auto-deploys to dev (still `dryRun=true`).

---

### PR4 — Enable the watcher in config (look-but-don't-book dev steady state)

**Scope.** Add a `[watcher]` block to `config/container.toml` (and keep `config/local.toml` /
`config/example.toml` in sync) with `enabled = true`. Confirm — with a test — that
`enabled=true` + `--dry-run true` does ALL the looking/ranking/logging and suppresses ONLY the
final POST. Decide the non-Sunday no-op behaviour. Do NOT enable `[one_booking_policy]` yet
(see §4.4 decision — defer the upgrade to a later, separately-verified change to keep M6's first
real run conservative).

**Files touched.**
- `config/container.toml` — add `[watcher]` block, `enabled = true`.
- `config/local.toml` — add/flip `[watcher]` block to match shape (local can stay `enabled = true`
  for dev parity).
- `config/example.toml` — already documents `[watcher]` (lines 90-97); update the comment to note
  the dev steady state is `enabled = true` + dry-run.
- `tests/test_cli.py` (or a new `tests/test_watch_cli.py`) — look-but-don't-book confirmation.
- `tests/test_config.py` — `[watcher]` parses with `enabled=true`.

**Watcher config block shape (TOML).**

```toml
# config/container.toml  (and mirrored in local.toml)
# Cancellation-monitor job (M-feature-1). ENABLED in v1 dev steady state.
# With --dry-run true the watcher does ALL looking/ranking/logging and suppresses ONLY
# the final booking POST (WatchOrchestrator._book_candidates returns DRY_RUN before the
# lock + POST). This is the intended dev posture: prove the poller works without booking.
[watcher]
enabled            = true
poll_interval_s    = 600   # 10 minutes; must be >= 300 (anti-bot floor)
polling_start_hour = 7     # course-local; no polling before 7 AM ET
polling_end_hour   = 22    # course-local; no polling after 10 PM ET

# one_booking_policy intentionally NOT enabled in M6 (see M6_PLAN.md §4.4). The watcher
# in M6 only logs/ranks newly available slots; cancel+rebook upgrades are a later,
# separately-verified step.
```

**"Look but don't book" — confirmed truthful (reviewer pre-emption: what actually executes?).**
Trace under `watcher.enabled=true`, `dry_run=true`, ForeUP adapter (NOT fake):
- `authenticate()` — **YES executes** (creds needed to `list_reservations` + `search`).
- `list_reservations()` — **YES** (login-response cache read; the layer-2 pre-book check).
- `search()` (GET /times) — **YES** (this is the "looking").
- `rank_slots_for_request` — **YES** (ranking happens).
- captcha solve — **NO**: `_build_adapters(..., dry_run=True)` sets `captcha_provider=None`
  (`__main__.py:366-367`); captcha is only solved inside `book()`.
- card POST / `book()` final POST — **NO**: `_book_candidates` returns `DRY_RUN` before lock/POST
  (`watch_orchestrator.py:328`).
- PII/card redaction — UNCHANGED; no attempt_log writes happen on the dry-run watch path beyond
  what the orchestrator already redacts (PLAN.md §10.1). No card data is even loaded for ForeUP.

So `enabled=true` + dry-run is genuinely "fully run, look, rank, log; never book." Confirmed.

**Red tests first.**
- `tests/test_config.py::test_watcher_enabled_parses` — load a config with `[watcher] enabled=true`;
  assert `cfg.watcher.enabled is True` and `to_watch_config()` round-trips the hours/interval.
- `tests/test_watch_cli.py::test_watch_enabled_dry_run_looks_but_does_not_book` — drive
  `_watch(cfg, dry_run=True, …, use_fake_adapter=True)` with a FakeAdapter scripted to return
  candidate slots (`set_search_response`) and assert: `search` was called, the returned result
  outcome is `DRY_RUN`, and **`adapter.book_call_count == 0`**. NO test-util change needed —
  `FakeAdapter.book_call_count` ALREADY exists (`fake_adapter.py:51`, incremented `:116`).
  (Reviewer item 4: the prior plan's "add book_call_count" extension E3 was a no-op; dropped.)
- `test_watch_enabled_logs_ranking` — assert an INFO log line containing the chosen slot is
  emitted (the verification surface PR6 relies on).
- `test_watch_disabled_still_exits_clean` — regression: `enabled=false` keeps the warn-and-exit-0
  behaviour (`__main__.py:228`).

**FakeAdapter — NO change needed (reviewer item 4).** `FakeAdapter.book_call_count` already
exists (`fake_adapter.py:51`) and is incremented on every `book()` entry (`:116`). The
look-but-don't-book test asserts `book_call_count == 0` directly. No test-util edit in PR4.

**Non-Sunday behaviour decision — run daily, clean no-op.** `_build_request` derives
`target_dates = today + target_offsets` (`__main__.py:447-448`), and the watch CLI uses
`request.target_dates[0]` = today+7 (`__main__.py:268`). **Decision: the watch cron stays daily;
on a non-Sunday it derives today+7 (also a non-Sunday), finds no managed booking and no matching
reservation, ranks any open slots, and (in dry-run) logs a DRY_RUN/None result — a clean no-op
with no booking.** Justification: there is no harm or cost in a daily no-op poll (single GET,
sub-cent ACA), and a Sunday-aligned watch cron would *miss* the window where it matters most.
A Sunday booking made for *next* Sunday means the slot exists and is upgradeable for the **entire
intervening week** — so polling every day Mon–Sun is exactly what catches a better slot opening on
a Wednesday. Narrowing the watch cron to Sundays would break the core watch value proposition.
(This is also why §5 keeps `watchCron` daily.)

**Doc updates.** PLAN.md §16 (M6 row: watcher enabled), root `CLAUDE.md` (status line: watcher
enabled in dev, look-but-don't-book), `infra/AZURE_PLAN.md` §7.1 (no new secrets — watcher uses
the same MB/PLAYER1 creds), README (status).

**Config parity — leave the local.toml PLAYER divergence ALONE (reviewer item 11).** A pre-existing
intentional divergence: `config/local.toml:27,32,37` reference `PLAYER2_EMAIL`/`PLAYER3_EMAIL`/
`PLAYER4_EMAIL` (local dev supplies real per-guest emails), while `config/container.toml:33-43`
makes guests 2–4 **name-only** (the foursome invariant — ForeUP's booking POST sends only the
player count, not guest contact info; see container.toml:20-25). PR4 adds a `[watcher]` block to
BOTH files but **must NOT** mirror the PLAYER2/3/4 refs into container.toml — doing so would break
the foursome invariant and add KV secrets that prod deliberately omits (AZURE_PLAN §7.1). The
parity test (`test_container_config_parity.py`) compares **container↔bicep**, not container↔local,
so the local-only PLAYER refs are invisible to it and intended. PR4 documents this in the local.toml
`[watcher]` comment so a future reader doesn't "fix" the apparent mismatch.

**CI/parity.** `[watcher]` adds NO new `*_env` refs → `test_container_config_parity.py` stays
green. `test_container_and_example_party_size_match` unaffected (players unchanged). Confirm the
parity test still passes (it should — watcher block has no env refs).

---

### PR5 — Bicep: Sunday-only booking schedule; keep watch cron daily

**Scope.** Drop the two `-sat` entries from `bookingJobs`, keeping `edt-sun` + `est-sun`. Keep
`watchCron = '*/10 * * * *'` (daily) per the §4.4 / PR4 justification. Update the module header
comment (currently says "Four scheduled triggers … Sat+Sun").

**Files touched.**
- `infra/bicep/modules/compute.bicep` — `bookingJobs` array → 2 entries; header comment; the
  `cronEdtSat`/`cronEstSat` vars become unused (remove them); the output comment
  ("Four jobs are created") → "Two jobs … -edt-sun, -est-sun".
- `infra/AZURE_PLAN.md` §5.3 (cron table → Sunday only).

**Output + consumer audit (reviewer item 10).** `output jobName = bookingJob[0].name`
(`compute.bicep:328`) is `index 0` of the `bookingJobs` array. Today index 0 = `-edt-sat`; after
PR5 removes the two `-sat` entries, **index 0 becomes `-edt-sun`** (the new first element). Audit
of consumers:
- `infra/bicep/main.bicep:147` — `output jobName string = compute.outputs.jobName`: pure
  re-export, no assumption about the suffix. OK, but its `@description` (if any) should say
  `-edt-sun`/`-est-sun`.
- `infra/AZURE_PLAN.md:407` + `:609` runbook — both use `teetime-job-<envName>` WITHOUT a suffix,
  which is already imprecise (no such resource exists without a `-edt-sun`/`-est-sun` suffix). PR5
  fixes these to name the two real jobs and notes that `az containerapp job start` must target a
  specific suffix.
- No consumer assumes `-edt-sat`. The output description string in `compute.bicep:327` ("Four jobs
  are created: -edt-sat, -est-sat, -edt-sun, -est-sun") is updated to "Two jobs: -edt-sun,
  -est-sun".
- A static test (`test_compute_bicep_schedule.py::test_jobname_output_is_a_real_job`) asserts the
  `output jobName` index resolves to a job whose suffix exists in the post-PR5 `bookingJobs`.

**Watch cron decision — STAYS DAILY (`*/10 * * * *`).** Reasoned in PR4: a Sunday booking for
next Sunday is upgradeable for the full 7 intervening days; daily 10-min polling (gated 7 AM–10 PM
ET by `WatchOrchestrator._is_outside_polling_hours`) is what catches mid-week cancellations.
Narrowing to Sundays would defeat the feature. The watch job needs no DST gate (it does not race
a wall-clock window; PLAN.md §20 / `watch_orchestrator.py:445`).

**Red tests first (static bicep assertions, parity-style).**
- `tests/test_compute_bicep_schedule.py::test_booking_jobs_are_sunday_only` — read
  `compute.bicep`; assert `bookingJobs` contains exactly two crons, both ending `* * 0` (Sunday),
  and NO `* * 6` (Saturday) cron remains.
- `test_watch_cron_is_daily_every_10_min` — assert `watchCron == '*/10 * * * *'`.
- `test_no_orphan_sat_cron_vars` — assert `cronEdtSat`/`cronEstSat` are not referenced (removed).

**Stub signature (bicep fragment).**

```bicep
// compute.bicep — Sunday-only booking schedule (M6). Two crons: EDT + EST, both Sunday.
// The bot's DST gate (core/dst_gate.py, --wait path) makes the wrong-season cron exit.
var cronEdtSun = '50 9 * * 0'    // 09:50 UTC = 05:50 EDT Sunday
var cronEstSun = '50 10 * * 0'   // 10:50 UTC = 05:50 EST Sunday

var bookingJobs = [
  { name: '${jobName}-edt-sun', cron: cronEdtSun }
  { name: '${jobName}-est-sun', cron: cronEstSun }
]
```

**Doc updates.** `infra/AZURE_PLAN.md` §5.3 (cron table), root `CLAUDE.md` (the
`.github/workflows/` cron note → Sunday only / ACA), PLAN.md §6.3 table (drop Saturday rows or
mark Sunday-only), README roadmap if it lists the schedule.

**CI/parity.** `az bicep build` + `what-if`. Note: removing job resources `-edt-sat`/`-est-sat`
means `what-if` will show **deletes** in dev — flag this loudly in the PR description so the
operator expects two job resources to be removed on dev auto-deploy. (No data loss; jobs are
stateless.) Merge order: PR5 before PR3.

---

### PR6 — Dev verification runbook + instrumentation + docs sync

**Scope.** Add the precise log lines that PROVE both jobs work under `dryRun=true`, any temporary
instrumentation, the on-demand dev trigger procedure, and the prod cutover checklist. Mostly docs;
plus small structured-log additions if the current logs don't already prove the two facts.

**Files touched.**
- `src/teetime/core/orchestrator.py` — add a single INFO log line at the moment the busy-wait
  *returns* (proving the bot fired at T0), e.g. after `busy_wait_until(...)` in `run()`:
  `log.info("race: busy-wait complete, firing at %s (T0=%s, drift_ms=%.1f)", now, t0, drift)`.
  This is the load-bearing verification line for "booker busy-waited and fired at 06:00:00.x".
- `src/teetime/__main__.py` — on the `--wait` path, log the resolved T0 and the NTP offset
  applied (so logs show the real scheduler was selected, not the demo one).
- `M6_PLAN.md` is this file; the runbook content also goes into `infra/AZURE_PLAN.md` §10 and a
  README "verifying the first run" subsection — INCLUDING the §6.5 `--fire-time` on-demand
  escape-hatch procedure and the §6.6 "live-Sunday-only timing" exit criterion.
- `tests/test_orchestrator.py` — assert the new race-complete log line is emitted at/after T0
  under FakeClock (caplog), so the verification surface itself is tested and can't silently drop.

**Red tests first.**
- `test_orchestrator_logs_race_complete_at_t0` — with FakeClock anchored before T0 and a
  FakeAdapter, run the orchestrator and assert a caplog record matches `race: busy-wait complete`
  AND the logged firing time is within `fine_accuracy_s` of T0. (Reuses the race-window canary
  pattern.)

**Stub signature (log line, illustrative).**

```python
# core/orchestrator.py — inside run(), immediately after busy_wait_until(...)
log.info(
    "race: busy-wait complete; firing at %s (T0=%s)",
    self._clock.now_utc().isoformat(),
    t0.isoformat(),
)
```

**Doc updates.** `infra/AZURE_PLAN.md` §10 (verification + cutover), README (verification), root
`CLAUDE.md` (status: M6 timing/DST/watcher landed; M6 first-run verification procedure pointer),
PLAN.md §16 (mark M6.T1/T2 design landed; M6.T3 live booking still pending operator action).

**CI/parity.** Log-only + test; no config/bicep change.

---

## 3. DST-gate design (consolidated)

- **Predicate:** `proceed ⇔ ET_wall_clock_hour(clock.now_utc()) == fire_time.hour - 1` (= 5 for
  the 06:00 fire time). Hour-only; `zoneinfo` resolves the offset.
- **Location:** pure function `core/dst_gate.py::should_proceed(clock, timezone, fire_time)`,
  invoked early in `__main__._run` **only on the `--wait` branch**, before `Orchestrator.run` /
  the busy-wait.
- **Interaction with busy_wait:** gate is upstream of `busy_wait_until`. `skip` → log + return 0
  (never reach busy-wait, which would otherwise return instantly on a past T0 and book late).
- **Manual bypass:** `--no-wait` (default; all local + ad-hoc `az containerapp job start`) never
  evaluates the gate. Mirrors the old `workflow_dispatch` always-proceed.
- **Jitter:** proceed only at ET hour == 5; do NOT proceed at hour 6 (drop already happened —
  watch job recovers). Wrong-season crons read 4 or 6 → skip.
- **Necessary in BOTH directions:** EST-cron-in-EDT (reads 6) without the gate would book
  ~50 min late (past T0); EDT-cron-in-EST (reads 4) without the gate would busy-wait ~70 min and
  blow the booking replicaTimeout (§5.1). The gate is not just a "book late" guard.
- **Test matrix:** see PR2 table (8+ FakeClock cases: both seasons correct/wrong, spring-forward,
  fall-back, generic `fire_hour-1`, no-wait bypass). All FakeClock-driven; no real wall-clock.

---

## 4. Watcher enablement (consolidated)

- **4.1 Config block:** `[watcher] enabled=true, poll_interval_s=600, polling_start_hour=7,
  polling_end_hour=22` in `container.toml` + `local.toml`; `example.toml` comment updated.
- **4.2 Look-but-don't-book:** confirmed truthful under `enabled=true` + `--dry-run true`
  (auth ✓, list_reservations ✓, search ✓, rank ✓; captcha ✗, book POST ✗, card ✗). Returns a
  `DRY_RUN` result; `book()` never called. Verified by `book_call_count == 0` test.
- **4.3 Target-date / day decision:** **daily watch cron, target = today+7, clean no-op on
  non-matching days.** Justified by the week-long upgrade window of a Sunday-for-next-Sunday
  booking. **Caveat (reviewer item 7):** with target=today+7, on a non-Sunday `today+7` is the
  newest bookable day, so slots usually EXIST and the watcher ranks + emits a DRY_RUN result every
  day — including dates the user never intended to play. This is **harmless ONLY because M6 keeps
  `one_booking_policy.enabled=false`**: under policy-off the watcher just logs/ranks, it never
  cancels or rebooks. Daily-watch + policy-ON would actively try to "upgrade" a booking on a date
  the user didn't ask for. See §4.4 and Open Question 6.
- **4.4 one_booking_policy:** **NOT enabled in M6.** M6's first real run is conservative: book at
  the drop + watch/log/rank. Enabling cancel+rebook upgrades is a separate, independently-verified
  change (it cancels a live booking — higher blast radius). Flagged as a follow-up. The watcher
  code path *supports* it; M6 just doesn't turn it on. This is a deliberate scope boundary, not an
  architecture change. **NEW open design question (reviewer item 7):** combining the daily watch
  cron (§4.3) with policy-on is NOT a settled design — daily polling against target=today+7 would
  let the upgrader act on unintended dates. Before policy is ever enabled, the watch cadence/target
  must be reconsidered (e.g. Sunday-aligned target, or an explicit "only upgrade the date matching
  a managed BOOKED terminal" guard). Tracked as Open Question 6; out of M6 scope.

---

## 5. Sunday-only bicep + watch cron + replica timeout

- **5.1 Booking replica timeout (reviewer item 1):** the booking job busy-waits up to ~10–12 min
  to T0 INSIDE the replica, so its timeout must cover (T0 − replica_start) + post-T0 poll + book.
  Worst tolerated early-jitter case (~:47 landing) ≈ 870 s; `replicaTimeout = 900` is too thin.
  **Decision: `bookingReplicaTimeout = 1200` (20 min)** for the booking jobs; the old shared
  `replicaTimeout = 900` var is removed. Watch job keeps `watchReplicaTimeout = 120`. The DST gate
  caps the wrong-season ~70-min busy-wait before it starts, so 1200 only covers the correct-season
  lead. Full arithmetic in §2 PR3.
- **Booking schedule:** `bookingJobs` → two Sunday crons (`50 9 * * 0`, `50 10 * * 0`); remove the
  two `-sat` crons and their now-unused vars. `what-if` shows two job deletes in dev (expected).
- **Watch:** `watchCron` stays `*/10 * * * *` (daily); no DST gate. Justified: the upgrade target
  exists for the full week between Sundays.

---

## 6. Dev verification procedure (dryRun=true)

**Problem:** with `dryRun=true` the final POST never fires, so logs are the only proof. Two facts
to prove: (a) the booker busy-waited and fired at 06:00:00.x ET; (b) the watcher actually polled
and ranked.

> **Log-string accuracy (reviewer item 6).** Grep/caplog assertions below are pinned to the
> ACTUAL rendered output, not the format string. The booking line at `__main__.py:140-145` is
> `"Booking run: target=%s …"` with `[str(d) for d in request.target_dates]`, so it renders a
> **quoted Python list**, e.g. `Booking run: target=['2026-06-07'] dry_run=True players=4`. The
> watch line at `__main__.py:270` is `"Watch check: target=%s …"` with a bare `date`, rendering
> `Watch check: target=2026-06-07 dry_run=True` (NO brackets/quotes). Match these exactly.

**6.1 Booker proof (Sunday ~06:00 ET, or on-demand via the §6.5 escape hatch).** In Log Analytics
for `teetime-job-dev-edt-sun` / `-est-sun`, assert the ordered presence of:
1. `Booking run: target=['<date = today+7>'] dry_run=True players=4` (`__main__.py:140-145` —
   quoted-list render; grep `target=\['`).
2. (PR6) `wait mode: real scheduler selected; T0=<…>Z ntp_offset=<…>` — proves `--wait` chose
   the real `cfg.scheduler`, NOT `_local_demo_scheduler`. (New PR6 line; exact text is ours to set.)
3. (PR2) For the wrong-season job on the same Sunday: `dst-gate: skip (ET hour=<4|6>, expected 5)`
   and an exit with no further work. The *correct*-season job logs `dst-gate: proceed (ET hour=5)`.
   (New PR2 lines; exact text ours to set.)
4. (PR6) `race: busy-wait complete; firing at <…T06:00:00.x…>Z (T0=<…T06:00:00.000…>Z)` — the
   load-bearing line. Assert the firing timestamp is within ~1 s of T0. (New PR6 line.)
5. A terminal outcome of `dry_run` (DRY_RUN) — `__main__.py:180` accepts it; the run exits 0.

> Reachability of line 4 (reviewer item 2): on the **real Sunday cron** the busy-wait fires at the
> true 06:00:00.x and this assertion is the production proof. For **on-demand dev** the busy-wait
> at fire_time=06:00 is unreachable at any other hour (the DST gate skips). Use the §6.5
> `--fire-time` escape hatch to observe line 4 on demand; treat the fire_time=06:00 real-cron
> timing as verifiable ONLY on the live Sunday cron (accepted M6 exit criterion, §6.6).

**6.2 Watcher proof (any day, during 07–22 ET).** In `teetime-watch-job-dev` logs, assert:
1. `Watch check: target=<today+7> dry_run=True` (`__main__.py:270` — BARE date, no brackets;
   grep `Watch check: target=20`) — proves enabled (not the "disabled" warning at `:232`).
2. Either `watch result: outcome=dry_run confirmation=None` (slots found + ranked, no book) OR a
   debug "no candidates / outside polling hours / target passed" line. The DRY_RUN result line
   proves search + rank ran without booking.
3. Absence of any `watch: booked …` line (`watch_orchestrator.py:360`) — confirms no POST.

**6.3 Temporary instrumentation.** The PR6 `race: busy-wait complete` line and the `wait mode`
line are the only additions; they are *permanent* (cheap, high-value), not temporary. No
debug-only flags needed. If finer drift detail is wanted for the very first run, temporarily raise
the orchestrator logger to DEBUG via `LOG_LEVEL` env (optional; not required to prove the two
facts).

**6.4 On-demand dev trigger (safe).** Dev is `dryRun=true`, so a manual run cannot book. To
trigger on demand **the operator runs** (agents MUST NOT run `az containerapp job start` without
explicit approval — `infra/CLAUDE.md`):
```
az containerapp job start --name teetime-job-dev-edt-sun --resource-group rg-teetime-dev
```
Because the booking job's args force `--wait` at `fire_time=06:00`, an on-demand start at, say,
2 PM ET hits the DST gate (ET hour 14 ≠ 5) and **exits cleanly without booking** — correct, but it
does NOT exercise the busy-wait. Three options to test on demand, in order of fidelity:
- (a) Wait for the real Sunday cron — the only way to observe production-identical timing (§6.6).
- (b) Use the **§6.5 `--fire-time` escape hatch** — observe the real busy-wait + DST gate +
  race-complete log at any hour, under dry-run. Highest-fidelity on-demand option.
- (c) A one-off `--no-wait` run — exercises search/rank/list_reservations end-to-end but BYPASSES
  the busy-wait and DST gate entirely (so it proves nothing about timing).
The watch job can be started on-demand any time during polling hours to prove the poller.

**6.5 On-demand busy-wait escape hatch (`--fire-time`) — reviewer item 2.** The DST gate plus the
fixed `fire_time=06:00` make the booking job's busy-wait **unobservable on demand**: at 2 PM ET the
gate skips before the busy-wait runs. To verify the busy-wait + race-complete log without waiting
for Sunday, the operator runs a **one-off command-override** execution of the booking job (ACA Job
`--command`/`--args` override on a manual start) that passes a near-future `--fire-time`:
```
# Operator-run (agents need explicit approval). Dev is dryRun=true so this cannot book.
az containerapp job start --name teetime-job-dev-edt-sun --resource-group rg-teetime-dev \
  --args 'run' '--config' '/app/config/container.toml' '--wait' '--dry-run' 'true' \
         '--fire-time' '<HH:MM:SS ~2 min from now, ET>'
```
`--fire-time` (PR1, E3) overrides `cfg.scheduler.fire_time` so:
- the DST gate predicate `ET_hour == fire_time.hour - 1` is satisfiable at the chosen hour, and
- `busy_wait_until` targets the near-future T0, fires within ~1 s, and emits the §6.1 line-4
  `race: busy-wait complete` log — all under `--dry-run true` (no POST).
`--fire-time` is **hard-refused unless `--dry-run true`** (PR1 `run_cmd` guard), so it can never
shift a real booking's race time. This makes the busy-wait path observable on demand in dev. (Unit
proof is the FakeClock PR6 test; this is the integration/live-container proof.)

**6.6 Accepted M6 exit criterion (reviewer item 2).** Production-identical booking timing
(`fire_time=06:00`, real ACA cron jitter, real cold-start) is verifiable ONLY on the live Sunday
cron. The `--fire-time` hatch (§6.5) proves the busy-wait MECHANISM on demand, and FakeClock proves
it deterministically in CI, but neither reproduces the real :50→:00 cron lead + ACA scheduling
jitter. **M6's exit criterion therefore explicitly includes observing the §6.1 sequence on at least
one real (dry-run) Sunday cron in dev before prod cutover, plus 3 clean real Sunday runs in prod
(§7.5).** This is stated plainly rather than pretending on-demand dev can prove the live timing.

---

## 7. Prod cutover checklist (dryRun=false)

Prerequisites and sequence (operator-run; agents do not run deploy/secret/job-start commands):

**7.1 Prod Key Vault secrets (must exist before first real run).** Per AZURE_PLAN §7.1, the prod
vault needs exactly: `MB-USERNAME`, `MB-PASSWORD`, `PLAYER1-EMAIL`, `PLAYER1-PHONE`,
`PLAYER1-MB-MEMBER`, `TWOCAPTCHA-API-KEY`. No PLAYER2/3/4 secrets (ForeUP books by count). No SMTP.
Verify with `az keyvault secret list` (read-only; agent-safe).

**7.2 Credential / provider validity.** Confirm the ForeUP login works (a live dry-run that
reaches `authenticate()` + `list_reservations()` without AUTH_FAILED) and the 2captcha key has
balance (captcha solve happens inside `book()` only on `dryRun=false`, so this is the first run
that exercises it — pre-fund the account).

**7.3 Budget + purge protection.** `enablePurgeProtection=true` in `main.bicepparam.prod` (KV);
budget module deployed (AZURE_PLAN §10.1 step 6). Confirm budget alert email is set (open question
AZURE_PLAN §12 Q4).

**7.4 Cutover sequence (AZURE_PLAN §10.3 + M6 specifics).**
1. Dev green: both jobs verified per §6 under `dryRun=true` (PRs 1–6 merged, auto-deployed to dev).
2. Confirm no v0 GH cron is active (book.yml/watch-tee-time.yml already removed — PLAN §21).
3. Push an `infra/v*` tag → `azure-iac.yml` deploys to the `prod` GitHub environment, which
   **requires manual approval** (AZURE_PLAN §8.1). `main.bicepparam.prod` has `dryRun=false`.
4. Approve the prod deploy. ACA picks up the new image/config on the next cron fire.
5. First real Sunday: monitor the correct-season job logs for the §6.1 sequence ending in a
   **BOOKED** terminal (not DRY_RUN) and a `TTB:<id>` confirmation_code, plus the course's own
   confirmation email to PLAYER1-EMAIL.

**7.5 First-run monitoring + rollback.**
- Watch the prod booking-job execution at 06:00 ET. Success = BOOKED + course email.
- **Rollback:** if the first real run misbooks or errors, set `dryRun=true` in
  `main.bicepparam.prod` and re-deploy via a new `infra/v*` tag (operator approval) — this halts
  all real POSTs immediately on the next cron. The watch job's upgrade path is OFF (PR4 §4.4), so
  the only real-money action is the single Sunday book; a bad slot can be cancelled manually on
  the ForeUP site. Keep the prior good image tag noted for a fast revert.
- Monitor for 3 consecutive clean Sunday runs (AZURE_PLAN §10.3 step 5) before declaring M6 done.

---

## 8. Prerequisites / risks / architecture extensions

**Extensions (NOT changes) to the ratified architecture — flagged loudly:**
- **E1 — `--wait/--no-wait` flag + `TEETIME_WAIT` env.** New CLI surface. The ratified design
  assumed "cron path uses the real scheduler" but never wired the selector (the code unconditionally
  used the demo scheduler). This is the missing wire, not a redesign.
- **E2 — `core/dst_gate.py`.** New module re-homing the deleted `book.yml` `dst` step. The gate
  *concept* is ratified (PLAN §6.3); only its *location* moves (workflow → entrypoint).
- **E3 — `--fire-time HH:MM:SS` dev/test override** on `teetime run` (refused unless
  `--dry-run true`). Lets an on-demand `--wait --dry-run true` run satisfy the DST gate + busy-wait
  at any wall-clock hour so the real timing path is observable in dev without waiting for the
  Sunday cron (§6.5). Dev-only surface; cannot affect a live booking.
- **E4 — one race-complete INFO log line** in the orchestrator. Observability addition (PLAN §10);
  no behaviour change.
- **E5 — `bookingReplicaTimeout = 1200`** (was a shared `replicaTimeout = 900`). Required because
  the booking job now busy-waits up to ~12 min inside the replica (§2 PR3 arithmetic, reviewer
  item 1). Watch job timeout unchanged.

> (Reviewer items 4 + 9: the prior plan's "E3 — FakeAdapter.book_call_count" was a no-op — the
> field already exists at `fake_adapter.py:51`. That extension is removed; E3 above is the new
> `--fire-time` dev override.)

None of these change a settled decision (ACA Jobs, InMemoryStore, list_reservations as source of
truth, cancel-before-book, midpoint ranking, Sunday foursome). They are the minimal wiring to make
the first real cron run happen.

**Risks / reviewer pre-emption:**
- **R1 — late ACA cron lands after 06:00 ET.** Gate reads hour 6 → skip; missed drop. Accepted v0
  posture (PLAN §6.2 "lose the race that day"); the watch job recovers within the week. The bot's
  busy-wait can't help if the runner isn't there before T0.
- **R2 — both DST crons fire same Sunday.** By design: correct one proceeds, wrong one DST-gates
  out. Covered by PR2 tests.
- **R3 — captcha solve inside `book()` is post-T0** (the booking Orchestrator does NOT call
  `prepare_book()` pre-race; only UpgradeOrchestrator does). The synchronous solve (~15–30 s, PLAN
  §18) lands *after* T0 in the POST latency, widening the window between slot-pick and POST. This is
  a pre-existing v0 characteristic, not introduced by M6. **Flagged for the reviewer:** if first
  real-run logs show the POST landing > ~30 s after T0 and losing slots, a follow-up should call
  `prepare_book()` pre-T0 in `Orchestrator.run` (mirror the upgrade path). Out of M6 scope; noted.
- **R4 — `what-if` shows job deletes** when the two `-sat` jobs are removed (PR5). Expected;
  stateless jobs; call out in the PR.
- **R5 — NTP UDP:123 blocked in ACA egress.** `measure_ntp_offset` already degrades to zero offset
  with a warning (`clock.py:72`). System-clock drift on the ACA host is the residual race risk;
  accepted (ratified).
- **R6 — parity test churn.** PR4 adds no `*_env`, so parity stays green; PR3/PR5 edit bicep but add
  the static-assertion tests in the same PR.
- **R7 — booking replica killed mid-busy-wait (reviewer item 1).** Before M6 the booking replica
  returned immediately; M6 makes it busy-wait ~10–12 min inside the replica. The pre-M6
  `replicaTimeout = 900` is too thin once early ACA jitter (:48/:47 landing) stacks the busy-wait
  + post-T0 captcha/POST. Mitigation: dedicated `bookingReplicaTimeout = 1200` (§2 PR3 arithmetic).
  The DST gate independently prevents the wrong-season ~70-min busy-wait that no timeout could
  absorb. Residual risk: a runner landing > ~3 min early AND a slow ForeUP/captcha could still
  approach 1200; PR6 first-run logs surface the actual replica wall-time so the value can be tuned.

---

## 9. Open questions for the user

1. **one_booking_policy in M6?** I scoped it OUT of the first real run (conservative: book + watch/log,
   no live cancel+rebook). Confirm you want upgrades OFF until a separate verified change, or
   whether the first prod Sunday should already auto-upgrade. (§4.4)
2. **Cold-start lead.** I keep the :50 cron (10-min lead) and only *document* the budget. If you've
   seen ACR pulls > 5 min on the dev consumption env, say so and I'll move the booking cron to :48.
   (§2 PR3)
3. **Late-cron (hour 6) policy.** I chose "skip once past 06:00 ET, let the watcher recover." If
   you'd rather a late run still attempt a public-window race (risk: churn, already-dropped slots),
   that changes the gate predicate. Confirm skip-on-late. (§3, R1)
4. **`TEETIME_WAIT` env in bicep.** I decided NOT to add it to `commonEnv` (booking job uses
   explicit `--wait` arg; watch ignores it). Confirm you don't want the env knob for ad-hoc starts.
   (§2 PR3)
5. **AZURE_PLAN §12 Q4 budget alert email** — still open upstream; needed before prod budget deploy.
6. **Daily-watch + policy-on design (reviewer item 7).** The watch cron is daily with target=today+7,
   so on non-Sundays it ranks slots on dates you never intended to play. This is harmless while
   `one_booking_policy.enabled=false` (M6's setting). But if/when you want auto-upgrades, daily-watch
   + policy-on would try to upgrade unintended dates. Before enabling policy, do you want (a) a
   Sunday-aligned watch target, (b) an "only upgrade a date with a managed BOOKED terminal" guard,
   or (c) something else? Out of M6 scope; flagging so it isn't silently inherited. (§4.3, §4.4)
7. **Booking replicaTimeout value (reviewer Q6).** I raised the booking job to
   `bookingReplicaTimeout = 1200` (20 min) so the busy-wait + early-jitter + post-T0 work fit with
   margin (§2 PR3 arithmetic). Confirm 1200 is acceptable, or specify a different ceiling. The watch
   job stays at 120 s. (§2 PR3, R7)
