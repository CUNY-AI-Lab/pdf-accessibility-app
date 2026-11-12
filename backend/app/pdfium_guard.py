"""One PDFium call at a time in this process.

PDFium is not thread-safe: two threads may not call it at once, even on
different documents (pypdfium2, "Incompatibility with Threading"). The runner
works on two jobs at a time and the pipeline does PDFium work in worker
threads, so without a lock the process corrupts its heap and dies, and
PDFium refuses to open valid files while another thread is inside it.

``install`` puts every PDFium function in ``pypdfium2.raw`` behind one
re-entrant lock, together with pypdfium2's close path. pypdfium2's helpers
look these functions up on ``pypdfium2.raw`` when they call them, so the lock
covers the helpers, the app's own raw calls, and the finalizers of objects
created after ``install``. Holding the lock through ``AutoCloseable.close``
and ``_close_template`` means an object the garbage collector finalizes on
another thread is never closed while its document is being closed.

The garbage collector can run a finalizer at almost any point in Python
code, including inside this lock's own bookkeeping, so a finalizer never
waits for the lock: it closes its object at once when it can take the lock
without waiting, and otherwise leaves the close for the lock's next release.

``app/__init__.py`` installs it, so any process that imports the app has it
before it opens a document.
"""

import functools
import threading
import time
from _ctypes import CFuncPtr
from collections import deque
from collections.abc import Callable
from typing import Any

import pypdfium2.raw as pdfium_c
from pypdfium2.internal import bases


class _Waiter:
    def __init__(self, ident: int, since: float) -> None:
        self.ident = ident
        self.since = since
        self.wake = threading.Lock()
        self.wake.acquire()

    def signal(self) -> None:
        if self.wake.locked():
            self.wake.release()


class FairRLock:
    """A re-entrant lock no thread waits on for long.

    ``threading.RLock`` lets the thread that releases it take it straight
    back, so a thread in a PDFium loop can keep it for the whole loop: on
    Linux, two threads reading PDFs got 110 rounds and 1. The pipeline also
    calls PDFium on the event loop's thread, where a wait that long would
    stall every task's lease renewal. Handing the lock to the next waiter
    on every release is fair but switches threads on every PDFium call.

    This follows Go's ``sync.Mutex``: a releasing thread may take the lock
    back, until the longest waiter has waited ``patience`` seconds; from then
    on each release hands it to the longest waiter, and newcomers queue.

    Finalizers use ``try_acquire`` and ``defer``, never ``acquire``: a
    finalizer the garbage collector runs inside this lock's bookkeeping, on
    the same thread, would otherwise wait for that bookkeeping to finish.
    """

    def __init__(self, patience: float = 0.001) -> None:
        self._state = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self._waiters: deque[_Waiter] = deque()
        self._patience = patience
        self._deferred: deque[Callable[[], object]] = deque()

    def _starving(self) -> bool:
        return bool(self._waiters) and time.monotonic() - self._waiters[0].since >= self._patience

    def acquire(self) -> bool:
        me = threading.get_ident()
        # Only this thread sets the owner to itself, or hands it on.
        if self._owner == me:
            self._depth += 1
            return True
        with self._state:
            if self._owner is None and not self._starving():
                self._owner, self._depth = me, 1
                return True
            waiter = _Waiter(me, time.monotonic())
            self._waiters.append(waiter)
        try:
            while True:
                waiter.wake.acquire(timeout=self._patience)
                with self._state:
                    if self._owner == me:  # handed over by a release
                        return True
                    if self._owner is None and self._waiters[0] is waiter:
                        self._waiters.popleft()
                        self._owner, self._depth = me, 1
                        return True
        except BaseException:
            with self._state:
                if self._owner != me:
                    self._waiters.remove(waiter)
                    if self._owner is None and self._waiters:
                        self._waiters[0].signal()
                    raise
            self.release()
            raise

    def try_acquire(self) -> bool:
        """Take the lock if this thread holds it or it is free with no one
        waiting. Never waits, not even for the lock's bookkeeping."""
        me = threading.get_ident()
        if self._owner == me:
            self._depth += 1
            return True
        if not self._state.acquire(blocking=False):
            return False
        try:
            if self._owner is None and not self._waiters:
                self._owner, self._depth = me, 1
                return True
            return False
        finally:
            self._state.release()

    def defer(self, work: Callable[[], object]) -> None:
        """Run ``work`` under the lock at its next release."""
        self._deferred.append(work)

    def release(self) -> None:
        if self._owner != threading.get_ident():
            raise RuntimeError("release of a FairRLock this thread does not hold")
        if self._depth > 1:
            self._depth -= 1
            return
        while self._deferred:
            work = self._deferred.popleft()
            try:
                work()
            except Exception:  # a finalizer's failure, which the collector would ignore too
                pass
        with self._state:
            if self._starving():
                head = self._waiters.popleft()
                self._owner, self._depth = head.ident, 1
                head.signal()
            else:
                self._owner, self._depth = None, 0
                if self._waiters:
                    self._waiters[0].signal()

    def held_by_me(self) -> bool:
        return self._owner == threading.get_ident()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


PDFIUM_LOCK = FairRLock()


def _serialized[T](function: Callable[..., T]) -> Callable[..., T]:
    @functools.wraps(function)
    def call(*args: Any, **kwargs: Any) -> T:
        with PDFIUM_LOCK:
            return function(*args, **kwargs)

    call.__pdfium_serialized__ = True  # type: ignore[attr-defined]
    return call


def _finalizer_safe(close: Callable[..., object]) -> Callable[..., object]:
    """pypdfium2's ``_close_template``, which finalizers call: closes now
    when the lock is free or this thread's, and otherwise at its release."""

    @functools.wraps(close)
    def call(*args: Any, **kwargs: Any) -> object:
        if not PDFIUM_LOCK.try_acquire():
            PDFIUM_LOCK.defer(functools.partial(close, *args, **kwargs))
            return None
        try:
            return close(*args, **kwargs)
        finally:
            PDFIUM_LOCK.release()

    call.__pdfium_serialized__ = True  # type: ignore[attr-defined]
    return call


def _is_serialized(function: object) -> bool:
    return getattr(function, "__pdfium_serialized__", False)


def installed() -> bool:
    return _is_serialized(bases.AutoCloseable.close)


def install() -> None:
    """Serialize this process's PDFium calls. Safe to call more than once."""
    with PDFIUM_LOCK:
        if installed():
            return
        if not callable(getattr(bases, "_close_template", None)) or not callable(
            getattr(bases.AutoCloseable, "close", None)
        ):
            raise RuntimeError(
                "pypdfium2's close path has changed; review app/pdfium_guard.py "
                "before using this pypdfium2 version"
            )
        for name, value in list(vars(pdfium_c).items()):
            # Library functions carry their symbol name; ctypes' own
            # memmove and memset, re-exported here, do not.
            if isinstance(value, CFuncPtr) and getattr(value, "__name__", None) == name:
                setattr(pdfium_c, name, _serialized(value))
        bases._close_template = _finalizer_safe(bases._close_template)
        bases.AutoCloseable.close = _serialized(bases.AutoCloseable.close)
