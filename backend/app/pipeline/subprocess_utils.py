"""Shared async subprocess utilities for pipeline steps."""

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence


class SubprocessTimeout(Exception):
    """Raised when an async subprocess exceeds its timeout."""

    def __init__(self, timeout: int | float, stderr: bytes = b""):
        self.timeout = timeout
        self.stderr = stderr
        super().__init__(f"Subprocess timed out after {timeout}s")


def subprocess_process_group_kwargs() -> dict[str, object]:
    """Return kwargs that isolate child process trees for timeout cleanup."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 5.0,
) -> None:
    if proc.returncode is not None:
        return

    if os.name == "nt":
        proc.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
            return
        proc.kill()
        await proc.wait()
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


async def communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    timeout: int | float | None,
) -> tuple[bytes, bytes]:
    """Run proc.communicate() with a timeout, killing the process if exceeded.

    Returns (stdout, stderr) on success.
    Raises SubprocessTimeout if the process does not finish within *timeout* seconds.
    If *timeout* is None the call waits indefinitely (no timeout applied).
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await _terminate_process_tree(proc)
        raise
    except TimeoutError:
        await _terminate_process_tree(proc)
        raise SubprocessTimeout(timeout if timeout is not None else 0)


def _terminate_sync(proc: subprocess.Popen, grace_seconds: float) -> None:
    if os.name == "nt":
        proc.kill()
        proc.wait()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace_seconds)
    # Kill what is left of the group even if the child itself exited: its
    # own children may have ignored SIGTERM.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def run_bounded(
    args: Sequence[str],
    *,
    timeout: float,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    env: Mapping[str, str] | None = None,
    grace_seconds: float = 5.0,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for synchronous code, with a required timeout.

    The child runs in its own process group, and a timeout or interruption
    kills the whole group, so tools that start helpers (Ghostscript,
    OCRmyPDF, Tesseract) cannot leave them running. Raises the same
    ``TimeoutExpired`` and ``CalledProcessError`` as ``subprocess.run``.
    """
    pipe = subprocess.PIPE if capture_output else None
    with subprocess.Popen(
        list(args),
        stdout=pipe,
        stderr=pipe,
        text=text,
        env=env,
        **subprocess_process_group_kwargs(),  # type: ignore[arg-type]
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_sync(proc, grace_seconds)
            raise subprocess.TimeoutExpired(list(args), timeout) from None
        except BaseException:
            _terminate_sync(proc, grace_seconds)
            raise
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, list(args), stdout, stderr)
    return subprocess.CompletedProcess(list(args), proc.returncode, stdout, stderr)
