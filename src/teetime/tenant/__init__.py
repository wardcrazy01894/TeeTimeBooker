"""Multi-user tenant layer (MULTIUSER_PLAN.md). STUBS ONLY: nothing here is wired yet.

Replaces the single-user shell (TOML config, CLI env-var creds, per-run ``InMemoryStore`` and
``ConsoleNotifier``) with durable users, course accounts, standing rules and dated request rows,
while reusing the engine (adapters, the three orchestrators, ranking, cutoff, redaction)
unchanged. Layering: ``tenant`` imports ``core`` + ``courses``; nothing in ``core`` or
``courses`` imports ``tenant`` (§2.2).
"""
