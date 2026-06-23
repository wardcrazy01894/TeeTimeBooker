# TeeTimeBooker

Python bot that books tee times at golf courses (ForeUP and TeeItUp platforms). Primary target: **Mangrove Bay Golf Course** (St. Petersburg, FL) at exactly 6:00 AM ET on **Saturday and Sunday** mornings (configurable — wanted days are derived from the per-day `[[request.time_windows]]` weekdays), 7 days in advance. Also supports TeeItUp-backed courses (e.g. **Sydney R. Marovitz**, Chicago Park District). Runs unattended via Azure Container Apps Jobs; the golf course sends booking confirmations directly.

**Status:** M1 + M2 + M5 + M-feature-1 + M-feature-2 + M-feature-3 + M-azure complete. M2 (orchestrator, idempotency, §9.1 state machine) is done. ForeUP adapter implemented (live dry-run confirmed, Mangrove Bay). TeeItUp adapter implemented (live booking + cancel confirmed, Sydney Marovitz, 2026-05-29). Azure v1 Bicep IaC implemented and dev auto-deploys on merge to main via `.github/workflows/azure-iac.yml` in permanent dry-run. Prod is deployed (`dryRun=false`; latest infra tag `infra/v2.6.0` — the infra-only shared-ACR consolidation, no booking-behavior change; the booking/runtime feature set shipped at `infra/v2.5.0`, 2026-06-22 — multi-day + per-day windows + 4PM-day-before cutoff + skip-days + within-window upgrade + captcha timeout recovery + the race-prewarm bundle (`infra/v2.4.0`) + the MB blind-POST race feature (`infra/v2.5.0`) all LIVE); there are no remaining v0 code tasks. M2.T3 (a synchronous in-run post-mortem reconciliation path) was **cut** — an UNCERTAIN book raises out loudly and the watcher reconciles it asynchronously on its next ≤10-min poll (PLAN.md §9.1). M3 (SQLite persistence) and M4 (email notifications) were intentionally cut — the live `list_reservations()` pre-book check is the double-booking guard, and the golf course sends confirmations directly. Cancellation watch job live (ACA watch job); auto-upgrade (M-feature-2) shipped — cancel-before-book + CAPTCHA pre-fetch, Spike S4 resolved. **Multi-day re-architecture complete in code** (plan-with-review ratified): books Saturday **and** Sunday (one reservation per day) via daily booking crons + a booking-day gate; the watcher polls every run and checks each wanted weekday; race-path CAPTCHA pre-fetch + 4xx multi-slot fallback merged. LIVE in prod since `infra/v2.1.0` (2026-06-10); the race-prewarm bundle (login pre-warm + multi-token CAPTCHA pool + search-sleep trim) went live at `infra/v2.4.0` and the Mangrove Bay blind-POST race feature (concurrent T0 blind book POSTs, keep-best + cancel-extras) at `infra/v2.5.0` (2026-06-22). Dev continues to auto-deploy in dry-run.

**Where to look:**
- [PLAN.md](./PLAN.md) — full design, milestone roadmap, state machine, DST math, spikes
- [CLAUDE.md](./CLAUDE.md) — operator and contributor notes (read this if you're picking up a milestone task)
- [infra/AZURE_PLAN.md](./infra/AZURE_PLAN.md) — v1 Azure serverless hosting design (Container Apps Jobs, Bicep IaC, OIDC CI)
- [FRONTEND_PLAN.md](./FRONTEND_PLAN.md) — proposed v2 web UI over the engine (list / cancel / edit prefs); no code yet
- [BACKLOG.md](./BACKLOG.md) — running list of future wants (courses to add, frontend ideas)
- `config/example.toml` — copy to `config/local.toml` and edit before running

---

## How it works

**6 AM booking job** (ACA Job — `compute.bicep`):
1. Two Azure Container Apps Jobs fire ~10 minutes before 6:00 AM ET **every morning** (two jobs, one per DST half; the wrong-season one exits via the DST gate). Each run computes `today + 7` and **fast-exits** unless that weekday has a configured window (the wanted days, derived from `[[request.time_windows]]`; default Sat+Sun) — the booking-day gate. So the daily crons book only the wanted days.
2. On a wanted day the bot busy-waits until T0 (±250 ms), pre-solving the CAPTCHA during the wait so the POST fires at the drop
3. It polls for available slots, picks the slot **closest to the midpoint** of the 08:45–10:00 ET window (midpoint-distance sort), and POSTs the booking (on a 4xx it tries the next-ranked slot). At a competitive drop Mangrove Bay also **blind-POSTs** the known morning grid at T0 — firing up to `scheduler.blind_post_max_count` (default 12) book POSTs concurrently for the in-window slots, keeping the best and cancelling the rest — to beat the search round-trip (set `blind_post_max_count = 0` to disable)
4. A live `list_reservations()` check immediately before the book POST guards against double-booking; the golf course sends the booking confirmation directly

**Cancellation watch job** (ACA Job — `compute.bicep`):
1. A third ACA Job fires every 10 minutes, year-round, and **polls on every run** (no time-of-day gate — so it sees the 6 AM drop + early cancellations)
2. Each run — whatever weekday it executes on — checks the next upcoming occurrence of **each** wanted weekday within the horizon (the upcoming Sat **and** Sun) and can book/upgrade any of them, one reservation per day. The search is scoped per **target date**: when evaluating the Saturday target it considers only Saturday slots+window, the Sunday target only Sunday's (so windows can differ per day) — this is not a limit based on the day the watcher runs
3. If a slot opens in the preferred window, it books immediately (including an early-morning recovery if the 6 AM race missed)

---

## Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) — `brew install uv` or `pip install uv`
- A ForeUP account with Mangrove Bay access

---

## Installation

```bash
git clone <repo-url>
cd TeeTimeBooker
uv sync
```

---

## Configuration

Copy the example config and fill in your preferences:

```bash
cp config/example.toml config/local.toml
```

Secrets are never stored in TOML — the config file references env var names, and the loader resolves them at runtime.

**Booking cutoff.** `request.booking_cutoff = { days_before = 1, time_of_day = 16:00:00 }` (the default) freezes a target date once wall-clock passes that time on the day before it — after the cutoff the bot makes no new booking and no upgrade for that date, so you're never surprised by a last-minute booking. Tune `days_before`/`time_of_day` to move the cutoff.

**Skip a day.** To tell the bot *not* to book a specific date (e.g. you're out of town), set the env var named by `request.skip_dates_env` (default `TEETIME_SKIP_DATES`) to a comma/space-separated ISO date list, e.g. `export TEETIME_SKIP_DATES="2026-06-14, 2026-06-21"`. Both the booking job and the watcher skip those dates (and won't upgrade a held booking on them — cancel it yourself on the course site and it'll stay cancelled). Unset/empty/malformed = no skips (it never crashes the bot). In the hosted deployment this is a Key Vault secret you edit in the Azure Portal with no redeploy — see `infra/AZURE_PLAN.md`.

**Blind-POST burst (Mangrove Bay only).** At the 06:00 ET drop the booking job fires up to `scheduler.blind_post_max_count` (default `12`) book POSTs **concurrently** for the known in-window morning grid, *without* waiting for the search round-trip, keeps the best-ranked reservation, and cancels the rest in the same run. The real search runs alongside as a grid-drift fallback. Set `scheduler.blind_post_max_count = 0` to disable it and fall back to the plain search-then-book path. This is capped by the pre-solved CAPTCHA pool and only ever fires for the primary, blind-capable course on the `--wait` race path.

Copy `.env.example` to `.env` and fill in your values. Wrap any value that contains special characters (`&`, `!`, `$`, etc.) in **single quotes**:

```bash
cp .env.example .env
$EDITOR .env
# e.g. MB_PASSWORD='yourpass&word'
```

Load for your current terminal session:

```bash
set -a && source .env && set +a
```

Variable names follow the `*_env` references in your config; the names are listed in `.env.example` and match the defaults in `config/example.toml`.

---

## Running

### Local demo (no ForeUP credentials needed)

The `--use-fake-adapter` flag wires an in-process scriptable adapter so you
can drive the full orchestrator flow locally without hitting ForeUP. Useful
for verifying the install, exploring the CLI, or developing against the booking pipeline.

```bash
cp .env.example .env
$EDITOR .env                # placeholders are fine for the fake adapter
set -a; source .env; set +a

# Print the resolved config with secrets masked.
uv run teetime show-config --config config/example.toml

# Dry run — full search/pick flow, no booking POST.
uv run teetime run --config config/example.toml --dry-run true --use-fake-adapter

# Demo a "successful" booking against the fake adapter.
uv run teetime run --config config/example.toml --dry-run false --use-fake-adapter
```

### Real bookings

```bash
cp config/example.toml config/local.toml
$EDITOR config/local.toml   # if your needs differ from the example

# Dry run — searches live ForeUP, prints result, skips the booking POST.
uv run teetime run --config config/local.toml --dry-run true

# Live booking — omit --dry-run or set it to false.
uv run teetime run --config config/local.toml --dry-run false
```

### Cancellation watch (one-shot check)

The watch command runs one availability check and exits. In production it is
called by the ACA watch Job every 10 minutes. You can trigger it
manually to test or to grab a cancellation slot immediately:

```bash
# Dry run — check for available slots but do not book.
uv run teetime watch --config config/local.toml --dry-run true

# Live check — book immediately if a slot is found.
uv run teetime watch --config config/local.toml --dry-run false

# Watch a specific date instead of the default (the next occurrence of each wanted weekday).
uv run teetime watch --config config/local.toml --dry-run true --date 2026-06-07
```

The watch feature is **enabled by default in the v1 configs** (`config/container.toml`
and `config/local.toml`, `watcher.enabled = true`). With `--dry-run true` it does all the
looking/ranking/logging but never books. When disabled, the command logs a warning and
exits 0. `one_booking_policy` (cancel + rebook to a closer-to-midpoint slot) is **enabled** — the watcher upgrades a booked day if a better slot opens (per date — Sat and Sun independent). Real effect is prod-only (dry-run suppresses the cancel/book POSTs).

---

## Docker (v1)

The bot can be built as a container for Azure deployment:

```bash
docker build -t teetime:dev .
```

The container reads config from `config/container.toml` (baked in) and secrets from environment variables — the same names as GitHub Actions secrets / Azure Key Vault references. For a local test run:

```bash
# Set-a sources .env; MB_USERNAME etc. must be present.
set -a && source .env && set +a
docker run --rm \
  -e MB_USERNAME -e MB_PASSWORD \
  -e PLAYER1_EMAIL -e PLAYER1_PHONE -e PLAYER1_MB_MEMBER \
  -e TWOCAPTCHA_API_KEY \
  teetime:dev \
  uv run teetime run --config /app/config/container.toml --dry-run true
```

The container always books a full foursome (4 player slots), but only **Player 1**
(the account holder) needs contact details. ForeUP's booking request transmits
only the player *count* — never per-guest name/email/phone (same as the website,
which never asks for guest emails) — so guests 2–4 in `config/container.toml` are
name-only and require no `PLAYER2/3/4_EMAIL` secrets. A CI test
(`tests/test_container_config_parity.py`) fails the build if `container.toml` ever
references an env var that isn't wired in `compute.bicep`.

The container notifier is `console` (stdout); booking confirmations come from the golf course directly. The bot is stateless — no state file is written or read between runs.

---

## Azure hosting

The booking and watch schedules run as **Azure Container Apps Jobs** — managed, serverless scheduled jobs that run the same Python container on a UTC cron. Secrets live in **Azure Key Vault**; the bot makes no authenticated Azure SDK calls at runtime (state is in-process only for the duration of each run).

The former GitHub Actions booking and watch workflows (`book.yml`, `watch-tee-time.yml`) have been removed — they are superseded by the ACA Jobs. The only remaining GitHub Actions workflows are `ci.yml` (lint / type-check / test / docker-smoke / secret-scan / bicep-lint) and `azure-iac.yml` (Bicep deploy).

**Cost:** ~$5/month (ACR Basic flat; Container Apps compute is within the free tier).

**IaC status: implemented.** All Bicep modules are complete (`identity`, `registry`, `keyvault`, `logs`, `compute`, `budget`, `killswitch` + `killswitch-rbac-prod`). The active CI workflow is `.github/workflows/azure-iac.yml` — it runs `bicep build` + `what-if` on PRs and **auto-deploys to dev on merge to main** (no required-reviewer gate for dev; prod requires manual approval). Dev runs in permanent dry-run (`dryRun = true`); **prod is live** (`dryRun = false`, latest infra tag `infra/v2.6.0` — the infra-only shared-ACR consolidation into a dedicated `rg-teetime-shared`, no booking-behavior change; runtime features deployed at `infra/v2.5.0` — multi-day/per-day + cutoff + skip-days + within-window upgrade + captcha timeout recovery + race-prewarm bundle (`v2.4.0`) + MB blind-POST race feature (`v2.5.0`)).

**Cost killswitch:** a $50 actual-spend budget fires a Logic App that disables and stops all ACA Jobs (live in dev). The $20 email-only budget remains the early-warning tier.

See [infra/AZURE_PLAN.md](./infra/AZURE_PLAN.md) for the full architecture, cost breakdown, security checklist, and deploy runbook.

**GitHub secrets required for v1 CI** (configured per AZURE_PLAN.md §8.2):

```
AZURE_CLIENT_ID       # 7a9c17a4-b65b-4028-99db-6a099d2b9524
AZURE_TENANT_ID       # 5151757e-ef5b-42a5-a09b-6410b40b2186
AZURE_SUBSCRIPTION_ID # 3f82c7e1-4b1b-4a55-b905-d79f65c6887d
```

No `AZURE_CLIENT_SECRET` is stored — authentication uses OIDC federated credentials.

---

## Development

```bash
uv run pytest                        # run tests
uv run pytest -m "not integration"  # skip live-network tests (default in CI)
uv run mypy                          # strict type-check (must pass to merge)
uv run ruff check .                  # lint
uv run ruff format .                 # format
```

**Live ForeUP API-drift canary (manual, pre-weekend).** `tests/test_foreup_canary.py` is the
ONLY guard that ForeUP changing its login/reservation/slot shape (or `BLIND_POST_TEMPLATE` drift)
is caught **before** a 06:00 drop — respx unit tests only assert what the bot *sends*. It is
`integration`-marked (excluded from CI's `-m "not integration"`) and skips unless `MB_USERNAME`/
`MB_PASSWORD` are set, so it never runs automatically. **Run it manually before a weekend cron**
(it does NOT book):

```bash
MB_USERNAME=… MB_PASSWORD=… uv run pytest -m integration tests/test_foreup_canary.py -v
```

This step is also in the operator runbook (`infra/AZURE_PLAN.md` §10.4).

---

## Architecture

```
CLI → Orchestrator      → CourseAdapter (ForeUP / TeeItUp)
   → WatchOrchestrator  → BookingStore  (InMemoryStore — in-process only)
                        → Notifier      (ConsoleNotifier — stdout)
```

All subsystems are `Protocol`-typed — orchestrators wire them together; nothing else crosses subsystem boundaries. `WatchOrchestrator` is single-invocation: it checks once and exits; the cron loop is external. See [`PLAN.md`](./PLAN.md) for the full design, milestone roadmap, and DST math. See [`CLAUDE.md`](./CLAUDE.md) for agent/contributor notes.

---

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Repo skeleton, stubs, plan | Done |
| M1 | Foundations: `Clock`, config loader, CLI | Done |
| M2 | Orchestrator core, state machine, idempotency | Done — M2.T3 in-run reconciliation cut; watcher reconciles async (§9.1) |
| M3 | SQLite persistence | Dropped — live `list_reservations()` check is the guard; durable store not needed for single-user low-frequency bot |
| M4 | Email notifications | Dropped — golf course sends confirmations directly; `ConsoleNotifier` (stdout) is sufficient |
| M5 | ForeUP adapter — live dry-run confirmed | Done |
| Spike S3 | TeeItUp adapter — live booking + cancel confirmed (Sydney Marovitz) | Done |
| M6 | End-to-end + prod cutover | Done — prod deployed (`dryRun=false`). A real booking race ran 2026-06-07 (lost on CAPTCHA latency → fixed in #67/#68). Superseded by the multi-day re-arch (Sat+Sun), first live `infra/v2.1.0` (2026-06-10); current prod `infra/v2.6.0` (shared-ACR consolidation, infra-only; runtime features at `infra/v2.5.0`, 2026-06-22, race-prewarm + MB blind-POST) |
| M-feature-3 | Prefer slot closest to the window midpoint (midpoint-distance sort) | Done |
| M-feature-1 | Cancellation watch job — poll every 10 min, book on cancellation | Done |
| M-feature-2 | One-booking policy: auto-upgrade to higher-priority slot (cancel-before-book + CAPTCHA pre-fetch) | Done |
| M-feature (blind-POST) | Concurrent blind book POSTs at T0 for the Mangrove Bay morning grid (keep best, cancel rest) + watcher crash-net reconcile | Done — LIVE in prod (`infra/v2.5.0`, 2026-06-22, `dryRun=false`) |
| M-azure (IaC) | Azure v1 Bicep IaC: all modules implemented; dev CI-deployed in dry-run | Done |
| M-azure (runtime) | Container entrypoint wiring; in-process `InMemoryStore` + `ConsoleNotifier` | Done |

See [PLAN.md §20](./PLAN.md) for the v0.5 milestone breakdown. See [PLAN.md §16](./PLAN.md) for the core milestone breakdown with owner files and dependencies.

---

## Security & compliance

- Secrets resolved from env vars at runtime — never written to TOML or logs
- Player PII SHA-256-prefixed before writing to the attempt log
- ForeUP: no credit-card data handled (card-on-file at ForeUP)
- TeeItUp: card credentials passed directly to `tr.gnsvc.com` (GolfNow payment service); stored only in `.env` (gitignored), never in config files or logs
- Anti-bot etiquette: honest User-Agent, ≥250 ms between requests, automatic 429 backoff. The one exception is the T0 blind-POST burst (Mangrove Bay), which fires several book POSTs concurrently at the 06:00 drop and immediately cancels all but the best — one booking per request still holds. See [PLAN.md §12](./PLAN.md)
