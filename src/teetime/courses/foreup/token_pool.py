"""Shared per-course CAPTCHA token pool (MULTIUSER_PLAN §5).

Replaces each ``ForeUpAdapter``'s private ``_captcha_tokens`` deque with a pool that several
adapters of the SAME course (one per tenant account) can share. A ForeUP reCAPTCHA v2 token is
bound to the site key + page URL, not to an account or session: the 2captcha provider never sees
a ForeUP cookie. So N accounts need ``burst x N + reserve`` solves, not ``5 x N`` (§5.1).

Two modes (§5.2):

* **Uncoordinated**: no demand registered for a key. ``prefetch(key, count)`` reproduces
  today's ``ForeUpAdapter.prepare_book`` exactly: ``count`` concurrent solves appended to the
  key's lease, with the NI10 raise contract (count == 1 re-raises, count > 1 never raises). An
  adapter built without a pool gets a private uncoordinated pool, so the single-user path is
  byte-identical. MU-2's gate is ``tests/test_captcha_pool.py`` passing UNMODIFIED.
* **Coordinated**: the tenant runner calls ``register(key, k)`` per account and
  ``set_reserve(r)`` BEFORE any orchestrator starts. The first ``prefetch`` launches ONE bounded
  fill, and every ``prefetch`` awaits it. Leases are granted round-robin in registration (draft)
  order, so every account's rank-0 POST has a token before anyone's surplus POST (no
  cross-account starvation). The latest arrivals go to the shared reserve (the freshest tokens
  for post-T0 fallbacks).

Preserved invariants: FIFO single-use pop within a lease; MF1 (a lease or reserve token counts as
"pooled", so a captcha-challenge on it gets exactly one inline re-solve + re-POST); the inline-
solve semaphore (moved here, so the bound is per COURSE across all accounts, which is stricter
than today's per-adapter bound). There is NO age-based discard: ForeUP's response + MF1 decide
staleness, as today.

Wired into ``ForeUpAdapter`` (MU-2, engine change E1): ``captcha_pool=`` + ``captcha_lease_key=``
inject a shared pool; without them the adapter builds a private pool whose single lease IS
today's ``_captcha_tokens`` deque.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NewType

from ...core.adapter import CaptchaError
from ...core.clock import Clock

_log = logging.getLogger(__name__)

# Opaque per-adapter lease key (the tenant runner uses the row id; a private pool uses a
# constant). A NewType so a lease key can't be confused with a SlotId or CourseId.
LeaseKey = NewType("LeaseKey", str)

# How often a coordinated prefetch re-checks wave-1 completion against the T0 deadline. Polled
# through the injected Clock (not asyncio.wait_for) so FakeClock tests are deterministic. This
# runs pre-T0 only, so a <= 0.25 s return latency is irrelevant.
_DEADLINE_POLL_S = 0.25


@dataclass(frozen=True, slots=True)
class FillReport:
    """Outcome of one coordinated fill, for the runner's diagnostics line (§5.3).

    ``granted`` maps each lease key to the number of tokens placed in its lease. ``reserve`` is
    the shared-reserve size at the moment ``prefetch`` returned (wave-2 tokens may keep landing
    afterwards). ``failures`` counts solves that raised or timed out.
    """

    demanded: int
    solved: int
    failures: int
    granted: dict[LeaseKey, int]
    reserve: int
    started_at: datetime
    finished_at: datetime


class SharedCaptchaPool:
    """One per (course, runner process). See the module docstring for semantics."""

    def __init__(
        self,
        *,
        provider: Callable[[], Awaitable[str]],
        clock: Clock,
        max_concurrent_solves: int = 12,
        max_concurrent_inline_solves: int = 6,
        fill_deadline_before_t0_s: float = 10.0,
    ) -> None:
        """``max_concurrent_solves`` is C in §5.3: burst capacity per drop, so N_blind =
        floor(C / burst). The default 12 is the concurrency that ran live 2026-06-22..29.
        ``max_concurrent_inline_solves`` is today's ``_captcha_solve_sem`` bound (default 6),
        now per course. ``fill_deadline_before_t0_s`` is the T0-10 s cutoff after which
        ``prefetch`` returns even if wave 1 is incomplete."""
        self._provider = provider
        self._clock = clock
        self._max_concurrent_solves = max(1, max_concurrent_solves)
        self._inline_sem = asyncio.Semaphore(max(1, max_concurrent_inline_solves))
        self._fill_deadline_before_t0 = timedelta(seconds=fill_deadline_before_t0_s)
        self._leases: dict[LeaseKey, collections.deque[str]] = {}
        self._reserve: collections.deque[str] = collections.deque()
        # Coordinated-mode state. _demand preserves registration (= draft) order.
        self._demand: dict[LeaseKey, int] = {}
        self._reserve_target = 0
        self._t0: datetime | None = None
        self._released: set[LeaseKey] = set()
        self._fill_task: asyncio.Task[None] | None = None
        self._wave1_done = asyncio.Event()
        self._grant_order: list[LeaseKey] = []
        self._next_grant = 0
        self._granted: dict[LeaseKey, int] = {}
        self._solved = 0
        self._failures = 0
        self._started_at: datetime | None = None
        self._report: FillReport | None = None

    # --- coordinated-mode setup (tenant runner, pre-T0, before any orchestrator runs) ----

    def register(self, key: LeaseKey, count: int) -> None:
        """Declare that ``key`` will want ``count`` burst tokens. Registration order IS the
        round-robin grant order (the §5.4 draft order). Raises if a fill already started."""
        self._refuse_after_fill("register")
        if key in self._demand:
            raise ValueError(f"lease key {key!r} is already registered")
        self._demand[key] = max(0, count)
        self._leases.setdefault(key, collections.deque())

    def set_reserve(self, count: int) -> None:
        """Size of the shared reserve R, filled by wave 2 (§5.3)."""
        self._refuse_after_fill("set_reserve")
        self._reserve_target = max(0, count)

    def arm(self, *, t0: datetime) -> None:
        """Record T0 so the fill can honour ``fill_deadline_before_t0_s``. Coordinated mode only.
        Without it a coordinated ``prefetch`` waits for wave 1 with no deadline."""
        self._t0 = t0

    def _refuse_after_fill(self, what: str) -> None:
        if self._fill_task is not None:
            raise RuntimeError(f"SharedCaptchaPool.{what}() after the coordinated fill started")

    # --- adapter-facing API (called from ForeUpAdapter.prepare_book / book) -------------

    def lease_deque(self, key: LeaseKey) -> collections.deque[str]:
        """The LIVE lease deque for ``key`` (created empty on first use). ``ForeUpAdapter``
        exposes it as ``_captcha_tokens``, so the private-pool path keeps today's attribute
        (``tests/test_captcha_pool.py`` seeds and inspects it directly)."""
        return self._leases.setdefault(key, collections.deque())

    async def prefetch(self, key: LeaseKey, count: int) -> None:
        """``prepare_book`` entry point.

        Uncoordinated key: solve ``count`` concurrently into ``key``'s lease, with today's NI10
        raise contract. Coordinated key: start the single fill if it is not running, then await
        wave-1 completion or the deadline. ``count`` is ignored because demand was registered.
        """
        if key not in self._demand:
            await self._prefetch_uncoordinated(key, count)
            return
        if self._fill_task is None:
            self._start_fill()
        await self._await_wave1()
        if self._report is None:
            self._report = self._snapshot_report()
            _log.info(
                "captcha pool: fill %d/%d solved (%d failed), leases %s, reserve %d.",
                self._report.solved,
                self._report.demanded,
                self._report.failures,
                self._report.granted,
                self._report.reserve,
            )

    async def _prefetch_uncoordinated(self, key: LeaseKey, count: int) -> None:
        """Today's ``ForeUpAdapter.prepare_book`` body, unchanged in behaviour."""
        lease = self.lease_deque(key)
        _log.info("ForeUP: pre-fetching %d CAPTCHA token(s) concurrently...", count)
        provider = self._provider
        results = await asyncio.gather(
            *(provider() for _ in range(count)),
            return_exceptions=True,
        )
        tokens = [r for r in results if isinstance(r, str)]
        lease.extend(tokens)
        failures = [r for r in results if isinstance(r, BaseException)]
        if tokens:
            _log.info(
                "ForeUP: pre-fetched %d/%d CAPTCHA token(s) — pool size %d.",
                len(tokens),
                count,
                len(lease),
            )
            return
        # Nothing solved.
        if count == 1 and failures:
            exc = failures[0]
            if isinstance(exc, TimeoutError):
                raise CaptchaError(f"CAPTCHA pre-fetch timed out: {exc}") from exc
            raise exc
        _log.warning("ForeUP: all %d CAPTCHA pre-fetches failed — book() will solve inline.", count)

    def _start_fill(self) -> None:
        # Round-robin grant sequence: round r gives one token to every key (in draft order)
        # whose demand exceeds r. Arrivals past the end of the sequence go to the reserve.
        rounds = max(self._demand.values(), default=0)
        self._grant_order = [k for r in range(rounds) for k, n in self._demand.items() if n > r]
        self._started_at = self._clock.now_utc()
        demanded = len(self._grant_order) + self._reserve_target
        if not self._grant_order:
            self._wave1_done.set()
        self._fill_task = asyncio.create_task(self._fill(demanded))

    async def _fill(self, demanded: int) -> None:
        # The C bound: at most max_concurrent_solves provider calls in flight. Wave 1 (lease
        # demand) is submitted first; wave 2 (reserve) takes workers as they free up.
        sem = asyncio.Semaphore(self._max_concurrent_solves)

        async def one() -> None:
            async with sem:
                try:
                    token = await self._provider()
                except Exception as exc:
                    self._failures += 1
                    _log.warning("captcha pool: a fill solve failed: %s", exc)
                else:
                    self._solved += 1
                    self._grant(token)
            self._maybe_wave1_done(demanded)

        await asyncio.gather(*(one() for _ in range(demanded)))
        self._wave1_done.set()

    def _grant(self, token: str) -> None:
        """Hand one arrival to the next lease in the round-robin sequence (skipping released
        keys), else to the reserve."""
        while self._next_grant < len(self._grant_order):
            key = self._grant_order[self._next_grant]
            self._next_grant += 1
            if key not in self._released:
                self._leases[key].append(token)
                self._granted[key] = self._granted.get(key, 0) + 1
                return
        self._reserve.append(token)

    def _maybe_wave1_done(self, demanded: int) -> None:
        # Wave 1 is resolved once every lease slot has been handed out, or every solve of the
        # fill has resolved (success or failure). Wave 2 keeps landing afterwards.
        if self._next_grant >= len(self._grant_order) or self._solved + self._failures >= demanded:
            self._wave1_done.set()

    async def _await_wave1(self) -> None:
        if self._t0 is None:
            await self._wave1_done.wait()
            return
        deadline = self._t0 - self._fill_deadline_before_t0
        while not self._wave1_done.is_set():
            remaining = (deadline - self._clock.now_utc()).total_seconds()
            if remaining <= 0:
                _log.warning(
                    "captcha pool: fill deadline (T0-%.0fs) reached with wave 1 incomplete.",
                    self._fill_deadline_before_t0.total_seconds(),
                )
                return
            await self._clock.sleep(min(_DEADLINE_POLL_S, remaining))

    def _snapshot_report(self) -> FillReport:
        assert self._started_at is not None
        return FillReport(
            demanded=len(self._grant_order) + self._reserve_target,
            solved=self._solved,
            failures=self._failures,
            granted=dict(self._granted),
            reserve=len(self._reserve),
            started_at=self._started_at,
            finished_at=self._clock.now_utc(),
        )

    def lease_size(self, key: LeaseKey) -> int:
        """Tokens currently in ``key``'s lease. Backs ``captcha_pool_size()``, so the
        orchestrator's burst ``n = min(len(blind_slots), captcha_pool_size())`` keeps its
        meaning (it excludes the shared reserve)."""
        lease = self._leases.get(key)
        return len(lease) if lease is not None else 0

    def pop(self, key: LeaseKey) -> str | None:
        """Pop the OLDEST token from ``key``'s lease, else from the shared reserve, else None
        (the caller inline-solves). Single-use; never returned to the pool."""
        lease = self._leases.get(key)
        if lease:
            return lease.popleft()
        if self._reserve:
            return self._reserve.popleft()
        return None

    async def solve_inline(self) -> str:
        """One inline solve under the per-course semaphore; ``TimeoutError`` -> ``CaptchaError``
        (today's ``ForeUpAdapter._solve_captcha_inline`` contract)."""
        try:
            async with self._inline_sem:
                return await self._provider()
        except TimeoutError as exc:
            raise CaptchaError(f"CAPTCHA solve timed out: {exc}") from exc

    def release(self, key: LeaseKey) -> None:
        """Move ``key``'s unused lease tokens into the shared reserve (the runner's ``finally``
        after that account's ``Orchestrator.run`` returns), so leftovers serve other fallbacks.

        Later fill arrivals skip a released key. The leftovers are OLDER than wave-2 reserve
        tokens, so they go to the FRONT of the reserve (FIFO pop keeps the freshest for last).
        """
        self._released.add(key)
        lease = self._leases.get(key)
        if lease:
            self._reserve.extendleft(reversed(lease))
            lease.clear()  # in place: the adapter holds a live reference to this deque

    def report(self) -> FillReport | None:
        """The coordinated fill's report, or None before or without a fill."""
        return self._report

    async def aclose(self) -> None:
        """Cancel any still-running fill solves (runner shutdown; tests). Idempotent."""
        if self._fill_task is not None and not self._fill_task.done():
            self._fill_task.cancel()
            await asyncio.gather(self._fill_task, return_exceptions=True)
