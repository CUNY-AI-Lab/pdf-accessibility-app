"""Shared async subprocess utilities for pipeline steps."""

import asyncio
import contextlib
import os
import signal
import subprocess


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
    except TimeoutError:
        await _terminate_process_tree(proc)
        raise SubprocessTimeout(timeout if timeout is not None else 0)
