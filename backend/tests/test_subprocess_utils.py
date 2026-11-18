import os

from app.pipeline.subprocess_utils import subprocess_process_group_kwargs


def test_subprocess_kwargs_create_process_group_for_timeout_cleanup():
    kwargs = subprocess_process_group_kwargs()
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs == {"start_new_session": True}


async def test_cancellation_terminates_owned_process_tree():
    import asyncio
    import sys

    import pytest

    from app.pipeline.subprocess_utils import communicate_with_timeout

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_process_group_kwargs(),
    )
    waiting = asyncio.create_task(communicate_with_timeout(proc, 30))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiting, 2)
    assert proc.returncode is not None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers signal 0; it is dead for our purposes.
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split()[2] != "Z"
    except FileNotFoundError:
        return False


def test_run_bounded_kills_the_whole_process_tree_on_timeout(tmp_path):
    import subprocess
    import sys
    import time

    import pytest

    from app.pipeline.subprocess_utils import run_bounded

    if os.name == "nt":
        pytest.skip("process groups are POSIX")
    pidfile = tmp_path / "grandchild"
    # The child starts a grandchild and waits on it, as gs or ocrmypdf do.
    script = (
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, "-c", script], timeout=1.0, capture_output=True)
    assert time.monotonic() - started < 10
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_run_bounded_behaves_like_subprocess_run():
    import subprocess
    import sys

    import pytest

    from app.pipeline.subprocess_utils import run_bounded

    done = run_bounded(
        [sys.executable, "-c", "print('ok')"], timeout=10, capture_output=True, text=True
    )
    assert done.returncode == 0
    assert done.stdout == "ok\n"
    with pytest.raises(subprocess.CalledProcessError):
        run_bounded([sys.executable, "-c", "raise SystemExit(3)"], timeout=10, check=True)


def test_no_unbounded_sync_subprocess_calls_in_the_app():
    # Every synchronous child process goes through run_bounded, so a timeout
    # kills its whole tree and none can run without a limit.
    import re
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        f"{path.relative_to(app)}:{number}"
        for path in app.rglob("*.py")
        if path.name != "subprocess_utils.py"
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\bsubprocess\.(run|call|check_call|check_output|Popen)\(", line)
    ]
    assert offenders == []


async def test_owned_thread_work_gives_up_on_a_thread_that_will_not_stop():
    import asyncio
    import threading

    import pytest

    from app.services.thread_work import ThreadsOverran, owned_thread_work, to_thread

    release = threading.Event()
    try:
        with pytest.raises(ThreadsOverran):
            async with owned_thread_work(join_timeout=0.2):
                work = asyncio.create_task(to_thread(release.wait, 30))
                await asyncio.sleep(0.05)
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
    finally:
        release.set()


async def test_owned_thread_work_still_joins_threads_that_finish():
    import asyncio
    import threading

    from app.services.thread_work import owned_thread_work, to_thread

    finished = threading.Event()

    def slow():
        import time

        time.sleep(0.1)
        finished.set()

    async with owned_thread_work(join_timeout=5):
        work = asyncio.create_task(to_thread(slow))
        await asyncio.sleep(0.01)
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert finished.is_set()
