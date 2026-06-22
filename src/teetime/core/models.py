"""Domain dataclasses shared across adapters, orchestrator, persistence, and notifier.

These are the only cross-module shapes. Anything that needs to flow between
two layers MUST be expressed as one of these (or composed of them).
Keep this module dependency-light: stdlib + pydantic only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import NewType
from uuid import UUID, uuid5

# --- Identity types -------------------------------------------------------

CourseId = NewType("CourseId", str)  # adapter-stable id, e.g. "foreup:19671:2149"
SlotId = NewType("SlotId", str)  # adapter-local opaque id for a tee time
RequestId = NewType("RequestId", UUID)  # one BookingRequest invocation

# Stable namespace UUID used by `derive_request_id` to fold a config fingerprint
# down to a UUIDv5. Constant by design — changing it invalidates every existing
# idempotency record. See PLAN.md §13.
_REQUEST_ID_NAMESPACE = UUID("a3e1d2c4-7f6b-4d8e-9a02-1f3b5c7d9e0f")


def derive_request_id(fingerprint: str) -> RequestId:
    """Construct a deterministic RequestId from a config fingerprint string.

    The fingerprint MUST be the canonical form documented in PLAN.md §13:
    course_ids|target_offsets|time_windows|party_fingerprint, joined with
    '|', with values sorted where order is not semantically meaningful.

    EXCLUDES the resolved target dates (which advance each weekend run) —
    including them would make every cron firing produce a fresh RequestId and
    defeat cross-run idempotency. See PLAN.md §13 and item 5 of the v0 review.
    """
    return RequestId(uuid5(_REQUEST_ID_NAMESPACE, fingerprint))


def build_request_fingerprint(
    *,
    course_ids: list[CourseId],
    target_offsets: list[int],
    time_windows: Sequence[tuple[int, TimeWindow]],
    players: list[Player],
) -> str:
    """Build the canonical fingerprint string per PLAN.md §13.1.

    Format: ``<courses>|<offsets>|<windows>|<party>`` where each segment is
    sorted and comma-joined:

    - ``courses``: sorted CourseId values.
    - ``offsets``: sorted integers as decimal strings.
    - ``windows``: ``<weekday>:HH:MM-HH:MM``, sorted lexically. The weekday index is
      in the token so a window applied to Saturday vs Sunday is a DISTINCT request
      identity (per-day windows, PERDAY_WINDOWS_PLAN §6). Caller passes
      ``(weekday_index, window)`` pairs so the domain ``TimeWindow`` stays weekday-free.
    - ``party``: per-player ``first_name|last_name`` (note: NOT comma-joined —
      the player tokens follow the outer pipe so the canonical form is
      ``courses|offsets|windows|first1|last1|first2|last2``). Email and phone
      are deliberately excluded so contact-info rotation does not change the
      RequestId.
    """
    courses_seg = ",".join(sorted(course_ids))
    offsets_seg = ",".join(str(o) for o in sorted(target_offsets))
    windows_seg = ",".join(
        sorted(
            f"{wd}:{w.earliest.strftime('%H:%M')}-{w.latest.strftime('%H:%M')}"
            for wd, w in time_windows
        )
    )
    party_tokens = sorted(f"{p.first_name}|{p.last_name}" for p in players)
    return "|".join([courses_seg, offsets_seg, windows_seg, *party_tokens])


# --- Enums ----------------------------------------------------------------


class BookingOutcome(StrEnum):
    """Terminal status of a single BookingRequest after orchestration."""

    BOOKED = "booked"
    NO_INVENTORY = "no_inventory"  # course had no slots matching criteria
    INVENTORY_NOT_PUBLISHED = "inventory_not_published"  # 7-day window not open yet
    PRICE_REJECTED = "price_rejected"  # slots existed but exceeded max_price
    AUTH_FAILED = "auth_failed"
    CAPTCHA_BLOCKED = "captcha_blocked"
    RATE_LIMITED = "rate_limited"
    ALREADY_BOOKED = "already_booked"  # idempotency hit
    DRY_RUN = "dry_run"  # everything succeeded except final POST
    ERROR = "error"  # uncategorized failure


class CartPreference(StrEnum):
    """User preference for cart vs walking. Adapter maps to course-specific options."""

    EITHER = "either"
    CART = "cart"
    WALKING = "walking"


# --- Player identity ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Player:
    """A single golfer participating in the booking. v0 books a foursome (4 players —
    the account holder + 3 guests); variable party sizes are future work. Email/phone are
    pass-through for the adapter; we don't share PII unnecessarily.
    """

    first_name: str
    last_name: str
    email: str
    phone: str | None = None
    member_number: str | None = None  # municipal ID etc.


# --- Search criteria & request -------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Acceptable tee-off window on a single calendar date.
    earliest <= slot.tee_time <= latest. Both are local wall-clock time at the course.
    """

    earliest: time
    latest: time

    def __post_init__(self) -> None:
        if self.earliest > self.latest:
            raise ValueError("earliest must be <= latest")


@dataclass(frozen=True, slots=True)
class BookingRequest:
    """A single user-issued goal. The orchestrator tries to satisfy it across one or
    more courses. Idempotent: identical (request_id) requests return the prior result.
    """

    request_id: RequestId
    target_dates: tuple[date, ...]  # ordered preference, first wins
    time_windows: tuple[TimeWindow, ...]  # any window matches
    players: tuple[Player, ...]  # length == party size
    course_preferences: tuple[CourseId, ...]  # ordered fallback list
    holes: int = 18
    max_price_per_player: Decimal | None = None  # None = no cap
    cart: CartPreference = CartPreference.EITHER
    dry_run: bool = False  # if True, never POST the booking


# --- Search results -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeeTimeSlot:
    """One bookable slot returned by an adapter's search()."""

    course_id: CourseId
    slot_id: SlotId
    tee_time: datetime  # tz-aware, course-local zone
    holes: int
    available_spots: int  # remaining seats in the group
    price_per_player: Decimal
    cart_included: bool
    raw: dict[str, object] = field(default_factory=dict)  # adapter-specific echo


@dataclass(frozen=True, slots=True)
class ExistingReservation:
    """A reservation already on the user's account, as discovered by
    `CourseAdapter.list_reservations`. Used by §9 layer 2 (the pre-book remote
    check) and by the watcher's asynchronous reconciliation. Fingerprint fields MUST be
    sufficient to match against a (target_date, time_window, party_size)
    triple deterministically — a server-side reservation either matches the
    intended request or it does not.
    """

    course_id: CourseId
    confirmation_code: str
    tee_time: datetime  # tz-aware, course-local zone
    party_size: int
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def is_managed(self) -> bool:
        """True if this reservation was made by TeeTimeBooker and is therefore
        eligible for automatic cancellation under the one-booking policy.

        Detection strategy: confirmation_code was stamped with the MANAGED_BOOKING_TAG
        prefix when booked. Manual reservations made through the ForeUP website will
        not have this prefix and will not be touched.

        See PLAN.md M-feature-2 §"Our vs manual booking detection".
        """
        return self.confirmation_code.startswith(MANAGED_BOOKING_TAG)


# Prefix stamped into the booking POST body's `notes` field (or equivalent)
# so we can identify our own bookings in list_reservations output.
# This sentinel must be short enough to fit in ForeUP's notes field, and
# unlikely to appear in manually-entered confirmation codes.
# The value is intentionally stable — changing it orphans all existing managed
# bookings (they lose is_managed=True) until they expire naturally.
MANAGED_BOOKING_TAG = "TTB:"


@dataclass(frozen=True, slots=True)
class BookingResult:
    """Terminal record of a BookingRequest. Persisted by BookingStore. Notifier reads it."""

    request_id: RequestId
    outcome: BookingOutcome
    course_id: CourseId | None
    slot: TeeTimeSlot | None
    confirmation_code: str | None  # course/ForeUP confirmation number
    booked_at: datetime | None  # tz-aware UTC
    attempts: int
    error_message: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


# --- Auth context (passed to adapters; never persisted in models) --------


@dataclass(frozen=True, slots=True)
class CourseCredentials:
    """Per-course credentials. The store may cache derived session tokens, but not these
    raw values — they live only in process memory, hydrated from secrets at startup.
    """

    username: str
    password: str
    extra: dict[str, str] = field(default_factory=dict)  # e.g. {"booking_class_id": "2149"}


# --- Watch / cancellation-monitor models (M-feature-1) ------------------


@dataclass(frozen=True, slots=True)
class WatchConfig:
    """Configuration for the cancellation-monitor job.

    The watch job polls on every run (no time-of-day gate) for newly available
    slots on each wanted upcoming date, whether or not that date was already
    attempted — so it both catches early cancellations and can perform an
    early-morning recovery booking on a date the 06:00 booker raced or missed.

    See PLAN.md M-feature-1 for the full design.
    """

    poll_interval_s: int = 600  # 10 minutes default; must be >= 300 (anti-bot floor)
    # NOTE: the time-of-day polling-hours gate was REMOVED (MULTIDAY PR4) — the watcher
    # polls on every run. Rate limiting is the cron cadence + the poll_interval_s floor.
    # The stop condition is purely the past-deadline check (local_date > target_date), so
    # there is no watch-duration field.

    def __post_init__(self) -> None:
        _min_poll_s = 300
        if self.poll_interval_s < _min_poll_s:
            raise ValueError(f"poll_interval_s must be >= {_min_poll_s} (anti-bot etiquette floor)")


# --- One-booking priority models (M-feature-2) ---------------------------


@dataclass(frozen=True, slots=True)
class PrioritySlot:
    """One entry in the user's ordered priority list for the one-booking policy.

    Priority 0 is highest (most preferred). The orchestrator picks the available
    slot with the lowest priority index. Within the same priority index, earlier
    tee_time wins (Feature 3).

    See PLAN.md M-feature-2 for the full design.
    """

    priority: int  # 0 = most preferred
    course_id: CourseId
    time_window: TimeWindow
    target_date: date
