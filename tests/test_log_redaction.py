"""Tests for the redacting LOG FILTER — defence-in-depth on stdout/stderr.

`redact_text` is only reachable at explicit call sites in adapter code, so any log record
emitted by a THIRD-PARTY logger bypasses it entirely. Observed live (prod, 2026-08-01): httpx
logs every request at INFO, including the 2captcha result-poll URL, so the API key landed in
Log Analytics in plaintext ~120x per booking run:

    HTTP Request: GET https://2captcha.com/res.php?key=<API_KEY>&action=get&id=... "HTTP/1.1 200 OK"

full-repo-scan 2026-07-09 security H1 fixed the ERROR path (a sanitized RuntimeError); this
covers the far noisier INFO path. The filter is installed on the root logger's HANDLERS — a
filter on a *logger* does not see records propagating up from child loggers like `httpx`,
so a handler filter is the only placement that catches everything.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from teetime.__main__ import cli
from teetime.core.redaction import RedactingLogFilter, install_log_redaction

# The exact shape httpx emits (lazy %-args, not a pre-formatted string) — the filter has to
# resolve args, not just look at record.msg.
_HTTPX_FMT = 'HTTP Request: %s %s "%s"'
_POLL_URL = "https://2captcha.com/res.php?key=deadbeefdeadbeefdeadbeefdeadbeef&action=get&id=8342"
# Synthetic stand-in, deliberately low-entropy: the REAL key must never enter the repo
# (gitleaks correctly rejects it, and this suite is exactly about not leaking it).
_KEY = "deadbeefdeadbeefdeadbeefdeadbeef"


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_filter_scrubs_api_key_from_lazy_args() -> None:
    rec = _record(_HTTPX_FMT, "GET", _POLL_URL, "HTTP/1.1 200 OK")
    assert RedactingLogFilter().filter(rec) is True  # never drops the record
    out = rec.getMessage()
    assert _KEY not in out, f"2captcha API key survived the filter: {out}"
    assert "key=<redacted-key>" in out
    # Non-secret context must survive for debugging.
    assert "action=get" in out
    assert "HTTP/1.1 200 OK" in out


def test_filter_scrubs_preformatted_message() -> None:
    rec = _record(f"HTTP Request: GET {_POLL_URL}")
    RedactingLogFilter().filter(rec)
    assert _KEY not in rec.getMessage()


def test_filter_is_idempotent_across_multiple_handlers() -> None:
    # A record reaching two handlers passes the filter twice; the second pass must not corrupt it.
    rec = _record(_HTTPX_FMT, "GET", _POLL_URL, "HTTP/1.1 200 OK")
    f = RedactingLogFilter()
    f.filter(rec)
    first = rec.getMessage()
    f.filter(rec)
    assert rec.getMessage() == first


def test_filter_leaves_clean_records_untouched() -> None:
    rec = _record("ForeUP: got %d raw slot(s) for %s, filtering...", 27, "2026-08-08")
    RedactingLogFilter().filter(rec)
    assert rec.getMessage() == "ForeUP: got 27 raw slot(s) for 2026-08-08, filtering..."


def test_filter_survives_malformed_args_and_still_scrubs_them() -> None:
    """A %-arity mismatch must neither raise nor leak.

    An exception from `filter()` propagates to the `log.…()` call site (logging only guards
    `emit()` with `handleError`), which at T0 would take down the booking run. And on a
    formatting failure `Handler.handleError` dumps "Message: %r / Arguments: %s" to stderr —
    so leaving `args` intact would print the very secret we are scrubbing.
    """
    rec = _record(f"two placeholders %s %s -- {_POLL_URL}", "only-one")
    assert RedactingLogFilter().filter(rec) is True
    assert rec.args == (), "args must be dropped so handleError cannot dump them raw"
    assert _KEY not in str(rec.msg), f"key survived the malformed-args path: {rec.msg}"


def test_filter_scrubs_exception_tracebacks() -> None:
    """`exc_info` is a real leak path, not a theoretical one.

    `__main__._run` logs `exc_error(..., exc_info=True)` on a failed run, and an httpx error
    embeds the full request URL. The filter only rewrote `record.msg`; `Formatter.format`
    appends `formatException(record.exc_info)` afterwards, which would be untouched.
    """
    try:
        raise RuntimeError(f"poll failed for {_POLL_URL}")
    except RuntimeError:
        rec = logging.LogRecord(
            name="teetime",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="run failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    RedactingLogFilter().filter(rec)
    rendered = logging.Formatter("%(message)s").format(rec)
    assert _KEY not in rendered, f"API key survived in the traceback: {rendered}"
    assert "RuntimeError" in rendered, "the traceback itself must still be there"


def test_filter_scrubs_stack_info() -> None:
    rec = _record("boom")
    rec.stack_info = f"Stack (most recent call last):\n  poll {_POLL_URL}"
    RedactingLogFilter().filter(rec)
    assert rec.stack_info is not None
    assert _KEY not in rec.stack_info


def test_install_wires_the_filter_onto_root_handlers() -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        install_log_redaction()
        assert any(isinstance(f, RedactingLogFilter) for f in handler.filters)
        # Idempotent: a second install must not stack duplicate filters.
        install_log_redaction()
        assert sum(isinstance(f, RedactingLogFilter) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)


def test_installed_filter_scrubs_an_httpx_record_end_to_end() -> None:
    """The actual prod regression: httpx -> root handler -> stderr, key must be gone."""
    root = logging.getLogger()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.INFO)
    try:
        install_log_redaction()
        logging.getLogger("httpx").info(_HTTPX_FMT, "GET", _POLL_URL, "HTTP/1.1 200 OK")
        emitted = buf.getvalue()
        assert _KEY not in emitted, f"API key reached the log stream: {emitted}"
        assert "key=<redacted-key>" in emitted
    finally:
        root.setLevel(prev_level)
        root.removeHandler(handler)


def test_install_before_basicconfig_is_inert() -> None:
    """WHY the ordering guard below checks order and not just presence.

    `basicConfig` is what CREATES the root handler. Installing first attaches the filter to
    nothing, and the leak stays wide open — while a naive presence check still passes.
    """
    root = logging.getLogger()
    buf = io.StringIO()
    saved = root.handlers[:]
    root.handlers = []
    prev_level = root.level
    try:
        install_log_redaction()  # no handlers yet -> attaches to nothing
        logging.basicConfig(level=logging.INFO, stream=buf, format="%(message)s")
        root.setLevel(logging.INFO)
        logging.getLogger("httpx").info(_HTTPX_FMT, "GET", _POLL_URL, "HTTP/1.1 200 OK")
        assert _KEY in buf.getvalue(), (
            "expected the wrong-order wiring to leak — if this stops leaking, the ordering "
            "guard below is testing nothing"
        )
    finally:
        root.setLevel(prev_level)
        root.handlers = saved


def test_every_logging_entrypoint_installs_the_filter_after_basicconfig() -> None:
    """Wiring guard: each `basicConfig` must be FOLLOWED by an `install_log_redaction`.

    Presence alone is not enough — see the test above: install-then-basicConfig counts the
    same but is completely inert. So this pairs them positionally: walking the source, every
    `logging.basicConfig(` must be followed by an `install_log_redaction()` before the next
    `basicConfig(`. Complemented by the functional test below, which proves the wiring works
    end-to-end through a real CLI entrypoint rather than by reading source.
    """
    # Globbed, not hard-coded to __main__.py: a future entrypoint that configures logging
    # from another module would otherwise escape this guard entirely.
    pkg = Path(__file__).resolve().parents[1] / "src" / "teetime"
    total_configs = 0
    for src in sorted(pkg.rglob("*.py")):
        text = src.read_text()
        if "logging.basicConfig(" not in text:
            continue
        events = sorted(
            [(m.start(), "config") for m in re.finditer(r"logging\.basicConfig\(", text)]
            + [(m.start(), "install") for m in re.finditer(r"install_log_redaction\(\)", text)]
        )
        kinds = [kind for _, kind in events]
        total_configs += kinds.count("config")
        assert kinds == ["config", "install"] * kinds.count("config"), (
            f"in {src.relative_to(pkg)}: every logging.basicConfig() must be immediately "
            f"followed by install_log_redaction() (installing first attaches to nothing); "
            f"saw {kinds}"
        )
    assert total_configs >= 3, "expected run / watch-disabled / watch to configure logging"


def test_watch_entrypoint_installs_the_filter_on_a_real_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Functional proof through a real CLI entrypoint, not a source-substring count.

    Uses the watcher-disabled path: it runs `basicConfig` + `install_log_redaction` and exits
    0 without touching the network, so it exercises the real wiring cheaply.
    """
    for k, v in {"MB_USERNAME": "u", "MB_PASSWORD": "p", "PLAYER1_EMAIL": "a@e.test"}.items():
        monkeypatch.setenv(k, v)
    cfg = tmp_path / "w.toml"
    cfg.write_text(
        """
[[courses]]
id = "foreup:mangrove_bay"
adapter = "foreup.mangrove_bay"
username_env = "MB_USERNAME"
password_env = "MB_PASSWORD"

[request]
target_offsets = [7]
holes = 18
course_preferences = ["foreup:mangrove_bay"]

[[request.players]]
first_name = "A"
last_name = "B"
email_env = "PLAYER1_EMAIL"

[[request.time_windows]]
weekday = "saturday"
earliest = "08:45:00"
latest = "10:00:00"

[scheduler]
timezone = "America/New_York"
fire_time = "06:00:00"

[notifier]
backend = "console"

[watcher]
enabled = false
poll_interval_s = 600
"""
    )
    result = CliRunner().invoke(cli, ["watch", "--config", str(cfg), "--dry-run", "true"])
    assert result.exit_code == 0, result.output
    handlers = logging.getLogger().handlers
    assert handlers, "entrypoint should have configured a root handler"
    assert any(any(isinstance(f, RedactingLogFilter) for f in h.filters) for h in handlers), (
        "the watch entrypoint ran but no root handler carries a RedactingLogFilter"
    )


def test_caplog_is_not_polluted_by_the_installs_above() -> None:
    """Regression guard for the autouse fixture in conftest.py.

    `install_log_redaction()` attaches to EVERY root handler — including pytest's
    session-scoped `LogCaptureHandler`. Without the fixture that restores handler filters,
    the tests above would silently redact `caplog` for the remainder of the session, and a
    future `assert secret not in caplog.text` test would pass VACUOUSLY. This test runs after
    them in file order and asserts the capture handler came back clean.
    """
    root = logging.getLogger()
    leaked = [h for h in root.handlers if any(isinstance(f, RedactingLogFilter) for f in h.filters)]
    assert not leaked, f"RedactingLogFilter leaked onto session handler(s): {leaked}"


@pytest.fixture(scope="module")
def orphan_cleanup() -> Iterator[list[logging.Handler]]:
    """Registry whose teardown removes handlers a test deliberately abandoned.

    It must NOT add the handler itself: higher-scoped fixtures are set up BEFORE the
    function-scoped autouse fixture, so a handler attached here would be captured in that
    fixture's snapshot and take the restore path, never the orphan-strip branch. The test body
    attaches it (after the snapshot); this only cleans up at MODULE teardown, i.e. after the
    autouse fixture has had its chance to see the orphan.

    Function-scoped `request.addfinalizer` cannot substitute: teardown finalizers are LIFO, so
    one registered during the CALL phase runs BEFORE the autouse fixture's (registered during
    SETUP), removing the handler before the branch can see it.
    """
    handlers: list[logging.Handler] = []
    yield handlers
    root = logging.getLogger()
    for handler in handlers:
        root.removeHandler(handler)


def test_orphan_handler_receives_the_filter(orphan_cleanup: list[logging.Handler]) -> None:
    """Setup half of the orphan-branch check; the assertion is in the test below."""
    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)  # AFTER the autouse snapshot -> a genuine orphan
    orphan_cleanup.append(handler)
    install_log_redaction()
    assert any(isinstance(f, RedactingLogFilter) for f in handler.filters)


def test_conftest_strips_the_filter_from_an_orphan_handler(
    orphan_cleanup: list[logging.Handler],
) -> None:
    """Covers the conftest fixture's orphan-strip branch — previously UNCOVERED.

    The test above left a `RedactingLogFilter` on a root handler the autouse fixture never
    snapshotted. If that branch regressed (e.g. back to a string class-name compare that stops
    matching after a rename), the filter would still be attached here and the leak would be
    reopened for every later test. Relies on file collection order, like the caplog guard
    above; shuffling collection would make it pass vacuously.
    """
    assert orphan_cleanup, "the previous test should have registered an orphan handler"
    for handler in orphan_cleanup:
        assert not any(isinstance(f, RedactingLogFilter) for f in handler.filters), (
            "conftest did not strip the redaction filter from an orphaned root handler"
        )
