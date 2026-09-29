"""Work the web does AFTER responding (operator report 2026-09-29).

Invite, Resend invite and Report a bug used to await the email send (ACS polls the operation
status every 2 s) and the GitHub issue before redirecting, so the page hung 5-20 s. Those
handlers now ``spawn`` the send and respond at once. ``BackgroundJobs`` keeps a strong reference
to each task (the event loop holds only a weak one, so an unreferenced task can be garbage
collected mid-flight) and logs a failure by exception CLASS NAME only (the message could carry
an address). ``drain`` awaits what is pending, bounded, then cancels the rest: the app's
lifespan calls it on shutdown, tests call it before asserting.

A job is best-effort: a process that dies mid-send loses it, exactly like a request that timed
out before. Each job bounds its own calls and writes its own audit entry when it finishes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)


class BackgroundJobs:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            log.warning("background job %s cancelled", task.get_name())
            return
        exc = task.exception()
        if exc is not None:
            log.warning("background job %s failed: %s", task.get_name(), type(exc).__name__)

    async def drain(self, timeout_s: float) -> None:
        """Wait up to ``timeout_s`` for every pending job, then cancel what is left."""
        tasks = set(self._tasks)
        if not tasks:
            return
        _, still_running = await asyncio.wait(tasks, timeout=timeout_s)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.wait(still_running)
