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

STUB — not wired into ``ForeUpAdapter`` yet. Implemented in MULTIUSER_PLAN MU-2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import NewType

from ...core.clock import Clock

_MU2 = "MULTIUSER_PLAN.md MU-2"

# Opaque per-adapter lease key (the tenant runner uses the row id; a private pool uses a
# constant). A NewType so a lease key can't be confused with a SlotId or CourseId.
LeaseKey = NewType("LeaseKey", str)


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
        raise NotImplementedError(_MU2)

    # --- coordinated-mode setup (tenant runner, pre-T0, before any orchestrator runs) ----

    def register(self, key: LeaseKey, count: int) -> None:
        """Declare that ``key`` will want ``count`` burst tokens. Registration order IS the
        round-robin grant order (the §5.4 draft order). Raises if a fill already started."""
        raise NotImplementedError(_MU2)

    def set_reserve(self, count: int) -> None:
        """Size of the shared reserve R, filled by wave 2 (§5.3)."""
        raise NotImplementedError(_MU2)

    def arm(self, *, t0: datetime) -> None:
        """Record T0 so the fill can honour ``fill_deadline_before_t0_s``. Coordinated mode only."""
        raise NotImplementedError(_MU2)

    # --- adapter-facing API (called from ForeUpAdapter.prepare_book / book) -------------

    async def prefetch(self, key: LeaseKey, count: int) -> None:
        """``prepare_book`` entry point.

        Uncoordinated key: solve ``count`` concurrently into ``key``'s lease, with today's NI10
        raise contract. Coordinated key: start the single fill if it is not running, then await
        wave-1 completion or the deadline. ``count`` is ignored because demand was registered.
        """
        raise NotImplementedError(_MU2)

    def lease_size(self, key: LeaseKey) -> int:
        """Tokens currently in ``key``'s lease. Backs ``captcha_pool_size()``, so the
        orchestrator's burst ``n = min(len(blind_slots), captcha_pool_size())`` keeps its
        meaning (it excludes the shared reserve)."""
        raise NotImplementedError(_MU2)

    def pop(self, key: LeaseKey) -> str | None:
        """Pop the OLDEST token from ``key``'s lease, else from the shared reserve, else None
        (the caller inline-solves). Single-use; never returned to the pool."""
        raise NotImplementedError(_MU2)

    async def solve_inline(self) -> str:
        """One inline solve under the per-course semaphore; ``TimeoutError`` -> ``CaptchaError``
        (today's ``ForeUpAdapter._solve_captcha_inline`` contract)."""
        raise NotImplementedError(_MU2)

    def release(self, key: LeaseKey) -> None:
        """Move ``key``'s unused lease tokens into the shared reserve (the runner's ``finally``
        after that account's ``Orchestrator.run`` returns), so leftovers serve other fallbacks."""
        raise NotImplementedError(_MU2)

    def report(self) -> FillReport | None:
        """The coordinated fill's report, or None before or without a fill."""
        raise NotImplementedError(_MU2)
