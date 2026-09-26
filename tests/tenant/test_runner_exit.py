"""MULTIUSER_PLAN MU-9b: the booking runner's exit contract (§4.5), one case per table row.

``exit_code_for`` is pure: it maps a ``RunReport`` to the process exit status. A missed drop
and one user's bad password are per-user outcomes (exit 0, the emails carry them); systemic
causes (store, keyring, any decrypt failure, CAPTCHA/OTP — including one the blind burst
swallowed — UNCERTAIN, the self-deadline, a failed outcome write, a failed operator summary)
exit non-zero.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from teetime.core.models import BookingOutcome
from teetime.tenant.models import RowId
from teetime.tenant.runner import (
    AccountOutcome,
    ExitStatus,
    RunReport,
    WatchReport,
    exit_code_for,
)

_ROW = RowId(uuid4())


def _outcome(**kw: Any) -> AccountOutcome:
    base = AccountOutcome(
        row_id=_ROW,
        outcome=None,
        error=None,
        uncertain=False,
        decrypt_failed=False,
        search_only=False,
    )
    return replace(base, **kw)


def _report(*outcomes: AccountOutcome, **kw: Any) -> RunReport:
    base = RunReport(
        event_key="mb0600et",
        rows_loaded=len(outcomes),
        rows_claimed=len(outcomes),
        outcomes=tuple(outcomes),
        systemic_error=None,
    )
    return replace(base, **kw)


# One case per §4.5 row (in table order), plus the per-outcome variants a row names.
_TABLE: list[tuple[str, RunReport, ExitStatus]] = [
    ("no pending rows", _report(rows_loaded=0), ExitStatus.OK),
    ("booked", _report(_outcome(outcome=BookingOutcome.BOOKED)), ExitStatus.OK),
    ("already booked", _report(_outcome(outcome=BookingOutcome.ALREADY_BOOKED)), ExitStatus.OK),
    ("dry run", _report(_outcome(outcome=BookingOutcome.DRY_RUN)), ExitStatus.OK),
    ("missed drop", _report(_outcome(outcome=BookingOutcome.NO_INVENTORY)), ExitStatus.OK),
    ("rate limited", _report(_outcome(outcome=BookingOutcome.RATE_LIMITED)), ExitStatus.OK),
    (
        "per-account AuthError",
        _report(_outcome(error="AuthError", auth_error=True)),
        ExitStatus.OK,
    ),
    (
        "credential decrypt failure (one row)",
        _report(_outcome(error="CredentialDecryptError", decrypt_failed=True)),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "keyring missing/invalid",
        _report(rows_loaded=0, rows_claimed=0, systemic_error="keyring: KeyringError"),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "DB read failure",
        _report(systemic_error="load_event_rows: TimeoutError"),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "claim failure",
        _report(systemic_error="claim_rows: RuntimeError"),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "CaptchaError out of orch.run",
        _report(_outcome(error="CaptchaError", captcha_error=True)),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "OtpChallengeError out of orch.run",
        _report(_outcome(error="OtpChallengeError", captcha_error=True)),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "UNCERTAIN (non-contract exception out of orch.run)",
        _report(_outcome(error="AdapterError", uncertain=True)),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "UNCERTAIN blind POST swallowed, a sibling booked",
        _report(_outcome(outcome=BookingOutcome.BOOKED, uncertain=True)),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "Captcha/OTP swallowed on a blind POST",
        _report(_outcome(outcome=BookingOutcome.BOOKED, swallowed_captcha_error=True)),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "operator-summary email failed",
        _report(_outcome(outcome=BookingOutcome.NO_INVENTORY), summary_email_failed=True),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "self-deadline reached",
        _report(_outcome(error="SelfDeadlineReachedError", uncertain=True), self_deadline_hit=True),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
    (
        "WRITE #2 failure after retries",
        _report(_outcome(outcome=BookingOutcome.BOOKED), outcome_write_failures=(_ROW,)),
        ExitStatus.SYSTEMIC_FAILURE,
    ),
]


@pytest.mark.parametrize(("name", "report", "expected"), _TABLE, ids=[c[0] for c in _TABLE])
def test_exit_contract_table(name: str, report: RunReport, expected: ExitStatus) -> None:
    assert exit_code_for(report) is expected, name


def test_runner_exit_contract_table_covers_every_section_4_5_row() -> None:
    """The §4.5 table has 11 rows and every one has at least one case above (the plan's
    ``test_runner_exit_contract_table``, pinned against a silent case deletion)."""
    assert len(_TABLE) >= 11


def test_misses_and_auth_errors_alongside_a_booking_exit_zero() -> None:
    report = _report(
        _outcome(outcome=BookingOutcome.BOOKED),
        replace(_outcome(outcome=BookingOutcome.NO_INVENTORY), row_id=RowId(uuid4())),
        replace(_outcome(error="AuthError", auth_error=True), row_id=RowId(uuid4())),
    )
    assert exit_code_for(report) == 0


def test_watch_report_systemic_error_is_nonzero() -> None:
    clean = WatchReport(
        rows_loaded=0,
        searches=0,
        logins=0,
        booked=(),
        upgraded=(),
        lost=(),
        rate_limited=True,
        systemic_error=None,
    )
    assert exit_code_for(clean) is ExitStatus.OK
    failed = replace(clean, systemic_error="load_watch_rows: X")
    assert exit_code_for(failed) is ExitStatus.SYSTEMIC_FAILURE
