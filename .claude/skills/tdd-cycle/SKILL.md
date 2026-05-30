---
name: tdd-cycle
description: Walk a single behavior change through this repo's mandatory red-green-refactor loop. Use when implementing a milestone task, filling in a NotImplementedError stub, or fixing a bug — anything that changes behavior. Enforces test-first.
argument-hint: <behavior or stub to implement, e.g. "SqliteStore.record_terminal" or "M3.T1">
allowed-tools: [Read, Edit, Write, Bash, Glob, Grep]
model: sonnet
---

# TDD cycle (red → green → refactor)

TDD is **mandatory** in this repo (CLAUDE.md "How we write code"). This skill
runs one behavior change through the loop. Do not skip a step; do not write
implementation before a failing test exists.

Target for this cycle: **$ARGUMENTS**

## 0. Orient (read first)

- Read the relevant section of `PLAN.md` (§16 for milestone inputs/outputs/deps;
  §20 for feature milestones) and any `NotImplementedError` stub you're filling.
- Identify the Protocol the change implements (`core/adapter.py`,
  `persistence/store.py`, `notifications/notifier.py`, `core/clock.py`) and the
  existing test that exercises it (e.g. `tests/test_adapter_stub.py` is the
  reference structural-contract pattern).
- Confirm the SUT and its collaborators. **Mock collaborators, never the SUT.**

## 1. RED — write the smallest failing test

- Add the minimal test that captures the desired behavior to the matching
  `tests/test_*.py`. For a Protocol impl, include both the structural check
  (`isinstance(impl, SomeProtocol)`) and one behavioral path.
- Run it and **confirm it fails for the right reason** (missing impl / wrong
  return), not an import typo or fixture error:

  ```bash
  uv run pytest -k <new_test_name> -x
  ```

  If it passes or errors for an unrelated reason, fix the test until it is red
  for the intended reason. Skipping red is how silent regressions ship.

## 2. GREEN — minimum implementation

- Write the least code that makes the test pass. No extra fields, no
  future-proofing, no untested branches.
- For race/time logic, take a `Clock` (use `FakeClock` in tests) — never
  `datetime.now`. For SQL, use `?` placeholders only.

  ```bash
  uv run pytest -k <new_test_name>
  ```

## 3. REFACTOR — clean up under green

- Improve names, dedupe, structure. Shared slot logic belongs in
  `core/slot_utils.py`; lock-ownership rules in the orchestrator docstrings.
- Re-run the full suite and the type/lint gate; all must stay green:

  ```bash
  uv run pytest -m "not integration"
  uv run mypy
  uv run ruff check . && uv run ruff format --check .
  ```

## 4. Docs + commit boundary

- Update any doc the change makes stale (README / CLAUDE.md / PLAN.md / AZURE_PLAN.md
  — see the documentation-standard table in CLAUDE.md). A new flag/env/milestone
  with no doc update is incomplete.
- One red→green→refactor unit is a good commit. Don't bundle ten cycles.
  Open the PR with the `/pr` skill (it runs the quality gate and checks git identity).

## Anti-patterns (reject these)

- Writing impl first, then tests that describe what it does (encodes bugs as features).
- Mocking the type under test.
- "Obviously passes" — always see it fail first.
- A test that passes only because it never calls the code path.
