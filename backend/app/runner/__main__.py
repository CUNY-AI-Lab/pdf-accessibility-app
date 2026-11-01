"""Pull work from the PDF Accessibility Worker and run it: ``python -m app.runner``.

The runner makes only outbound calls. It needs RUNNER_WORKER_URL (the
Worker's runner API, reached through Doorway) and RUNNER_TOKEN; Docling,
OCR, and validation use the shared pipeline settings.
"""

import asyncio
import logging
import signal
import socket
from enum import Enum
from pathlib import Path

import httpx

from app.config import get_settings
from app.runner.client import WorkerClient
from app.runner.state import cleanup_abandoned_attempts
from app.runner.tasks import run_task
from app.services.file_storage import ensure_dirs

logger = logging.getLogger("app.runner")
IDLE_BACKOFF_SECONDS = 30.0


class RunnerMode(Enum):
    ACCEPTING = "accepting"
    STOPPING = "stopping"
    STOPPED = "stopped"


class RunnerControl:
    def __init__(self):
        self.mode = RunnerMode.ACCEPTING
        self.stopping = asyncio.Event()

    def transition(self, target: RunnerMode) -> None:
        allowed = {
            RunnerMode.ACCEPTING: {RunnerMode.STOPPING},
            RunnerMode.STOPPING: {RunnerMode.STOPPED},
            RunnerMode.STOPPED: set(),
        }
        if target not in allowed[self.mode]:
            raise ValueError(f"Illegal claim-loop transition: {self.mode.value} -> {target.value}")
        self.mode = target
        if target is RunnerMode.STOPPING:
            self.stopping.set()

    def stop(self) -> None:
        if self.mode is RunnerMode.ACCEPTING:
            self.transition(RunnerMode.STOPPING)


async def acquire_slot(slots: asyncio.Semaphore, stopping: asyncio.Event) -> bool:
    acquire = asyncio.create_task(slots.acquire())
    stop = asyncio.create_task(stopping.wait())
    try:
        await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        acquire.cancel()
        stop.cancel()
        await asyncio.gather(acquire, stop, return_exceptions=True)
    acquired = not acquire.cancelled() and acquire.exception() is None
    if acquired and stopping.is_set():
        slots.release()
    return acquired and not stopping.is_set()


async def main() -> None:
    settings = get_settings()
    if not settings.runner_worker_url.strip() or not settings.runner_token.strip():
        raise SystemExit("RUNNER_WORKER_URL and RUNNER_TOKEN are required")
    ensure_dirs()
    cleanup_abandoned_attempts(settings)
    runner_id = settings.runner_id.strip() or socket.gethostname()
    client = WorkerClient(settings.runner_worker_url, settings.runner_token)
    slots = asyncio.Semaphore(max(1, settings.runner_slots))
    running: set[asyncio.Task] = set()
    control = RunnerControl()
    stopping = control.stopping
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, control.stop)

    logger.info("Runner %s taking work from %s", runner_id, client.base_url)
    alive = Path(settings.runner_alive_file) if settings.runner_alive_file.strip() else None
    heartbeat = asyncio.create_task(_stay_alive(alive, stopping)) if alive is not None else None
    try:
        while not stopping.is_set():
            if not await acquire_slot(slots, stopping):
                break
            try:
                task = await client.claim(runner_id)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                slots.release()
                logger.warning("Claim failed (%s); waiting", exc)
                await _sleep_unless(stopping, IDLE_BACKOFF_SECONDS)
                continue
            if stopping.is_set():
                slots.release()
                break
            if task is None:
                slots.release()
                await _sleep_unless(stopping, settings.runner_poll_seconds)
                continue
            logger.info(
                "Took %s task for job %s (attempt %s)", task.kind, task.job_id, task.attempt
            )
            job = asyncio.create_task(run_task(client, task, settings))
            running.add(job)
            job.add_done_callback(running.discard)
            job.add_done_callback(lambda _done: slots.release())
    finally:
        control.stop()
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        # Unfinished tasks go back to the queue when their leases lapse.
        for job in running:
            job.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await client.close()
        control.transition(RunnerMode.STOPPED)


async def _stay_alive(path: Path, stopping: asyncio.Event) -> None:
    """Touch the health check's file while the runner is running."""
    while not stopping.is_set():
        path.touch()
        await _sleep_unless(stopping, 30.0)


async def _sleep_unless(stopping: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
    except TimeoutError:
        pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(main())
