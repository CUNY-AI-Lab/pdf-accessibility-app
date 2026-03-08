"""Shared async subprocess utilities for pipeline steps."""

import asyncio


class SubprocessTimeout(Exception):
    """Raised when an async subprocess exceeds its timeout."""

    def __init__(self, timeout: int | float, stderr: bytes = b""):
        self.timeout = timeout
        self.stderr = stderr
        super().__init__(f"Subprocess timed out after {timeout}s")


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
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SubprocessTimeout(timeout if timeout is not None else 0)
