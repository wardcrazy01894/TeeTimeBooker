"""Credential encryption for stored course passwords (MULTIUSER_PLAN §9.2).

AES-256-GCM with associated data. The format is ``v1:<kid>:<b64 nonce(12)>:<b64 ct+tag>``. AAD =
``course_account_id|course_id|username``, so a ciphertext copied onto another account's row fails
to decrypt. Fernet was rejected because it has no AAD.

The keyring comes from ONE Key Vault secret (``TENANT-CREDS-KEYRING``), injected as an env var at
container start. The keyring is never fetched through an SDK call (the Cosmos data plane uses MI
auth, MULTIUSER_PLAN §10.2), and it is kept out of Cosmos, so a DB-only compromise yields
ciphertexts, not keys.
Format: ``{"active": "<kid>", "keys": {"<kid>": "<b64 32 bytes>"}}``. Readers accept any kid in
the ring; writers always use ``active``. Rotation: add a kid -> set active -> ``teetime
tenant-rekey`` (idempotent) -> drop the old kid.

Plaintext exists only in process memory, and every decrypted value is registered with the log
filter (``core.redaction.register_secret_literals``, engine hook E7) before first use.

STUB — the ``cryptography`` dependency lands with the implementation in MULTIUSER_PLAN MU-7.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import CourseAccount

_MU7 = "MULTIUSER_PLAN.md MU-7"

KEYRING_ENV_VAR = "TENANT_CREDS_KEYRING"  # env-var NAME only (never a literal key in config)


class CredentialDecryptError(RuntimeError):
    """Unknown kid, bad format, or AEAD tag/AAD mismatch. In the booking runner, this skips the
    row AND makes the execution exit non-zero (an operator bug, §4.5). The message NEVER includes
    key material or ciphertext."""


@dataclass(frozen=True, slots=True)
class Keyring:
    active_kid: str
    # repr=False: key bytes must never appear in a repr/log line.
    keys: dict[str, bytes] = field(repr=False)


def load_keyring(env_value: str | None) -> Keyring:
    """Parse the keyring JSON. FAIL-CLOSED (unlike skip dates): missing/malformed/active-kid-
    absent raises, because a job that can't decrypt must exit before T0 (§4.5)."""
    raise NotImplementedError(_MU7)


def credential_aad(account: CourseAccount) -> bytes:
    """``f"{account.id}|{account.course_id}|{account.username}".encode()``."""
    raise NotImplementedError(_MU7)


def encrypt_password(keyring: Keyring, plaintext: str, *, aad: bytes) -> str:
    """Encrypt under ``keyring.active_kid`` with a fresh random 96-bit nonce."""
    raise NotImplementedError(_MU7)


def decrypt_password(keyring: Keyring, blob: str, *, aad: bytes) -> str:
    """Decrypt a ``v1:`` blob; raises ``CredentialDecryptError`` on any failure."""
    raise NotImplementedError(_MU7)


def needs_rekey(keyring: Keyring, blob: str) -> bool:
    """True iff ``blob`` is not encrypted under the active kid (drives ``tenant-rekey``)."""
    raise NotImplementedError(_MU7)
