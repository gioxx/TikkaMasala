"""Answer a request within a grace period and let longer work finish on the server's event loop.

Long manual runs (Back up all, Run now) must not hit a reverse proxy's timeout. The work runs
as a task; the caller waits up to ``BACKGROUND_GRACE_SECONDS`` and, if it is still going, tells
the user it continues in the background. The task keeps running on this process's loop (a
restart drops it); there is no progress polling.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Read at call time, so tests (and a future setting) can change it.
BACKGROUND_GRACE_SECONDS = 25.0

# Strong references: the event loop keeps only weak ones, so an unreferenced task could be
# garbage-collected mid-run. Each task removes itself when it ends.
_background_tasks: set[asyncio.Task[Any]] = set()


def _log_unawaited_outcome(name: str, task: asyncio.Task[Any]) -> None:
    """Report how a task ended once nobody waits for it any more."""
    if task.cancelled():
        logger.warning("Background task %s was cancelled.", name)
        return
    exc = task.exception()  # also marks the exception as retrieved
    if exc is not None:
        logger.error("Background task %s failed.", name, exc_info=exc)


async def finish_or_continue(
    coro: Coroutine[Any, Any, T], *, name: str, grace: float | None = None
) -> tuple[bool, T | None]:
    """Run ``coro`` as a task and wait up to ``grace`` seconds (default ``BACKGROUND_GRACE_SECONDS``).

    Finished in time: ``(True, result)``; an exception raised by the work propagates to the caller.
    Still running: ``(False, None)``; the task carries on, and an exception it ends with is logged.
    Any lock the work holds stays held until the work itself ends. Cancelling the caller does not
    cancel the work.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    timeout = grace if grace is not None else BACKGROUND_GRACE_SECONDS
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        # The caller was cancelled while waiting: nobody will read the outcome, so log it.
        task.add_done_callback(lambda finished: _log_unawaited_outcome(name, finished))
        raise
    if task in done:
        return True, task.result()
    task.add_done_callback(lambda finished: _log_unawaited_outcome(name, finished))
    return False, None
