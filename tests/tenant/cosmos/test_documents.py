"""MU-8a: the Cosmos document mapping (MULTIUSER_PLAN §3.1/§3.2/§10.2).

Pure ``to_*_doc`` / ``from_*_doc`` per persisted type, deterministic document ids, partition keys,
the ``type`` discriminator, ``schemaVersion``, per-item TTLs and ``_etag`` pass-through. No SDK is
involved: the store (MU-8b) hands these dicts to ``azure-cosmos`` unchanged.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import pytest

from teetime.core.models import CourseId
from teetime.tenant.cosmos.documents import (
    AUDIT_TTL_S,
    CI_CONTAINER_DEFAULT_TTL_S,
    GLOBAL_CONTAINER,
    GLOBAL_PK_PREFIXES,
    PROBE_TTL_S,
    SCHEMA_VERSION,
    TENANT_CONTAINER,
    AuditRecord,
    ClaimKind,
    ClaimState,
    DocumentError,
    LoginProbe,
    RuleDayPointer,
    SlotPointer,
    UniquenessClaim,
    account_doc_id,
    audit_doc_id,
    audit_pk,
    booking_doc_id,
    claim_doc_id,
    claim_key_hash,
    container_of,
    course_count_claim_key,
    event_row_from_docs,
    from_account_doc,
    from_audit_doc,
    from_booking_doc,
    from_claim_doc,
    from_doc,
    from_probe_doc,
    from_row_doc,
    from_rule_doc,
    from_ruleday_doc,
    from_slot_doc,
    from_snapshot_doc,
    from_user_doc,
    identity_claim_key,
    invite_claim_key,
    partition_key_of,
    probe_bucket,
    probe_doc_id,
    row_doc_id,
    row_fingerprint_of,
    rule_doc_id,
    ruleday_doc_id,
    slot_doc_id,
    snapshot_doc_id,
    to_account_doc,
    to_audit_doc,
    to_booking_doc,
    to_claim_doc,
    to_doc,
    to_probe_doc,
    to_row_doc,
    to_rule_doc,
    to_ruleday_doc,
    to_slot_doc,
    to_snapshot_doc,
    to_user_doc,
    user_doc_id,
    username_claim_key,
)
from teetime.tenant.models import (
    CANCEL_REASONS,
    SYSTEM_WITHDRAW_REASONS,
    USER_WITHDRAW_REASON,
    AccountProvenance,
    AccountStatus,
    BookingSource,
    BookingState,
    CourseAccount,
    CourseAccountId,
    EventRow,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    ReservationSnapshot,
    RowFingerprint,
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
    derive_account_id,
    row_request_id,
    rule_row_id,
)

MB = CourseId("foreup:19671:2149")
ET = ZoneInfo("America/New_York")
TZ = "America/New_York"
USER = UserId(UUID("11111111-1111-4111-8111-111111111111"))
ACCOUNT = derive_account_id(USER, MB)
RULE = RuleId(UUID("22222222-2222-4222-8222-222222222222"))
# The US DST end (2026-11-01, 01:30 is ambiguous) and start (2026-03-08, 02:30 does not exist).
DST_END_DATE = date(2026, 11, 1)
DST_START_DATE = date(2026, 3, 8)
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _rule_row(target: date = DST_END_DATE) -> RequestRow:
    """A rule row with EVERY optional field populated (DST-ambiguous local instants included)."""
    rid = rule_row_id(RULE, target)
    return RequestRow(
        id=rid,
        course_account_id=ACCOUNT,
        course_id=MB,
        target_date=target,
        timezone=TZ,
        window_earliest=time(8, 45),
        window_latest=time(10, 0),
        party_size=2,
        status=RowStatus.BOOKED,
        source=RowSource.RULE,
        cutoff_at=datetime.combine(target - timedelta(days=1), time(16, 0), tzinfo=ET),
        request_id=row_request_id(rid),
        version=3,
        rule_id=RULE,
        status_reason="external",
        booked_tee_time=datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=1),
        booked_confirmation="TTB:12345",
        booked_raw_id="12345",
        booked_at=datetime(2026, 10, 25, 10, 0, 0, 123456, tzinfo=UTC),
        needs_reconcile=True,
        upgrade_started_at=datetime(2026, 10, 26, 11, 0, tzinfo=UTC),
        superseded_from=RowStatus.SKIPPED,
        lease_owner="watcher-abc",
        lease_expires_at=datetime(2026, 10, 26, 11, 5, tzinfo=UTC),
        last_outcome="booked",
        last_outcome_at=datetime(2026, 10, 25, 10, 0, 1, tzinfo=UTC),
        group_id=UUID("33333333-3333-4333-8333-333333333333"),
        group_rank=1,
        max_price=Decimal("85.50"),
    )


def _explicit_row(target: date = DST_START_DATE) -> RequestRow:
    """An explicit row with every optional field at its default (None / False)."""
    rid = RowId(UUID("44444444-4444-4444-8444-444444444444"))
    return RequestRow(
        id=rid,
        course_account_id=ACCOUNT,
        course_id=MB,
        target_date=target,
        timezone=TZ,
        window_earliest=time(7, 0),
        window_latest=time(12, 0),
        party_size=4,
        status=RowStatus.PENDING,
        source=RowSource.EXPLICIT,
        cutoff_at=datetime.combine(target - timedelta(days=1), time(16, 0), tzinfo=ET),
        request_id=row_request_id(rid),
        version=1,
    )


def _account() -> CourseAccount:
    return CourseAccount(
        id=ACCOUNT,
        user_id=USER,
        course_id=MB,
        provenance=AccountProvenance.USER_SUPPLIED,
        username="golfer@example.com",
        password_ciphertext="v1:kid1:bm9uY2U=:Y2lwaGVy",
        key_id="kid1",
        status=AccountStatus.AUTH_FAILED,
        otp_mailbox="otp+golfer@example.com",
        consecutive_soft_auth_failures=2,
        verified_at=datetime(2026, 9, 1, 15, 30, tzinfo=ET),
        default_max_price=Decimal("120.00"),
    )


def _rule() -> StandingRule:
    return StandingRule(
        id=RULE,
        course_account_id=ACCOUNT,
        weekday=5,
        window_earliest=time(8, 45),
        window_latest=time(10, 0),
        party_size=2,
        active=True,
        materialized_through=date(2026, 10, 17),
        version=4,
        max_price=Decimal("75"),
        group_id=UUID("55555555-5555-4555-8555-555555555555"),
        group_rank=2,
    )


def _booking() -> OwnedBooking:
    return OwnedBooking(
        id=OwnedBookingId(UUID("55555555-5555-4555-8555-555555555555")),
        row_id=_rule_row().id,
        course_account_id=ACCOUNT,
        course_id=MB,
        target_date=DST_END_DATE,
        raw_reservation_id="12345",
        tee_time=datetime(2026, 11, 1, 9, 22, tzinfo=ET),
        party_size=2,
        source=BookingSource.BLIND,
        state=BookingState.HELD,
    )


def _snapshot() -> ReservationSnapshot:
    return ReservationSnapshot(
        course_account_id=ACCOUNT,
        observed_at=NOW,
        source="watcher",
        trusted=False,
        entries=(
            SnapshotEntry(
                raw_id="R1", tee_time=datetime(2026, 10, 3, 9, 22, tzinfo=ET), party_size=2
            ),
            SnapshotEntry(
                raw_id="R2", tee_time=datetime(2026, 10, 4, 9, 30, tzinfo=ET), party_size=3
            ),
        ),
    )


def _slot() -> SlotPointer:
    return SlotPointer(account_id=ACCOUNT, target_date=DST_END_DATE, active_row_id=_rule_row().id)


def _ruleday() -> RuleDayPointer:
    return RuleDayPointer(account_id=ACCOUNT, weekday=5, active_rule_id=RULE)


# --- round trips: the tenant container ---------------------------------------------------------


class TestRoundtripTenant:
    def test_roundtrip_request_row_all_fields(self) -> None:
        row = _rule_row()
        stored = from_row_doc(to_row_doc(row))
        # ``booked_tee_time`` sits INSIDE the DST fold (01:30 ET on 2026-11-01, fold=1 = the
        # second, EST occurrence). PEP 495 makes an inter-zone ``==`` on such a value always
        # False, so that one field is compared as an instant; everything else by equality.
        assert replace(stored.item, booked_tee_time=None) == replace(row, booked_tee_time=None)
        assert stored.item.booked_tee_time is not None and row.booked_tee_time is not None
        assert stored.item.booked_tee_time.timestamp() == row.booked_tee_time.timestamp()
        # The fold was honoured: fold=1 is EST (-05:00), so the UTC form is 06:30, not 05:30.
        assert stored.item.booked_tee_time == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        # Instants are re-read tz-AWARE (never naive).
        assert stored.item.booked_tee_time.utcoffset() is not None
        assert stored.item.cutoff_at.utcoffset() is not None

    def test_roundtrip_request_row_defaults(self) -> None:
        row = _explicit_row()
        assert from_row_doc(to_row_doc(row)).item == row

    @pytest.mark.parametrize("status", list(RowStatus))
    def test_roundtrip_request_row_every_status(self, status: RowStatus) -> None:
        row = replace(_explicit_row(), status=status, superseded_from=status)
        assert from_row_doc(to_row_doc(row)).item == row

    def test_roundtrip_standing_rule(self) -> None:
        rule = _rule()
        assert from_rule_doc(to_rule_doc(rule)).item == rule
        inactive = replace(rule, active=False, materialized_through=None)
        assert from_rule_doc(to_rule_doc(inactive)).item == inactive

    def test_roundtrip_course_account(self) -> None:
        account = _account()
        assert from_account_doc(to_account_doc(account)).item == account
        bare = replace(account, otp_mailbox=None, verified_at=None)
        assert from_account_doc(to_account_doc(bare)).item == bare

    def test_roundtrip_slot_pointer(self) -> None:
        slot = _slot()
        assert from_slot_doc(to_slot_doc(slot)).item == slot

    def test_roundtrip_ruleday_pointer(self) -> None:
        ptr = RuleDayPointer(account_id=ACCOUNT, weekday=6, active_rule_id=RULE)
        assert from_ruleday_doc(to_ruleday_doc(ptr)).item == ptr

    def test_roundtrip_owned_booking(self) -> None:
        booking = _booking()
        assert from_booking_doc(to_booking_doc(booking)).item == booking

    def test_roundtrip_reservation_snapshot(self) -> None:
        snap = _snapshot()
        assert from_snapshot_doc(to_snapshot_doc(snap)).item == snap
        empty = replace(snap, entries=(), trusted=True)
        assert from_snapshot_doc(to_snapshot_doc(empty)).item == empty

    def test_row_doc_carries_every_model_field(self) -> None:
        """The mapping must persist EVERYTHING the in-memory store persists: a new model field
        that is not mapped would be silently dropped on the way to Cosmos."""
        doc = to_row_doc(_rule_row())
        for f in fields(RequestRow):
            camel = "".join(w.capitalize() if i else w for i, w in enumerate(f.name.split("_")))
            key = (
                "rowId"
                if f.name == "id"
                else "accountId"
                if f.name == "course_account_id"
                else camel
            )
            assert key in doc, f"RequestRow.{f.name} is not mapped (expected doc key {key!r})"


# --- ids + partition keys ----------------------------------------------------------------------


class TestIds:
    def test_ids_are_deterministic_and_unique_within_partition(self) -> None:
        rule_row, explicit = _rule_row(), _explicit_row()
        ids = [
            account_doc_id(_account()),
            rule_doc_id(_rule()),
            row_doc_id(rule_row),
            row_doc_id(explicit),
            slot_doc_id(_slot()),
            ruleday_doc_id(_ruleday()),
            booking_doc_id(_booking()),
            snapshot_doc_id(_snapshot()),
        ]
        assert len(set(ids)) == len(ids), ids
        # Deterministic: the same object always maps to the same id and the same document.
        assert row_doc_id(_rule_row()) == row_doc_id(rule_row)
        assert to_row_doc(rule_row) == to_row_doc(_rule_row())

    def test_rule_row_id_encodes_rule_and_date(self) -> None:
        assert row_doc_id(_rule_row()) == f"row|rule|{RULE}|2026-11-01"

    def test_explicit_row_id_encodes_uuid(self) -> None:
        row = _explicit_row()
        assert row_doc_id(row) == f"row|x|{row.id}"

    def test_rule_row_with_non_derived_id_is_rejected(self) -> None:
        """UNIQUE(rule_id, date) IS the document id, so a rule row whose RowId is not
        ``rule_row_id(rule_id, date)`` would break that guarantee: refuse to map it."""
        row = replace(_rule_row(), id=RowId(uuid4()))
        with pytest.raises(DocumentError, match="rule_row_id"):
            to_row_doc(row)
        with pytest.raises(DocumentError, match="rule"):
            to_row_doc(replace(_rule_row(), rule_id=None))

    def test_slot_doc_id_encodes_date(self) -> None:
        slot = replace(_slot(), target_date=date(2026, 3, 8))
        assert slot_doc_id(slot) == "slot|2026-03-08"

    def test_ruleday_doc_id_encodes_weekday(self) -> None:
        ptr = RuleDayPointer(account_id=ACCOUNT, weekday=6, active_rule_id=RULE)
        assert ruleday_doc_id(ptr) == "ruleday|6"

    def test_booking_id_encodes_course_and_raw_id(self) -> None:
        assert booking_doc_id(_booking()) == f"booking|{MB}|12345"

    def test_singleton_ids_per_partition(self) -> None:
        assert account_doc_id(_account()) == "account"
        assert snapshot_doc_id(_snapshot()) == "snapshot"
        assert rule_doc_id(_rule()) == f"rule|{RULE}"

    def test_ids_reject_cosmos_forbidden_characters(self) -> None:
        """Cosmos ids may not contain ``/``, ``\\``, ``?`` or ``#``; a raw reservation id that
        does would make the write fail far from the cause."""
        with pytest.raises(DocumentError, match="forbidden"):
            booking_doc_id(replace(_booking(), raw_reservation_id="a/b"))
        with pytest.raises(DocumentError, match="forbidden"):
            to_booking_doc(replace(_booking(), raw_reservation_id="x#1"))

    def test_partition_key_is_account_for_tenant_docs(self) -> None:
        docs = [
            to_account_doc(_account()),
            to_rule_doc(_rule()),
            to_row_doc(_rule_row()),
            to_row_doc(_explicit_row()),
            to_slot_doc(_slot()),
            to_ruleday_doc(_ruleday()),
            to_booking_doc(_booking()),
            to_snapshot_doc(_snapshot()),
        ]
        for doc in docs:
            assert doc["accountId"] == str(ACCOUNT), doc["type"]
            assert "pk" not in doc, doc["type"]
        assert TENANT_CONTAINER == "tenant"
        assert GLOBAL_CONTAINER == "global"
        assert CI_CONTAINER_DEFAULT_TTL_S == 7 * 86400
        assert SCHEMA_VERSION == 1
        assert uuid5(USER, str(MB)) == ACCOUNT  # the §3.1 derivation the partition rests on
        assert isinstance(CourseAccountId(ACCOUNT), UUID)


# --- the global container ----------------------------------------------------------------------


def _user() -> User:
    return User(
        id=USER,
        oauth_provider="google",
        oauth_subject="Sub-123",
        email="Golfer@Example.com",
        display_name="Golfer",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


def _claim(kind: ClaimKind = ClaimKind.USERNAME) -> UniquenessClaim:
    return UniquenessClaim(
        kind=kind,
        key_hash=claim_key_hash(kind, username_claim_key(MB, "Golfer@Example.com")),
        state=ClaimState.PENDING,
        created_at=NOW,
        owner_id=ACCOUNT,
        count=0,
    )


def _probe() -> LoginProbe:
    return LoginProbe(
        id=UUID("66666666-6666-4666-8666-666666666666"),
        user_id=USER,
        course_id=MB,
        username_hash="a" * 64,
        ok=False,
        at=datetime(2026, 11, 1, 1, 59, 59, tzinfo=ET, fold=1),  # inside the DST fold
    )


def _audit() -> AuditRecord:
    return AuditRecord(
        id=UUID("77777777-7777-4777-8777-777777777777"),
        user_id=USER,
        action="row.skip",
        row_id=_explicit_row().id,
        detail={"from": "pending", "to": "skipped", "nested": {"n": 1, "flag": True}},
        at=NOW,
    )


class TestRoundtripGlobal:
    def test_roundtrip_user(self) -> None:
        user = _user()
        assert from_user_doc(to_user_doc(user)).item == user
        invited = replace(
            user, oauth_subject=None, status=UserStatus.INVITED, role=UserRole.OPERATOR
        )
        assert from_user_doc(to_user_doc(invited)).item == invited

    @pytest.mark.parametrize("kind", list(ClaimKind))
    def test_roundtrip_uniqueness_claim(self, kind: ClaimKind) -> None:
        claim = _claim(kind)
        assert from_claim_doc(to_claim_doc(claim)).item == claim
        bound = replace(claim, state=ClaimState.BOUND, owner_id=None, count=3)
        assert from_claim_doc(to_claim_doc(bound)).item == bound

    def test_roundtrip_login_probe(self) -> None:
        probe = _probe()
        stored = from_probe_doc(to_probe_doc(probe)).item
        assert replace(stored, at=NOW) == replace(probe, at=NOW)
        assert stored.at == datetime(2026, 11, 1, 6, 59, 59, tzinfo=UTC)  # fold=1 → EST

    def test_roundtrip_audit_record(self) -> None:
        audit = _audit()
        assert from_audit_doc(to_audit_doc(audit)).item == audit
        system = replace(audit, user_id=None, row_id=None, detail={})
        assert from_audit_doc(to_audit_doc(system)).item == system


class TestGlobalKeys:
    def test_global_pk_prefixes_never_collide(self) -> None:
        prefixes = GLOBAL_PK_PREFIXES
        assert set(prefixes) == {"user", "claim", "probe", "audit"}
        values = list(prefixes.values())
        assert len(set(values)) == len(values)
        for a in values:
            for b in values:
                assert a == b or not b.startswith(a), (a, b)
        docs = {
            "user": to_user_doc(_user()),
            "claim": to_claim_doc(_claim()),
            "probe": to_probe_doc(_probe()),
            "audit": to_audit_doc(_audit()),
        }
        for doc_type, doc in docs.items():
            assert doc["type"] == doc_type
            pk = doc["pk"]
            assert isinstance(pk, str) and pk.startswith(prefixes[doc_type]), (doc_type, pk)
            assert "accountId" not in doc

    def test_user_and_claim_ids_are_their_partition_keys(self) -> None:
        user_doc = to_user_doc(_user())
        assert user_doc["id"] == user_doc["pk"] == f"user:{USER}" == user_doc_id(_user())
        claim_doc = to_claim_doc(_claim())
        expected = f"claim:{_claim().key_hash}"
        assert claim_doc["id"] == claim_doc["pk"] == expected == claim_doc_id(_claim())

    def test_probe_partitions_by_utc_hour_bucket(self) -> None:
        probe = _probe()
        assert probe_bucket(probe.at) == "2026-11-01T06"
        doc = to_probe_doc(probe)
        assert doc["pk"] == "probe:2026-11-01T06"
        assert doc["id"] == probe_doc_id(probe) == f"probe|2026-11-01T06:59:59+00:00|{probe.id}"

    def test_audit_partitions_by_user_or_system(self) -> None:
        audit = _audit()
        assert audit_pk(audit) == f"audit:{USER}"
        assert audit_pk(replace(audit, user_id=None)) == "audit:system"
        assert audit_doc_id(audit) == f"audit|{NOW.isoformat()}|{audit.id}"

    def test_ttl_only_on_ttl_types(self) -> None:
        assert PROBE_TTL_S == 2 * 3600
        assert AUDIT_TTL_S == 400 * 86400
        assert to_probe_doc(_probe())["ttl"] == PROBE_TTL_S
        assert to_audit_doc(_audit())["ttl"] == AUDIT_TTL_S
        no_ttl = [
            to_user_doc(_user()),
            to_claim_doc(_claim()),
            to_account_doc(_account()),
            to_rule_doc(_rule()),
            to_row_doc(_rule_row()),
            to_slot_doc(_slot()),
            to_ruleday_doc(_ruleday()),
            to_booking_doc(_booking()),
            to_snapshot_doc(_snapshot()),
        ]
        for doc in no_ttl:
            assert "ttl" not in doc, doc["type"]

    def test_claim_key_hash_is_deterministic_and_canonical(self) -> None:
        h = claim_key_hash(ClaimKind.USERNAME, username_claim_key(MB, "Golfer@Example.com"))
        assert len(h) == 64 and int(h, 16) >= 0
        # UNIQUE(course, username) is case-insensitive (the in-memory store casefolds).
        assert h == claim_key_hash(ClaimKind.USERNAME, username_claim_key(MB, "golfer@example.com"))
        assert h != claim_key_hash(
            ClaimKind.USERNAME, username_claim_key(CourseId("x:1:1"), "golfer@example.com")
        )
        # Invite emails are matched case-insensitively too (bind_invited_user casefolds).
        assert invite_claim_key(" Golfer@Example.com ") == invite_claim_key("golfer@example.com")
        # OAuth subjects are opaque and case-SENSITIVE; providers are namespaced in.
        assert identity_claim_key("google", "Sub-123") != identity_claim_key("google", "sub-123")
        assert identity_claim_key("google", "s") != identity_claim_key("github", "s")
        assert course_count_claim_key(MB) == str(MB)
        # The kind is hashed in, so the same key text under two kinds never collides.
        assert claim_key_hash(ClaimKind.INVITE, "k") != claim_key_hash(ClaimKind.IDENTITY, "k")

    def test_container_and_partition_key_of_every_doc(self) -> None:
        tenant_docs = [
            to_account_doc(_account()),
            to_rule_doc(_rule()),
            to_row_doc(_rule_row()),
            to_slot_doc(_slot()),
            to_ruleday_doc(_ruleday()),
            to_booking_doc(_booking()),
            to_snapshot_doc(_snapshot()),
        ]
        for doc in tenant_docs:
            assert container_of(doc) == TENANT_CONTAINER
            assert partition_key_of(doc) == str(ACCOUNT)
        for doc in [
            to_user_doc(_user()),
            to_claim_doc(_claim()),
            to_probe_doc(_probe()),
            to_audit_doc(_audit()),
        ]:
            assert container_of(doc) == GLOBAL_CONTAINER
            assert partition_key_of(doc) == doc["pk"]
        with pytest.raises(DocumentError):
            container_of({"type": "mystery"})


# --- dispatch, envelope validation, etag -------------------------------------------------------


def _every_object() -> list[object]:
    """One instance of EVERY persisted type (both row sources, every claim kind)."""
    return [
        _account(),
        _rule(),
        _rule_row(),
        _explicit_row(),
        _slot(),
        _ruleday(),
        _booking(),
        _snapshot(),
        _user(),
        *(_claim(kind) for kind in ClaimKind),
        _probe(),
        _audit(),
    ]


def _every_doc() -> list[dict[str, object]]:
    return [to_doc(obj) for obj in _every_object()]  # type: ignore[arg-type]


# Keys whose absence must be refused (dataclass fields with no default, plus the envelope).
# A defaulted field may be absent (a document written before the field existed, N-1 §10.2).
_ENVELOPE = ("id", "type", "schemaVersion")
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "account": (
        "accountId",
        "userId",
        "courseId",
        "provenance",
        "username",
        "passwordCiphertext",
        "keyId",
        "status",
    ),
    "rule": (
        "ruleId",
        "accountId",
        "weekday",
        "windowEarliest",
        "windowLatest",
        "partySize",
        "active",
        "materializedThrough",
        "version",
    ),
    "row": (
        "rowId",
        "accountId",
        "courseId",
        "targetDate",
        "timezone",
        "windowEarliest",
        "windowLatest",
        "partySize",
        "status",
        "source",
        "cutoffAt",
        "requestId",
        "version",
    ),
    "slot": ("accountId", "targetDate", "activeRowId"),
    "ruleday": ("accountId", "weekday", "activeRuleId"),
    "booking": (
        "bookingId",
        "rowId",
        "accountId",
        "courseId",
        "targetDate",
        "rawReservationId",
        "teeTime",
        "partySize",
        "source",
        "state",
    ),
    "snapshot": ("accountId", "observedAt", "source", "trusted", "entries"),
    "user": ("userId", "oauthProvider", "oauthSubject", "email", "displayName", "role", "status"),
    "claim": ("kind", "keyHash", "state", "createdAt"),
    "probe": ("probeId", "userId", "courseId", "usernameHash", "ok", "at"),
    "audit": ("auditId", "userId", "action", "rowId", "detail", "at"),
}


class TestDispatch:
    def test_to_doc_dispatches_to_the_typed_mapper(self) -> None:
        assert to_doc(_rule_row()) == to_row_doc(_rule_row())
        assert to_doc(_account()) == to_account_doc(_account())
        assert to_doc(_audit()) == to_audit_doc(_audit())
        fingerprint = RowFingerprint(status=RowStatus.PENDING, version=1, booked_raw_id=None)
        for not_persisted in (object(), fingerprint):
            with pytest.raises(DocumentError, match="not a persisted"):
                to_doc(not_persisted)  # type: ignore[arg-type]

    @pytest.mark.parametrize("obj", _every_object(), ids=lambda o: type(o).__name__)
    def test_from_doc_roundtrips_every_type(self, obj: object) -> None:
        stored = from_doc(to_doc(obj))  # type: ignore[arg-type]
        item = stored.item
        if isinstance(obj, RequestRow) and obj.booked_tee_time is not None:
            # DST-fold instants compare as instants (see test_roundtrip_request_row_all_fields).
            assert isinstance(item, RequestRow)
            assert replace(item, booked_tee_time=None) == replace(obj, booked_tee_time=None)
        elif isinstance(obj, LoginProbe):
            assert isinstance(item, LoginProbe)
            assert item.at.timestamp() == obj.at.timestamp()
            assert replace(item, at=NOW) == replace(obj, at=NOW)
        else:
            assert item == obj
        assert stored.etag is None

    def test_from_doc_rejects_unknown_type(self) -> None:
        doc = to_doc(_account())
        for bad in ("mystery", None, 7, "Account"):
            with pytest.raises(DocumentError, match="type"):
                from_doc({**doc, "type": bad})
        missing = dict(doc)
        del missing["type"]
        with pytest.raises(DocumentError, match="type"):
            from_doc(missing)

    def test_doc_type_discriminator_present(self) -> None:
        seen = set()
        for doc in _every_doc():
            assert isinstance(doc["type"], str)
            assert doc["schemaVersion"] == SCHEMA_VERSION
            assert container_of(doc) in {TENANT_CONTAINER, GLOBAL_CONTAINER}
            seen.add(doc["type"])
        assert seen == set(_REQUIRED_KEYS)
        # A typed reader refuses another type's document even when the fields would parse.
        with pytest.raises(DocumentError, match="expected a 'slot' document"):
            from_slot_doc({**to_slot_doc(_slot()), "type": "ruleday"})


class TestEnvelopeValidation:
    @pytest.mark.parametrize("version", [0, 2, 99, -1, "1", 1.0, True, None])
    def test_unknown_schema_version_rejected(self, version: object) -> None:
        for doc in _every_doc():
            with pytest.raises(DocumentError, match="schemaVersion"):
                from_doc({**doc, "schemaVersion": version})

    def test_missing_schema_version_rejected(self) -> None:
        for doc in _every_doc():
            bare = dict(doc)
            del bare["schemaVersion"]
            with pytest.raises(DocumentError, match="schemaVersion"):
                from_doc(bare)

    def test_from_doc_rejects_missing_required_field(self) -> None:
        for doc in _every_doc():
            doc_type = doc["type"]
            assert isinstance(doc_type, str)
            for key in (*_ENVELOPE, *_REQUIRED_KEYS[doc_type]):
                assert key in doc, (doc_type, key)
                broken = {k: v for k, v in doc.items() if k != key}
                with pytest.raises(DocumentError):
                    from_doc(broken)

    def test_defaulted_fields_may_be_absent(self) -> None:
        """An N-1 document written before a defaulted field existed reads back with the
        dataclass default (expand/contract, §10.2)."""
        doc = to_doc(_explicit_row())
        for key in ("needsReconcile", "supersededFrom", "groupRank", "leaseOwner"):
            del doc[key]
        assert from_doc(doc).item == _explicit_row()

    def test_price_fields_absent_read_as_defaults(self) -> None:
        """MU-R1 read-compat (§16.5): documents written before the price and rule-group fields
        existed read back with the account default $100 and no per-request override."""
        account_doc = to_doc(_account())
        del account_doc["defaultMaxPrice"]
        assert from_doc(account_doc).item.default_max_price == Decimal("100.00")
        rule_doc = to_doc(_rule())
        for key in ("maxPrice", "groupId", "groupRank"):
            del rule_doc[key]
        rule = from_doc(rule_doc).item
        assert (rule.max_price, rule.group_id, rule.group_rank) == (None, None, None)
        row_doc = to_doc(_rule_row())
        del row_doc["maxPrice"]
        assert from_doc(row_doc).item.max_price is None

    def test_prices_are_stored_as_exact_decimal_strings(self) -> None:
        """Money never round-trips through a float: the document holds the decimal string."""
        assert to_doc(_account())["defaultMaxPrice"] == "120.00"
        assert to_doc(_rule_row())["maxPrice"] == "85.50"
        assert to_doc(_explicit_row())["maxPrice"] is None
        with pytest.raises(DocumentError):
            from_doc({**to_doc(_rule_row()), "maxPrice": 85.5})
        with pytest.raises(DocumentError):
            from_doc({**to_doc(_rule_row()), "maxPrice": "NaN"})

    def test_wrong_value_types_rejected(self) -> None:
        doc = to_doc(_explicit_row())
        for key, bad in (
            ("partySize", "2"),
            ("partySize", True),
            ("needsReconcile", 1),
            ("status", "frozen"),
            ("targetDate", "2026-13-01"),
            ("cutoffAt", "2026-03-07T16:00:00"),  # naive instant
            ("rowId", "not-a-uuid"),
        ):
            with pytest.raises(DocumentError):
                from_doc({**doc, key: bad})

    def test_identity_mismatch_rejected(self) -> None:
        for doc in _every_doc():
            with pytest.raises(DocumentError, match="id"):
                from_doc({**doc, "id": "row|x|00000000-0000-4000-8000-000000000000"})


_ETAG = '"00000a00-0000-0000-0000-000000000000"'
_SYSTEM_PROPS = {"_rid": "abc==", "_self": "dbs/x", "_ts": 1_790_000_000, "_attachments": ""}


class TestEtag:
    def test_etag_passthrough(self) -> None:
        for doc in _every_doc():
            assert "_etag" not in doc, doc["type"]  # never WRITTEN
            stored = from_doc({**doc, **_SYSTEM_PROPS, "_etag": _ETAG})
            assert stored.etag == _ETAG
            assert from_doc(doc).etag is None
            # Re-writing a read item never echoes the etag or the system properties back.
            rewritten = to_doc(stored.item)
            assert not any(k.startswith("_") for k in rewritten), rewritten

    def test_non_string_etag_rejected(self) -> None:
        with pytest.raises(DocumentError):
            from_doc({**to_doc(_account()), "_etag": 12})


# --- vocabularies survive the round trip ------------------------------------------------------

_ALL_REASONS = sorted(SYSTEM_WITHDRAW_REASONS | CANCEL_REASONS | {USER_WITHDRAW_REASON})


class TestVocabularies:
    @pytest.mark.parametrize("reason", [None, *_ALL_REASONS])
    @pytest.mark.parametrize("status", list(RowStatus))
    def test_every_status_and_reason_roundtrips(
        self, status: RowStatus, reason: str | None
    ) -> None:
        row = replace(_explicit_row(), status=status, status_reason=reason, superseded_from=status)
        doc = to_doc(row)
        assert doc["status"] == status.value
        assert doc["statusReason"] == reason
        assert doc["supersededFrom"] == status.value
        assert from_doc(doc).item == row

    def test_every_enum_value_roundtrips(self) -> None:
        cases: list[object] = []
        cases += [replace(_booking(), source=s) for s in BookingSource]
        cases += [replace(_booking(), state=s) for s in BookingState]
        cases += [replace(_account(), status=s) for s in AccountStatus]
        cases += [replace(_account(), provenance=p) for p in AccountProvenance]
        cases += [replace(_user(), role=r) for r in UserRole]
        cases += [replace(_user(), status=s) for s in UserStatus]
        cases += [replace(_claim(), state=s) for s in ClaimState]
        for obj in cases:
            doc = to_doc(obj)  # type: ignore[arg-type]
            assert all(not isinstance(v, Enum) for v in doc.values()), doc
            assert from_doc(doc).item == obj


# --- read-side projections: RowFingerprint and EventRow ---------------------------------------


class TestProjections:
    def test_row_fingerprint_of_a_stored_row(self) -> None:
        fp = row_fingerprint_of(to_row_doc(_rule_row()))
        assert fp == RowFingerprint(status=RowStatus.BOOKED, version=3, booked_raw_id="12345")
        explicit = row_fingerprint_of(to_row_doc(_explicit_row()))
        assert explicit == RowFingerprint(status=RowStatus.PENDING, version=1, booked_raw_id=None)
        with pytest.raises(DocumentError, match="'row'"):
            row_fingerprint_of(to_account_doc(_account()))

    def test_event_row_from_docs(self) -> None:
        row, account = _explicit_row(), _account()
        event = event_row_from_docs(to_row_doc(row), to_account_doc(account))
        assert event == EventRow(row=row, account=account)

    def test_event_row_refuses_a_foreign_account(self) -> None:
        other_user = UserId(UUID("88888888-8888-4888-8888-888888888888"))
        other = replace(_account(), id=derive_account_id(other_user, MB), user_id=other_user)
        with pytest.raises(DocumentError, match="account"):
            event_row_from_docs(to_row_doc(_explicit_row()), to_account_doc(other))
        with pytest.raises(DocumentError, match="'account'"):
            event_row_from_docs(to_row_doc(_explicit_row()), to_row_doc(_explicit_row()))
