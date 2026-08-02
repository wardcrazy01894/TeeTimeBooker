"""Shared pytest fixtures.

Currently one job: keep `install_log_redaction()` from leaking across tests.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from teetime.core.redaction import RedactingLogFilter


@pytest.fixture(autouse=True)
def _restore_root_log_filters() -> Iterator[None]:
    """Snapshot/restore the filter list of every root-logger handler around each test.

    `install_log_redaction()` attaches a `RedactingLogFilter` to EVERY handler on the root
    logger. Under pytest one of those is `_pytest.logging.LogCaptureHandler`, which is built
    ONCE per session and reused for every test item — so without this fixture the first test
    that installs the filter silently redacts `caplog` for the whole rest of the run.

    That leak is not confined to the redaction tests: `tests/test_cli.py` drives `_run`/
    `_watch`, which call `install_log_redaction()` as production code, and it sorts near the
    front of the suite. The failure mode is nasty and order-dependent — downstream tests see
    `record.args == ()` and redacted `caplog.text`, and, worst of all, a future security test
    shaped `assert secret not in caplog.text` would pass VACUOUSLY.

    Autouse and global (not scoped to the redaction tests) because any test that exercises a
    CLI entrypoint installs the filter, whether or not it means to.

    LIMIT: being function-scoped, this cannot contain an install performed in a MODULE- or
    SESSION-scoped fixture's setup/teardown — that runs outside the window. No such fixture
    exists today (the non-function-scoped ones in this suite only read bicep text), but a
    future one calling a CLI entrypoint would need its own guard.
    """
    root = logging.getLogger()
    saved = [(h, list(h.filters)) for h in root.handlers]
    try:
        yield
    finally:
        for handler, filters in saved:
            handler.filters = filters
        # Handlers ADDED during the test (and not removed) never had a snapshot; strip any
        # redaction filter they picked up so the leak can't ride out on them either.
        # `isinstance`, matching install_log_redaction — a string class-name compare would
        # silently stop matching after a rename and quietly reopen the leak.
        for handler in root.handlers:
            if not any(handler is h for h, _ in saved):
                handler.filters = [
                    f for f in handler.filters if not isinstance(f, RedactingLogFilter)
                ]
