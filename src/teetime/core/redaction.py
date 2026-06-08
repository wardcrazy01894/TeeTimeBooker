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
