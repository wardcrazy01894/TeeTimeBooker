"""Cosmos document mapping for the tenant store (MULTIUSER_PLAN §3.1/§3.2/§10.2, MU-8a).

Pure and dependency-free: ``to_*_doc(obj) -> dict`` / ``from_*_doc(dict) -> Stored[obj]`` for
every persisted type, plus the deterministic document ids and partition keys that carry the §3.2
uniqueness invariants. ``CosmosTenantStore`` (MU-8b) hands these dicts to ``azure-cosmos``
unchanged; nothing here imports an Azure SDK.

Container layout (§3.1): ``tenant`` is partitioned by ``/accountId`` (= ``course_account_id``) and
holds ``account``, ``rule``, ``row``, ``slot`` / ``ruleday`` pointer, ``booking`` (ownership
ledger) and ``snapshot`` docs, so every state change of one account is ONE transactional batch.
``global`` is partitioned by a prefixed ``/pk`` and holds ``user``, ``claim``, ``probe`` and
``audit`` docs; only ``probe`` and ``audit`` carry a per-item ``ttl`` (§10.2).

Encoding rules: field names are camelCase (the §3.2 index policy names ``/accountId``,
``/courseId``, ``/targetDate``, ``/status``, ``/cutoffAt``, ``/userId``); instants are ISO-8601
in UTC (tz-aware in, tz-aware out — a naive datetime is a ``DocumentError``, never a silent
guess); dates and times are ISO strings; enums are their string values; UUIDs are their canonical
string form. Every document carries ``type`` (the discriminator) and ``schemaVersion``; readers
accept ``READABLE_SCHEMA_VERSIONS`` (N and N-1, expand/contract §10.2) and refuse anything else.
The Cosmos system property ``_etag`` is never WRITTEN (``to_*_doc`` omits it) and is READ into
``Stored.etag`` for the store's IfMatch replaces.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass, fields
from datetime import UTC, date, datetime, time
from enum import Enum, StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from ...core.models import CourseId
from ..models import (
    AccountProvenance,
    AccountStatus,
    BookingSource,
    BookingState,
    CourseAccount,
    CourseAccountId,
    OwnedBooking,
    RequestRow,
    ReservationSnapshot,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    SnapshotEntry,
    StandingRule,
    User,
    UserId,
    UserRole,
    UserStatus,
    rule_row_id,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

# Writers write this; readers accept it and its predecessor (expand/contract, §10.2).
SCHEMA_VERSION = 1
READABLE_SCHEMA_VERSIONS: frozenset[int] = frozenset(
    v for v in (SCHEMA_VERSION - 1, SCHEMA_VERSION) if v >= 1
)

TENANT_CONTAINER = "tenant"
GLOBAL_CONTAINER = "global"
TENANT_PARTITION_KEY_PATH = "/accountId"
GLOBAL_PARTITION_KEY_PATH = "/pk"
# ``global`` partition-key prefixes (§3.1): ``user:<userId>``, ``claim:<sha256>``,
# ``probe:<bucket>``, ``audit:<userId>``. Each doc type owns one prefix, so two types can never
# share a logical partition, let alone an id.
GLOBAL_PK_PREFIXES: Mapping[str, str] = {
    "user": "user:",
    "claim": "claim:",
    "probe": "probe:",
    "audit": "audit:",
}
# Per-item TTLs (§3.1/§10.2): the ``global`` container default is -1 (on, no default), so ONLY
# these two document types expire.
PROBE_TTL_S = 2 * 3_600
AUDIT_TTL_S = 400 * 86_400
# The ``tenant-ci`` / ``global-ci`` containers carry a CONTAINER default TTL (Bicep-owned,
# §10.2) that sweeps what a crashed integration run left behind. It is not a per-item field:
# documents written there inherit it by carrying no ``ttl`` of their own.
CI_CONTAINER_DEFAULT_TTL_S = 7 * 86_400
_TENANT_DOC_TYPES = frozenset({"account", "rule", "row", "slot", "ruleday", "booking", "snapshot"})
_GLOBAL_DOC_TYPES = frozenset(GLOBAL_PK_PREFIXES)

# Cosmos forbids these in ``id`` (and caps it at 255 characters).
_FORBIDDEN_ID_CHARS = frozenset("/\\?#")
_MAX_ID_LEN = 255
_SUNDAY = 6  # date.weekday(): Mon=0 .. Sun=6


class DocumentError(ValueError):
    """A document cannot be produced from, or read back into, a domain object."""


@dataclass(frozen=True, slots=True)
class Stored[T]:
    """A domain object read from Cosmos together with the document's ``_etag`` (None when the
    document had none, e.g. one built locally), for the store's IfMatch replaces (§3.5)."""

    item: T
    etag: str | None


@dataclass(frozen=True, slots=True)
class SlotPointer:
    """The ``slot|<date>`` doc: "one ACTIVE row per (account, date)" (§3.2). A row is active
    iff the slot points at it."""

    account_id: CourseAccountId
    target_date: date
    active_row_id: RowId


@dataclass(frozen=True, slots=True)
class RuleDayPointer:
    """The ``ruleday|<weekday>`` doc: "one ACTIVE rule per (account, weekday)" (§3.2)."""

    account_id: CourseAccountId
    weekday: int  # Python date.weekday(): Mon=0 .. Sun=6
    active_rule_id: RuleId


# --- codecs ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Codec:
    encode: Callable[[object], object]
    decode: Callable[[object], object]


def _expect[T](kind: type[T], value: object) -> T:
    # bool is an int subclass; an int is never an acceptable bool and vice versa.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise DocumentError(f"expected {kind.__name__}, got {type(value).__name__}")
    return value


def _scalar[T](kind: type[T]) -> _Codec:
    return _Codec(encode=lambda v: _expect(kind, v), decode=lambda v: _expect(kind, v))


def _encode_uuid(value: object) -> object:
    return str(_expect(UUID, value))


def _decode_uuid(value: object) -> object:
    return UUID(_expect(str, value))


def _encode_date(value: object) -> object:
    if isinstance(value, datetime):  # datetime is a date subclass; refuse the wider type
        raise DocumentError("expected date, got datetime")
    return _expect(date, value).isoformat()


def _decode_date(value: object) -> object:
    return date.fromisoformat(_expect(str, value))


def _encode_time(value: object) -> object:
    return _expect(time, value).isoformat()


def _decode_time(value: object) -> object:
    return time.fromisoformat(_expect(str, value))


def _encode_datetime(value: object) -> object:
    dt = _expect(datetime, value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise DocumentError("naive datetime; instants must be tz-aware")
    return dt.astimezone(UTC).isoformat()


def _decode_datetime(value: object) -> object:
    dt = datetime.fromisoformat(_expect(str, value))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise DocumentError("naive datetime in document; instants must carry an offset")
    return dt.astimezone(UTC)


def _enum[E: Enum](kind: type[E]) -> _Codec:
    def decode(value: object) -> object:
        try:
            return kind(_expect(str, value))
        except ValueError as exc:
            raise DocumentError(f"{value!r} is not a {kind.__name__}") from exc

    return _Codec(encode=lambda v: _expect(kind, v).value, decode=decode)


def _optional(inner: _Codec) -> _Codec:
    return _Codec(
        encode=lambda v: None if v is None else inner.encode(v),
        decode=lambda v: None if v is None else inner.decode(v),
    )


_STR = _scalar(str)
_INT = _scalar(int)
_BOOL = _scalar(bool)
_UUID = _Codec(encode=_encode_uuid, decode=_decode_uuid)
_DATE = _Codec(encode=_encode_date, decode=_decode_date)
_TIME = _Codec(encode=_encode_time, decode=_decode_time)
_DATETIME = _Codec(encode=_encode_datetime, decode=_decode_datetime)


@dataclass(frozen=True, slots=True)
class _Field:
    attr: str  # dataclass attribute
    key: str  # document key (camelCase)
    codec: _Codec


def _encode_fields(obj: object, spec: tuple[_Field, ...]) -> dict[str, object]:
    out: dict[str, object] = {}
    for f in spec:
        try:
            out[f.key] = f.codec.encode(getattr(obj, f.attr))
        except (DocumentError, TypeError, ValueError) as exc:
            raise DocumentError(f"{type(obj).__name__}.{f.attr}: {exc}") from exc
    return out


def _decode_fields[T: DataclassInstance](
    doc: Mapping[str, object], cls: type[T], spec: tuple[_Field, ...]
) -> T:
    # A key absent from the document takes the dataclass default when there is one (a document
    # written before that field existed, N-1 §10.2); a required field's absence is an error.
    defaults = {f.name: f.default for f in fields(cls) if f.default is not MISSING}
    kwargs: dict[str, object] = {}
    for f in spec:
        if f.key not in doc:
            if f.attr in defaults:
                kwargs[f.attr] = defaults[f.attr]
                continue
            raise DocumentError(f"{cls.__name__} document is missing required field {f.key!r}")
        try:
            kwargs[f.attr] = f.codec.decode(doc[f.key])
        except (DocumentError, TypeError, ValueError) as exc:
            raise DocumentError(f"{cls.__name__} document field {f.key!r}: {exc}") from exc
    return cls(**kwargs)


# --- envelope ----------------------------------------------------------------------------------


def _checked_id(doc_id: str) -> str:
    bad = _FORBIDDEN_ID_CHARS.intersection(doc_id)
    if bad:
        raise DocumentError(f"document id {doc_id!r} contains forbidden character(s) {sorted(bad)}")
    if not doc_id or len(doc_id) > _MAX_ID_LEN:
        raise DocumentError(f"document id must be 1..{_MAX_ID_LEN} characters: {doc_id!r}")
    return doc_id


def _tenant_envelope(*, doc_id: str, account_id: UUID, doc_type: str) -> dict[str, object]:
    return {
        "id": _checked_id(doc_id),
        "accountId": str(account_id),
        "type": doc_type,
        "schemaVersion": SCHEMA_VERSION,
    }


def _open(doc: Mapping[str, object], *, doc_type: str) -> None:
    """Validate the discriminator and schema version before decoding anything."""
    found = doc.get("type")
    if found != doc_type:
        raise DocumentError(f"expected a {doc_type!r} document, got type={found!r}")
    version = doc.get("schemaVersion")
    if not isinstance(version, int) or version not in READABLE_SCHEMA_VERSIONS:
        raise DocumentError(
            f"unreadable schemaVersion {version!r} (readable: {sorted(READABLE_SCHEMA_VERSIONS)})"
        )


def _etag_of(doc: Mapping[str, object]) -> str | None:
    etag = doc.get("_etag")
    if etag is None:
        return None
    return _expect(str, etag)


def _check_identity(doc: Mapping[str, object], *, doc_id: str, account_id: UUID) -> None:
    """The deterministic id and the partition key are DERIVED from the fields; a document whose
    stored id or accountId disagrees with its body is corrupt and must not be trusted."""
    if doc.get("id") != doc_id:
        raise DocumentError(f"document id {doc.get('id')!r} does not match its body ({doc_id!r})")
    if doc.get("accountId") != str(account_id):
        raise DocumentError("document accountId does not match its body")


# --- rows --------------------------------------------------------------------------------------

_ROW_FIELDS: tuple[_Field, ...] = (
    _Field("id", "rowId", _UUID),
    _Field("course_account_id", "accountId", _UUID),
    _Field("course_id", "courseId", _STR),
    _Field("target_date", "targetDate", _DATE),
    _Field("timezone", "timezone", _STR),
    _Field("window_earliest", "windowEarliest", _TIME),
    _Field("window_latest", "windowLatest", _TIME),
    _Field("party_size", "partySize", _INT),
    _Field("status", "status", _enum(RowStatus)),
    _Field("source", "source", _enum(RowSource)),
    _Field("cutoff_at", "cutoffAt", _DATETIME),
    _Field("request_id", "requestId", _UUID),
    _Field("version", "version", _INT),
    _Field("rule_id", "ruleId", _optional(_UUID)),
    _Field("status_reason", "statusReason", _optional(_STR)),
    _Field("booked_tee_time", "bookedTeeTime", _optional(_DATETIME)),
    _Field("booked_confirmation", "bookedConfirmation", _optional(_STR)),
    _Field("booked_raw_id", "bookedRawId", _optional(_STR)),
    _Field("booked_at", "bookedAt", _optional(_DATETIME)),
    _Field("needs_reconcile", "needsReconcile", _BOOL),
    _Field("upgrade_started_at", "upgradeStartedAt", _optional(_DATETIME)),
    _Field("superseded_from", "supersededFrom", _optional(_enum(RowStatus))),
    _Field("lease_owner", "leaseOwner", _optional(_STR)),
    _Field("lease_expires_at", "leaseExpiresAt", _optional(_DATETIME)),
    _Field("last_outcome", "lastOutcome", _optional(_STR)),
    _Field("last_outcome_at", "lastOutcomeAt", _optional(_DATETIME)),
    _Field("group_id", "groupId", _optional(_UUID)),
    _Field("group_rank", "groupRank", _optional(_INT)),
)


def row_doc_id(row: RequestRow) -> str:
    """``row|rule|<rule_id>|<date>`` for a rule row (UNIQUE(rule_id, date) IS the id, §3.1), or
    ``row|x|<uuid>`` for an explicit row. A rule row whose ``id`` is not ``rule_row_id(rule_id,
    date)`` is refused: the id-uniqueness guarantee would be false for it."""
    if row.source is RowSource.RULE:
        if row.rule_id is None:
            raise DocumentError(f"rule row {row.id} has no rule_id")
        if row.id != rule_row_id(row.rule_id, row.target_date):
            raise DocumentError(
                f"rule row {row.id} is not rule_row_id({row.rule_id}, {row.target_date})"
            )
        return _checked_id(f"row|rule|{row.rule_id}|{row.target_date.isoformat()}")
    return _checked_id(f"row|x|{row.id}")


def to_row_doc(row: RequestRow) -> dict[str, object]:
    envelope = _tenant_envelope(
        doc_id=row_doc_id(row), account_id=row.course_account_id, doc_type="row"
    )
    return envelope | _encode_fields(row, _ROW_FIELDS)


def from_row_doc(doc: Mapping[str, object]) -> Stored[RequestRow]:
    _open(doc, doc_type="row")
    row = _decode_fields(doc, RequestRow, _ROW_FIELDS)
    _check_identity(doc, doc_id=row_doc_id(row), account_id=row.course_account_id)
    return Stored(row, _etag_of(doc))


# --- rules -------------------------------------------------------------------------------------

_RULE_FIELDS: tuple[_Field, ...] = (
    _Field("id", "ruleId", _UUID),
    _Field("course_account_id", "accountId", _UUID),
    _Field("weekday", "weekday", _INT),
    _Field("window_earliest", "windowEarliest", _TIME),
    _Field("window_latest", "windowLatest", _TIME),
    _Field("party_size", "partySize", _INT),
    _Field("active", "active", _BOOL),
    _Field("materialized_through", "materializedThrough", _optional(_DATE)),
    _Field("version", "version", _INT),
)


def rule_doc_id(rule: StandingRule) -> str:
    """``rule|<rule_id>`` (§3.1 lists no rule id; this is the MU-8a choice)."""
    return _checked_id(f"rule|{rule.id}")


def to_rule_doc(rule: StandingRule) -> dict[str, object]:
    envelope = _tenant_envelope(
        doc_id=rule_doc_id(rule), account_id=rule.course_account_id, doc_type="rule"
    )
    return envelope | _encode_fields(rule, _RULE_FIELDS)


def from_rule_doc(doc: Mapping[str, object]) -> Stored[StandingRule]:
    _open(doc, doc_type="rule")
    rule = _decode_fields(doc, StandingRule, _RULE_FIELDS)
    _check_identity(doc, doc_id=rule_doc_id(rule), account_id=rule.course_account_id)
    return Stored(rule, _etag_of(doc))


# --- accounts ----------------------------------------------------------------------------------

_ACCOUNT_FIELDS: tuple[_Field, ...] = (
    _Field("id", "accountId", _UUID),
    _Field("user_id", "userId", _UUID),
    _Field("course_id", "courseId", _STR),
    _Field("provenance", "provenance", _enum(AccountProvenance)),
    _Field("username", "username", _STR),
    _Field("password_ciphertext", "passwordCiphertext", _STR),
    _Field("key_id", "keyId", _STR),
    _Field("status", "status", _enum(AccountStatus)),
    _Field("otp_mailbox", "otpMailbox", _optional(_STR)),
    _Field("consecutive_soft_auth_failures", "consecutiveSoftAuthFailures", _INT),
    _Field("verified_at", "verifiedAt", _optional(_DATETIME)),
)


def account_doc_id(account: CourseAccount) -> str:
    """``account``: the partition IS the account (``accountId = uuid5(user_id, course_id)``,
    §3.1), so the id is a per-partition singleton."""
    del account
    return "account"


def to_account_doc(account: CourseAccount) -> dict[str, object]:
    envelope = _tenant_envelope(
        doc_id=account_doc_id(account), account_id=account.id, doc_type="account"
    )
    return envelope | _encode_fields(account, _ACCOUNT_FIELDS)


def from_account_doc(doc: Mapping[str, object]) -> Stored[CourseAccount]:
    _open(doc, doc_type="account")
    account = _decode_fields(doc, CourseAccount, _ACCOUNT_FIELDS)
    _check_identity(doc, doc_id=account_doc_id(account), account_id=account.id)
    return Stored(account, _etag_of(doc))


# --- pointer docs ------------------------------------------------------------------------------

_SLOT_FIELDS: tuple[_Field, ...] = (
    _Field("account_id", "accountId", _UUID),
    _Field("target_date", "targetDate", _DATE),
    _Field("active_row_id", "activeRowId", _UUID),
)
_RULEDAY_FIELDS: tuple[_Field, ...] = (
    _Field("account_id", "accountId", _UUID),
    _Field("weekday", "weekday", _INT),
    _Field("active_rule_id", "activeRuleId", _UUID),
)


def slot_doc_id(slot: SlotPointer) -> str:
    """``slot|<date>`` (§3.1)."""
    return _checked_id(f"slot|{slot.target_date.isoformat()}")


def to_slot_doc(slot: SlotPointer) -> dict[str, object]:
    envelope = _tenant_envelope(
        doc_id=slot_doc_id(slot), account_id=slot.account_id, doc_type="slot"
    )
    return envelope | _encode_fields(slot, _SLOT_FIELDS)


def from_slot_doc(doc: Mapping[str, object]) -> Stored[SlotPointer]:
    _open(doc, doc_type="slot")
    slot = _decode_fields(doc, SlotPointer, _SLOT_FIELDS)
    _check_identity(doc, doc_id=slot_doc_id(slot), account_id=slot.account_id)
    return Stored(slot, _etag_of(doc))


def ruleday_doc_id(pointer: RuleDayPointer) -> str:
    """``ruleday|<weekday>`` (§3.1), weekday in ``date.weekday()`` terms (0..6)."""
    if not 0 <= pointer.weekday <= _SUNDAY:
        raise DocumentError(f"weekday must be 0..{_SUNDAY}, got {pointer.weekday}")
    return _checked_id(f"ruleday|{pointer.weekday}")


def to_ruleday_doc(pointer: RuleDayPointer) -> dict[str, object]:
    envelope = _tenant_envelope(
        doc_id=ruleday_doc_id(pointer), account_id=pointer.account_id, doc_type="ruleday"
    )
    return envelope | _encode_fields(pointer, _RULEDAY_FIELDS)


def from_ruleday_doc(doc: Mapping[str, object]) -> Stored[RuleDayPointer]:
    _open(doc, doc_type="ruleday")
    pointer = _decode_fields(doc, RuleDayPointer, _RULEDAY_FIELDS)
    _check_identity(doc, doc_id=ruleday_doc_id(pointer), account_id=pointer.account_id)
    return Stored(pointer, _etag_of(doc))


# --- ownership ledger --------------------------------------------------------------------------

_BOOKING_FIELDS: tuple[_Field, ...] = (
    _Field("id", "bookingId", _UUID),
    _Field("row_id", "rowId", _UUID),
    _Field("course_account_id", "accountId", _UUID),
    _Field("course_id", "courseId", _STR),
    _Field("target_date", "targetDate", _DATE),
    _Field("raw_reservation_id", "rawReservationId", _STR),
    _Field("tee_time", "teeTime", _DATETIME),
    _Field("party_size", "partySize", _INT),
    _Field("source", "source", _enum(BookingSource)),
    _Field("state", "state", _enum(BookingState)),
)


def booking_doc_id(booking: OwnedBooking) -> str:
    """``booking|<course_id>|<raw_id>``: UNIQUE(course, raw id) within the account (§3.1)."""
    return _checked_id(f"booking|{booking.course_id}|{booking.raw_reservation_id}")


def to_booking_doc(booking: OwnedBooking) -> dict[str, object]:
    envelope = _tenant_envelope(
        doc_id=booking_doc_id(booking), account_id=booking.course_account_id, doc_type="booking"
    )
    return envelope | _encode_fields(booking, _BOOKING_FIELDS)


def from_booking_doc(doc: Mapping[str, object]) -> Stored[OwnedBooking]:
    _open(doc, doc_type="booking")
    booking = _decode_fields(doc, OwnedBooking, _BOOKING_FIELDS)
    _check_identity(doc, doc_id=booking_doc_id(booking), account_id=booking.course_account_id)
    return Stored(booking, _etag_of(doc))


# --- snapshots ---------------------------------------------------------------------------------

_ENTRY_FIELDS: tuple[_Field, ...] = (
    _Field("raw_id", "rawId", _STR),
    _Field("tee_time", "teeTime", _DATETIME),
    _Field("party_size", "partySize", _INT),
)


def _encode_entries(value: object) -> object:
    return [_encode_fields(_expect(SnapshotEntry, e), _ENTRY_FIELDS) for e in _expect(tuple, value)]


def _decode_entries(value: object) -> object:
    return tuple(
        _decode_fields(_expect(dict, e), SnapshotEntry, _ENTRY_FIELDS) for e in _expect(list, value)
    )


_SNAPSHOT_FIELDS: tuple[_Field, ...] = (
    _Field("course_account_id", "accountId", _UUID),
    _Field("observed_at", "observedAt", _DATETIME),
    _Field("source", "source", _STR),
    _Field("trusted", "trusted", _BOOL),
    _Field("entries", "entries", _Codec(encode=_encode_entries, decode=_decode_entries)),
)


def snapshot_doc_id(snapshot: ReservationSnapshot) -> str:
    """``snapshot``: one latest per account (§3.1), a per-partition singleton."""
    del snapshot
    return "snapshot"


def to_snapshot_doc(snapshot: ReservationSnapshot) -> dict[str, object]:
    envelope = _tenant_envelope(
        doc_id=snapshot_doc_id(snapshot),
        account_id=snapshot.course_account_id,
        doc_type="snapshot",
    )
    return envelope | _encode_fields(snapshot, _SNAPSHOT_FIELDS)


def from_snapshot_doc(doc: Mapping[str, object]) -> Stored[ReservationSnapshot]:
    _open(doc, doc_type="snapshot")
    snapshot = _decode_fields(doc, ReservationSnapshot, _SNAPSHOT_FIELDS)
    _check_identity(doc, doc_id=snapshot_doc_id(snapshot), account_id=snapshot.course_account_id)
    return Stored(snapshot, _etag_of(doc))


# =============================================================================================
# The ``global`` container (partition key ``/pk``, prefixed per type)
# =============================================================================================


def _global_envelope(
    *, doc_id: str, pk: str, doc_type: str, ttl_s: int | None = None
) -> dict[str, object]:
    prefix = GLOBAL_PK_PREFIXES[doc_type]
    if not pk.startswith(prefix):
        raise DocumentError(f"{doc_type} pk {pk!r} must start with {prefix!r}")
    doc: dict[str, object] = {
        "id": _checked_id(doc_id),
        "pk": pk,
        "type": doc_type,
        "schemaVersion": SCHEMA_VERSION,
    }
    if ttl_s is not None:
        doc["ttl"] = ttl_s
    return doc


def _check_global_identity(doc: Mapping[str, object], *, doc_id: str, pk: str) -> None:
    if doc.get("id") != doc_id:
        raise DocumentError(f"document id {doc.get('id')!r} does not match its body ({doc_id!r})")
    if doc.get("pk") != pk:
        raise DocumentError("document pk does not match its body")


# --- users -------------------------------------------------------------------------------------

_USER_FIELDS: tuple[_Field, ...] = (
    _Field("id", "userId", _UUID),
    _Field("oauth_provider", "oauthProvider", _STR),
    _Field("oauth_subject", "oauthSubject", _optional(_STR)),
    _Field("email", "email", _STR),
    _Field("display_name", "displayName", _STR),
    _Field("role", "role", _enum(UserRole)),
    _Field("status", "status", _enum(UserStatus)),
)


def user_pk(user: User) -> str:
    return f"{GLOBAL_PK_PREFIXES['user']}{user.id}"


def user_doc_id(user: User) -> str:
    """``user:<userId>``: the user is alone in its partition, so id == pk."""
    return _checked_id(user_pk(user))


def to_user_doc(user: User) -> dict[str, object]:
    envelope = _global_envelope(doc_id=user_doc_id(user), pk=user_pk(user), doc_type="user")
    return envelope | _encode_fields(user, _USER_FIELDS)


def from_user_doc(doc: Mapping[str, object]) -> Stored[User]:
    _open(doc, doc_type="user")
    user = _decode_fields(doc, User, _USER_FIELDS)
    _check_global_identity(doc, doc_id=user_doc_id(user), pk=user_pk(user))
    return Stored(user, _etag_of(doc))


# --- uniqueness claims (§3.2) ------------------------------------------------------------------


class ClaimKind(StrEnum):
    """The cross-partition uniqueness keys held as claim docs (§3.1)."""

    IDENTITY = "identity"  # UNIQUE(provider, subject)
    INVITE = "invite"  # a verified-email invite, matched case-insensitively
    USERNAME = "username"  # UNIQUE(course, username)
    COURSE_COUNT = "course-count"  # the ``max_accounts_per_course`` IfMatch counter


class ClaimState(StrEnum):
    PENDING = "pending"
    BOUND = "bound"


@dataclass(frozen=True, slots=True)
class UniquenessClaim:
    """A ``claim`` doc. ``key_hash`` is ``claim_key_hash(kind, key)``: the raw key (an email, a
    course login) is NOT stored, the SHA-256 is the id. ``owner_id`` is the accountId (username)
    or userId (identity, invite) the claim is for; None for a counter. ``count`` is the
    ``course-count`` value (0 otherwise). The pending -> bound protocol is §3.2 (round-2 SF5)."""

    kind: ClaimKind
    key_hash: str
    state: ClaimState
    created_at: datetime
    owner_id: UUID | None = None
    count: int = 0


def claim_key_hash(kind: ClaimKind, key: str) -> str:
    """SHA-256 hex of ``<kind>|<key>``: the kind is hashed in so the same key text under two
    kinds can never share an id."""
    return hashlib.sha256(f"{kind.value}|{key}".encode()).hexdigest()


def identity_claim_key(provider: str, subject: str) -> str:
    """``<provider>|<subject>``: OAuth subjects are opaque and case-sensitive, kept verbatim."""
    return f"{provider}|{subject}"


def invite_claim_key(email: str) -> str:
    """Casefolded + stripped, matching ``bind_invited_user``'s case-insensitive email match."""
    return email.strip().casefold()


def username_claim_key(course_id: CourseId, username: str) -> str:
    """``<course_id>|<casefolded username>``: UNIQUE(course, username) is case-insensitive in
    the in-memory reference (``upsert_account`` casefolds)."""
    return f"{course_id}|{username.casefold()}"


def course_count_claim_key(course_id: CourseId) -> str:
    return str(course_id)


_CLAIM_FIELDS: tuple[_Field, ...] = (
    _Field("kind", "kind", _enum(ClaimKind)),
    _Field("key_hash", "keyHash", _STR),
    _Field("state", "state", _enum(ClaimState)),
    _Field("created_at", "createdAt", _DATETIME),
    _Field("owner_id", "ownerId", _optional(_UUID)),
    _Field("count", "count", _INT),
)


def claim_pk(claim: UniquenessClaim) -> str:
    return f"{GLOBAL_PK_PREFIXES['claim']}{claim.key_hash}"


def claim_doc_id(claim: UniquenessClaim) -> str:
    """``claim:<sha256>`` (§3.1): one claim per partition, so id == pk and a second create of
    the same key is a 409."""
    return _checked_id(claim_pk(claim))


def to_claim_doc(claim: UniquenessClaim) -> dict[str, object]:
    envelope = _global_envelope(doc_id=claim_doc_id(claim), pk=claim_pk(claim), doc_type="claim")
    return envelope | _encode_fields(claim, _CLAIM_FIELDS)


def from_claim_doc(doc: Mapping[str, object]) -> Stored[UniquenessClaim]:
    _open(doc, doc_type="claim")
    claim = _decode_fields(doc, UniquenessClaim, _CLAIM_FIELDS)
    _check_global_identity(doc, doc_id=claim_doc_id(claim), pk=claim_pk(claim))
    return Stored(claim, _etag_of(doc))


# --- login probes (TTL 2 h) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginProbe:
    """One ``record_login_probe`` call (the in-memory store's ``_Probe`` plus an id)."""

    id: UUID
    user_id: UserId
    course_id: CourseId
    username_hash: str
    ok: bool
    at: datetime


_PROBE_FIELDS: tuple[_Field, ...] = (
    _Field("id", "probeId", _UUID),
    _Field("user_id", "userId", _UUID),
    _Field("course_id", "courseId", _STR),
    _Field("username_hash", "usernameHash", _STR),
    _Field("ok", "ok", _BOOL),
    _Field("at", "at", _DATETIME),
)


def probe_bucket(at: datetime) -> str:
    """The UTC hour the probe landed in, ``YYYY-MM-DDTHH``. ``count_login_probes`` filters by
    ``since`` with EITHER ``user_id`` or ``username_hash`` (or both), so no single id-keyed
    partition serves both; an hour bucket keeps the 2 h TTL window to at most three partitions
    for either filter. MU-8a choice (§3.1 says only ``probe:<bucket>``)."""
    return _expect(datetime, at).astimezone(UTC).strftime("%Y-%m-%dT%H")


def probe_pk(probe: LoginProbe) -> str:
    return f"{GLOBAL_PK_PREFIXES['probe']}{probe_bucket(probe.at)}"


def probe_doc_id(probe: LoginProbe) -> str:
    """``probe|<at UTC>|<uuid>``: time-sortable within the bucket, unique by the uuid."""
    return _checked_id(f"probe|{_encode_datetime(probe.at)}|{probe.id}")


def to_probe_doc(probe: LoginProbe) -> dict[str, object]:
    envelope = _global_envelope(
        doc_id=probe_doc_id(probe), pk=probe_pk(probe), doc_type="probe", ttl_s=PROBE_TTL_S
    )
    return envelope | _encode_fields(probe, _PROBE_FIELDS)


def from_probe_doc(doc: Mapping[str, object]) -> Stored[LoginProbe]:
    _open(doc, doc_type="probe")
    probe = _decode_fields(doc, LoginProbe, _PROBE_FIELDS)
    _check_global_identity(doc, doc_id=probe_doc_id(probe), pk=probe_pk(probe))
    return Stored(probe, _etag_of(doc))


# --- audit log (TTL 400 days) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One ``append_audit`` call (the in-memory store's ``AuditEntry`` plus an id). ``detail``
    must already be redacted (``core.redaction.redact_payload``) and JSON-plain; the mapping
    passes it through untouched."""

    id: UUID
    user_id: UserId | None
    action: str
    row_id: RowId | None
    detail: Mapping[str, object]
    at: datetime


def _encode_detail(value: object) -> object:
    return dict(_expect(dict, value))


_DETAIL = _Codec(encode=_encode_detail, decode=_encode_detail)
_AUDIT_FIELDS: tuple[_Field, ...] = (
    _Field("id", "auditId", _UUID),
    _Field("user_id", "userId", _optional(_UUID)),
    _Field("action", "action", _STR),
    _Field("row_id", "rowId", _optional(_UUID)),
    _Field("detail", "detail", _DETAIL),
    _Field("at", "at", _DATETIME),
)
_SYSTEM_AUDIT_ACTOR = "system"


def audit_pk(audit: AuditRecord) -> str:
    """``audit:<userId>`` (§3.1), or ``audit:system`` for an entry with no user."""
    actor = _SYSTEM_AUDIT_ACTOR if audit.user_id is None else str(audit.user_id)
    return f"{GLOBAL_PK_PREFIXES['audit']}{actor}"


def audit_doc_id(audit: AuditRecord) -> str:
    """``audit|<at UTC>|<uuid>``: time-sortable within the user's partition."""
    return _checked_id(f"audit|{_encode_datetime(audit.at)}|{audit.id}")


def to_audit_doc(audit: AuditRecord) -> dict[str, object]:
    envelope = _global_envelope(
        doc_id=audit_doc_id(audit), pk=audit_pk(audit), doc_type="audit", ttl_s=AUDIT_TTL_S
    )
    return envelope | _encode_fields(audit, _AUDIT_FIELDS)


def from_audit_doc(doc: Mapping[str, object]) -> Stored[AuditRecord]:
    _open(doc, doc_type="audit")
    audit = _decode_fields(doc, AuditRecord, _AUDIT_FIELDS)
    _check_global_identity(doc, doc_id=audit_doc_id(audit), pk=audit_pk(audit))
    return Stored(audit, _etag_of(doc))


# --- routing -----------------------------------------------------------------------------------


def container_of(doc: Mapping[str, object]) -> str:
    """Which container a document belongs in, by its ``type``."""
    doc_type = doc.get("type")
    if doc_type in _TENANT_DOC_TYPES:
        return TENANT_CONTAINER
    if doc_type in _GLOBAL_DOC_TYPES:
        return GLOBAL_CONTAINER
    raise DocumentError(f"unknown document type {doc_type!r}")


def partition_key_of(doc: Mapping[str, object]) -> str:
    """The partition-key VALUE the store passes alongside the document (``accountId`` for the
    ``tenant`` container, ``pk`` for ``global``)."""
    key = "accountId" if container_of(doc) == TENANT_CONTAINER else "pk"
    try:
        return _expect(str, doc[key])
    except KeyError as exc:
        raise DocumentError(f"document has no {key!r}") from exc
