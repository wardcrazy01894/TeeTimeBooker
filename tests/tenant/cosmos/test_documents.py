"""MU-8a: the Cosmos document mapping (MULTIUSER_PLAN §3.1/§3.2/§10.2).

Pure ``to_*_doc`` / ``from_*_doc`` per persisted type, deterministic document ids, partition keys,
the ``type`` discriminator, ``schemaVersion``, per-item TTLs and ``_etag`` pass-through. No SDK is
involved: the store (MU-8b) hands these dicts to ``azure-cosmos`` unchanged.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import pytest
from teetime.tenant.cosmos.documents import (
    CI_CONTAINER_DEFAULT_TTL_S,
    GLOBAL_CONTAINER,
    SCHEMA_VERSION,
    TENANT_CONTAINER,
    DocumentError,
    RuleDayPointer,
    SlotPointer,
    account_doc_id,
    booking_doc_id,
    from_account_doc,
    from_booking_doc,
    from_row_doc,
    from_rule_doc,
    from_ruleday_doc,
    from_slot_doc,
    from_snapshot_doc,
    row_doc_id,
    rule_doc_id,
    ruleday_doc_id,
    slot_doc_id,
    snapshot_doc_id,
    to_account_doc,
    to_booking_doc,
    to_row_doc,
    to_rule_doc,
    to_ruleday_doc,
    to_slot_doc,
    to_snapshot_doc,
)

from teetime.core.models import CourseId
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    BookingSource,
    BookingState,
    CourseAccount,
    CourseAccountId,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    ReservationSnapshot,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    SnapshotEntry,
    StandingRule,
    UserId,
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
        assert stored.item == row
        # Instants survive as instants, and are re-read tz-AWARE (never naive).
        assert stored.item.booked_tee_time is not None
        assert stored.item.booked_tee_time.utcoffset() is not None
        assert stored.item.booked_tee_time == row.booked_tee_time

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
