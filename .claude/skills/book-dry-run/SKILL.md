---
name: book-dry-run
description: Run a local dry-run booking attempt and interpret the result. Use when you want to exercise the full search/pick flow (no booking POST) against the configured course, or to sanity-check a change before a live run. Runs the type/lint gate first.
argument-hint: "[config path; default config/local.toml] [--fake to use the fake adapter]"
allowed-tools: [Read, Bash, Glob, Grep]
model: sonnet
---

# Dry-run a booking attempt

Drives the orchestrator end-to-end **without** sending the final booking POST.
Use it to verify a change, explore the CLI, or confirm live ForeUP/TeeItUp still
parses (dry-run still authenticates and searches).

Arguments: **$ARGUMENTS** (a config path and/or `--fake`; defaults below).

## 1. Pre-checks (fast feedback before any network)

```bash
uv run ruff check . && uv run mypy
```

Stop and report if either fails — don't run the bot on a red tree.

## 2. Pick the invocation

- **Real adapter** (default): hits live ForeUP/TeeItUp up to but not including the
  booking POST. Requires creds in the environment:

  ```bash
  set -a && source .env && set +a       # load MB_*/SM_* etc.
  uv run teetime run --config config/local.toml --dry-run true
  ```

  If `config/local.toml` is absent, fall back to `config/example.toml` and say so.

- **Fake adapter** (`--fake`, no creds needed): exercises the pipeline offline.

  ```bash
  uv run teetime run --config config/example.toml --dry-run true --use-fake-adapter
  ```

Honor an explicit config path passed in `$ARGUMENTS`.

## 3. Read the structured log and report

The bot logs `request_id`, `course_id`, `attempt` per line. Surface, in plain terms:

- **Outcome**: `dry_run` (slot found, POST skipped — the success case here),
  `already_booked` (layer-2 guard saw an existing reservation), `no_inventory`
  (nothing in the window), or an error outcome.
- **Which slot** was picked and how it ranked (midpoint-distance within the
  08:45–10:00 window) — if a slot was found.
- For real runs, note whether **authenticate + search** succeeded (this is also a
  live API-drift signal; if it errors, the adapter may need updating before the
  next cron — see `tests/test_foreup_canary.py`).
- For live ForeUP non-dry runs only, the CAPTCHA path needs `TWOCAPTCHA_API_KEY`;
  dry-run does not solve CAPTCHAs (no POST), so its absence is expected here.

## 4. Don't

- Don't pass `--dry-run false` from this skill — that books for real. If the user
  wants a live booking, they run it explicitly.
- Don't commit `config/local.toml` (gitignored) or echo secret values.
