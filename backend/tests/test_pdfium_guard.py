"""PDFium work from two threads at once, as two runner slots do it.

Without the guard in app/pdfium_guard.py this corrupts the heap within
seconds: PDFium refuses valid files ("Data format error"), and the process
dies of a segfault or a malloc abort. The work runs in a child process so a
crash fails the test instead of the test run.
"""

import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pypdfium2.raw as pdfium_c
import pytest
from pypdfium2.internal import bases

from app import pdfium_guard
from tests.pdf_fixtures import tagged_text_pages

WORKER = textwrap.dedent(
    """
    import gc, json, sys, threading, time
    from pathlib import Path

    from app.pipeline.pdfium_text import PdfiumText, pagination_artifact_indices, read_page_text

    paths = [Path(p) for p in json.loads(sys.argv[1])]
    seconds = float(sys.argv[2])

    def read(path):
        pages = read_page_text(path)
        text = PdfiumText(path)  # left to the garbage collector, as some call sites do
        words = [text.words(page) for page in range(len(text.document))]
        del text
        return [page.chars for page in pages], words, sorted(map(sorted, pagination_artifact_indices(path)))

    expected = {path: read(path) for path in paths}
    errors = []
    rounds = [0, 0]
    deadline = time.monotonic() + seconds

    def slot(index):
        turn = index
        while time.monotonic() < deadline:
            path = paths[turn % len(paths)]
            turn += 1
            try:
                if read(path) != expected[path]:
                    errors.append(f"{path.name}: different text")
            except Exception as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            if turn % 5 == 0:
                gc.collect()
            rounds[index] += 1

    threads = [threading.Thread(target=slot, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(json.dumps({"rounds": rounds, "errors": errors[:5], "error_count": len(errors)}))
    """
)


def test_two_threads_read_pdfs_without_corruption(tmp_path: Path) -> None:
    paths = [
        tagged_text_pages(
            tmp_path / f"doc{index}.pdf", pages=6, running_head=True, tagged_words=60 + 10 * index
        )
        for index in range(6)
    ]
    result = subprocess.run(
        [sys.executable, "-c", WORKER, json.dumps([str(path) for path in paths]), "4"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr[-2000:]}"
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["error_count"] == 0, report
    assert min(report["rounds"]) > 0, report


def test_guard_covers_raw_calls_and_the_close_path() -> None:
    serialized = [
        pdfium_c.FPDF_LoadDocument,
        pdfium_c.FPDFText_GetUnicode,
        pdfium_c.FPDF_ClosePage,
        bases._close_template,
        bases.AutoCloseable.close,
    ]
    assert all(getattr(function, "__pdfium_serialized__", False) for function in serialized)
    pdfium_guard.install()  # a second install adds no second layer
    assert not getattr(pdfium_c.FPDF_LoadDocument.__wrapped__, "__pdfium_serialized__", False)


def test_a_released_lock_goes_to_a_thread_that_has_waited() -> None:
    lock = pdfium_guard.FairRLock()
    order: list[str] = []
    lock.acquire()

    def waiter() -> None:
        with lock:
            order.append("waiter")

    thread = threading.Thread(target=waiter)
    thread.start()
    deadline = time.monotonic() + 10
    while not lock._waiters:  # wait for the waiter to queue; the deadline only guards a hang
        assert time.monotonic() < deadline, "the waiter never queued"
        time.sleep(0.001)
    time.sleep(0.02)  # longer than the lock's patience: the waiter is due
    lock.release()
    with lock:  # straight back, as a PDFium loop does: the waiter goes first
        order.append("holder")
    thread.join(timeout=10)
    assert order == ["waiter", "holder"]


def test_the_lock_is_reentrant_and_owned() -> None:
    lock = pdfium_guard.FairRLock()
    with lock, lock:
        assert lock.held_by_me()
    assert not lock.held_by_me()
    with pytest.raises(RuntimeError):
        lock.release()


def test_a_finalizer_never_waits_for_the_lock() -> None:
    lock = pdfium_guard.FairRLock()
    taken: list[bool] = []
    # The collector can run a finalizer inside the lock's own bookkeeping;
    # waiting there would deadlock the thread. Another thread stands in for
    # it so that a regression fails here instead of hanging.
    with lock._state:
        probe = threading.Thread(target=lambda: taken.append(lock.try_acquire()))
        probe.start()
        probe.join(timeout=5)
    assert taken == [False]


def test_a_close_another_thread_blocks_runs_at_its_release() -> None:
    closes: list[bool] = []
    close = pdfium_guard._finalizer_safe(lambda: closes.append(pdfium_guard.PDFIUM_LOCK.held_by_me()))
    holding, finalized = threading.Event(), threading.Event()

    def holder() -> None:
        with pdfium_guard.PDFIUM_LOCK:
            holding.set()
            assert finalized.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=10)
    close()  # as the collector would, on this thread, while the holder has the lock
    assert closes == []
    finalized.set()
    thread.join(timeout=10)
    assert closes == [True]
