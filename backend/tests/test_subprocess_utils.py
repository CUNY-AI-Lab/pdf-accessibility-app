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
