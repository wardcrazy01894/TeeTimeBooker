# TeeTimeBooker

Python bot that books tee times at golf courses (ForeUP and TeeItUp platforms). Primary target: **Mangrove Bay Golf Course** (St. Petersburg, FL) at exactly 6:00 AM ET every Sunday, 7 days in advance. Also supports TeeItUp-backed courses (e.g. **Sydney R. Marovitz**, Chicago Park District). Runs unattended via Azure Container Apps Jobs; the golf course sends booking confirmations directly.

**Status:** M1 + M5 + M-feature-1 + M-feature-2 + M-feature-3 + M-azure complete. M2 core (orchestrator, idempotency) is done; only M2.T3 (post-mortem reconciliation) remains. ForeUP adapter implemented (live dry-run confirmed, Mangrove Bay). TeeItUp adapter implemented (live booking + cancel confirmed, Sydney Marovitz, 2026-05-29). Azure v1 Bicep IaC implemented and dev auto-deploys on merge to main via `.github/workflows/azure-iac.yml` in permanent dry-run. Prod is deployed (tag `infra/v1.0.0`, `dryRun=false`); the only remaining v0 code task is M2.T3 (post-mortem reconciliation). M3 (SQLite persistence) and M4 (email notifications) were intentionally cut — the live `list_reservations()` pre-book check is the double-booking guard, and the golf course sends confirmations directly. Cancellation watch job live (ACA watch job); auto-upgrade (M-feature-2) shipped — cancel-before-book + CAPTCHA pre-fetch, Spike S4 resolved.

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
1. Two Azure Container Apps Jobs fire ~10 minutes before 6:00 AM ET on Sunday (two jobs, one per DST half; the wrong-season one exits via the DST gate)
2. The bot busy-waits until T0 (±250 ms)
3. It polls for available slots, picks the slot **closest to the midpoint** of the 08:45–10:00 ET window (midpoint-distance sort), and POSTs the booking
4. A live `list_reservations()` check immediately before the book POST guards against double-booking; the golf course sends the booking confirmation directly

**Cancellation watch job** (ACA Job — `compute.bicep`):
1. A third ACA Job fires every 10 minutes, year-round
2. Each run performs one availability check for the upcoming target Sunday (anchored to `target_weekday` + offset, so it holds that date all week — it does not drift daily)
3. If a slot opens in the preferred window, it books immediately
4. Polling is suppressed outside 7 AM – 10 PM ET (handled internally — no DST gate needed)

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

# Watch a specific date instead of the default (the upcoming target Sunday + offset).
uv run teetime watch --config config/local.toml --dry-run true --date 2026-06-07
```

The watch feature is **enabled by default in the v1 configs** (`config/container.toml`
and `config/local.toml`, `watcher.enabled = true`). With `--dry-run true` it does all the
looking/ranking/logging but never books. When disabled, the command logs a warning and
exits 0. `one_booking_policy` (cancel + rebook to a closer-to-midpoint slot) is **enabled** — the watcher upgrades the booked Sunday if a better slot opens. Real effect is prod-only (dry-run suppresses the cancel/book POSTs).

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

**IaC status: implemented.** All Bicep modules are complete (`identity`, `registry`, `keyvault`, `logs`, `compute`, `budget`, `killswitch` + `killswitch-rbac-prod`). The active CI workflow is `.github/workflows/azure-iac.yml` — it runs `bicep build` + `what-if` on PRs and **auto-deploys to dev on merge to main** (no required-reviewer gate for dev; prod requires manual approval). Dev runs in permanent dry-run (`dryRun = true`); **prod is live** (`dryRun = false`, deployed at tag `infra/v1.0.0`).

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
| M2 | Orchestrator core, state machine, idempotency | Core done; M2.T3 (reconciliation) pending |
| M3 | SQLite persistence | Dropped — live `list_reservations()` check is the guard; durable store not needed for single-user low-frequency bot |
| M4 | Email notifications | Dropped — golf course sends confirmations directly; `ConsoleNotifier` (stdout) is sufficient |
| M5 | ForeUP adapter — live dry-run confirmed | Done |
| Spike S3 | TeeItUp adapter — live booking + cancel confirmed (Sydney Marovitz) | Done |
| M6 | End-to-end + prod cutover | Done — prod deployed at `infra/v1.0.0`; first live Sunday booking run is the final validation |
| M-feature-3 | Prefer slot closest to the window midpoint (midpoint-distance sort) | Done |
| M-feature-1 | Cancellation watch job — poll every 10 min, book on cancellation | Done |
| M-feature-2 | One-booking policy: auto-upgrade to higher-priority slot (cancel-before-book + CAPTCHA pre-fetch) | Done |
| M-azure (IaC) | Azure v1 Bicep IaC: all modules implemented; dev CI-deployed in dry-run | Done |
| M-azure (runtime) | Container entrypoint wiring; in-process `InMemoryStore` + `ConsoleNotifier` | Done |

See [PLAN.md §20](./PLAN.md) for the v0.5 milestone breakdown. See [PLAN.md §16](./PLAN.md) for the core milestone breakdown with owner files and dependencies.

---

## Security & compliance

- Secrets resolved from env vars at runtime — never written to TOML or logs
- Player PII SHA-256-prefixed before writing to the attempt log
- ForeUP: no credit-card data handled (card-on-file at ForeUP)
- TeeItUp: card credentials passed directly to `tr.gnsvc.com` (GolfNow payment service); stored only in `.env` (gitignored), never in config files or logs
- Anti-bot etiquette: honest User-Agent, ≥250 ms between requests, automatic 429 backoff
