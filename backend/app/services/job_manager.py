import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel value to signal SSE streams to close
_DONE = object()


class JobManager:
    """Manages background pipeline tasks and SSE progress broadcasting."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._progress_channels: dict[str, list[asyncio.Queue]] = defaultdict(list)

    async def submit_job(
        self, job_id: str, coro: Coroutine[Any, Any, Any]
    ) -> asyncio.Task:
        # Guard against double-submission
        existing = self._tasks.get(job_id)
        if existing and not existing.done():
            logger.warning(f"Job {job_id} already running, skipping duplicate submission")
            return existing

        task = asyncio.create_task(coro, name=f"job-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._on_task_done(job_id))
        return task

    def _on_task_done(self, job_id: str):
        self._tasks.pop(job_id, None)
        # Signal all SSE subscribers that this job is done
        for queue in self._progress_channels.get(job_id, []):
            try:
                queue.put_nowait(_DONE)
            except asyncio.QueueFull:
                pass

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


_job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager
