"""Pull work from the PDF Accessibility Worker and run it: ``python -m app.runner``.

The runner makes only outbound calls. It needs RUNNER_WORKER_URL (the
Worker's runner API, reached through Doorway) and RUNNER_TOKEN; Docling,
OCR, and validation use the same settings as v1.
"""

import asyncio
import logging
import signal
import socket

import httpx

from app.config import get_settings
from app.runner.client import WorkerClient
from app.runner.tasks import run_task
from app.services.file_storage import ensure_dirs

logger = logging.getLogger("app.runner")
IDLE_BACKOFF_SECONDS = 30.0


async def main() -> None:
    settings = get_settings()
    if not settings.runner_worker_url.strip() or not settings.runner_token.strip():
        raise SystemExit("RUNNER_WORKER_URL and RUNNER_TOKEN are required")
    ensure_dirs()
    runner_id = settings.runner_id.strip() or socket.gethostname()
    client = WorkerClient(settings.runner_worker_url, settings.runner_token)
    slots = asyncio.Semaphore(max(1, settings.runner_slots))
    running: set[asyncio.Task] = set()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)

    logger.info("Runner %s taking work from %s", runner_id, client.base_url)
    try:
        while not stopping.is_set():
            await slots.acquire()
            try:
                task = await client.claim(runner_id)
            except httpx.HTTPError as exc:
                slots.release()
                logger.warning("Claim failed (%s); waiting", exc)
                await _sleep_unless(stopping, IDLE_BACKOFF_SECONDS)
                continue
            if task is None:
                slots.release()
                await _sleep_unless(stopping, settings.runner_poll_seconds)
                continue
            logger.info("Took %s task for job %s (attempt %s)", task.kind, task.job_id, task.attempt)
            job = asyncio.create_task(run_task(client, task, settings))
            running.add(job)
            job.add_done_callback(running.discard)
            job.add_done_callback(lambda _done: slots.release())
    finally:
        # Unfinished tasks go back to the queue when their leases lapse.
        for job in running:
            job.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await client.close()


async def _sleep_unless(stopping: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
    except TimeoutError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
