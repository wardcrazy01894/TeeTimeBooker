"""Tests for core.redaction.redact_payload — the PCI/PII guard for attempt_log writes.

PLAN.md §10.1: card fields (PAN/CVV/expiry/billing) MUST be dropped before any attempt_log
write. The TeeItUp booking POSTs them to tr.gnsvc.com under the `Payment.*`/`Payments_*`
namespace; the cred-style keys (card_number, cvv, ...) also appear in CourseCredentials.extra.
`redact_payload` must redact both shapes, recursively, without mutating the input. The store
boundary (`InMemoryStore.append_attempt`) applies it — see test_in_memory_store.py for that wiring.
"""

from __future__ import annotations

from teetime.core.redaction import redact_payload


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
    # The recursive guard must descend into nested lists (M2.T3 payload shapes are unknown).
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
