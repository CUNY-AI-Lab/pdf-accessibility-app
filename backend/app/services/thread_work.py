"""Keep a runner attempt's files alive until its owned thread work has joined."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

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
async def owned_thread_work():
    owned: set[asyncio.Task] = set()
    token = _owned_threads.set(owned)
    try:
        yield
    finally:
        joined = asyncio.gather(*owned, return_exceptions=True)
        cancelled = False
        try:
            while not joined.done():
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    # Shutdown can cancel an attempt already joining after lease
                    # loss. Keep its file owner alive through repeated cancellation.
                    cancelled = True
            joined.result()
        finally:
            _owned_threads.reset(token)
        if cancelled:
            raise asyncio.CancelledError
