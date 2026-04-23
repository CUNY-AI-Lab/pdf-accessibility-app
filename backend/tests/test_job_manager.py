import asyncio

import pytest

from app.services.job_manager import _DONE, JobManager


@pytest.mark.asyncio
async def test_submit_job_closes_duplicate_coroutine():
    manager = JobManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def running():
        started.set()
        await release.wait()

    task = await manager.submit_job("job-1", running())
    await started.wait()

    async def duplicate():
        await asyncio.sleep(0)

    duplicate_coro = duplicate()
    returned = await manager.submit_job("job-1", duplicate_coro)

    assert returned is task
    assert duplicate_coro.cr_frame is None

    release.set()
    await task


@pytest.mark.asyncio
async def test_cancel_job_cancels_running_task():
    manager = JobManager()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def running():
        try:
            started.set()
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = await manager.submit_job("job-1", running())
    await started.wait()

    assert await manager.cancel_job("job-1") is True
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert task.cancelled()
    assert manager.is_running("job-1") is False


@pytest.mark.asyncio
async def test_subscribe_after_completion_receives_done_sentinel():
    manager = JobManager()

    async def completed():
        return None

    task = await manager.submit_job("job-1", completed())
    await task
    await asyncio.sleep(0)

    queue = manager.subscribe("job-1")

    assert await asyncio.wait_for(queue.get(), timeout=1) is _DONE


@pytest.mark.asyncio
async def test_submit_job_limits_concurrent_execution():
    manager = JobManager(max_concurrent_jobs=1)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    async def first():
        first_started.set()
        await release_first.wait()

    async def second():
        second_started.set()
        await release_second.wait()

    first_task = await manager.submit_job("job-1", first())
    second_task = await manager.submit_job("job-2", second())

    await asyncio.wait_for(first_started.wait(), timeout=1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_started.wait(), timeout=0.05)

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    release_second.set()
    await asyncio.gather(first_task, second_task)
