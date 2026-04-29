# CLAUDE.md

Operator/agent notes for working in this repo. The authoritative design doc is
[PLAN.md](./PLAN.md) — read it first if you're new. This file gives the local
shape, common commands, and architectural notes that aren't obvious from
filenames.

## What this is

Python 3.12+ bot that books a tee time at **Mangrove Bay Golf Course** (St.
Petersburg, FL — ForeUP-backed) at 06:00 America/New_York, 7 days in advance.
v0 is single-user, GitHub Actions-driven. No frontend.

## Status

Stubs + plan. No real ForeUP traffic yet — that lands after Spike S1 (PLAN.md §17).

## Package layout

```
src/teetime/
  core/             # models, adapter Protocol, orchestrator, config, clock
  persistence/      # BookingStore Protocol + SqliteStore
  notifications/    # Notifier Protocol + EmailNotifier
  courses/foreup/   # Shared ForeUP HTTP base + per-course IDs
  courses/chronogolf/  # placeholder; not used in v0
config/             # example.toml; secrets via env-var refs only
.github/workflows/  # GH Actions cron (2 entries for DST)
tests/              # pytest; vcrpy cassettes go in tests/cassettes/
```

The orchestrator is the only thing that knows about all four subsystems.
Persistence, notifications, and adapters all see each other via Protocols
in `core/` — never directly. This is the cut line for parallel work.

## Common commands

(All are stubs until M1 lands. They will work once `pyproject.toml`'s deps
are synced.)

| Command                                  | Purpose                                  |
|------------------------------------------|------------------------------------------|
| `uv sync`                                | Install deps + dev deps from pyproject.  |
| `uv run pytest`                          | Run the test suite.                      |
| `uv run pytest -m "not integration"`     | Skip live-network tests (default in CI). |
| `uv run mypy`                            | `strict` type-check (must pass to merge).|
| `uv run ruff check .`                    | Lint.                                    |
| `uv run ruff format .`                   | Format.                                  |
| `uv run teetime run --config config/local.toml --dry-run true` | One-shot booking attempt, no final POST. |
| `uv run teetime show-config --config config/local.toml` | Print resolved AppConfig (with secrets redacted). |
| `gh workflow run book-tee-time -f dry_run=true` | Trigger the workflow on demand.   |

## Architectural notes (non-obvious)

- **Stubs raise `NotImplementedError`** with a PLAN.md milestone reference. If
  you implement one, also make its tests pass before merging.
- **Protocols over ABCs.** Most contracts are `Protocol` (`runtime_checkable`).
  Subclassing the Protocol is fine for shared state, but tests should not
  require subclassing — structural typing is the contract.
- **Clock is injectable everywhere.** Anything that touches wall-clock time
  takes a `Clock`, not `datetime.now`. Tests use `FakeClock`. The 6:00 AM race
  is otherwise untestable.
- **No secrets in TOML.** Config files reference env vars by name. Loader
  resolves them; missing env raises a clear error.
- **No credit-card data, ever.** ForeUP keeps card-on-file; we never POST PAN
  or CVV. If a course requires it, that's a fatal error, not a feature.
- **Double-booking defense is layered.** Idempotency check, pre-book remote
  list, single-attempt-per-slot rule, post-mortem reconciliation, advisory
  lock, GH Actions concurrency group. PLAN.md §9 has the full flow; §9.1 has
  the explicit state machine that M2.T1 implements. `list_reservations` is
  on the `CourseAdapter` Protocol from M0 — it is NOT optional.
- **DST handled by `zoneinfo`** + two GH Actions crons + a "DST-half" gate
  in the workflow that ACTUALLY checks ET wall-clock hour (book.yml `dst`
  step). Math in PLAN.md §6.3.
- **Cross-run state via `actions/cache`** (key `teetime-state-v1`). SQLite
  file in `state/teetime.db` survives between runs; cache loss is caught by
  `list_reservations` pre-book check, never produces phantom bookings.
  See PLAN.md §9.2.
- **Idempotency key is `(RequestId, resolved_date)`**, NOT just `RequestId`.
  This lets `target_offsets = [7]` produce one stable RequestId across the
  daily cron while still booking a fresh date each day. See PLAN.md §13.1.
- **Player PII redacted before write** to `attempt_log` (SHA-256 prefix).
  See PLAN.md §10.1. The store is a workflow artifact — assume contents
  are visible to anyone with repo read access.

## Mangrove Bay specifics

- Booking URL: `https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes`
- `course_pk = 19671`, `booking_class_id = 2149`. `schedule_id` TBD by Spike S1.
- 7-day window opens 06:00 America/New_York exactly.

## When in doubt

- Implementing a new milestone task? Read PLAN.md §16 for inputs/outputs/deps.
- Adding a new course? Drop a sibling next to `mangrove_bay.py` if ForeUP-backed,
  or stand up `chronogolf/base.py` if Chronogolf-backed (Spike S2 first).
- Touching the orchestrator? Make sure FakeAdapter + FakeClock + InMemoryStore
  tests still cover your change. Fixtures live in `tests/conftest.py`. The
  race-window test is the canary.
- Modifying anti-bot etiquette? Re-read PLAN.md §12 first. ToS posture is not
  ours to negotiate around.
