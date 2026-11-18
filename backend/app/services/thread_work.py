"""Keep a runner attempt's files alive until its owned thread work has joined."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any


class ThreadsOverran(Exception):
    """Owned threads did not finish within the join limit.

    Python cannot stop a thread, so the process that owns one must exit;
    see ``app.runner.__main__``.
    """


_owned_threads: ContextVar[set[asyncio.Task] | None] = ContextVar("owned_threads", default=None)


async def to_thread[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    owned = _owned_threads.get()
    if owned is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    owned.add(work)

    def joined(done: asyncio.Task) -> None:
        owned.discard(done)
        if not done.cancelled():
            done.exception()

    work.add_done_callback(joined)
    # Shield the thread's join handle, not its caller: cancellation still stops
    # the pipeline immediately, and the owner joins before removing any files.
    return await asyncio.shield(work)


@asynccontextmanager
async def owned_thread_work(*, join_timeout: float | None = None):
    """Own the threads started inside, and join them all on the way out.

    With ``join_timeout``, threads still running that many seconds after the
    body ends raise ``ThreadsOverran`` instead of holding the caller forever.
    """
    owned: set[asyncio.Task] = set()
    token = _owned_threads.set(owned)
    try:
        yield
    finally:
        joined = asyncio.gather(*owned, return_exceptions=True)
        loop = asyncio.get_running_loop()
        give_up_at = None if join_timeout is None else loop.time() + join_timeout
        cancelled = False
        try:
            while not joined.done():
                remaining = None if give_up_at is None else give_up_at - loop.time()
                if remaining is not None and remaining <= 0:
                    raise ThreadsOverran(
                        f"{len(owned)} thread(s) still running"
                        f" {join_timeout:.0f} s after the task ended"
                    )
                try:
                    # asyncio.wait never cancels what it waits for.
                    await asyncio.wait({joined}, timeout=remaining)
                except asyncio.CancelledError:
                    # Shutdown can cancel an attempt already joining after lease
                    # loss. Keep its file owner alive through repeated cancellation.
                    cancelled = True
            joined.result()
        finally:
            _owned_threads.reset(token)
        if cancelled:
            raise asyncio.CancelledError
