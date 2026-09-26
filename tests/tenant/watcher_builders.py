"""Builders shared by the ``tenant.watcher`` tests (MULTIUSER_PLAN MU-10a). Not a test module.

Every datetime is tz-aware in the COURSE timezone (``TZ``), the convention the engine's
``slot_utils`` assumes: ``midpoint_distance_minutes`` reads ``tee_time.time()`` directly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from teetime.core.models import (
    CourseId,
    ExistingReservation,
    SlotId,
    TeeTimeSlot,
)
from teetime.tenant.models import (
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
    RowId,
    RowSource,
    RowStatus,
    SnapshotEntry,
    UserId,
    derive_account_id,
    row_request_id,
)

MB = CourseId("foreup:19671:2149")
OTHER_COURSE = CourseId("foreup:1:1")
TZ = "America/New_York"
ZONE = ZoneInfo(TZ)
TARGET = date(2026, 10, 3)  # a Saturday
NEXT_DAY = date(2026, 10, 4)  # the Sunday
# The operator's window: 08:45-10:00, midpoint 09:22:30.
WINDOW = (time(8, 45), time(10, 0))
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)  # a Friday, a week before TARGET
TARGET_CUTOFF = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)  # 16:00 EDT on 10/2

_USER_NS = UUID("0f3b2c8e-1a4d-4c7b-9e2f-6d5a8b1c3e7f")


def account(n: int = 0, *, course: CourseId = MB) -> CourseAccount:
    """A deterministic ACTIVE account per ``n`` (stable ids keep cadence tests reproducible)."""
    user_id = UserId(uuid5(_USER_NS, f"user-{n}"))
    return CourseAccount(
        id=derive_account_id(user_id, course),
        user_id=user_id,
        course_id=course,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=f"golfer-{n}",
        password_ciphertext="v1:k1:nonce:ct",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )


def local(day: date, t: time) -> datetime:
    """A course-local tz-aware instant on ``day`` at wall-clock ``t``."""
    return datetime.combine(day, t, tzinfo=ZONE)


def row(
    acct: CourseAccount,
    *,
    status: RowStatus = RowStatus.PENDING,
    target: date = TARGET,
    party_size: int = 4,
    window: tuple[time, time] = WINDOW,
    booked_tee: time | None = None,
    booked_raw_id: str | None = None,
    needs_reconcile: bool = False,
    upgrade_started_at: datetime | None = None,
    lease: tuple[str, datetime] | None = None,
    row_id: RowId | None = None,
) -> RequestRow:
    rid = row_id if row_id is not None else RowId(uuid4())
    if status is RowStatus.BOOKED and booked_tee is None:
        booked_tee = time(9, 30)
    return RequestRow(
        id=rid,
        course_account_id=acct.id,
        course_id=acct.course_id,
        target_date=target,
        timezone=TZ,
        window_earliest=window[0],
        window_latest=window[1],
        party_size=party_size,
        status=status,
        source=RowSource.EXPLICIT,
        cutoff_at=TARGET_CUTOFF,
        request_id=row_request_id(rid),
        version=1,
        booked_tee_time=local(target, booked_tee) if booked_tee is not None else None,
        booked_raw_id=booked_raw_id,
        booked_at=NOW - timedelta(days=1) if status is RowStatus.BOOKED else None,
        needs_reconcile=needs_reconcile,
        upgrade_started_at=upgrade_started_at,
        lease_owner=lease[0] if lease is not None else None,
        lease_expires_at=lease[1] if lease is not None else None,
    )


def event(acct: CourseAccount, **row_kwargs: object) -> EventRow:
    return EventRow(row=row(acct, **row_kwargs), account=acct)  # type: ignore[arg-type]


def slot(
    t: time,
    *,
    day: date = TARGET,
    spots: int = 4,
    course: CourseId = MB,
    holes: int = 18,
) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=course,
        slot_id=SlotId(f"{day.isoformat()}T{t.isoformat()}"),
        tee_time=local(day, t),
        holes=holes,
        available_spots=spots,
        price_per_player=Decimal("42"),
        cart_included=True,
    )


def entry(raw_id: str, t: time, *, day: date = TARGET, party_size: int = 4) -> SnapshotEntry:
    return SnapshotEntry(raw_id=raw_id, tee_time=local(day, t), party_size=party_size)


def snapshot(
    acct: CourseAccount,
    *,
    at: datetime,
    trusted: bool = True,
    entries: tuple[SnapshotEntry, ...] = (),
    source: str = "watcher",
) -> ReservationSnapshot:
    return ReservationSnapshot(
        course_account_id=acct.id,
        observed_at=at,
        source=source,
        trusted=trusted,
        entries=entries,
    )


def owned(
    r: RequestRow,
    raw_id: str,
    *,
    state: BookingState = BookingState.HELD,
    source: BookingSource = BookingSource.BLIND,
    tee: time = time(9, 30),
) -> OwnedBooking:
    return OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=r.id,
        course_account_id=r.course_account_id,
        course_id=r.course_id,
        target_date=r.target_date,
        raw_reservation_id=raw_id,
        tee_time=local(r.target_date, tee),
        party_size=r.party_size,
        source=source,
        state=state,
    )


def reservation(
    raw_id: str,
    t: time,
    *,
    day: date = TARGET,
    party_size: int = 4,
    course: CourseId = MB,
) -> ExistingReservation:
    """A server-sourced reservation: RAW id (no ``TTB:``), as ``list_reservations`` returns."""
    return ExistingReservation(
        course_id=course,
        confirmation_code=raw_id,
        tee_time=local(day, t),
        party_size=party_size,
    )


__all__ = [
    "MB",
    "NEXT_DAY",
    "NOW",
    "OTHER_COURSE",
    "TARGET",
    "TZ",
    "WINDOW",
    "ZONE",
    "CourseAccountId",
    "account",
    "entry",
    "event",
    "local",
    "owned",
    "reservation",
    "row",
    "slot",
    "snapshot",
]
