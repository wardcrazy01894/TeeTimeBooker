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
tenant-rekey`` (idempotent, built on ``rekey_password``) -> drop the old kid.

Plaintext exists only in process memory. Every decrypted value is to be registered with the log
filter (``core.redaction.register_secret_literals``, engine hook E7 — lands in MU-4) before first
use; until E7 exists, callers must keep decrypted values out of every log call.

Leak discipline (pinned by ``tests/test_tenant_crypto.py``): no key byte, ciphertext, or plaintext
ever reaches a ``repr``, an exception message, or an exception CAUSE chain — every low-level error
is re-raised ``from None`` so a rendered traceback cannot carry it either. Only key IDs (operator
labels, never secret) are echoed.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import CourseAccount

KEYRING_ENV_VAR = "TENANT_CREDS_KEYRING"  # env-var NAME only (never a literal key in config)

_BLOB_VERSION = "v1"
_BLOB_FIELDS = 4  # version:kid:nonce:ciphertext
_SEP = ":"
_NONCE_BYTES = 12  # 96-bit GCM nonce (the NIST-recommended size for AESGCM)
_KEY_BYTES = 32  # AES-256


class KeyringError(RuntimeError):
    """The keyring env value is unset, malformed, or names an ``active`` kid that is not in the
    ring. FAIL-CLOSED: a job that cannot decrypt must exit before T0 (§4.5), so this raises at
    load, not on the first row. The message NEVER includes key material."""


class CredentialEncryptError(ValueError):
    """The plaintext cannot be encoded (e.g. a lone surrogate). The message NEVER includes the
    plaintext — a raw ``UnicodeEncodeError`` would carry it in ``.object`` and its repr."""


class CredentialDecryptError(RuntimeError):
    """Unknown kid, bad format, or AEAD tag/AAD mismatch. In the booking runner, this skips the
    row AND makes the execution exit non-zero (an operator bug, §4.5). The message NEVER includes
    key material, ciphertext, or plaintext."""


# A kid is a short operator LABEL ("k1", "2026-09-25", "rot.2_a"), never key-shaped: the
# 32-char cap alone rejects a base64 AES-256 key (44 chars), and the alphabet keeps the blob
# delimiter (':'), whitespace, and the base64 '+', '/', '=' out. Load-time errors additionally
# never echo a kid at all (they name the entry POSITION), so a secret pasted on the wrong
# side of the JSON during a hand rotation cannot reach the exception text (review round 1).
_KID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_KID_RULE = "a 1-32 char label of [A-Za-z0-9._-]"


def _is_valid_kid(kid: object) -> bool:
    return isinstance(kid, str) and _KID_RE.fullmatch(kid) is not None


@dataclass(frozen=True, slots=True)
class Keyring:
    active_kid: str
    # repr=False: key bytes must never appear in a repr/log line.
    keys: dict[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        # Invariants hold on the TYPE, not just on `load_keyring`, so a hand-built ring in a
        # test or a future loader cannot bypass them. Messages name entry POSITIONS, never
        # kids — a kid may be a mis-pasted secret.
        if not self.keys:
            raise KeyringError("keyring has no keys")
        for position, (kid, key) in enumerate(self.keys.items(), start=1):
            if not _is_valid_kid(kid):
                raise KeyringError(
                    f"keyring `keys` entry #{position} has an invalid key id (must be {_KID_RULE})"
                )
            if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
                raise KeyringError(
                    f"keyring `keys` entry #{position} must be {_KEY_BYTES} bytes (AES-256)"
                )
        if not _is_valid_kid(self.active_kid):
            raise KeyringError(f"keyring `active` is not a valid key id (must be {_KID_RULE})")
        if self.active_kid not in self.keys:
            raise KeyringError("keyring `active` key id is not in `keys`")


class _DuplicateJsonKeyError(ValueError):
    """Raised by the `object_pairs_hook` when a JSON object repeats a key."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # `json.loads` is last-wins on duplicate keys; for a keyring that would silently drop a
    # key (or an `active`) the operator believes is present.
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateJsonKeyError
        out[key] = value
    return out


def load_keyring(env_value: str | None) -> Keyring:
    """Parse the keyring JSON. FAIL-CLOSED (unlike skip dates): missing/malformed/active-kid-
    absent raises, because a job that can't decrypt must exit before T0 (§4.5)."""
    if env_value is None or not env_value.strip():
        raise KeyringError(f"{KEYRING_ENV_VAR} is unset or empty")
    try:
        parsed: object = json.loads(env_value, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateJsonKeyError:
        raise KeyringError(f"{KEYRING_ENV_VAR} JSON has a duplicate key") from None
    except ValueError:
        # `from None`: the JSONDecodeError carries no content, but the discipline is uniform.
        raise KeyringError(f"{KEYRING_ENV_VAR} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise KeyringError(f"{KEYRING_ENV_VAR} must be a JSON object")
    active = parsed.get("active")
    raw_keys = parsed.get("keys")
    if not isinstance(active, str):
        raise KeyringError(f"{KEYRING_ENV_VAR} `active` must be a string key id")
    if not isinstance(raw_keys, dict):
        raise KeyringError(f"{KEYRING_ENV_VAR} `keys` must be a JSON object of kid -> base64")
    keys: dict[str, bytes] = {}
    for position, (kid, raw) in enumerate(raw_keys.items(), start=1):
        if not _is_valid_kid(kid):
            raise KeyringError(
                f"{KEYRING_ENV_VAR} `keys` entry #{position} has an invalid key id "
                f"(must be {_KID_RULE})"
            )
        if not isinstance(raw, str):
            raise KeyringError(
                f"{KEYRING_ENV_VAR} `keys` entry #{position} must be a base64 string"
            )
        try:
            keys[kid] = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise KeyringError(
                f"{KEYRING_ENV_VAR} `keys` entry #{position} is not valid base64"
            ) from None
    return Keyring(active_kid=active, keys=keys)


def load_keyring_from_env(environ: Mapping[str, str] | None = None) -> Keyring:
    """``load_keyring`` over ``environ[KEYRING_ENV_VAR]`` (default ``os.environ``). The ONLY
    place the env var name is dereferenced, so entrypoints share one lookup."""
    env = os.environ if environ is None else environ
    return load_keyring(env.get(KEYRING_ENV_VAR))


def credential_aad(account: CourseAccount) -> bytes:
    """``f"{account.id}|{account.course_id}|{account.username}".encode()``."""
    return f"{account.id}|{account.course_id}|{account.username}".encode()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def encrypt_password(keyring: Keyring, plaintext: str, *, aad: bytes) -> str:
    """Encrypt under ``keyring.active_kid`` with a fresh random 96-bit nonce."""
    kid = keyring.active_kid
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(keyring.keys[kid]).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return _SEP.join((_BLOB_VERSION, kid, _b64(nonce), _b64(ciphertext)))


def _parse_blob(blob: str) -> tuple[str, bytes, bytes]:
    """Split a ``v1:`` blob into ``(kid, nonce, ciphertext)`` or raise ``CredentialDecryptError``.
    Echoes nothing from the blob except a well-formed kid."""
    parts = blob.split(_SEP)
    if len(parts) != _BLOB_FIELDS:
        raise CredentialDecryptError(
            f"malformed credential blob (expected {_BLOB_FIELDS} ':'-separated fields)"
        )
    version, kid, nonce_b64, ct_b64 = parts
    if version != _BLOB_VERSION:
        raise CredentialDecryptError(
            f"unsupported credential blob version (expected {_BLOB_VERSION!r})"
        )
    if not _is_valid_kid(kid):
        raise CredentialDecryptError("credential blob has an empty key id")
    try:
        nonce = base64.b64decode(nonce_b64, validate=True)
        ciphertext = base64.b64decode(ct_b64, validate=True)
    except (binascii.Error, ValueError):
        raise CredentialDecryptError(
            "credential blob nonce/ciphertext is not valid base64"
        ) from None
    if len(nonce) != _NONCE_BYTES:
        raise CredentialDecryptError(f"credential blob nonce must be {_NONCE_BYTES} bytes")
    return kid, nonce, ciphertext


def decrypt_password(keyring: Keyring, blob: str, *, aad: bytes) -> str:
    """Decrypt a ``v1:`` blob; raises ``CredentialDecryptError`` on any failure."""
    kid, nonce, ciphertext = _parse_blob(blob)
    key = keyring.keys.get(kid)
    if key is None:
        raise CredentialDecryptError(
            # The blob's kid is echoed: it passed `_KID_RE` (a ≤32-char label, not key-shaped)
            # and blobs are our own `encrypt_password` output. The ring's OTHER kids are not
            # listed — nothing more is needed to act on this.
            f"unknown key id {kid!r}: not in the keyring (active {keyring.active_kid!r})"
        )
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        raise CredentialDecryptError(
            f"authentication failed under key id {kid!r}: wrong key, AAD mismatch (blob bound "
            "to a different account), or tampered ciphertext"
        ) from None
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        # A UnicodeDecodeError message embeds the offending BYTES — never let it propagate.
        raise CredentialDecryptError("decrypted credential is not valid UTF-8") from None


def needs_rekey(keyring: Keyring, blob: str) -> bool:
    """True iff ``blob`` is not encrypted under the active kid (drives ``tenant-rekey``).
    A malformed blob raises ``CredentialDecryptError`` (it can't be rekeyed either)."""
    kid, _nonce, _ciphertext = _parse_blob(blob)
    return kid != keyring.active_kid


def rekey_password(keyring: Keyring, blob: str, *, aad: bytes) -> str:
    """Re-encrypt ``blob`` under the active kid. IDEMPOTENT: a blob already on the active kid is
    returned unchanged (byte for byte — no fresh nonce), so a re-run of ``tenant-rekey`` writes
    nothing. The retired kid must still be in the ring (rotation step order, §9.2)."""
    if not needs_rekey(keyring, blob):
        return blob
    return encrypt_password(keyring, decrypt_password(keyring, blob, aad=aad), aad=aad)
