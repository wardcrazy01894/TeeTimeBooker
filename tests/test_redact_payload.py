"""Tests for orchestrator._redact_payload — the PCI/PII guard for attempt_log writes.

PLAN.md §10.1: card fields (PAN/CVV/expiry/billing) MUST be dropped before any attempt_log
write. The TeeItUp booking POSTs them to tr.gnsvc.com under the `Payment.*`/`Payments_*`
namespace; the cred-style keys (card_number, cvv, ...) also appear in CourseCredentials.extra.
`_redact_payload` must redact both shapes, recursively, without mutating the input.
"""

from __future__ import annotations

from teetime.core.orchestrator import _redact_payload


def test_redacts_cred_style_card_keys() -> None:
    out = _redact_payload(
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
    out = _redact_payload(
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
    out = _redact_payload(
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
    out = _redact_payload(payload)
    assert payload["card_number"] == "4111"  # original untouched
    assert out["card_number"] == "***"


def test_non_sensitive_substring_not_over_redacted() -> None:
    # Avoid dangerous substring tokens: "success" (contains "cc"), "company" (contains "pan"),
    # and bare "name" (course_name/job_name are useful audit fields, not PII).
    out = _redact_payload(
        {"success": True, "company": "ACME", "account": "x", "course_name": "Mangrove Bay"}
    )
    assert out["success"] is True
    assert out["company"] == "ACME"
    assert out["account"] == "x"
    assert out["course_name"] == "Mangrove Bay"


def test_redacts_player_pii() -> None:
    # PLAN §10.1 requires player PII (email/phone/member/name) redaction, not just card data.
    out = _redact_payload(
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


def test_redacts_nested_lists_of_lists() -> None:
    # The recursive guard must descend into nested lists (M2.T3 payload shapes are unknown).
    out = _redact_payload({"deep": [[{"cvv": "9"}], [{"ok": "keep"}]]})
    assert out["deep"][0][0]["cvv"] == "***"
    assert out["deep"][1][0]["ok"] == "keep"
