"""Pull work from the PDF Accessibility Worker and run it: ``python -m app.runner``.

The runner makes only outbound calls. It needs RUNNER_WORKER_URL (the
Worker's runner API, reached through Doorway) and RUNNER_TOKEN; Docling,
OCR, and validation use the shared pipeline settings.
"""

import asyncio
import logging
import signal
import socket
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path

import httpx

from app.config import get_settings
from app.runner.client import LeaseLost, Task, WorkerClient
from app.runner.state import cleanup_abandoned_attempts
from app.runner.tasks import run_task
from app.services.file_storage import ensure_dirs

logger = logging.getLogger("app.runner")
IDLE_BACKOFF_SECONDS = 30.0
HEALTH_BEAT_SECONDS = 30.0
# Consecutive 401/403 answers to a claim before the runner exits. A revoked or
# wrong RUNNER_TOKEN never heals by retrying, so it should restart loudly.
AUTH_REFUSAL_LIMIT = 3


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


class RunnerHealth:
    """Whether the runner can take work, judged by the Worker's answers.

    Healthy means the Worker answered recently, through a claim or a task's
    lease renewal, so a runner whose slots are all busy still reads as alive.
    After ``give_up_after`` seconds without an answer the runner stops with an
    error, so Docker restarts it and the failure shows.
    """

    def __init__(
        self,
        client,
        *,
        started: float,
        stale_after: float,
        give_up_after: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.started = started
        self.stale_after = stale_after
        self.give_up_after = give_up_after
        self.clock = clock
        self.failure: str | None = None

    def _silence(self) -> float:
        last = self.client.last_contact
        return self.clock() - (self.started if last is None else last)

    def fresh(self) -> bool:
        return self.client.last_contact is not None and self._silence() < self.stale_after

    def gave_up(self) -> bool:
        return self._silence() >= self.give_up_after


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
    running: dict[asyncio.Task, Task] = {}
    control = RunnerControl()
    stopping = control.stopping
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, control.stop)

    logger.info("Runner %s taking work from %s", runner_id, client.base_url)
    health = RunnerHealth(
        client,
        started=time.monotonic(),
        stale_after=settings.runner_health_stale_seconds,
        give_up_after=settings.runner_contact_timeout_seconds,
    )
    alive = Path(settings.runner_alive_file) if settings.runner_alive_file.strip() else None
    heartbeat = (
        asyncio.create_task(_stay_alive(alive, control, health)) if alive is not None else None
    )
    refusals = 0
    try:
        while not stopping.is_set():
            if not await acquire_slot(slots, stopping):
                break
            try:
                task = await client.claim(runner_id)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                slots.release()
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                refusals = refusals + 1 if status in {401, 403} else 0
                if refusals >= AUTH_REFUSAL_LIMIT:
                    health.failure = (
                        f"the Worker refused RUNNER_TOKEN {refusals} times in a row (HTTP {status})"
                    )
                    break
                logger.warning("Claim failed (%s); waiting", exc)
                await _sleep_unless(stopping, IDLE_BACKOFF_SECONDS)
                continue
            refusals = 0
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
            running[job] = task
            job.add_done_callback(lambda done: running.pop(done, None))
            job.add_done_callback(lambda _done: slots.release())
    finally:
        control.stop()
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await _drain(client, running, settings.runner_drain_seconds)
        await client.close()
        control.transition(RunnerMode.STOPPED)
    if health.failure is not None:
        logger.error("Runner stopping: %s", health.failure)
        raise SystemExit(f"Runner stopped: {health.failure}")


async def _drain(
    client: WorkerClient, running: dict[asyncio.Task, Task], grace_seconds: float
) -> None:
    """Let running tasks finish for a while, then hand the rest back to the Worker.

    A released task is queued again without spending one of its attempts;
    one that can't be released comes back when its lease lapses.
    """
    if running:
        logger.info("Waiting up to %.0f s for %d task(s) to finish", grace_seconds, len(running))
        await asyncio.wait(set(running), timeout=grace_seconds)
    unfinished = {job: task for job, task in running.items() if not job.done()}
    for job in unfinished:
        job.cancel()
    # Release before joining: a cancelled task may wait on a thread that
    # won't stop, and the process may be killed before it does.
    results = await asyncio.gather(
        *(client.release(task) for task in unfinished.values()), return_exceptions=True
    )
    for task, result in zip(unfinished.values(), results, strict=True):
        if isinstance(result, LeaseLost):
            logger.info("Job %s was no longer held, so not released", task.job_id)
        elif isinstance(result, BaseException):
            logger.warning("Could not release job %s: %r", task.job_id, result)
        else:
            logger.info("Released job %s back to the queue", task.job_id)
    await asyncio.gather(*unfinished, return_exceptions=True)


async def _stay_alive(path: Path, control: RunnerControl, health: RunnerHealth) -> None:
    """Touch the health check's file while the Worker answers; give up when it won't."""
    while not control.stopping.is_set():
        if health.gave_up():
            health.failure = (
                f"no answer from the Worker for {health.give_up_after:.0f} seconds"
            )
            control.stop()
            return
        if health.fresh():
            path.touch()
        await _sleep_unless(control.stopping, HEALTH_BEAT_SECONDS)


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
