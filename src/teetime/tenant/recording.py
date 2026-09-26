"""In-memory recording adapter decorator (MULTIUSER_PLAN §4.6, round-1 M1/M2).

``Orchestrator.run`` returns only the kept ``best``. Surplus bookings, ``_cancel_extras``
failures and swallowed blind-POST errors never reach the caller. The same holds for the watcher:
an upgrade that cancels and then fails to rebook returns the OLD terminal. Wrapping each
account's adapter in a recorder lets the tenant runner see every book, cancel and UNCERTAIN
failure, with NO I/O (it appends to a list), so the T0 path gains zero calls and
``orchestrator.py`` stays unmodified.

**Capability fidelity (SF1).** Python >= 3.12 ``runtime_checkable`` ``isinstance`` uses
``inspect.getattr_static``, so a ``__getattr__``-forwarding proxy FAILS
``isinstance(proxy, ReservationCacheRefreshable)``. ``_reguard_before_fallback`` would then fall
back to the idempotent ``authenticate()``, read the stale pre-burst cache, and could double-book.
``make_recording_adapter`` therefore returns a CONCRETE class per inner capability set: each
opt-in Protocol (``ReservationCacheRefreshable``, ``AuthStateReportable``,
``ReservationSnapshotHealth``) has a mixin that defines its member EXPLICITLY, and the factory
composes exactly the mixins ``inner`` satisfies (one memoised class per combination, so the
ForeUP variant defines ``refresh_reservations``, ``is_authenticated`` and ``snapshot_trusted``
while the FakeAdapter/TeeItUp variant defines none of them). ``capabilities`` is copied. Pinned
by ``test_recording_adapter_isinstance_mirrors_inner_for_each_capability``, which discovers the
Protocols rather than listing them.

**Blind-POST members (round-2 SF1).** ``capabilities`` is copied, so a Mangrove Bay recorder
reports ``blind_post=True``, and the orchestrator ``cast``s it to ``BlindPostCapable`` and CALLS
``synthesize_blind_slots`` / ``captcha_pool_size`` on it. A cast is not a check, so a missing
method would fail silently (swallowed in the pre-warm gather) and then fatally at T0.
``make_recording_adapter`` therefore returns a ``BlindCapableRecordingAdapter`` variant iff
``inner.capabilities.blind_post``, passing those two through PLUS the MU-3 allowlist hook
(``set_blind_allowlist`` / ``blind_allowlist``) the tenant runner sets before T0 — an inner that
promises blind_post without the hook is refused at wrap time (``TypeError``), so the runner
fails at 05:51 rather than at T0. The runner also asserts their presence
(``inspect.getattr_static``) before T0. Pinned by
``test_recording_adapter_blind_capable_end_to_end``.

**What is recorded** (instants come from the injected ``Clock`` — the SAME clock the
orchestrator runs on, so a ``book()``'s ``at`` IS its send instant):

- every ``book()`` that returned BOOKED: raw id (``TTB:`` stripped), slot, send instant;
- every ``book()`` that raised anything other than ``SlotGoneError`` — UNCERTAIN, the POST may
  have landed — including a Captcha/OTP error the blind burst swallows (flagged ``captcha``);
- every ``cancel_reservation()`` with its outcome (ok / exception class name);
- every other member's raise (op + class name) and the ``authenticate`` / ``refresh_reservations``
  call counts, for diagnostics.

Exception CLASS NAMES only, never messages (they can carry PII). Nothing here changes control
flow: every recorded exception is re-raised as the very same object.
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, cast

from ..core.adapter import (
    AdapterCapabilities,
    AuthStateReportable,
    BlindPostCapable,
    CaptchaError,
    CourseAdapter,
    ReservationCacheRefreshable,
    ReservationSnapshotHealth,
    SlotGoneError,
)
from ..core.clock import Clock
from ..core.models import (
    MANAGED_BOOKING_TAG,
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    SlotId,
    TeeTimeSlot,
)


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
    only (messages can carry PII); ``captcha`` is True for the ``CaptchaError`` family (which
    includes ``OtpChallengeError``) so the runner can make the exit non-zero (§4.5)."""

    slot: TeeTimeSlot
    error: str
    at: datetime
    captcha: bool = False


@dataclass(frozen=True, slots=True)
class RecordedCancel:
    raw_id: str
    ok: bool
    error: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class RecordedError:
    """A raise from any member OTHER than ``book()``/``cancel_reservation()`` — diagnostics only
    (``op`` is the member name, ``error`` the exception class name)."""

    op: str
    error: str
    at: datetime


@dataclass(frozen=True, slots=True)
class RecordingLog:
    books: tuple[RecordedBook, ...]
    book_failures: tuple[RecordedBookFailure, ...]
    cancels: tuple[RecordedCancel, ...]
    errors: tuple[RecordedError, ...] = ()
    authenticates: int = 0
    refreshes: int = 0

    # --- §4.6 ownership helpers ("ownership derived at write time") ----------

    def _final_cancel_outcomes(self) -> dict[str, bool]:
        """Per raw id, whether the LAST recorded cancel succeeded (a retry after a failure
        supersedes the failure)."""
        final: dict[str, bool] = {}
        for c in self.cancels:
            final[c.raw_id] = c.ok
        return final

    def owned_raw_ids(self) -> frozenset[str]:
        """Every raw id this recorder saw BOOKED that was not later cancelled OK: the kept
        ``best`` (held) AND any surplus whose cancel FAILED (held_extra — still owned, so the
        watcher's owned-only reconcile collapses it). A cancelled-OK extra is no longer owned;
        a reservation found by a guard (ALREADY_BOOKED) was never booked here, so it is unowned
        unless its raw id is already in this set."""
        final = self._final_cancel_outcomes()
        return frozenset(
            b.raw_id for b in self.books if b.raw_id is not None and not final.get(b.raw_id, False)
        )

    def is_owned(self, confirmation_code: str | None) -> bool:
        """Whether ``confirmation_code`` (raw or ``TTB:``-prefixed) is an owned raw id."""
        if confirmation_code is None:
            return False
        return _raw_id(confirmation_code) in self.owned_raw_ids()

    def held_extras(self) -> tuple[str, ...]:
        """Raw ids booked here whose (last) in-run cancel FAILED — live on the server, OWNED,
        ledgered ``held_extra`` so the watcher collapses them next run (§4.6)."""
        final = self._final_cancel_outcomes()
        return tuple(
            b.raw_id
            for b in self.books
            if b.raw_id is not None and b.raw_id in final and not final[b.raw_id]
        )

    def cancelled_extras(self) -> tuple[str, ...]:
        """Raw ids booked here and later cancelled OK (ledgered ``cancelled_extra``)."""
        final = self._final_cancel_outcomes()
        return tuple(b.raw_id for b in self.books if b.raw_id is not None and final.get(b.raw_id))

    def captcha_failures(self) -> tuple[RecordedBookFailure, ...]:
        """The ``book()`` failures from the ``CaptchaError`` family (incl. OTP) — the blind burst
        swallows these, and only this record lets the runner exit non-zero."""
        return tuple(f for f in self.book_failures if f.captcha)

    def needs_reconcile(self) -> bool:
        """True iff something UNCERTAIN happened: a ``book()`` raised a non-SlotGone error (the
        POST may have landed) or returned BOOKED with no confirmation code. The watcher then
        adopts a matching reservation by EXACT tee time against ``book_failures`` (§4.6)."""
        return bool(self.book_failures) or any(b.raw_id is None for b in self.books)


def _raw_id(confirmation_code: str) -> str:
    return confirmation_code.removeprefix(MANAGED_BOOKING_TAG)


class _BlindAllowlistCapable(Protocol):
    """The MU-3 engine hook E2 (``MangroveBayAdapter.set_blind_allowlist``) — typing-only."""

    @property
    def blind_allowlist(self) -> frozenset[SlotId] | None: ...

    def set_blind_allowlist(self, allowlist: frozenset[SlotId] | None) -> None: ...


class RecordingAdapter:
    """Base recorder: the core ``CourseAdapter`` members, each delegating to ``inner`` and
    appending to the log. Never constructed directly; use ``make_recording_adapter``."""

    course_id: CourseId
    capabilities: AdapterCapabilities

    def __init__(self, *, inner: CourseAdapter, clock: Clock) -> None:
        self._inner = inner
        self._clock = clock
        self.course_id = inner.course_id
        self.capabilities = inner.capabilities
        self._books: list[RecordedBook] = []
        self._book_failures: list[RecordedBookFailure] = []
        self._cancels: list[RecordedCancel] = []
        self._errors: list[RecordedError] = []
        self._authenticates = 0
        self._refreshes = 0

    @property
    def inner(self) -> CourseAdapter:
        return self._inner

    def log(self) -> RecordingLog:
        """An immutable snapshot of everything recorded so far."""
        return RecordingLog(
            books=tuple(self._books),
            book_failures=tuple(self._book_failures),
            cancels=tuple(self._cancels),
            errors=tuple(self._errors),
            authenticates=self._authenticates,
            refreshes=self._refreshes,
        )

    def _record_error(self, op: str, exc: BaseException, at: datetime) -> None:
        self._errors.append(RecordedError(op=op, error=type(exc).__name__, at=at))

    async def authenticate(self, creds: CourseCredentials) -> None:
        self._authenticates += 1
        at = self._clock.now_utc()
        try:
            await self._inner.authenticate(creds)
        except BaseException as exc:
            self._record_error("authenticate", exc, at)
            raise

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        at = self._clock.now_utc()
        try:
            return await self._inner.search(request, skip_initial_spacing=skip_initial_spacing)
        except BaseException as exc:
            self._record_error("search", exc, at)
            raise

    async def prepare_book(
        self, slot: TeeTimeSlot | None, request: BookingRequest, *, count: int = 1
    ) -> None:
        at = self._clock.now_utc()
        try:
            await self._inner.prepare_book(slot, request, count=count)
        except BaseException as exc:
            self._record_error("prepare_book", exc, at)
            raise

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        """Delegate; record BOOKED -> ``RecordedBook``; non-SlotGone raise ->
        ``RecordedBookFailure``; then re-raise unchanged (engine control flow is untouched).

        ``at`` is read BEFORE delegating, so it is the SEND instant (§4.6). A ``SlotGoneError``
        means the platform definitively created nothing, so it is neither UNCERTAIN nor
        recorded. Any other ``BaseException`` (a ``CancelledError`` mid-POST included) leaves the
        POST's fate unknown and is recorded as UNCERTAIN.
        """
        at = self._clock.now_utc()
        try:
            result = await self._inner.book(slot, request)
        except SlotGoneError:
            raise
        except BaseException as exc:
            self._book_failures.append(
                RecordedBookFailure(
                    slot=slot,
                    error=type(exc).__name__,
                    at=at,
                    captcha=isinstance(exc, CaptchaError),
                )
            )
            raise
        if result.outcome == BookingOutcome.BOOKED:
            conf = result.confirmation_code
            self._books.append(
                RecordedBook(
                    raw_id=None if conf is None else _raw_id(conf),
                    slot=result.slot if result.slot is not None else slot,
                    at=at,
                )
            )
        return result

    async def list_reservations(self) -> list[ExistingReservation]:
        at = self._clock.now_utc()
        try:
            return await self._inner.list_reservations()
        except BaseException as exc:
            self._record_error("list_reservations", exc, at)
            raise

    async def cancel_reservation(self, confirmation_code: str) -> None:
        """Delegate; record ok/failure; re-raise unchanged."""
        at = self._clock.now_utc()
        raw = _raw_id(confirmation_code)
        try:
            await self._inner.cancel_reservation(confirmation_code)
        except BaseException as exc:
            self._cancels.append(
                RecordedCancel(raw_id=raw, ok=False, error=type(exc).__name__, at=at)
            )
            raise
        self._cancels.append(RecordedCancel(raw_id=raw, ok=True, error=None, at=at))

    async def aclose(self) -> None:
        at = self._clock.now_utc()
        try:
            await self._inner.aclose()
        except BaseException as exc:
            self._record_error("aclose", exc, at)
            raise


# --- opt-in capability mixins (one per runtime_checkable Protocol) ------------
#
# Each mixin defines its Protocol member EXPLICITLY on the class, which is what
# `inspect.getattr_static` (and so `isinstance` against a runtime_checkable Protocol) sees.
# They rely on RecordingAdapter's `_inner`/`_clock`/`_refreshes` and are only ever composed
# on top of it by `_variant_class`.


class _RefreshableRecordingMixin:
    """``ReservationCacheRefreshable`` pass-through (counted; load-bearing for the re-guard)."""

    _inner: CourseAdapter
    _clock: Clock
    _refreshes: int
    _errors: list[RecordedError]

    async def refresh_reservations(self, creds: CourseCredentials) -> None:
        self._refreshes += 1
        at = self._clock.now_utc()
        try:
            await cast(ReservationCacheRefreshable, self._inner).refresh_reservations(creds)
        except BaseException as exc:
            self._errors.append(
                RecordedError(op="refresh_reservations", error=type(exc).__name__, at=at)
            )
            raise


class _AuthStateRecordingMixin:
    """``AuthStateReportable`` pass-through."""

    _inner: CourseAdapter

    @property
    def is_authenticated(self) -> bool:
        return cast(AuthStateReportable, self._inner).is_authenticated


class _SnapshotHealthRecordingMixin:
    """``ReservationSnapshotHealth`` pass-through."""

    _inner: CourseAdapter

    @property
    def snapshot_trusted(self) -> bool:
        return cast(ReservationSnapshotHealth, self._inner).snapshot_trusted


class BlindCapableRecordingAdapter(RecordingAdapter):
    """Recorder variant for ``capabilities.blind_post=True`` adapters: adds the ``BlindPostCapable``
    members AND the MU-3 allowlist hook as pure pass-throughs to ``inner`` (no recording, no
    I/O). The capability-mirroring rule still applies: a variant also defines the opt-in members
    ``inner`` has (composed by ``_variant_class`` on top of this class)."""

    def captcha_pool_size(self) -> int:
        return cast(BlindPostCapable, self._inner).captcha_pool_size()

    def synthesize_blind_slots(
        self,
        request: BookingRequest,
        target_date: date,
        *,
        max_count: int,
    ) -> list[TeeTimeSlot]:
        return cast(BlindPostCapable, self._inner).synthesize_blind_slots(
            request, target_date, max_count=max_count
        )

    @property
    def blind_allowlist(self) -> frozenset[SlotId] | None:
        """The allowlist currently applied by synthesize_blind_slots (None = unfiltered)."""
        return cast(_BlindAllowlistCapable, self._inner).blind_allowlist

    def set_blind_allowlist(self, allowlist: frozenset[SlotId] | None) -> None:
        """Pass-through to the inner's engine hook E2 (``MangroveBayAdapter``'s)."""
        cast(_BlindAllowlistCapable, self._inner).set_blind_allowlist(allowlist)


# (Protocol, mixin, short tag for the generated class name) — in a fixed order so the memo key
# and the MRO are deterministic.
_OPT_INS: tuple[tuple[type, type, str], ...] = (
    (ReservationCacheRefreshable, _RefreshableRecordingMixin, "Refreshable"),
    (AuthStateReportable, _AuthStateRecordingMixin, "AuthState"),
    (ReservationSnapshotHealth, _SnapshotHealthRecordingMixin, "SnapshotHealth"),
)

# Every member the blind variant passes through. `inspect.getattr_static` resolves the
# `blind_allowlist` PROPERTY to its property object, so the presence check covers it too —
# an inner shipping only the setter must be refused here, not fail lazily on first access.
_BLIND_HOOK_MEMBERS = (
    "captcha_pool_size",
    "synthesize_blind_slots",
    "set_blind_allowlist",
    "blind_allowlist",
)


@functools.cache
def _variant_class(opt_ins: tuple[bool, ...], *, blind: bool) -> type[RecordingAdapter]:
    """The CONCRETE recorder class for one capability combination (memoised, so two adapters
    with the same capability set share a class)."""
    base: type[RecordingAdapter] = BlindCapableRecordingAdapter if blind else RecordingAdapter
    mixins = [mixin for (_, mixin, _), on in zip(_OPT_INS, opt_ins, strict=True) if on]
    if not mixins:
        return base
    tags = "".join(tag for (_, _, tag), on in zip(_OPT_INS, opt_ins, strict=True) if on)
    name = f"{base.__name__}[{tags}]"
    return cast(
        type[RecordingAdapter],
        type(name, (*mixins, base), {"__module__": __name__, "__qualname__": name}),
    )


def make_recording_adapter(inner: CourseAdapter, *, clock: Clock) -> RecordingAdapter:
    """Return a recorder whose CONCRETE class exposes exactly ``inner``'s opt-in capability
    members (see module docstring, SF1) and, iff ``inner.capabilities.blind_post``, the blind
    members + allowlist hook. A blind-capable ``inner`` missing any of those is refused."""
    blind = inner.capabilities.blind_post
    if blind:
        missing = [m for m in _BLIND_HOOK_MEMBERS if inspect.getattr_static(inner, m, None) is None]
        if missing:
            raise TypeError(
                f"{type(inner).__name__} reports capabilities.blind_post=True but lacks "
                f"{', '.join(missing)}; the tenant runner needs captcha_pool_size, "
                "synthesize_blind_slots, set_blind_allowlist and blind_allowlist on every "
                "blind-capable adapter"
            )
    opt_ins = tuple(isinstance(inner, proto) for proto, _, _ in _OPT_INS)
    cls = _variant_class(opt_ins, blind=blind)
    return cls(inner=inner, clock=clock)


# Keep the module's public surface explicit for the runner/watcher (MU-9a/MU-10).
__all__: tuple[str, ...] = (
    "BlindCapableRecordingAdapter",
    "RecordedBook",
    "RecordedBookFailure",
    "RecordedCancel",
    "RecordedError",
    "RecordingAdapter",
    "RecordingLog",
    "make_recording_adapter",
)
