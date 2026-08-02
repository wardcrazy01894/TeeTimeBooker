"""Tests for core.redaction.redact_payload — the PCI/PII guard for attempt_log writes.

PLAN.md §10.1: card fields (PAN/CVV/expiry/billing) MUST be dropped before any attempt_log
write. The TeeItUp booking POSTs them to tr.gnsvc.com under the `Payment.*`/`Payments_*`
namespace; the cred-style keys (card_number, cvv, ...) also appear in CourseCredentials.extra.
`redact_payload` must redact both shapes, recursively, without mutating the input. The store
boundary (`InMemoryStore.append_attempt`) applies it — see test_in_memory_store.py for that wiring.
"""

from __future__ import annotations

import time

from teetime.core.redaction import (
    _BEARER_RE,
    _EMAIL_RE,
    _PAN_TEXT_RE,
    _PHONE_RE,
    redact_payload,
    redact_text,
)


def test_redact_text_masks_email_and_phone() -> None:
    """redact_text scrubs account-holder PII (email, separator-formatted phone) from a
    free-text upstream error body before it is logged. Full-repo-scan security finding."""
    body = 'ForeUP error: {"holder":"jane@example.com","phone":"727-555-0142"}'
    out = redact_text(body)
    assert "jane@example.com" not in out
    assert "727-555-0142" not in out
    assert "<redacted-email>" in out
    assert "<redacted-phone>" in out


def test_redact_text_keeps_bare_digit_runs() -> None:
    """A bare numeric run (e.g. a ForeUP confirmation id or HTTP status) is NOT masked —
    the error body is logged precisely to keep these debuggable."""
    body = "Slot gone (409): reservation 1234567890 already held"
    out = redact_text(body)
    assert "1234567890" in out
    assert "409" in out


def test_redact_text_passthrough_when_no_pii() -> None:
    assert redact_text("Slot unbookable (HTTP 400): teesheet full") == (
        "Slot unbookable (HTTP 400): teesheet full"
    )


def test_redact_text_masks_jwt() -> None:
    """A JWT echoed in an upstream error body (ForeUP reflects request context on some 4xx)
    must be masked — it is a session-bearing credential, not a debuggable id. Full-repo-scan
    security finding L7."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOjQyfQ.s3cr3t-SignatureValue_123"
    out = redact_text(f'login rejected: {{"token":"{jwt}"}}')
    assert jwt not in out
    assert "<redacted-token>" in out


def test_redact_text_masks_bearer_token() -> None:
    """An `Authorization: Bearer <token>` value echoed back must be masked."""
    out = redact_text("upstream said: Authorization: Bearer abc123.DEF456-ghi_789")
    assert "abc123.DEF456-ghi_789" not in out
    assert "Bearer <redacted-token>" in out


def test_redact_text_does_not_eat_prose_bearer() -> None:
    """The Bearer pattern is case-sensitive + length-floored so it does NOT over-redact the
    English word "bearer" in a narrative error body (PR #144 review): "the bearer of bad
    news" must survive intact — over-redaction degrades debuggability."""
    body = "the bearer of bad news: request 409 rejected"
    out = redact_text(body)
    assert out == body  # unchanged — no token masked
    assert "<redacted-token>" not in out


def test_redact_text_token_masking_keeps_status_and_ids() -> None:
    """Token masking must NOT over-redact: bare numeric ids / HTTP statuses / short words
    survive so the logged error body stays debuggable."""
    out = redact_text("Slot gone (409): reservation 1234567890 held; rate_type=walking")
    assert "409" in out
    assert "1234567890" in out
    assert "walking" in out
    assert "<redacted-token>" not in out


def test_redact_text_masks_bare_pan() -> None:
    """A raw PAN echoed in a free-text upstream message (e.g. a GNSVC payment-decline
    `Message`) must be masked — `redact_payload`'s Luhn value-guard only covers STRUCTURED
    writes, so the free-text log path needs its own scrub. Full-repo-scan security finding."""
    out = redact_text("card 4111111111111111 declined: insufficient funds")
    assert "4111111111111111" not in out
    assert "<redacted-pan>" in out
    assert "insufficient funds" in out  # surrounding prose survives


def test_redact_text_masks_pan_with_separators() -> None:
    """Group-separated PAN formats (spaces or dashes) are masked too."""
    assert "4242 4242 4242 4242" not in redact_text("paid with 4242 4242 4242 4242 ok")
    assert "4242-4242-4242-4242" not in redact_text("paid with 4242-4242-4242-4242 ok")


def test_redact_text_pan_scrub_keeps_non_luhn_and_short_ids() -> None:
    """The PAN scrub is Luhn-gated and length-bounded (13-19 digits), so it does NOT eat
    a bare confirmation id: a 10-digit id and a 16-digit NON-Luhn run both survive."""
    out = redact_text("reservation 1234567890 / ref 4111111111111112 still pending")
    assert "1234567890" in out  # 10 digits: below the 13-digit PAN floor
    assert "4111111111111112" in out  # 16 digits but Luhn-invalid → not a PAN
    assert "<redacted-pan>" not in out


def test_redact_text_masks_url_query_credential_params() -> None:
    """BELT-AND-SUSPENDERS (full-repo-scan 2026-07-09 H1): an httpx error message can
    embed a full request URL, and the 2captcha result poll carries the API key as a
    `key=` query param. If such a string ever reaches a free-text log (e.g. via an
    `exc_info` traceback message), the credential-shaped query values must be masked.
    Non-secret params survive for diagnosis."""
    out = redact_text(
        "Server error '500' for url "
        "'https://2captcha.com/res.php?key=aaaabbbbccccddddaaaabbbbccccdddd"
        "&action=get&id=789&json=1'"
    )
    assert "aaaabbbbccccddddaaaabbbbccccdddd" not in out
    assert "key=<redacted-key>" in out
    assert "action=get" in out  # non-secret params survive
    assert "id=789" in out
    assert "500" in out


def test_redact_text_query_scrub_keeps_non_credential_params() -> None:
    """The query scrub is name-gated: only credential-named params (key/api_key/token/
    secret/password …) are masked. Ordinary params — including ones whose NAME merely
    CONTAINS 'key' (googlekey, sitekey) — survive, so a logged search URL stays
    debuggable."""
    out = redact_text(
        "GET https://example.com/api?googlekey=6LeFakeKeyAAAAAAAAAAAAAA&date=2026-07-11"
        "&schedule_id=2149"
    )
    assert "googlekey=6LeFakeKeyAAAAAAAAAAAAAA" in out
    assert "date=2026-07-11" in out
    assert "schedule_id=2149" in out
    assert "<redacted-key>" not in out


def test_redacts_cred_style_card_keys() -> None:
    out = redact_payload(
        {
            "card_number": "4111111111111111",
            "cvv": "123",
            "expiry_month": "10",
            "expiry_year": "2030",
            "billing_address": "1 Main St",
            "billing_postal_code": "33701",
            "name_on_card": "Alex L",
            "password": "hunter2",
            "course_id": "teeitup:sydney_marovitz",  # NOT sensitive — preserved
        }
    )
    for k in (
        "card_number",
        "cvv",
        "expiry_month",
        "expiry_year",
        "billing_address",
        "billing_postal_code",
        "name_on_card",
        "password",
    ):
        assert out[k] == "***", k
    assert out["course_id"] == "teeitup:sydney_marovitz"


def test_redacts_gnsvc_payment_namespace() -> None:
    out = redact_payload(
        {
            "Payment.CC.CreditCardNumber": "4111111111111111",
            "Payment.CC.CVVCode": "123",
            "Payment.CC.ExpirationMonth": "10",
            "Payment.Address.Line1": "1 Main St",
            "Payment.Name": "Alex L",
            "Payments_1_CreditCard_NameOnCard": "Alex L",
            "PaymentReturnURL": "https://example.test/cb",  # under Payment* → redacted (safe over-redaction)
            "ReservationStatusID": 3,  # NOT sensitive — preserved
            "Success": True,
        }
    )
    for k in (
        "Payment.CC.CreditCardNumber",
        "Payment.CC.CVVCode",
        "Payment.CC.ExpirationMonth",
        "Payment.Address.Line1",
        "Payment.Name",
        "Payments_1_CreditCard_NameOnCard",
        "PaymentReturnURL",
    ):
        assert out[k] == "***", k
    assert out["ReservationStatusID"] == 3
    assert out["Success"] is True


def test_redacts_recursively_in_nested_dicts_and_lists() -> None:
    out = redact_payload(
        {
            "outer": "keep",
            "nested": {"card_number": "4111", "ok": "keep"},
            "items": [{"cvv": "999"}, {"plain": "keep"}],
        }
    )
    assert out["outer"] == "keep"
    assert out["nested"]["card_number"] == "***"
    assert out["nested"]["ok"] == "keep"
    assert out["items"][0]["cvv"] == "***"
    assert out["items"][1]["plain"] == "keep"


def test_does_not_mutate_input() -> None:
    payload = {"card_number": "4111", "ok": "keep"}
    out = redact_payload(payload)
    assert payload["card_number"] == "4111"  # original untouched
    assert out["card_number"] == "***"


def test_non_sensitive_substring_not_over_redacted() -> None:
    # Avoid dangerous substring tokens: "success" (contains "cc"), "company" (contains "pan"),
    # and bare "name" (course_name/job_name are useful audit fields, not PII).
    out = redact_payload(
        {"success": True, "company": "ACME", "account": "x", "course_name": "Mangrove Bay"}
    )
    assert out["success"] is True
    assert out["company"] == "ACME"
    assert out["account"] == "x"
    assert out["course_name"] == "Mangrove Bay"


def test_redacts_player_pii() -> None:
    # PLAN §10.1 requires player PII (email/phone/member/name) redaction, not just card data.
    out = redact_payload(
        {
            "CustomerEmail": "alex@example.test",
            "BookerEmail": "alex@example.test",
            "customerMobile": "555-0001",
            "Payment.PhoneNumber": "555-0002",
            "member_number": "12345",
            "first_name": "Alex",
            "last_name": "Lancaster",
            "course_id": "foreup:mangrove_bay",  # NOT PII — preserved
        }
    )
    for k in (
        "CustomerEmail",
        "BookerEmail",
        "customerMobile",
        "Payment.PhoneNumber",
        "member_number",
        "first_name",
        "last_name",
    ):
        assert out[k] == "***", k
    assert out["course_id"] == "foreup:mangrove_bay"


def test_redacts_gnsvc_camelcase_name_and_payment_token() -> None:
    # The real GNSVC booking POST uses camelCase name fields (bookerFirstName/bookerLastName,
    # and firstName/lastName in the profile body) NOT under the Payment* namespace, plus a
    # short-lived bearer payment token. None contain underscores, so the snake_case
    # first_name/last_name tokens miss them. See courses/teeitup/base.py:503,544,557.
    out = redact_payload(
        {
            "bookerFirstName": "Alex",
            "bookerLastName": "Lancaster",
            "firstName": "Alex",
            "lastName": "Lancaster",
            "Token": "tr-bearer-abc123",
            "ReservationId": 42,  # NOT sensitive — preserved
        }
    )
    for k in ("bookerFirstName", "bookerLastName", "firstName", "lastName", "Token"):
        assert out[k] == "***", k
    assert out["ReservationId"] == 42


def test_redacts_nested_lists_of_lists() -> None:
    # The recursive guard must descend into nested lists (attempt_log payload shapes are unknown).
    out = redact_payload({"deep": [[{"cvv": "9"}], [{"ok": "keep"}]]})
    assert out["deep"][0][0]["cvv"] == "***"
    assert out["deep"][1][0]["ok"] == "keep"


def test_pan_shaped_value_redacted_regardless_of_key() -> None:
    # Belt-and-suspenders (security finding, /full-repo-scan): the key-based allowlist
    # can't catch a PAN that arrives under an UNRECOGNISED key (e.g. a future GNSVC field
    # rename like {"acctRef": "<PAN>"}). A value that is card-number-shaped — 13-19 digits,
    # Luhn-valid, digits+separators only — is masked no matter the key. The Luhn gate keeps
    # ordinary long IDs (reservation ids, timestamps) from being over-redacted.
    out = redact_payload(
        {
            "acctRef": "4111111111111111",  # Luhn-valid 16-digit PAN under an innocuous key
            "spaced": "4111 1111 1111 1111",  # separators tolerated
            "dashed": "5555-5555-5555-4444",  # Luhn-valid Mastercard test number
            "nested": {"renamed_pan": "4012888888881881"},  # caught at depth
            "order_id": "4111111111111112",  # 16 digits but FAILS Luhn → preserved (not a PAN)
            "short_num": "123456789012",  # 12 digits, too short → preserved
            "ReservationId": 42,  # small int control → preserved
        }
    )
    assert out["acctRef"] == "***"
    assert out["spaced"] == "***"
    assert out["dashed"] == "***"
    assert out["nested"]["renamed_pan"] == "***"
    assert out["order_id"] == "4111111111111112"  # non-Luhn long number is not a PAN
    assert out["short_num"] == "123456789012"
    assert out["ReservationId"] == 42


# Every card / payment-credential key that actually appears in the live TeeItUp GNSVC
# booking payload (extracted from courses/teeitup/base.py). This is the authoritative
# set the store-boundary redaction MUST mask. It pins BOTH coverage paths in
# core/redaction.py: the `payment` namespace prefix (covers the Payment.*/Payments_*
# keys) AND the `_SENSITIVE_KEY_TOKENS` substrings (cover the non-namespaced cred-style
# keys — e.g. `card_number` relies solely on the "card" token). A future edit that drops
# a token or renames a GNSVC field then fails loudly here instead of silently leaking a
# raw PAN/CVV into the in-memory attempt_log. (Security finding from /full-repo-scan: the
# redaction is an allowlist, so a dropped token is otherwise an invisible regression.)
_LIVE_CARD_KEYS = (
    "Payment.CC.CreditCardNumber",
    "Payment.CC.CVVCode",
    "Payment.CC.ExpirationMonth",
    "Payment.CC.ExpirationYear",
    "Payment.Address.Line1",
    "Payment.Address.PostalCode",
    "Payment.Address.Country",
    "Payment.Name",
    "Payment.PhoneNumber",
    "Payments_1_CreditCard_NameOnCard",
    "Token",  # short-lived GNSVC bearer payment credential
    "card_number",
    "name_on_card",
    "expiry_month",
    "expiry_year",
    "billing_address",
    "billing_postal_code",
)


def test_redacts_every_live_card_field() -> None:
    # A single sentinel value per key; if any key survives unmasked, the PAN/CVV leaks.
    payload = {k: "SECRET-4111111111111111" for k in _LIVE_CARD_KEYS}
    payload["ReservationStatusID"] = 3  # non-sensitive control
    out = redact_payload(payload)
    leaked = [k for k in _LIVE_CARD_KEYS if out[k] != "***"]
    assert not leaked, f"card fields NOT redacted (raw PAN/CVV would reach attempt_log): {leaked}"
    # The raw sentinel must appear nowhere in the redacted output's values.
    assert "SECRET-4111111111111111" not in repr(out)
    assert out["ReservationStatusID"] == 3  # control preserved


# --- _EMAIL_RE bounds (ReDoS guard) ---------------------------------------
# `_EMAIL_RE`'s segments are length-bounded because `RedactingLogFilter` feeds `redact_text`
# arbitrary log records. NEARLY every other caller clamps its input (`r.text[:300]` and
# friends — every ForeUP site does; teeitup/base.py's GNSVC decline `Message` does not, and
# never did), so the unbounded `+` quantifiers' quadratic backtracking was mostly latent
# before; with the filter installed, one long unbroken word-char run in a third-party log
# line could stall the T0 drop.
# These pin BOTH directions: the bound must stay (perf) and must not tighten (real addresses
# must still be redacted).


def test_redact_text_masks_long_but_valid_addresses() -> None:
    """The bound must not be tightened to the point of missing real addresses.

    64 chars is the RFC 5321 local-part maximum, and multi-label subdomains are ordinary.

    Asserts EXACT output, not `addr not in out`. A too-tight bound produces a PARTIAL match —
    `aaaa…aa<redacted-email>` — which satisfies both "the full address is absent" and
    "<redacted-email> is present" while leaking 40+ characters of a real address into the log.
    Substring assertions cannot see that; equality can.
    """
    local = "a" * 60  # long, but inside the 64-char RFC local-part limit
    addr = f"{local}@mail.corp.example.co.uk"
    out = redact_text(f"holder={addr} status=400")
    assert out == "holder=<redacted-email> status=400", f"partial or missed match: {out}"


def test_redact_text_masks_plus_and_dotted_locals() -> None:
    """Plus-addressing and dotted locals are the shapes this project actually uses.

    Exact-output assertions, for the partial-match reason in the test above.
    """
    for addr in ("alanc3939+claude@gmail.com", "first.last@sub.example.org", "a_b-c@e-x.test"):
        out = redact_text(f"contact: {addr}")
        assert out == "contact: <redacted-email>", f"partial or missed match for {addr}: {out}"


def test_redact_text_is_not_quadratic_on_a_long_word_run() -> None:
    """Perf pin: a long unbroken word-char run must not blow up the T0 booking run.

    The LONG UNBROKEN RUN is the load-bearing part. An earlier version of this test used only
    `("a"*50 + "1"*50 + "Bearer ")*1000`, which looks varied but caps the longest word-char run
    at 106 chars — far too short to trigger quadratic backtracking. That made the pin VACUOUS:
    the fully unbounded pattern passed it in 20 ms. Measured on the real thing: bounded 41 ms
    vs unbounded 15,360 ms, so the 1 s ceiling sits ~24x above a passing run and ~15x below a
    regressed one — wide enough that a loaded CI runner cannot flake it. (Do not read 375x
    anywhere here: that is failure/pass, NOT the detection margin. The number that governs how
    far the ceiling can safely be relaxed is the ~15x gap to failure.)
    """
    # Mixed prefix + a long unbroken tail. The TAIL pins _EMAIL_RE (today's only
    # backtracking-prone pattern); the PREFIX makes this degrade into a general redact_text
    # pin if a different pattern ever becomes superlinear.
    #
    # The prefix's shapes are load-bearing and easy to get subtly wrong — an earlier version
    # used `"a"*50 + "1"*50 + "Bearer "`, which armed NOTHING: `\bBearer` needs a word
    # boundary and was preceded by a digit, and _PAN_TEXT_RE's `(?!\d)` tail rejects a
    # 50-digit run. So the arming is asserted below rather than assumed.
    unit = " 4111111111111111 Bearer abcdefgh12345678 x@y.test 727-555-0142 " + "a" * 40
    payload = unit * 1_000 + "a" * 100_000
    for name, pattern in (
        ("_EMAIL_RE", _EMAIL_RE),
        ("_PAN_TEXT_RE", _PAN_TEXT_RE),
        ("_BEARER_RE", _BEARER_RE),
        ("_PHONE_RE", _PHONE_RE),
    ):
        assert pattern.search(unit), (
            f"{name} is not armed by the payload — pin is narrower than it claims"
        )
    start = time.perf_counter()
    redact_text(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"redact_text took {elapsed:.2f}s on a {len(payload)}-char run"
