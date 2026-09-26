"""MULTIUSER_PLAN MU-9a: the tenant booking runner core (``tenant.runner``, §4).

Every multi-account timing test runs on a ``VirtualClock`` (§4.4 proof 3): the runner's N
concurrent UNMODIFIED orchestrators, its streamed outcome writer and its self-deadline all sleep
on the same clock, and each account's POST instants are read from its recording decorator.
"""

from __future__ import annotations

import pytest

from teetime.core.adapter import AdapterCapabilities
from teetime.core.redaction import redact_text
from teetime.tenant.models import EventRow
from teetime.tenant.runner import assert_blind_methods_present, resolve_credentials

from .runner_builders import KEYRING, new_store, seed_account

# --- resolve_credentials (§4.2 decrypt step, E7) ---------------------------------------------


async def test_resolve_credentials_decrypts_and_registers_secret_literals() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    b = await seed_account(store, n=2)
    rows = [EventRow(row=a.row, account=a.account), EventRow(row=b.row, account=b.account)]
    assert redact_text(f"pw={a.password}") == f"pw={a.password}"  # not masked before

    creds, failed = resolve_credentials(rows, keyring=KEYRING)

    assert failed == frozenset()
    assert creds[a.row.id].username == a.account.username
    assert creds[a.row.id].password == a.password
    assert creds[b.row.id].password == b.password
    # E7: both decrypted passwords are masked in every redacted log line from now on.
    assert a.password not in redact_text(f"pw={a.password}")
    assert b.password not in redact_text(f"pw={b.password}")


async def test_resolve_credentials_skips_a_row_whose_decrypt_fails() -> None:
    """A per-row decrypt failure never raises (§4.5): that row is reported, the others resolve."""
    store = new_store()
    good = await seed_account(store, n=1)
    bad = await seed_account(store, n=2, ciphertext="v1:k1:AAAA:BBBB")
    rows = [
        EventRow(row=good.row, account=good.account),
        EventRow(row=bad.row, account=bad.account),
    ]

    creds, failed = resolve_credentials(rows, keyring=KEYRING)

    assert failed == frozenset({bad.row.id})
    assert set(creds) == {good.row.id}


# --- assert_blind_methods_present (round-2 SF1) -----------------------------------------------


class _BlindWithoutMethods:
    capabilities = AdapterCapabilities(blind_post=True)


class _NonBlind:
    capabilities = AdapterCapabilities(blind_post=False)


def test_assert_blind_methods_present_refuses_blind_adapter_missing_methods() -> None:
    with pytest.raises(TypeError, match="synthesize_blind_slots"):
        assert_blind_methods_present([_BlindWithoutMethods()])  # type: ignore[list-item]


def test_assert_blind_methods_present_ignores_non_blind_adapters() -> None:
    assert_blind_methods_present([_NonBlind()])  # type: ignore[list-item]
