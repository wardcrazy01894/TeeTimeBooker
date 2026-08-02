"""PCI/PII redaction for attempt_log payloads.

`redact_payload` returns a deep copy of a payload with card fields (PAN/CVV/expiry/
billing) and player PII (email/phone/member/name) replaced by ``"***"``. It is applied
at the STORE boundary (`BookingStore.append_attempt`) so no caller can leak card data by
forgetting to redact — the TeeItUp booking payload POSTs raw PAN/CVV to tr.gnsvc.com under
the `Payment.*`/`Payments_*` namespace, and CourseCredentials.extra carries the cred-style
card keys. See PLAN.md §10.1.

This lives in `core/` (the shared kernel) so both the orchestrator and the persistence
layer can import it without depending on each other.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

# Card + player-PII keys whose VALUES must never reach the attempt_log (PLAN.md §10.1).
# Matched case-insensitively. Two card shapes: the TeeItUp GNSVC POST namespaces all card
# fields under "Payment"/"Payments_" (Payment.CC.CreditCardNumber, Payment.CC.CVVCode,
# Payment.Address.*, …), and CourseCredentials.extra carries the cred-style keys (card_number,
# cvv, expiry_*, billing_*, name_on_card, password). §10.1 ALSO requires player PII (email,
# phone, member number, name) be redacted. We DROP to "***" (stronger than §10.1's SHA-256
# prefix — a hash of a low-entropy phone number is reversible; for an audit blob, drop is safer).
# Tokens avoid dangerous substrings — NOT "cc" (hits "success"), "pan" (hits "company"), or bare
# "name" (would clobber course_name/job_name audit fields; player names use first_/last_name).
_SENSITIVE_KEY_TOKENS = (
    "card",
    "cvv",
    "expir",  # expiry_month/year, ExpirationMonth/Year
    "billing",
    "password",
    "securitycode",
    "name_on_card",
    "token",  # tr_token: short-lived GNSVC bearer payment credential (replayable in-window)
    # player PII (§10.1)
    "email",
    "mail",
    "phone",
    "mobile",
    "member",
    # name fields appear snake_case (CourseCredentials.extra: first_name) AND camelCase
    # (the GNSVC POST: bookerFirstName/bookerLastName, firstName/lastName). The no-underscore
    # tokens catch the camelCase forms once the key is lowercased; keep the underscore tokens
    # for the snake_case ones. Still avoids bare "name" (course_name/job_name are audit fields).
    "first_name",
    "last_name",
    "firstname",
    "lastname",
)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    # The whole GNSVC payment block is sensitive (card number, CVV, expiry, billing address,
    # cardholder name, phone). Redacting the namespace is intentional over-redaction.
    if k.startswith("payment"):
        return True
    return any(tok in k for tok in _SENSITIVE_KEY_TOKENS)


# Card numbers (PANs) are 13-19 digits; the value-level guard masks any scalar in this
# length range that also passes the Luhn checksum (digits + separators only).
# KNOWN TRADE-OFF: ~10% of random 13-digit numbers pass Luhn, and a 13-digit epoch-
# MILLISECOND timestamp sits in this window — so a millis int could be over-redacted to
# "***". This is the intended masking-over-leaking default for an audit blob. To avoid it
# in real payloads, store timestamps as ISO-8601 strings (the '-'/':'/'T' fail the
# whole-string guard) or epoch SECONDS (10 digits, below the floor) rather than millis ints.
_PAN_MIN_DIGITS = 13
_PAN_MAX_DIGITS = 19
_LUHN_DOUBLED_MAX = 9  # a doubled digit > 9 has 9 subtracted (equivalent to summing its digits)


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn mod-10 checksum. Real card numbers always pass; random long IDs
    almost never do, so it sharply limits false positives in the value-level PAN guard."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > _LUHN_DOUBLED_MAX:
                d -= _LUHN_DOUBLED_MAX
        total += d
    return total % 10 == 0


def _looks_like_pan(v: object) -> bool:
    """True if a SCALAR value is card-number-shaped: 13-19 digits, Luhn-valid, and made of
    digits + separators (spaces/dashes) only. Value-level backstop for the key-based
    allowlist — a PAN arriving under an unrecognised key (e.g. a renamed GNSVC field) is
    still masked. Whole-string-only (not a substring scan), so it never redacts prose; the
    Luhn gate keeps ordinary long IDs / timestamps from being over-redacted. bool is an int
    subclass and is excluded."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        s = str(abs(v))
    elif isinstance(v, str):
        s = v
    else:
        return False
    if any(not (ch.isdigit() or ch in " -") for ch in s):
        return False
    digits = "".join(ch for ch in s if ch.isdigit())
    return _PAN_MIN_DIGITS <= len(digits) <= _PAN_MAX_DIGITS and _luhn_ok(digits)


def _redact_value(v: object) -> object:
    """Recursively redact a value: dict → redact_payload, list/tuple → element-wise
    (including nested lists), a card-number-shaped scalar → "***", any other scalar →
    returned as-is. Returns new containers (no aliasing)."""
    if isinstance(v, Mapping):
        return redact_payload(v)
    if isinstance(v, (list, tuple)):
        return [_redact_value(i) for i in v]
    if _looks_like_pan(v):
        return "***"
    return v


def redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a deep copy of ``payload`` with card + player-PII values replaced by ``"***"``.

    Applied by ``BookingStore.append_attempt`` to every payload before it is written to the
    attempt_log — the TeeItUp booking payload contains raw PAN/CVV/billing (`Payment.*`/
    `Payments_*`), CourseCredentials.extra carries the cred-style card keys, and §10.1 requires
    player PII (email/phone/member/name) be redacted too. Recurses into nested dicts AND lists
    (any depth); does not mutate the input. See PLAN.md §10.1.
    """
    out: dict[str, object] = {}
    for raw_k, v in payload.items():
        k = str(raw_k)
        out[k] = "***" if _is_sensitive_key(k) else _redact_value(v)
    return out


# Free-text PII scrubbing for log / exception strings — e.g. a raw ForeUP error-response
# body, which can echo the account holder's email / phone, and which ACA forwards to Log
# Analytics. Deliberately conservative: emails, and phone numbers that carry separators.
# A BARE digit run is NOT masked, so numeric confirmation ids / HTTP status codes survive
# for debugging (the whole reason the error body is logged).
# Segment lengths are BOUNDED. Unbounded `+` quantifiers backtrack quadratically on a long
# unbroken word-char run — measured ~0.8 s at 20k chars and ~58 s at 200k. That never mattered
# while every caller clamped its input (`r.text[:300]` and friends), but `RedactingLogFilter`
# now feeds this arbitrary log records that nobody vetted for length. Bounds: 64 = the RFC 5321
# local-part maximum; 255 is deliberately over-generous for the domain segments (a DNS LABEL is
# <=63 octets per RFC 1035, 255 is the whole-name limit) — being loose here costs nothing and
# avoids clipping unusual-but-real hostnames. Scope, stated honestly: only RFC-INVALID shapes
# change, and a >64-char local part degrades to a PARTIAL match (a prefix stays visible) rather
# than no match. `tests/test_redact_payload.py` pins both directions with EXACT-output asserts
# (a substring check would pass on a partial match, leaking a 40+ char prefix) plus a ReDoS
# ceiling.
_EMAIL_RE = re.compile(r"[\w.+-]{1,64}@[\w-]{1,255}\.[\w.-]{1,255}")
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
# A reflected session credential is the one thing in an error body more dangerous than PII.
# Both patterns are HIGH-CONFIDENCE so they cannot swallow numeric ids / status codes:
#   - JWT: three base64url segments, the first starting with "eyJ" (base64 of '{"') — this
#     shape never collides with a bare id or a normal word.
#   - Bearer: the literal "Bearer " prefix (CASE-SENSITIVE — the real HTTP auth-scheme casing,
#     so the English word "bearer" in prose is NOT a match) followed by a >=8-char token run
#     (real tokens are long; the length floor stops it eating a short following word like "of").
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
# A raw PAN can appear in a free-text upstream message (e.g. a GNSVC payment-decline
# `Message`), which `redact_payload`'s value-level Luhn guard does NOT cover (it only sees
# STRUCTURED payloads). Candidate = a 13-19-digit run, optionally grouped by single spaces or
# dashes, not embedded in a longer pure-digit run. We mask only candidates that ALSO pass Luhn
# (see `_looks_like_pan`/`_luhn_ok`), so bare numeric ids / statuses and non-card long numbers
# survive for debugging — same masking-over-leaking trade-off as the value guard (a 13-digit
# epoch-millis int could be over-masked; log timestamps as ISO-8601 to avoid it).
_PAN_TEXT_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
# A credential passed as a URL QUERY PARAM (e.g. 2captcha's `res.php?key=<API_KEY>&…`)
# can be reflected into a logged exception message — httpx.HTTPStatusError embeds the
# full request URL. NAME-gated to credential-shaped param names only (the `\b` keeps
# `googlekey=`/`sitekey=` and other *key-suffixed params out), with an 8-char value
# floor so trivial non-secret values survive. full-repo-scan 2026-07-09 security H1
# belt-and-suspenders (the primary fix is not raising URL-bearing errors at all).
_URL_CRED_PARAM_RE = re.compile(
    r"(?i)\b(key|api_?key|access_token|auth_token|token|secret|password|client_secret)"
    r"=([^&\s'\"<>]{8,})"
)


def _mask_pan_match(m: re.Match[str]) -> str:
    digits = "".join(ch for ch in m.group(0) if ch.isdigit())
    return "<redacted-pan>" if _luhn_ok(digits) else m.group(0)


def redact_text(text: str) -> str:
    """Scrub account-holder PII (email, separator-formatted phone), reflected session
    credentials (JWTs, ``Bearer`` tokens, credential-named URL query params), AND
    Luhn-valid PANs from free text.

    For logging arbitrary upstream response bodies, which may echo account-holder PII OR
    request context — including auth tokens or (on a payment decline) a raw card number —
    back on a 4xx (the ForeUP/TeeItUp error paths log the body to diagnose a rejected
    booking). The token + PAN patterns are deliberately high-confidence (the PAN scrub is
    Luhn-gated and length-bounded) so a bare numeric confirmation id / HTTP status survives
    for debugging. NOT a substitute for ``redact_payload`` on structured attempt_log writes —
    this is a free-text log helper.
    """
    text = _BEARER_RE.sub("Bearer <redacted-token>", text)
    text = _JWT_RE.sub("<redacted-token>", text)
    text = _URL_CRED_PARAM_RE.sub(r"\1=<redacted-key>", text)
    text = _PAN_TEXT_RE.sub(_mask_pan_match, text)
    return _PHONE_RE.sub("<redacted-phone>", _EMAIL_RE.sub("<redacted-email>", text))


class RedactingLogFilter(logging.Filter):
    """Route a log record's rendered message, traceback, and stack info through `redact_text`.

    `redact_text` only runs where a call site remembers to call it, which covers our own
    adapter code and nothing else. Third-party loggers bypass it: httpx logs each request at
    INFO, so the 2captcha result-poll URL — `res.php?key=<API_KEY>&…` — reached prod stdout
    (and Log Analytics) in plaintext roughly 120x per booking run.

    Placement matters: `logging.Filter`s attached to a LOGGER only see records created by
    that logger, NOT records propagating up from children (`httpx`, `httpcore`, …). Only a
    filter on the root logger's HANDLERS sees every record that is actually emitted — hence
    `install_log_redaction` wires handlers, not loggers.

    Covered: `record.msg` + `%`-args (resolved eagerly via `getMessage()`, because the secret
    usually lives in an ARG — httpx passes the URL as `%s` — not in the format string),
    `exc_info` tracebacks (pre-rendered into `record.exc_text`, which `Formatter.format`
    reuses instead of recomputing), and `stack_info`.

    NOT covered — a traceback printed by Python's default excepthook rather than by logging.
    `__main__._run` does `log.error(..., exc_info=True)` and then re-raises; click propagates,
    and the interpreter prints the same traceback to stderr a SECOND time without passing
    through logging at all. This filter cannot see that path. Keeping credentials out of
    exception messages in the first place (as #167 did for the 2captcha poll error) remains
    the primary defence; this filter is depth, not a substitute.

    Never drops a record (always returns True) and never raises `Exception`: one thrown from
    `filter()` propagates to the `log.…()` call site — `logging` only guards `emit()` with
    `handleError` — and at T0 that would take down the booking run. (A `BaseException` from a
    pathological `msg.__str__` would still escape; that is not worth defending against.)

    CAVEAT: pre-populating `exc_text` OVERRIDES a custom formatter's `formatException` —
    `Formatter.format` reuses a non-empty `exc_text` instead of calling it. Inert today (the
    entrypoints use plain `basicConfig(format=…)`), but a future structured/JSON formatter
    would silently get the default traceback rendering instead of its own.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Idempotent: redact_text over already-redacted text is a no-op (every
            # replacement token contains '<'/'>', which the patterns' value classes
            # exclude), so a record fanned out to several filtered handlers survives
            # repeated passes unchanged.
            record.msg = redact_text(record.getMessage())
            record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = _FALLBACK_FORMATTER.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_text(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_text(record.stack_info)
        except Exception:
            # Most likely a %-arity mismatch in getMessage(). Formatting will fail again in
            # the handler, and `Handler.handleError` dumps "Message: %r / Arguments: %s" to
            # stderr — which would print the raw args we were trying to scrub. So scrub what
            # we safely can (the un-formatted msg) and drop the args entirely.
            try:
                record.msg = redact_text(str(record.msg))
                record.args = ()
            except Exception:
                pass
        return True


# Module-level so an exception record doesn't allocate a Formatter per emit.
_FALLBACK_FORMATTER = logging.Formatter()


def install_log_redaction() -> None:
    """Attach a `RedactingLogFilter` to every root-logger handler, idempotently.

    Call once per entrypoint AFTER `logging.basicConfig` — basicConfig is what CREATES the
    handler, so installing first attaches to nothing and silently leaves the leak open.
    `test_install_before_basicconfig_is_inert` pins that MECHANISM; the entrypoints' actual
    ordering is pinned by source position (`test_every_logging_entrypoint_installs_the_filter_
    after_basicconfig`), because under pytest the root logger already has handlers and the
    wrong order would still appear to work.

    Re-invocation is safe: a handler that already carries the filter is skipped, so the
    filter never stacks. Handlers added AFTER this call are not covered — every entrypoint
    configures logging once at startup, so that does not arise today.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingLogFilter) for f in handler.filters):
            handler.addFilter(RedactingLogFilter())
