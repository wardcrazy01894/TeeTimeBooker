"""E7 (MULTIUSER_PLAN §2.3 / §9.4): exact-literal secret masking.

`RedactingLogFilter` masks what its PATTERNS recognise (URL credential params, JWTs, Bearer
tokens, Luhn PANs, emails, phones). A keyring key or a decrypted ForeUP password has no
recognisable shape, so the hosted path registers each one as a LITERAL at startup / on
decrypt, and every rendered message, resolved `%`-arg, `exc_text` traceback and
`stack_info` is scrubbed of it. An empty registry is a no-op (today's behaviour).

The literals below are synthetic and deliberately low-entropy — real secrets never enter
the repo.
"""

from __future__ import annotations

import logging
import sys

import pytest

from teetime.core.redaction import (
    SECRET_LITERAL_MIN_LEN,
    RedactingLogFilter,
    redact_text,
    register_secret_literals,
)

_PASSWORD = "hunter2-correct-horse"
_KEYRING_KEY = "keyring-key-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_MARK = "<redacted-secret>"
_POLL_URL = "https://2captcha.com/res.php?key=deadbeefdeadbeefdeadbeefdeadbeef&action=get&id=8342"
_PAN = "4111 1111 1111 1111"  # the canonical Luhn-valid test PAN


def _record(
    msg: str, *args: object, exc: bool = False, stack: str | None = None
) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise RuntimeError(f"login failed for password={_PASSWORD}")
        except RuntimeError:
            exc_info = sys.exc_info()
    rec = logging.LogRecord(
        name="teetime.tenant",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    rec.stack_info = stack
    return rec


def test_registered_literal_masked_in_args_and_traceback() -> None:
    register_secret_literals([_PASSWORD, _KEYRING_KEY])
    rec = _record(
        "decrypted %s with key %s",
        _PASSWORD,
        _KEYRING_KEY,
        exc=True,
        stack=f"Stack (most recent call last):\n  secret={_PASSWORD}",
    )

    assert RedactingLogFilter().filter(rec) is True
    out = rec.getMessage()
    assert _PASSWORD not in out
    assert _KEYRING_KEY not in out
    assert out == f"decrypted {_MARK} with key {_MARK}"
    assert rec.exc_text is not None
    assert _PASSWORD not in rec.exc_text
    assert _MARK in rec.exc_text
    assert rec.stack_info is not None
    assert _PASSWORD not in rec.stack_info
    assert _MARK in rec.stack_info


def test_unregistered_secret_is_not_masked() -> None:
    """Non-vacuity: masking is by REGISTRATION, not by accident of the patterns."""
    register_secret_literals([_PASSWORD])
    other = "some-other-password-value"
    rec = _record("pw %s / %s", _PASSWORD, other)
    RedactingLogFilter().filter(rec)
    assert rec.getMessage() == f"pw {_MARK} / {other}"


def test_redact_text_masks_registered_literal() -> None:
    """Call-site `redact_text` (exception messages, error bodies) gets the same coverage."""
    register_secret_literals([_PASSWORD])
    assert redact_text(f"body echoed {_PASSWORD}!") == f"body echoed {_MARK}!"


def test_empty_registry_is_a_noop() -> None:
    text = f"GET {_POLL_URL} card {_PAN} hunter2-correct-horse"
    register_secret_literals([])
    assert redact_text(text) == (
        "GET https://2captcha.com/res.php?key=<redacted-key>&action=get&id=8342 "
        "card <redacted-pan> hunter2-correct-horse"
    )


def test_existing_pattern_masking_unchanged_with_literals_registered() -> None:
    register_secret_literals([_PASSWORD, _KEYRING_KEY])
    rec = _record('HTTP Request: GET %s "%s"', _POLL_URL, f"card {_PAN}")
    RedactingLogFilter().filter(rec)
    assert rec.getMessage() == (
        "HTTP Request: GET https://2captcha.com/res.php?key=<redacted-key>&action=get&id=8342 "
        '"card <redacted-pan>"'
    )


@pytest.mark.parametrize("short", ["", "a", "e", "abc", "1234567"])
def test_short_literals_are_ignored(short: str) -> None:
    """A literal below the floor would shred ordinary text (masking "e" destroys every
    line), so it is ignored rather than registered."""
    assert len(short) < SECRET_LITERAL_MIN_LEN
    register_secret_literals([short])
    text = "the quick 1234567 abc fox"
    assert redact_text(text) == text


def test_min_length_floor_is_eight() -> None:
    """§9.4: minimum length 8. An 8-char literal IS masked."""
    assert SECRET_LITERAL_MIN_LEN == 8
    register_secret_literals(["abcdefgh"])
    assert redact_text("x abcdefgh y") == f"x {_MARK} y"


def test_longest_literal_wins_on_overlap() -> None:
    """A registered secret that is a PREFIX of another must not leave the longer one's
    tail visible."""
    register_secret_literals(["secretvalue1", "secretvalue1-extended-part"])
    assert redact_text("a secretvalue1-extended-part b secretvalue1 c") == (
        f"a {_MARK} b {_MARK} c"
    )


def test_registration_is_idempotent_and_additive() -> None:
    register_secret_literals([_PASSWORD])
    register_secret_literals([_PASSWORD])
    register_secret_literals([_KEYRING_KEY])
    assert redact_text(f"{_PASSWORD} {_KEYRING_KEY}") == f"{_MARK} {_MARK}"


def test_filter_idempotent_across_handlers_with_literals() -> None:
    register_secret_literals([_PASSWORD])
    rec = _record("pw %s", _PASSWORD)
    f = RedactingLogFilter()
    f.filter(rec)
    first = rec.getMessage()
    f.filter(rec)
    assert rec.getMessage() == first == f"pw {_MARK}"


def test_literal_inside_a_marker_is_ignored() -> None:
    """A literal that occurs inside a replacement marker would re-match the marker on every
    pass (non-idempotent, growing output across handler fan-out), so it is refused."""
    register_secret_literals(["redacted-secret"])
    once = redact_text("x redacted-secret y")
    assert redact_text(once) == once


def test_regex_metacharacters_are_literal() -> None:
    weird = "p@ss.w*rd+(x)[y]$"
    register_secret_literals([weird])
    assert redact_text(f"a {weird} b pXss.wwrd") == f"a {_MARK} b pXss.wwrd"


def test_filter_never_raises_with_literals_and_bad_arity() -> None:
    register_secret_literals([_PASSWORD])
    rec = _record(f"%s %s {_PASSWORD}", "only-one")
    assert RedactingLogFilter().filter(rec) is True
    assert _PASSWORD not in str(rec.msg)
