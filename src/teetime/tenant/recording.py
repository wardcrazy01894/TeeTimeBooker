"""In-memory recording adapter decorator (MULTIUSER_PLAN §4.6, round-1 M1/M2).

``Orchestrator.run`` returns only the kept ``best`` (``orchestrator.py:577-588``). Surplus bookings,
``_cancel_extras`` failures (``:680-688``) and swallowed blind-POST errors (``:525-537``) never
reach the caller. The same holds for the watcher: an upgrade that cancels and then fails to rebook
returns the OLD terminal. Wrapping each account's adapter in a recorder lets the tenant runner see
every book, cancel and UNCERTAIN failure, with NO I/O (it appends to a list), so the T0 path gains
zero calls and ``orchestrator.py`` stays unmodified.

**Capability fidelity (SF1).** Python >= 3.12 ``runtime_checkable`` ``isinstance`` uses
``inspect.getattr_static``, so a ``__getattr__``-forwarding proxy FAILS
``isinstance(proxy, ReservationCacheRefreshable)``. ``_reguard_before_fallback`` would then fall
back to the idempotent ``authenticate()``, read the stale pre-burst cache, and could double-book.
``make_recording_adapter`` therefore returns a CONCRETE class per inner capability set: the ForeUP
variant defines ``refresh_reservations``, ``is_authenticated`` and ``snapshot_trusted`` explicitly;
the FakeAdapter/TeeItUp variant defines none of them. ``capabilities`` is copied.
Pinned by ``test_recording_adapter_isinstance_matches_inner``.

**Blind-POST members (round-2 SF1).** ``capabilities`` is copied, so a Mangrove Bay recorder reports
``blind_post=True``, and the orchestrator ``cast``s it to ``BlindPostCapable`` and CALLS
``synthesize_blind_slots`` / ``captcha_pool_size`` on it (``orchestrator.py:421, 428, 890``). A cast
is not a check, so a missing method would fail silently (swallowed in the pre-warm gather) and then
fatally at T0. ``make_recording_adapter`` therefore returns a ``BlindCapableRecordingAdapter``
variant iff ``inner.capabilities.blind_post``, with both as pure pass-throughs. The runner also
asserts their presence (``inspect.getattr_static``) before T0. Pinned by
``test_recording_adapter_blind_capable_end_to_end``.

STUB — implemented in MULTIUSER_PLAN MU-9a0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..core.adapter import AdapterCapabilities, CourseAdapter
from ..core.clock import Clock
from ..core.models import (
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
)

_MU9A = "MULTIUSER_PLAN.md MU-9a0"


@dataclass(frozen=True, slots=True)
class RecordedBook:
    """A ``book()`` that returned BOOKED. ``raw_id`` is the ``TTB:`` prefix stripped, or None if
    the adapter returned no confirmation code (then the watcher adopts by exact tee time)."""

    raw_id: str | None
    slot: TeeTimeSlot
    at: datetime


@dataclass(frozen=True, slots=True)
class RecordedBookFailure:
    """A ``book()`` that raised anything other than ``SlotGoneError``: UNCERTAIN (the POST may have
    landed) or a Captcha/OTP error the blind burst swallowed. ``error`` is the exception CLASS NAME
    only (messages can carry PII)."""

    slot: TeeTimeSlot
    error: str
    at: datetime


@dataclass(frozen=True, slots=True)
class RecordedCancel:
    raw_id: str
    ok: bool
    error: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class RecordingLog:
    books: tuple[RecordedBook, ...]
    book_failures: tuple[RecordedBookFailure, ...]
    cancels: tuple[RecordedCancel, ...]
    refreshes: int


class RecordingAdapter:
    """Base recorder: the core ``CourseAdapter`` members, each delegating to ``inner`` and
    appending to the log. Never constructed directly; use ``make_recording_adapter``."""

    course_id: CourseId
    capabilities: AdapterCapabilities

    def __init__(self, *, inner: CourseAdapter, clock: Clock) -> None:
        raise NotImplementedError(_MU9A)

    def log(self) -> RecordingLog:
        raise NotImplementedError(_MU9A)

    async def authenticate(self, creds: CourseCredentials) -> None:
        raise NotImplementedError(_MU9A)

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        raise NotImplementedError(_MU9A)

    async def prepare_book(
        self, slot: TeeTimeSlot | None, request: BookingRequest, *, count: int = 1
    ) -> None:
        raise NotImplementedError(_MU9A)

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        """Delegate; record BOOKED -> ``RecordedBook``; non-SlotGone raise ->
        ``RecordedBookFailure``; then re-raise unchanged (engine control flow is untouched)."""
        raise NotImplementedError(_MU9A)

    async def list_reservations(self) -> list[ExistingReservation]:
        raise NotImplementedError(_MU9A)

    async def cancel_reservation(self, confirmation_code: str) -> None:
        """Delegate; record ok/failure; re-raise unchanged."""
        raise NotImplementedError(_MU9A)

    async def aclose(self) -> None:
        raise NotImplementedError(_MU9A)


class BlindCapableRecordingAdapter(RecordingAdapter):
    """Recorder variant for ``capabilities.blind_post=True`` adapters: adds the ``BlindPostCapable``
    members as pure pass-throughs to ``inner`` (no recording, no I/O). The capability-mirroring
    rule still applies: a variant also defines the opt-in members ``inner`` has."""

    def captcha_pool_size(self) -> int:
        raise NotImplementedError(_MU9A)

    def synthesize_blind_slots(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        max_count: int,
    ) -> list[TeeTimeSlot]:
        raise NotImplementedError(_MU9A)


def make_recording_adapter(inner: CourseAdapter, *, clock: Clock) -> RecordingAdapter:
    """Return a recorder whose CONCRETE class exposes exactly ``inner``'s opt-in capability
    members (see module docstring, SF1)."""
    raise NotImplementedError(_MU9A)
