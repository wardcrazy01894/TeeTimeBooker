# TeeTimeBooker

A Python bot that books golf tee times the instant a course's online window opens. The primary
target is **Mangrove Bay Golf Course** (St. Petersburg, FL), which releases tee times at exactly
06:00 ET, 7 days ahead; the bot books **Saturday and Sunday** mornings (configurable: wanted days
are derived from the per-day `[[request.time_windows]]`). It speaks the **ForeUP** and **TeeItUp**
platforms (e.g. Sydney R. Marovitz, Chicago Park District), runs unattended as Azure Container
Apps Jobs, and the golf course sends booking confirmations directly.

<!-- toc -->
## Contents

- [Status](#status)
- [Documentation](#documentation)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Running](#running)
  - [Local demo (no ForeUP credentials needed)](#local-demo-no-foreup-credentials-needed)
  - [Real bookings](#real-bookings)
  - [Cancellation watch (one-shot)](#cancellation-watch-one-shot)
- [Multi-user web app](#multi-user-web-app)
  - [Pages](#pages)
  - [Web settings](#web-settings)
  - [Tenant store](#tenant-store)
  - [Tenant jobs](#tenant-jobs)
- [Docker](#docker)
- [Azure hosting](#azure-hosting)
- [Development](#development)
- [Architecture](#architecture)
- [Roadmap](#roadmap)
- [Security and compliance](#security-and-compliance)

<!-- /toc -->

## Status

> **Prod is live** (`dryRun=false`) on the multi-user tenant path at the
> latest infra tag `infra/v3.0.0` (2026-09-27, MU-18 stage B): tenant booker and watcher over Cosmos, the invite-only
> web app, and ACS email. Release history: [docs/RELEASES.md](./docs/RELEASES.md).
>
> **Dev** runs the same tenant path in dry-run (since MU-17). The single-user TOML path (`teetime
> run` / `watch`) is kept for local course testing and as prod's rollback.

- ForeUP adapter: live bookings at Mangrove Bay since M6. TeeItUp adapter: live booking + cancel
  confirmed against Sydney Marovitz (2026-05-29).
- Live in prod: Sat+Sun booking (one reservation per day) via daily crons and a booking-day gate,
  per-day windows, a 16:00-day-before cutoff, Portal-editable skip-days, the cancellation watcher
  with auto-upgrade, and the 06:00 race path (login pre-warm, pre-solved CAPTCHAs, a staggered
  blind-POST burst).
- Cut on purpose: M2.T3 in-run reconciliation (the watcher reconciles an uncertain booking on its
  next poll), M3 SQLite and M4 email for the single-user path (the live `list_reservations()`
  check is the double-booking guard; the course emails confirmations).

## Documentation

| Doc | Read it for |
|-----|-------------|
| [PLAN.md](./PLAN.md) | Engine design: state machine, DST math, etiquette, milestones |
| [CLAUDE.md](./CLAUDE.md) | Contributor and agent notes: current status, invariants, TDD, docs rules |
| [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md) | The multi-user website design and its milestones |
| [infra/AZURE_PLAN.md](./infra/AZURE_PLAN.md) | Azure hosting: Container Apps Jobs, Bicep, OIDC CI, runbooks |
| [BACKLOG.md](./BACKLOG.md) | Future wants (courses to add, deferred items) |
| [docs/RELEASES.md](./docs/RELEASES.md) | Every prod infra tag |
| [docs/MULTIUSER_AS_BUILT.md](./docs/MULTIUSER_AS_BUILT.md) | How each multi-user milestone was built |
| [docs/plans/](./docs/plans/) | Shipped design plans (historical) |
| `config/example.toml` | Copy to `config/local.toml` and edit before running |

## How it works

**06:00 booking job** (ACA Job, `compute.bicep`):

1. Two jobs fire at 05:50 ET **every morning**, one per DST half; the wrong-season one exits via
   the DST gate. Each run computes `today + 7` and exits unless that weekday has a configured
   window (the booking-day gate), so only wanted days are booked.
2. On a wanted day the bot logs in and pre-solves CAPTCHAs during the wait, then busy-waits to T0.
3. At T0 Mangrove Bay **blind-POSTs** the top slots nearest the window midpoint without waiting
   for a search: up to `scheduler.blind_post_max_count` (default 3) POSTs, staggered
   `-500 / -250 / 0` ms across the open. It keeps the best booking and cancels any extras.
   ForeUP's one-reservation-per-day rule rejects the surplus POSTs once one lands; that is the
   accepted cost of the hedge.
4. Only if no blind POST books does it re-check for an existing booking and run one fresh search,
   picking the slot **closest to the window midpoint** (a 4xx moves to the next-ranked slot).
5. A live `list_reservations()` check before booking guards against double-booking.

**Cancellation watch job** (ACA Job, every 10 minutes in prod, year-round):

1. Polls on every run (no time-of-day gate), so it sees the 06:00 drop and early cancellations.
2. Each run checks the next occurrence of **each** wanted weekday and can book or upgrade any of
   them, one reservation per day. The search is scoped per target date, so Saturday and Sunday can
   have different windows.
3. Books an open slot in the preferred window immediately (including an early-morning recovery if
   the 06:00 race missed), and upgrades a held booking to a closer-to-midpoint slot.

## Quick start

Prerequisites: Python 3.12+, [`uv`](https://docs.astral.sh/uv/) (`brew install uv`), and a ForeUP
account with Mangrove Bay access (not needed for the fake-adapter demo).

```bash
git clone <repo-url>
cd TeeTimeBooker
uv sync
cp config/example.toml config/local.toml
cp .env.example .env && $EDITOR .env
set -a && source .env && set +a
uv run teetime run --config config/example.toml --dry-run true --use-fake-adapter
```

## Configuration

Secrets never live in TOML: the config names env vars and the loader resolves them at runtime.
Variable names are listed in `.env.example` and match the `*_env` defaults in
`config/example.toml`. Wrap values containing `&`, `!` or `$` in **single quotes**
(`MB_PASSWORD='yourpass&word'`).

| Setting | What it does |
|---------|--------------|
| `[[request.time_windows]]` | Each window carries a `weekday`; wanted days are derived from them. Several windows may share a day (list order is preference, one booking per day) |
| `request.booking_cutoff` | Default `{ days_before = 1, time_of_day = 16:00:00 }`: after 16:00 the day before, no new booking and no upgrade for that date |
| `request.skip_dates_env` | Names an env var (default `TEETIME_SKIP_DATES`) holding ISO dates to skip, e.g. `"2026-06-14, 2026-06-21"`. Unset, empty or malformed means no skips; it never crashes the bot. Hosted: a Key Vault secret you edit in the Portal with no redeploy |
| `scheduler.blind_post_max_count` | Blind-POST burst size at T0, Mangrove Bay only (default `3`; `0` disables it and uses plain search-then-book). Capped by the pre-solved CAPTCHA pool; primary course, `--wait` path only |
| `scheduler.blind_post_stagger_ms` | Per-POST offsets from T0 (default `[-500, -250, 0]`), paired with the ranked slots. The first equals `-early_arrival_ms`, so the best slot keeps its old timing |
| `scheduler.blind_post_fallback_token_reserve` | Spare pre-solved CAPTCHA tokens kept for the fallback search (default `2`) |

Why the burst is staggered: every drop in the log window came back 3/3 or 0/3, never mixed, which
a real slot race cannot produce. A POST landing before ForeUP's release flip gets the same
`400 "Time not available."` as a claimed slot. Staggering guarantees one POST after T0 and orders
the outcome by offset, so a pre-open rejection is distinguishable from a lost race.

## Running

### Local demo (no ForeUP credentials needed)

`--use-fake-adapter` wires an in-process scriptable adapter, so the full orchestrator runs without
touching ForeUP. Placeholder values in `.env` are fine.

```bash
uv run teetime show-config --config config/example.toml                              # secrets masked
uv run teetime run --config config/example.toml --dry-run true --use-fake-adapter    # no POST
uv run teetime run --config config/example.toml --dry-run false --use-fake-adapter   # fake booking
```

### Real bookings

```bash
uv run teetime run --config config/local.toml --dry-run true    # live search, no booking POST
uv run teetime run --config config/local.toml --dry-run false   # live booking
```

### Cancellation watch (one-shot)

In production the ACA watch job runs this; run it by hand to test or to grab a cancellation now.

```bash
uv run teetime watch --config config/local.toml --dry-run true
uv run teetime watch --config config/local.toml --dry-run false
uv run teetime watch --config config/local.toml --dry-run true --date 2026-06-07   # one date
```

The watcher is enabled in the shipped configs (`watcher.enabled = true`); with `--dry-run true` it
looks, ranks and logs but never books. `one_booking_policy` (cancel + rebook to a closer slot) is
enabled per date. When the watcher is disabled the command logs a warning and exits 0.

## Multi-user web app

The invite-only site from [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md). It is **deployed to prod**
(`teetime-web-prod`, MU-18) and to dev (`teetime-web-dev`, dry-run, MU-17). Users bring their own ForeUP
login (stored AES-GCM-encrypted), pick ranked (course, time window) options, and the tenant jobs
book for every account at the drop.

```bash
uv run teetime web --host 127.0.0.1 --port 8000
```

### Pages

| Page | What it does |
|------|--------------|
| `/` | Dashboard: your dates for the next 21 days, status, booked tee time, and when the course account was last checked ("as of 07:53") |
| `/dates` | "Book a date" (the ranked form below); skip or unskip a rule date, withdraw a one-off, re-request after a cancel, and **cancel a booked tee time** (60 s lease, one login, needs a trustworthy reservation list; a booking the bot did not make needs an extra confirm; a dry-run site never cancels) |
| `/rules` | "New weekly booking" and the list of standing rules. A single-window rule edits in place (from the next drop); a ranked one is replaced by deactivating it and saving a new one |
| `/accounts` | Connect a course login (checked with ONE live login, never retried; at most 5 attempts per user and 3 per login per hour, 30 site-wide, a 15-minute pause after 2 attempts on one login), set the default max price per player, re-verify a rejected login, and **Refresh from course** (cached 2 minutes, at most 6 per account per hour) |
| `/admin/users` | Operator only: invite users |

**The ranked form** (MU-R3): party size, up to 6 (course, window) options each with a rank
(courses may repeat and interleave; ranks are renumbered 1..N), and a max price per player per
course (blank = that course's default, $100 unless changed on `/accounts`). It saves one row or
rule per course sharing a group; the bot books the best-ranked option available, holds one tee
time per day, and moves to a better-ranked option if one opens. If one course cannot be saved the
others still are, and the page says which. Skip, withdraw and deactivate act on the whole group.

Plain HTML forms; the one script (`/static/app.js`, same-origin) only adds conveniences such as
"Add another time slot", and every page works without it. Courses are shown by name ("Mangrove
Bay", from `courses/names.py`), never by course id; one stylesheet with light and dark themes.
The operator also gets **Adopt existing bookings** on `/accounts`, used once at the prod cutover to
record the old bot's live reservations as the bot's own (MU-16b). Sign-in is Google OAuth in every deployed env (GitHub is supported
in code but unwired in infra); only an email the operator invited can sign in. Without
`TENANT_CREDS_KEYRING`, connect/refresh/cancel answer "not available".

### Web settings

The site fails closed if a required setting is missing.

| Env var | Purpose |
|---------|---------|
| `TEETIME_PUBLIC_BASE_URL` | Public origin used for the OAuth callback URL |
| `WEB_SESSION_SECRET` | Signs the session cookie |
| `OAUTH_GOOGLE_CLIENT_ID` / `OAUTH_GOOGLE_CLIENT_SECRET` | Google sign-in (at least one provider is required; deployed as Key Vault secrets) |
| `OAUTH_GITHUB_CLIENT_ID` / `OAUTH_GITHUB_CLIENT_SECRET` | GitHub sign-in (optional; unset in every deployed env) |
| `TEETIME_OPERATOR_EMAIL` | The only account allowed on `/admin/users` (deployed: from the `OPERATOR-NOTIFY-EMAIL` Key Vault secret) |
| `TEETIME_WEB_DRY_RUN` | `true` (default) or `false`; a dry-run site never cancels a real reservation |
| `WEB_FORWARDED_ALLOW_IPS` | Proxy IPs uvicorn trusts for forwarded headers (default `127.0.0.1`, ACA's ingress sidecar) |

### Tenant store

Every tenant command (`tenant-run`, `tenant-plan`, `tenant-watch`, `tenant-migrate`, `web`) opens
the store the same way: with `TENANT_COSMOS_ENDPOINT` set, Cosmos (Entra auth only: the managed
identity in Azure, your `az login` locally); with nothing set, an empty in-memory store and a loud
WARNING; anything half-configured refuses to start.

| Env var | Purpose |
|---------|---------|
| `TENANT_COSMOS_ENDPOINT` | Cosmos endpoint (`https://<account>.documents.azure.com:443/`); unset = in memory |
| `TENANT_COSMOS_DATABASE` | `dev` or `prod`; REQUIRED with the endpoint, never defaulted |
| `AZURE_CLIENT_ID` | The user-assigned managed identity's client id (set by Bicep in tenant mode) |
| `TENANT_COSMOS_CONTAINER_SUFFIX` | `-ci` selects the integration suite's CI containers (dev only); never set by a job |

### Tenant jobs

`teetime tenant-run --event mb0600et [--dry-run true|false] [--wait/--no-wait]` is one multi-user
booking run for a release event; `--wait` is the cron path (NTP offset, DST gate, then the
unchanged per-account race). It exits non-zero only for systemic causes (store, keyring, a decrypt
failure, CAPTCHA/OTP, an uncertain booking, the self-deadline, a failed outcome write, or an
operator summary that could not be sent); one user's miss or bad password exits 0 and is reported
by email. `teetime tenant-plan --event mb0600et` prints the event's pending rows and blind-slot
allocation with no ForeUP call.

`teetime tenant-watch --dry-run true` is one tenant-watcher pass: shared searches, logins only on
an opportunity, snapshot trust, vanish and adoption handling, ownership-gated upgrades.

`teetime tenant-migrate` runs the ordered data migrations (v1 ships none). It requires Cosmos and is
what the Manual `teetime-migrate-<env>` job runs; CI starts it after deploy pass 2 in an env whose
`bookingMode`/`watchMode` is `tenant`.

| Env var | Read by | Purpose |
|---------|---------|---------|
| `TENANT_CREDS_KEYRING` | `tenant-run`, `tenant-watch`, `web` | Credential keyring (required by the jobs; optional for `web`) |
| `TWOCAPTCHA_API_KEY` | `tenant-run`, `tenant-watch` | 2captcha key for the shared per-course CAPTCHA pool (not needed with `--dry-run true`) |
| `OPERATOR_NOTIFY_EMAIL` | `tenant-run` | Operator-summary recipient; unset means every summary send fails and a run with anything to report exits non-zero |
| `ACS_EMAIL_CONNECTION` | all three | ACS connection string (a Key Vault secret; the access key is masked in logs) |
| `ACS_EMAIL_SENDER` | all three | Sender on the Azure-managed domain, e.g. `DoNotReply@<guid>.azurecomm.net` |

Notifications go out as plain-text email through Azure Communication Services over REST (no SDK).

## Docker

```bash
docker build -t teetime:dev .
set -a && source .env && set +a
docker run --rm \
  -e MB_USERNAME -e MB_PASSWORD \
  -e PLAYER1_EMAIL -e PLAYER1_PHONE -e PLAYER1_MB_MEMBER \
  -e TWOCAPTCHA_API_KEY \
  teetime:dev \
  uv run teetime run --config /app/config/container.toml --dry-run true
```

The image bakes in `config/container.toml` and reads secrets from env vars (the same names as the
Key Vault references). It always books a foursome, but only **Player 1** (the account holder)
needs contact details: ForeUP transmits only the player count, so guests 2–4 are name-only.
`tests/test_container_config_parity.py` fails the build if `container.toml` references an env var
that `compute.bicep` does not wire. The TOML path is stateless between runs and logs to stdout.

## Azure hosting

The booking and watch schedules run as **Azure Container Apps Jobs** on UTC crons, secrets live in
**Azure Key Vault**, and the multi-user site is a scale-to-zero **Container App**. The TOML path
makes no authenticated Azure SDK calls at runtime; the tenant path reads Cosmos with its managed
identity. About $5/month (ACR Basic; compute sits in the free grant).

| | Dev | Prod |
|---|---|---|
| Deploy | Auto on merge to `main` | Manual approval, tagged `infra/vX.Y.Z` |
| Mode | `dryRun = true` | `dryRun = false` |
| Booking/watch | Tenant mode (`tenant-run` / `tenant-watch`), watcher hourly | Tenant mode since MU-18 stage B (`infra/v3.0.0`; was TOML `run` / `watch`), watcher every 10 min |
| Web app, ACS email, Cosmos | Deployed (`deployWebApp`/`deployAcsEmail = true`) | Deployed since MU-18 stage A (`infra/v2.17.0`) |

- **IaC:** Bicep modules `identity`, `registry` (one shared ACR in `rg-teetime-shared`),
  `keyvault`, `logs`, `compute`, `budget`, `killswitch` + `killswitch-rbac-prod`, `webapp`,
  `email`, and the standalone `cosmos` account. Booking jobs are derived from
  `infra/bicep/release_events.json`. `.github/workflows/azure-iac.yml` runs `bicep build` +
  `what-if` on PRs and deploys.
- **Cost killswitch:** a $50 actual-spend budget fires a Logic App that disables and stops every
  ACA Job and stops the web apps; a $20 email-only budget is the early warning.
- **CI auth:** OIDC federated credentials, no client secret. GitHub secrets (AZURE_PLAN §8.2):

```
AZURE_CLIENT_ID       # 7a9c17a4-b65b-4028-99db-6a099d2b9524
AZURE_TENANT_ID       # 5151757e-ef5b-42a5-a09b-6410b40b2186
AZURE_SUBSCRIPTION_ID # 3f82c7e1-4b1b-4a55-b905-d79f65c6887d
```

See [infra/AZURE_PLAN.md](./infra/AZURE_PLAN.md) for the architecture, cost breakdown, security
checklist and deploy runbooks.

## Development

```bash
uv run pytest                        # tests
uv run pytest -m "not integration"   # skip live-network tests (CI default)
uv run mypy                          # strict type-check (must pass to merge)
uv run ruff check . && uv run ruff format .
git config core.hooksPath .githooks  # once per clone: pre-push runs CI's checks
```

Work is strict red-green TDD; see [CLAUDE.md](./CLAUDE.md).

**Live ForeUP drift canary (manual, before a weekend).** `tests/test_foreup_canary.py` is the only
guard that catches ForeUP changing its login, reservation or slot shape (or `BLIND_POST_TEMPLATE`
drift) before a 06:00 drop; respx tests only check what the bot sends. It is
`integration`-marked, skips without credentials, and does NOT book. Also in AZURE_PLAN §10.4.

```bash
MB_USERNAME=… MB_PASSWORD=… uv run pytest -m integration tests/test_foreup_canary.py -v
```

**Cosmos tenant-store conformance (manual).** CI runs the full `TenantStoreConformance` suite over
an in-process fake of the Cosmos container API. The same suite runs against the real free-tier
`dev` database, `integration`-marked and skipped unless the variables below are set. It uses only
the `tenant-ci` / `global-ci` containers (the `-ci` suffix is required), authenticates with your
`az login` (key auth is disabled), and needs your principal to hold Data Contributor on those
containers (MULTIUSER_PLAN §10.5). Every test EMPTIES both CI containers, so run it from one
machine at a time.

```bash
az login
TENANT_COSMOS_ENDPOINT=https://cosmos-teetime-shared.documents.azure.com:443/ \
TENANT_COSMOS_CONTAINER_SUFFIX=-ci TENANT_COSMOS_DATABASE=dev \
  uv run pytest -m integration tests/tenant/cosmos/test_cosmos_store.py -v
```

## Architecture

```
CLI (TOML path) → Orchestrator / WatchOrchestrator → CourseAdapter (ForeUP / TeeItUp)
                                                   → BookingStore  (InMemoryStore, per run)
                                                   → Notifier      (ConsoleNotifier, stdout)

CLI (tenant path) → tenant runner / watcher → the same orchestrators, once per account
                                            → TenantStore (Cosmos) + ACS email
web (FastAPI)     → TenantStore + one-login connect / refresh / cancel
```

Every subsystem is `Protocol`-typed; the orchestrators wire them together and nothing else crosses
subsystem boundaries. Orchestrators are single-invocation: one check per job execution, with the
cron loop outside. See [PLAN.md](./PLAN.md) and [MULTIUSER_PLAN.md](./MULTIUSER_PLAN.md).

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0–M2 | Skeleton, foundations (`Clock`, config, CLI), orchestrator + state machine + idempotency | Done (M2.T3 cut: the watcher reconciles asynchronously, PLAN §9.1) |
| M3 / M4 | SQLite persistence / email notifications (single-user) | Dropped: the live `list_reservations()` check is the guard; the course sends confirmations |
| M5 | ForeUP adapter | Done |
| Spike S3 | TeeItUp adapter (Sydney Marovitz, live booking + cancel) | Done |
| M6 | End to end + prod cutover | Done; a real race on 2026-06-07 lost on CAPTCHA latency and led to #67/#68; superseded by the multi-day re-architecture, live since `infra/v2.1.0` |
| M-feature-1/2/3 | Cancellation watcher, auto-upgrade, midpoint-distance ranking | Done |
| Blind-POST | Concurrent T0 book POSTs for the MB morning grid + watcher crash-net, later staggered | Done, live since `infra/v2.5.0` |
| M-azure | Bicep IaC, container runtime | Done |
| Multi-user site | MULTIUSER_PLAN MU-1 … MU-18, MU-R1 … MU-R3 | Done; live in prod since MU-18 (`infra/v3.0.0`) |

Engine milestones in detail: [PLAN.md §16](./PLAN.md) and §20. Multi-user milestones:
[MULTIUSER_PLAN.md §12](./MULTIUSER_PLAN.md).

## Security and compliance

- Secrets come from env vars (Key Vault in Azure), never TOML; log handlers redact secrets,
  including exact registered literals such as decrypted passwords.
- Player PII is SHA-256-prefixed before any attempt-log write.
- ForeUP: no card data handled (card on file at ForeUP). TeeItUp: card details go directly to
  `tr.gnsvc.com` (GolfNow payments), stored only in `.env` (gitignored), never in config or logs.
- Multi-user: ForeUP passwords are AES-GCM-encrypted with an account-bound AAD; sign-in is
  invite-only.
- Anti-bot etiquette: honest User-Agent, ≥250 ms between requests, automatic 429 backoff. The one
  exception is the Mangrove Bay T0 blind-POST burst: a handful of book POSTs staggered across a
  ~500 ms window, no slot POSTed twice, all but the best cancelled at once, still one booking per
  request. See [PLAN.md §12](./PLAN.md).
