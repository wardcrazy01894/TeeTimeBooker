"""MULTIUSER_PLAN MU-7 — ``tenant.crypto`` (AES-256-GCM credential encryption, §9.2).

Pins the contract the runner (MU-9a) and the web connect flow (MU-14) rely on:
  * a blob decrypts only with the SAME AAD it was encrypted under (account binding),
  * the blob format is versioned and carries the key id, so rotation can find stale blobs,
  * the keyring is FAIL-CLOSED (missing / malformed / active-kid-absent raises at load),
  * plaintext and key material never surface in a repr or an exception message.

Never mocks the SUT: every test drives the real AESGCM path with a real 32-byte key.
"""

from __future__ import annotations

import base64
import json
import os
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from teetime.core.models import CourseId
from teetime.tenant.crypto import (
    KEYRING_ENV_VAR,
    CredentialDecryptError,
    CredentialEncryptError,
    Keyring,
    KeyringError,
    credential_aad,
    decrypt_password,
    encrypt_password,
    load_keyring,
    load_keyring_from_env,
    needs_rekey,
    rekey_password,
)
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    CourseAccount,
    CourseAccountId,
    UserId,
)

# Deterministic 32-byte keys so a failing assertion message is reproducible. They are
# TEST fixtures, not secrets — but the tests still assert they never appear in a repr.
_KEY_A = bytes(range(32))
_KEY_B = bytes(range(32, 64))
_PLAINTEXT = "hunter2-correct-horse-battery"  # long enough to be a meaningful literal
_AAD = b"account-1|foreup:19671:2149|golfer@example.com"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _ring_json(active: str, keys: dict[str, bytes]) -> str:
    return json.dumps({"active": active, "keys": {kid: _b64(k) for kid, k in keys.items()}})


@pytest.fixture
def ring_a() -> Keyring:
    return load_keyring(_ring_json("k1", {"k1": _KEY_A}))


@pytest.fixture
def ring_ab() -> Keyring:
    """Rotation state: ``k2`` is active, ``k1`` is retired but still readable."""
    return load_keyring(_ring_json("k2", {"k1": _KEY_A, "k2": _KEY_B}))


# --- round trip + format ------------------------------------------------------------------


def test_roundtrip(ring_a: Keyring) -> None:
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    assert decrypt_password(ring_a, blob, aad=_AAD) == _PLAINTEXT


def test_roundtrip_handles_unicode_and_empty_password(ring_a: Keyring) -> None:
    for pw in ("", "pässwörd-✓-🔐"):
        blob = encrypt_password(ring_a, pw, aad=_AAD)
        assert decrypt_password(ring_a, blob, aad=_AAD) == pw


def test_blob_format_is_versioned_and_carries_kid(ring_a: Keyring) -> None:
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    version, kid, nonce_b64, ct_b64 = blob.split(":")
    assert version == "v1"
    assert kid == "k1"
    assert len(base64.b64decode(nonce_b64, validate=True)) == 12  # 96-bit GCM nonce
    # ciphertext = len(plaintext) + 16-byte GCM tag
    assert len(base64.b64decode(ct_b64, validate=True)) == len(_PLAINTEXT.encode()) + 16


def test_fresh_nonce_per_encrypt(ring_a: Keyring) -> None:
    a = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    b = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    assert a != b
    assert a.split(":")[2] != b.split(":")[2]


# --- failure modes -------------------------------------------------------------------------


def test_aad_mismatch_fails(ring_a: Keyring) -> None:
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    other_account = b"account-2|foreup:19671:2149|golfer@example.com"
    with pytest.raises(CredentialDecryptError):
        decrypt_password(ring_a, blob, aad=other_account)


def test_unknown_kid_fails(ring_a: Keyring) -> None:
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    ring_other = load_keyring(_ring_json("k9", {"k9": _KEY_B}))
    with pytest.raises(CredentialDecryptError, match="k1"):
        decrypt_password(ring_other, blob, aad=_AAD)


def test_tampered_ciphertext_fails(ring_a: Keyring) -> None:
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    version, kid, nonce_b64, ct_b64 = blob.split(":")
    ct = bytearray(base64.b64decode(ct_b64))
    ct[0] ^= 0x01  # flip one bit in the ciphertext body → GCM tag no longer verifies
    tampered = ":".join([version, kid, nonce_b64, _b64(bytes(ct))])
    with pytest.raises(CredentialDecryptError):
        decrypt_password(ring_a, tampered, aad=_AAD)


@pytest.mark.parametrize(
    "blob",
    [
        "",
        "not-a-blob",
        "v2:k1:AAAAAAAAAAAAAAAA:AAAA",  # unsupported version
        "v1:k1:AAAA",  # too few fields
        "v1:k1:!!!!:AAAA",  # nonce not base64
        "v1:k1:AAAAAAAAAAAAAAAA:!!!!",  # ciphertext not base64
    ],
)
def test_malformed_blob_fails(ring_a: Keyring, blob: str) -> None:
    with pytest.raises(CredentialDecryptError):
        decrypt_password(ring_a, blob, aad=_AAD)


# --- keyring loading -----------------------------------------------------------------------


def test_keyring_env_var_is_a_name_only() -> None:
    # The code carries only the env var NAME; the value is a Key-Vault-injected secret.
    assert KEYRING_ENV_VAR == "TENANT_CREDS_KEYRING"


def test_load_keyring_from_env_reads_the_named_var() -> None:
    ring = load_keyring_from_env({KEYRING_ENV_VAR: _ring_json("k1", {"k1": _KEY_A})})
    assert ring.active_kid == "k1"


def test_load_keyring_from_env_unset_fails_loud_naming_the_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEYRING_ENV_VAR, raising=False)
    with pytest.raises(KeyringError, match=KEYRING_ENV_VAR):
        load_keyring_from_env()  # defaults to os.environ
    with pytest.raises(KeyringError, match=KEYRING_ENV_VAR):
        load_keyring_from_env({})


def test_load_keyring_parses_active_and_retired_keys(ring_ab: Keyring) -> None:
    assert ring_ab.active_kid == "k2"
    assert set(ring_ab.keys) == {"k1", "k2"}


def test_keyring_missing_active_fails_loud() -> None:
    # `active` names a kid that is not in `keys` → a job that cannot encrypt must exit
    # before T0, not discover it on the first row.
    with pytest.raises(KeyringError, match="active"):
        load_keyring(_ring_json("k2", {"k1": _KEY_A}))


@pytest.mark.parametrize(
    "raw",
    [
        None,  # env var unset
        "",  # env var empty
        "   ",
        "{not json",
        "[]",  # wrong top-level shape
        '{"keys": {}}',  # no `active`
        '{"active": "k1"}',  # no `keys`
        '{"active": "k1", "keys": {}}',  # empty ring
        '{"active": "k1", "keys": {"k1": "!!!not-base64!!!"}}',
        '{"active": "k1", "keys": {"k1": 123}}',  # key not a string
        '{"active": 1, "keys": {"1": "' + _b64(_KEY_A) + '"}}',  # active not a string
        '{"active": "", "keys": {"": "' + _b64(_KEY_A) + '"}}',  # empty kid
        '{"active": "a:b", "keys": {"a:b": "' + _b64(_KEY_A) + '"}}',  # ':' is the delimiter
    ],
)
def test_keyring_env_malformed_fails_loud(raw: str | None) -> None:
    with pytest.raises(KeyringError):
        load_keyring(raw)


@pytest.mark.parametrize("length", [16, 31, 33, 64])
def test_keyring_rejects_non_256_bit_key(length: int) -> None:
    with pytest.raises(KeyringError, match="32"):
        load_keyring(_ring_json("k1", {"k1": bytes(length)}))


def test_keyring_error_never_echoes_key_material() -> None:
    # A malformed ring must fail loud WITHOUT reflecting the (possibly partly valid) secret
    # value back into the exception text, which lands in logs / tracebacks.
    good_key = _b64(_KEY_A)
    raw = '{"active": "k1", "keys": {"k1": "' + good_key + '", "k2": "' + good_key + '", "x": 1}}'
    with pytest.raises(KeyringError) as info:
        load_keyring(raw)
    assert good_key not in str(info.value)
    assert good_key not in repr(info.value)


# --- rotation ------------------------------------------------------------------------------


def test_needs_rekey_is_true_only_for_non_active_kid(ring_a: Keyring, ring_ab: Keyring) -> None:
    old_blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)  # under k1
    new_blob = encrypt_password(ring_ab, _PLAINTEXT, aad=_AAD)  # under k2 (active)
    assert needs_rekey(ring_ab, old_blob) is True
    assert needs_rekey(ring_ab, new_blob) is False


def test_needs_rekey_malformed_blob_fails_loud(ring_a: Keyring) -> None:
    with pytest.raises(CredentialDecryptError):
        needs_rekey(ring_a, "garbage")


def test_rekey_idempotent(ring_a: Keyring, ring_ab: Keyring) -> None:
    old_blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)  # under retired k1
    once = rekey_password(ring_ab, old_blob, aad=_AAD)
    assert once != old_blob
    assert once.split(":")[1] == "k2"
    assert decrypt_password(ring_ab, once, aad=_AAD) == _PLAINTEXT
    # Second pass is a no-op: already on the active kid → the SAME blob comes back, byte for
    # byte (no fresh nonce), so a re-run of `tenant-rekey` writes nothing.
    twice = rekey_password(ring_ab, once, aad=_AAD)
    assert twice == once


def test_rekey_requires_the_retired_key_to_still_be_in_the_ring(ring_a: Keyring) -> None:
    old_blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)  # under k1
    ring_k2_only = load_keyring(_ring_json("k2", {"k2": _KEY_B}))  # k1 dropped too early
    with pytest.raises(CredentialDecryptError, match="k1"):
        rekey_password(ring_k2_only, old_blob, aad=_AAD)


def test_rekey_with_wrong_aad_fails(ring_a: Keyring, ring_ab: Keyring) -> None:
    old_blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    with pytest.raises(CredentialDecryptError):
        rekey_password(ring_ab, old_blob, aad=b"someone-else")


# --- AAD derivation ------------------------------------------------------------------------


def _account(**overrides: object) -> CourseAccount:
    base: dict[str, object] = {
        "id": CourseAccountId(UUID("11111111-1111-1111-1111-111111111111")),
        "user_id": UserId(UUID("22222222-2222-2222-2222-222222222222")),
        "course_id": CourseId("foreup:19671:2149"),
        "provenance": AccountProvenance.USER_SUPPLIED,
        "username": "golfer@example.com",
        "password_ciphertext": "",
        "key_id": "k1",
        "status": AccountStatus.ACTIVE,
        "verified_at": datetime(2026, 9, 25, tzinfo=UTC),
    }
    base.update(overrides)
    return CourseAccount(**base)  # type: ignore[arg-type]


def test_credential_aad_binds_account_course_and_username() -> None:
    aad = credential_aad(_account())
    assert aad == b"11111111-1111-1111-1111-111111111111|foreup:19671:2149|golfer@example.com"


def test_credential_aad_changes_when_any_component_changes() -> None:
    base = credential_aad(_account())
    assert credential_aad(_account(username="other@example.com")) != base
    assert credential_aad(_account(course_id=CourseId("foreup:1:2"))) != base
    assert (
        credential_aad(_account(id=CourseAccountId(UUID("33333333-3333-3333-3333-333333333333"))))
        != base
    )


def test_blob_copied_onto_another_account_row_fails(ring_a: Keyring) -> None:
    # The §9.1 threat: a ciphertext moved to another account's row must not decrypt.
    victim = _account()
    attacker = _account(id=CourseAccountId(UUID("33333333-3333-3333-3333-333333333333")))
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=credential_aad(victim))
    assert decrypt_password(ring_a, blob, aad=credential_aad(victim)) == _PLAINTEXT
    with pytest.raises(CredentialDecryptError):
        decrypt_password(ring_a, blob, aad=credential_aad(attacker))


# --- no leakage ----------------------------------------------------------------------------


def test_plaintext_never_in_repr(ring_a: Keyring) -> None:
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    assert _PLAINTEXT not in blob
    # Keyring repr must not carry key bytes in ANY encoding.
    for rendering in (repr(ring_a), str(ring_a)):
        assert _PLAINTEXT not in rendering
        assert _b64(_KEY_A) not in rendering
        assert repr(_KEY_A) not in rendering
        assert _KEY_A.hex() not in rendering
    # A failed decrypt (wrong AAD) must not reflect plaintext, ciphertext, or key material.
    with pytest.raises(CredentialDecryptError) as info:
        decrypt_password(ring_a, blob, aad=b"wrong")
    for rendering in (str(info.value), repr(info.value)):
        assert _PLAINTEXT not in rendering
        assert blob.split(":")[3] not in rendering
        assert _b64(_KEY_A) not in rendering


# --- review round 1 (PR #220): must-fix 1 — key material never echoed from a load error -----
# During rotation an operator hand-edits `TENANT-CREDS-KEYRING` in the Portal; if the new key
# lands on the WRONG side of the JSON (as a kid, or as `active`) the ring must still fail closed
# WITHOUT the key reaching the exception text — the runner logs that exception to Log
# Analytics, and the E7 literal registry cannot help because it loads from the ring that
# failed to parse.

_SWAPPED_KEY = os.urandom(32)
_SWAPPED_KEY_B64 = _b64(_SWAPPED_KEY)


@pytest.mark.parametrize(
    "template",
    [
        '{"active": "k1", "keys": {"<K>": "k1"}}',  # kid and value swapped
        '{"active": "<K>", "keys": {"k1": "<K>"}}',  # key pasted as `active`
        '{"active": "k1", "keys": {"<K>": "<K16>"}}',  # key as kid, short value
        '{"active": "k1", "keys": {"<K>": "<K>"}}',  # key as kid, valid 32-byte value
        '{"active": "k1", "keys": {"k1": "<K>", "<K>": "<K>"}}',  # extra key-shaped kid
    ],
)
def test_swapped_kid_and_key_never_echoes_key_material(template: str) -> None:
    raw = template.replace("<K16>", _b64(_SWAPPED_KEY[:16])).replace("<K>", _SWAPPED_KEY_B64)
    with pytest.raises(KeyringError) as info:
        load_keyring(raw)
    rendered = "".join(traceback.format_exception(info.value))
    for text in (str(info.value), repr(info.value), rendered):
        assert _SWAPPED_KEY_B64 not in text
        assert _SWAPPED_KEY_B64[:20] not in text  # nor any truncated echo of it
        assert _SWAPPED_KEY.hex() not in text


@pytest.mark.parametrize("kid", ["k1", "2026-09-25", "rot.2_a", "K" * 32])
def test_kid_label_pattern_accepts_short_labels(kid: str) -> None:
    ring = load_keyring(_ring_json(kid, {kid: _KEY_A}))
    assert ring.active_kid == kid


@pytest.mark.parametrize(
    "kid",
    ["", "k 1", "k/1", "k+1", "k=1", "k:1", "K" * 33, "kïd", _b64(_KEY_A)],
)
def test_kid_label_pattern_rejects_key_shaped_or_unsafe_kids(kid: str) -> None:
    # A base64 AES-256 key is 44 chars of [A-Za-z0-9+/=]; the 32-char cap alone rejects it,
    # and the alphabet keeps the blob delimiter (':') and whitespace out.
    with pytest.raises(KeyringError, match="key id"):
        load_keyring(_ring_json(kid, {kid: _KEY_A}))


def test_keyring_post_init_errors_never_echo_kids() -> None:
    # The type-level invariants (a hand-built ring) must not echo kids either: a kid is
    # operator data that may be a mis-pasted secret, so load errors name ENTRY POSITIONS.
    with pytest.raises(KeyringError) as info:
        Keyring(active_kid="k1", keys={"k1": _KEY_A, _SWAPPED_KEY_B64: _KEY_B})
    assert _SWAPPED_KEY_B64 not in str(info.value)
    with pytest.raises(KeyringError) as info:
        Keyring(active_kid=_SWAPPED_KEY_B64, keys={"k1": _KEY_A})
    assert _SWAPPED_KEY_B64 not in str(info.value)
    with pytest.raises(KeyringError) as info:
        Keyring(active_kid="k1", keys={"k1": _KEY_A[:16]})
    assert "#1" in str(info.value)  # positional, not by name


# --- review round 1: should-fix 1 — duplicate JSON keys are rejected, not last-wins ---------


@pytest.mark.parametrize(
    "raw",
    [
        '{"active": "k1", "keys": {"k1": "<A>", "k1": "<B>"}}',  # duplicate kid
        '{"active": "k1", "keys": {"k1": "<A>"}, "keys": {"k1": "<B>"}}',  # duplicate `keys`
        '{"active": "k1", "active": "k2", "keys": {"k1": "<A>", "k2": "<B>"}}',  # dup active
    ],
)
def test_keyring_duplicate_json_keys_fail_loud(raw: str) -> None:
    raw = raw.replace("<A>", _b64(_KEY_A)).replace("<B>", _b64(_KEY_B))
    with pytest.raises(KeyringError, match="duplicate"):
        load_keyring(raw)


# --- review round 1: should-fix 2/3/4 — no secret survives in the exception CHAIN ------------
# `raise X from None` only sets __suppress_context__; __context__ STILL points at the inner
# exception, and a UnicodeDecodeError's `.object` is the plaintext bytes, a UnicodeEncodeError's
# `.object` is the password, and a JSONDecodeError's `.doc` is the WHOLE keyring text. So every
# wrapped error must carry NO chain at all (raised outside the `except` block), and a RENDERED
# traceback must be clean too.


def _assert_unchained(exc: BaseException) -> None:
    assert exc.__cause__ is None
    assert exc.__context__ is None


def _rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def test_invalid_utf8_decrypt_fails_without_leaking_bytes(ring_a: Keyring) -> None:
    # Only reachable with a blob NOT produced by `encrypt_password` (which always encodes
    # UTF-8), so build the ciphertext with the library directly.
    raw = b"\xff\xfe-not-utf8-secret-bytes"
    nonce = os.urandom(12)
    ct = AESGCM(_KEY_A).encrypt(nonce, raw, _AAD)
    blob = ":".join(["v1", "k1", _b64(nonce), _b64(ct)])
    with pytest.raises(CredentialDecryptError, match="UTF-8") as info:
        decrypt_password(ring_a, blob, aad=_AAD)
    _assert_unchained(info.value)
    for text in (str(info.value), repr(info.value), _rendered(info.value)):
        assert "not-utf8-secret-bytes" not in text
        assert "\\xff" not in text
        assert _b64(ct) not in text


def test_encrypt_lone_surrogate_fails_without_leaking_plaintext(ring_a: Keyring) -> None:
    bad = "pw-\ud800-surrogate-secret"
    with pytest.raises(CredentialEncryptError) as info:
        encrypt_password(ring_a, bad, aad=_AAD)
    _assert_unchained(info.value)
    for text in (str(info.value), repr(info.value), _rendered(info.value)):
        assert "surrogate-secret" not in text
        assert "pw-" not in text
        assert "\\ud800" not in text


def _wrong_aad(ring: Keyring) -> None:
    decrypt_password(ring, encrypt_password(ring, _PLAINTEXT, aad=_AAD), aad=b"wrong")


def _tampered(ring: Keyring) -> None:
    version, kid, nonce_b64, ct_b64 = encrypt_password(ring, _PLAINTEXT, aad=_AAD).split(":")
    ct = bytearray(base64.b64decode(ct_b64))
    ct[-1] ^= 0x01
    decrypt_password(ring, ":".join([version, kid, nonce_b64, _b64(bytes(ct))]), aad=_AAD)


def _bad_nonce_b64(ring: Keyring) -> None:
    decrypt_password(ring, "v1:k1:!!!!:AAAA", aad=_AAD)


def _bad_ct_b64(ring: Keyring) -> None:
    decrypt_password(ring, "v1:k1:AAAAAAAAAAAAAAAA:!!!!", aad=_AAD)


def _bad_json(_: Keyring) -> None:
    load_keyring('{"active": "k1", "keys": {"k1": "' + _b64(_KEY_A) + '"')  # truncated


def _dup_json(_: Keyring) -> None:
    load_keyring('{"active": "k1", "active": "k1", "keys": {"k1": "' + _b64(_KEY_A) + '"}}')


def _bad_key_b64(_: Keyring) -> None:
    load_keyring('{"active": "k1", "keys": {"k1": "!!!not-base64!!!"}}')


@pytest.mark.parametrize(
    "trigger",
    [_wrong_aad, _tampered, _bad_nonce_b64, _bad_ct_b64, _bad_json, _dup_json, _bad_key_b64],
    ids=lambda f: f.__name__,
)
def test_every_wrapped_error_has_no_exception_chain(
    ring_a: Keyring, trigger: Callable[[Keyring], None]
) -> None:
    with pytest.raises((CredentialDecryptError, KeyringError)) as info:
        trigger(ring_a)
    _assert_unchained(info.value)


def test_rendered_traceback_contains_no_plaintext_or_key(ring_a: Keyring) -> None:
    blob = encrypt_password(ring_a, _PLAINTEXT, aad=_AAD)
    with pytest.raises(CredentialDecryptError) as decrypt_info:
        decrypt_password(ring_a, blob, aad=b"wrong")
    key_b64 = _b64(_KEY_A)
    with pytest.raises(KeyringError) as load_info:
        load_keyring('{"active": "k1", "keys": {"k1": "' + key_b64 + '", "k2": 5}}')
    for text in (_rendered(decrypt_info.value), _rendered(load_info.value)):
        assert _PLAINTEXT not in text
        assert key_b64 not in text
        assert _KEY_A.hex() not in text
        assert blob.split(":")[3] not in text


# --- review round 1: nits 1 + 2 --------------------------------------------------------------


def test_keyring_keys_are_read_only_and_detached_from_the_source_dict() -> None:
    # `frozen=True` only freezes attribute REBINDING; a plain dict value could still be
    # mutated (`ring.keys.clear()`), bypassing the __post_init__ invariants.
    source = {"k1": _KEY_A}
    ring = Keyring(active_kid="k1", keys=source)
    with pytest.raises((TypeError, AttributeError)):
        ring.keys["k2"] = _KEY_B  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        ring.keys.clear()  # type: ignore[attr-defined]
    source["k1"] = _KEY_B  # mutating the dict handed in must not reach the ring
    assert ring.keys["k1"] == _KEY_A


_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def test_non_canonical_base64_in_blob_is_rejected(ring_a: Keyring) -> None:
    # Python's b64decode (even validate=True) accepts non-zero padding bits, so one ciphertext
    # has several base64 spellings. Blobs are OUR output and always canonical; a non-canonical
    # spelling is corruption or tampering with the stored row, not a blob we wrote.
    blob = encrypt_password(ring_a, "abc", aad=_AAD)  # 19-byte ct → 28 chars, '=' padded
    version, kid, nonce_b64, ct_b64 = blob.split(":")
    assert ct_b64.endswith("=") and not ct_b64.endswith("==")
    i = _B64_ALPHABET.index(ct_b64[-2])
    non_canonical = ct_b64[:-2] + _B64_ALPHABET[(i & 0b111100) | 0b11] + "="
    assert non_canonical != ct_b64
    # sanity: the two spellings decode to the SAME bytes, so GCM alone would accept it
    assert base64.b64decode(non_canonical, validate=True) == base64.b64decode(ct_b64)
    with pytest.raises(CredentialDecryptError, match="canonical"):
        decrypt_password(ring_a, ":".join([version, kid, nonce_b64, non_canonical]), aad=_AAD)
