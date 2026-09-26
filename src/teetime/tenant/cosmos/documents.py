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

from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass, fields
from datetime import UTC, date, datetime, time
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

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
# The ``tenant-ci`` / ``global-ci`` containers carry a CONTAINER default TTL (Bicep-owned,
# §10.2) that sweeps what a crashed integration run left behind. It is not a per-item field:
# documents written there inherit it by carrying no ``ttl`` of their own.
CI_CONTAINER_DEFAULT_TTL_S = 7 * 86_400

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
