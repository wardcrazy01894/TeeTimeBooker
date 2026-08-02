"""Tests for the redacting LOG FILTER — the last line of defence on stdout/stderr.

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
from pathlib import Path

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


def test_filter_survives_malformed_args_without_dropping_the_record() -> None:
    # A %-arity mismatch must not make the filter raise (a crash in a log filter at T0 would
    # take down the booking run); the record is passed through unchanged.
    rec = _record("two placeholders %s %s", "only-one")
    assert RedactingLogFilter().filter(rec) is True


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


def test_every_logging_entrypoint_installs_the_filter() -> None:
    """Wiring guard: each `basicConfig` call must be followed by `install_log_redaction`.

    The filter is worthless if an entrypoint forgets it, and the two booking/watch paths are
    exactly where the 2captcha polling happens. Pinned as source structure because the real
    entrypoints are `asyncio.run`-driven CLI coroutines that need live config + network.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "teetime" / "__main__.py"
    text = src.read_text()
    configs = text.count("logging.basicConfig(")
    installs = text.count("install_log_redaction()")
    assert configs >= 3, "expected the run/watch-disabled/watch entrypoints to configure logging"
    assert configs == installs, (
        f"{configs} logging.basicConfig() call(s) but {installs} install_log_redaction() call(s) "
        "in __main__.py — every entrypoint that configures logging must install the filter"
    )
