# TeeTimeBooker

Python bot that books a tee time at **Mangrove Bay Golf Course** (St. Petersburg, FL) at exactly 6:00 AM ET, 7 days in advance. Runs unattended via GitHub Actions; emails you the result.

**Status:** v0 — framework and stubs committed, no live ForeUP traffic yet. Real booking logic lands in Milestone 1+.

**Where to look:**
- [PLAN.md](./PLAN.md) — full design, milestone roadmap, state machine, DST math, spikes
- [CLAUDE.md](./CLAUDE.md) — operator and contributor notes (read this if you're picking up a milestone task)
- `config/example.toml` — copy to `config/local.toml` and edit before running

---

## How it works

1. A GitHub Actions cron fires ~10 minutes before 6:00 AM ET (two entries handle DST)
2. The bot busy-waits until T0 (±250 ms)
3. It polls for available slots, picks the best match from your config, and POSTs the booking
4. It emails you success or failure, and persists the result to SQLite for idempotency

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

Secrets are never stored in TOML — the config file references env var names, and the loader resolves them at runtime:

```bash
# Required
export MB_USERNAME="your_foreup_username"
export MB_PASSWORD="your_foreup_password"
export PLAYER1_EMAIL="you@example.com"
export PLAYER1_PHONE="555-1234"
export SMTP_HOST="smtp.gmail.com"
export SMTP_USER="you@example.com"
export SMTP_PASS="your_app_password"

# Optional — set only if your config references them
export PLAYER1_MB_MEMBER="your_foreup_member_number"
export PLAYER2_EMAIL="guest@example.com"
```

Variable names follow the `*_env` references in your config; the names above are the defaults from `config/example.toml`.

---

## Running

### Local demo (no ForeUP credentials needed)

The `--use-fake-adapter` flag wires an in-process scriptable adapter so you
can drive the full orchestrator flow locally without hitting ForeUP. Useful
for verifying the install, exploring the CLI, or developing against the
booking pipeline before Spike S1 / M5 lands.

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

Without `--use-fake-adapter`, the CLI exits non-zero with a clear message —
the real ForeUP adapter is gated behind Spike S1 / M5 (PLAN.md §17).

### Real bookings (post-M5)

```bash
cp config/example.toml config/local.toml
$EDITOR config/local.toml   # if your needs differ from the example

# Once M5 lands, drop --use-fake-adapter:
uv run teetime run --config config/local.toml --dry-run true
```

---

## GitHub Actions setup

The workflow at `.github/workflows/book.yml` runs on two daily crons to handle DST:

| Cron (UTC)  | Covers  |
|-------------|---------|
| `50 9 * * *`  | EDT (UTC−4) |
| `50 10 * * *` | EST (UTC−5) |

A workflow step verifies the ET wall-clock hour before proceeding, so only one cron actually fires on any given day.

**Required repository secrets** (Settings → Secrets → Actions):

```
MB_USERNAME, MB_PASSWORD
PLAYER1_EMAIL, PLAYER1_PHONE
SMTP_HOST, SMTP_USER, SMTP_PASS
```

State persists between runs via `actions/cache` (key `teetime-state-v1`), storing `state/teetime.db`. Cache loss is safe — a `list_reservations()` check prevents double-booking even on a cold cache.

**Manual trigger:**

```bash
gh workflow run book-tee-time -f dry_run=true
```

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
CLI → Orchestrator → CourseAdapter (ForeUP)
                  → BookingStore  (SQLite)
                  → Notifier      (Email)
```

All subsystems are `Protocol`-typed — the orchestrator wires them together; nothing else crosses subsystem boundaries. See [`PLAN.md`](./PLAN.md) for the full design, milestone roadmap, and DST math. See [`CLAUDE.md`](./CLAUDE.md) for agent/contributor notes.

---

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Repo skeleton, stubs, plan | Done |
| M1 | Foundations: `Clock`, config loader, CLI | Pending |
| M2 | Orchestrator core, state machine, idempotency | Pending |
| M3 | SQLite persistence | Pending |
| M4 | Email notifications | Pending |
| M5 | ForeUP adapter (gated by Spike S1) | Pending |
| M6 | End-to-end, first production run | Pending |

See [PLAN.md §16](./PLAN.md) for the full milestone breakdown with owner files, dependencies, and which tasks can run in parallel. A handful of v0 architectural decisions remain open — see PLAN.md §17 and the per-section "open questions" notes.

---

## Security & compliance

- Secrets resolved from env vars at runtime — never written to TOML or logs
- Player PII SHA-256-prefixed before writing to the attempt log
- No credit-card data handled (ForeUP uses card-on-file)
- Anti-bot etiquette: honest User-Agent, ≥250 ms between requests, automatic 429 backoff
