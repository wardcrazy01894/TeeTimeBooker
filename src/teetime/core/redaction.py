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
    # player PII (§10.1)
    "email",
    "mail",
    "phone",
    "mobile",
    "member",
    "first_name",
    "last_name",
)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    # The whole GNSVC payment block is sensitive (card number, CVV, expiry, billing address,
    # cardholder name, phone). Redacting the namespace is intentional over-redaction.
    if k.startswith("payment"):
        return True
    return any(tok in k for tok in _SENSITIVE_KEY_TOKENS)


def _redact_value(v: object) -> object:
    """Recursively redact a value: dict → redact_payload, list/tuple → element-wise
    (including nested lists), scalar → returned as-is. Returns new containers (no aliasing)."""
    if isinstance(v, Mapping):
        return redact_payload(v)
    if isinstance(v, (list, tuple)):
        return [_redact_value(i) for i in v]
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
