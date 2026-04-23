import asyncio
import json
import logging
from collections import OrderedDict, defaultdict
from collections.abc import Coroutine
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# Sentinel value to signal SSE streams to close
_DONE = object()


_MAX_COMPLETED_JOBS = 500


class JobManager:
    """Manages background pipeline tasks and SSE progress broadcasting."""

    def __init__(self, *, max_concurrent_jobs: int = 2):
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self._semaphore = asyncio.Semaphore(self.max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task] = {}
        self._progress_channels: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._completed_jobs: OrderedDict[str, None] = OrderedDict()

    async def _run_with_slot(self, coro: Coroutine[Any, Any, Any]) -> Any:
        acquired = False
        try:
            async with self._semaphore:
                acquired = True
                return await coro
        finally:
            if not acquired:
                coro.close()

    async def submit_job(
        self, job_id: str, coro: Coroutine[Any, Any, Any]
    ) -> asyncio.Task:
        # Guard against double-submission
        existing = self._tasks.get(job_id)
        if existing and not existing.done():
            coro.close()
            logger.warning(f"Job {job_id} already running, skipping duplicate submission")
            return existing

        try:
            task = asyncio.create_task(self._run_with_slot(coro), name=f"job-{job_id}")
        except Exception:
            coro.close()
            raise
        self._completed_jobs.pop(job_id, None)
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._on_task_done(job_id, t))
        return task

    def _on_task_done(self, job_id: str, task: asyncio.Task):
        self._tasks.pop(job_id, None)
        self._completed_jobs[job_id] = None
        # Prevent unbounded growth — evict oldest entries (FIFO)
        while len(self._completed_jobs) > _MAX_COMPLETED_JOBS:
            self._completed_jobs.popitem(last=False)
        try:
            if not task.cancelled():
                exception = task.exception()
                if exception:
                    logger.error(
                        f"Job {job_id} task failed with unhandled exception",
                        exc_info=(type(exception), exception, exception.__traceback__),
                    )
        except asyncio.CancelledError:
            pass

        # Signal all SSE subscribers that this job is done
        for queue in self._progress_channels.get(job_id, []):
            try:
                queue.put_nowait(_DONE)
            except asyncio.QueueFull:
                pass
        # Clean up empty channels to prevent dict growth
        if job_id in self._progress_channels and not self._progress_channels[job_id]:
            del self._progress_channels[job_id]

    def emit_progress(self, job_id: str, **event_data):
        event = json.dumps(event_data)
        for queue in self._progress_channels.get(job_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"Progress queue full for job {job_id}")

    def subscribe(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._progress_channels[job_id].append(queue)
        if job_id in self._completed_jobs:
            queue.put_nowait(_DONE)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue):
        channels = self._progress_channels.get(job_id, [])
        if queue in channels:
            channels.remove(queue)
        if not channels:
            self._progress_channels.pop(job_id, None)

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def cancel_job(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def shutdown(self):
        """Cancel all running tasks for graceful shutdown."""
        tasks_to_cancel = [
            task for task in self._tasks.values() if not task.done()
        ]
        if not tasks_to_cancel:
            return
        logger.info(f"Cancelling {len(tasks_to_cancel)} running job(s)...")
        for task in tasks_to_cancel:
            task.cancel()
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        logger.info("All job tasks cancelled")


_job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager(max_concurrent_jobs=get_settings().max_concurrent_jobs)
    return _job_manager
