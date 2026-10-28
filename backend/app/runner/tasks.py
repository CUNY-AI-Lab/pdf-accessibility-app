"""One task from the Worker, start to finish.

A ``process`` task runs the whole pipeline on an uploaded PDF. A ``review``
task restores the job's saved state, applies one review action (undo,
revise, or edit), and re-runs tagging, as v1's review routes do. A ``sync``
task restores the state, records the statuses a person changed with keep or
resolve, and re-renders the views and report. Every task ends by uploading
what the Worker serves and calling ``complete`` (or ``fail``); nothing stays
on the host.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import update

from app.api.jobs import PIPELINE_STEPS
from app.api.review import apply_review_action
from app.config import Settings
from app.models import AppliedChange, Job, JobStep, ReviewTask
from app.pipeline.orchestrator import run_pipeline, run_tagging_and_validation
from app.runner.client import LeaseLost, Task, WorkerClient
from app.runner.state import (
    input_path,
    open_job_database,
    pack_state,
    remove_job_files,
    unpack_state,
)
from app.runner.views import OUTPUT_READY_STATUSES, RenderedViews, render_views
from app.services.job_state import clear_terminal_artifacts, reset_rerun_steps
from app.services.llm_client import job_model_access
from app.services.pdf_preflight import PdfUploadPreflightError, preflight_pdf_upload

logger = logging.getLogger(__name__)

RUNNER_OWNER = "runner"
DEADLINE_ERROR = "Processing took longer than the time limit, so it was stopped."
INTERNAL_ERROR = "The PDF could not be processed because of an internal error."


class ProgressSink:
    """The pipeline's job manager on the runner: queues step events for the Worker."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit_progress(self, job_id: str, **event: Any) -> None:
        self.events.put_nowait(
            json.loads(json.dumps({k: v for k, v in event.items() if v is not None}, default=str))
        )


@dataclass
class Outcome:
    views: RenderedViews | None = None
    failed: str | None = None
    review_refused: str | None = None


async def run_task(client: WorkerClient, task: Task, settings: Settings) -> None:
    remove_job_files(settings, task.job_id)
    sink = ProgressSink()
    work = asyncio.create_task(_perform(client, task, settings, sink))
    forward = asyncio.create_task(
        _forward_progress(client, task, sink, settings.runner_heartbeat_seconds)
    )
    try:
        remaining = task.deadline_ms / 1000 - time.time()
        done, _ = await asyncio.wait(
            {work, forward}, timeout=max(remaining, 0), return_when=asyncio.FIRST_COMPLETED
        )
        if work not in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            if forward in done:
                logger.warning("Stopped job %s: %s", task.job_id, forward.exception())
                return
            logger.warning("Job %s reached its deadline", task.job_id)
            await _report_failure(client, task, DEADLINE_ERROR)
            return
        forward.cancel()
        await asyncio.gather(forward, return_exceptions=True)
        await _finish(client, task, settings, work.result())
    except LeaseLost as exc:
        logger.warning("Job %s is no longer this runner's: %s", task.job_id, exc)
    except Exception:
        logger.exception("Task %s for job %s failed", task.kind, task.job_id)
        await _report_failure(client, task, INTERNAL_ERROR)
    finally:
        for pending in (work, forward):
            pending.cancel()
        await asyncio.gather(work, forward, return_exceptions=True)
        remove_job_files(settings, task.job_id)


async def _report_failure(client: WorkerClient, task: Task, error: str) -> None:
    try:
        await client.fail(task, error)
    except LeaseLost:
        pass
    except httpx.HTTPError:
        logger.exception("Could not report the failure of job %s", task.job_id)


async def _forward_progress(
    client: WorkerClient, task: Task, sink: ProgressSink, heartbeat_seconds: float
) -> None:
    """Send step events as they come, and an empty one to renew the lease."""
    while True:
        try:
            event = await asyncio.wait_for(sink.events.get(), timeout=heartbeat_seconds)
        except TimeoutError:
            event = {}
        try:
            await client.progress(task, event)
        except LeaseLost:
            raise
        except httpx.HTTPError as exc:
            logger.warning("Progress for job %s not delivered: %s", task.job_id, exc)


async def _perform(
    client: WorkerClient, task: Task, settings: Settings, sink: ProgressSink
) -> Outcome:
    with job_model_access(client.model_endpoint(task)):
        if task.kind == "process":
            return await _process(client, task, settings, sink)
        return await _revisit(client, task, settings, sink)


async def _process(
    client: WorkerClient, task: Task, settings: Settings, sink: ProgressSink
) -> Outcome:
    path = input_path(settings, task.job_id)
    await client.download(task, "input", path)
    try:
        await asyncio.to_thread(preflight_pdf_upload, path, settings=settings)
    except PdfUploadPreflightError as exc:
        return Outcome(failed=exc.detail)

    engine, session_maker = await open_job_database(settings, task.job_id)
    try:
        async with session_maker() as db:
            db.add(
                Job(
                    id=task.job_id,
                    filename=path.name,
                    original_filename=task.original_filename,
                    owner_session_hash=RUNNER_OWNER,
                    input_path=str(path),
                    file_size_bytes=path.stat().st_size,
                )
            )
            for step_name in PIPELINE_STEPS:
                db.add(JobStep(job_id=task.job_id, step_name=step_name))
            await db.commit()
        await run_pipeline(task.job_id, session_maker, settings, sink)
        async with session_maker() as db:
            return Outcome(views=await render_views(db, task.job_id))
    finally:
        await engine.dispose()


async def _revisit(
    client: WorkerClient, task: Task, settings: Settings, sink: ProgressSink
) -> Outcome:
    archive = input_path(settings, task.job_id).with_suffix(".state.tar.gz")
    await client.download(task, "state", archive)
    unpack_state(settings, task.job_id, archive)
    archive.unlink()

    engine, session_maker = await open_job_database(settings, task.job_id)
    try:
        async with session_maker() as db:
            await apply_overrides(db, task.job_id, task.overrides)
            job = await db.get(Job, task.job_id)
            if task.kind == "review":
                action = task.action or {}
                try:
                    await apply_review_action(
                        job=job,
                        db=db,
                        action=str(action.get("type")),
                        change_id=int(action.get("change_id", 0)),
                        feedback=action.get("feedback"),
                        text=action.get("text"),
                    )
                except HTTPException as exc:
                    await db.rollback()
                    return Outcome(review_refused=str(exc.detail))
                job.status = "processing"
                job.error = None
                clear_terminal_artifacts(job)
                await reset_rerun_steps(db, task.job_id)
                await db.commit()
                await run_tagging_and_validation(task.job_id, db, settings, sink)
            else:
                await db.commit()
        async with session_maker() as db:
            return Outcome(views=await render_views(db, task.job_id))
    finally:
        await engine.dispose()


async def apply_overrides(db, job_id: str, overrides: dict[str, dict[str, str]]) -> None:
    """Record the review statuses a person set in the Worker since the last task."""
    for change_id, status in (overrides.get("changes") or {}).items():
        if status in {"kept", "undone", "pending_review"}:
            await db.execute(
                update(AppliedChange)
                .where(AppliedChange.job_id == job_id, AppliedChange.id == int(change_id))
                .values(review_status=status)
            )
    for task_id, status in (overrides.get("tasks") or {}).items():
        if status in {"resolved", "pending_review"}:
            await db.execute(
                update(ReviewTask)
                .where(ReviewTask.job_id == job_id, ReviewTask.id == int(task_id))
                .values(status=status)
            )


async def _finish(client: WorkerClient, task: Task, settings: Settings, outcome: Outcome) -> None:
    if outcome.failed is not None:
        await client.fail(task, outcome.failed)
        return
    if outcome.review_refused is not None:
        await client.complete(
            task,
            {"review_refused": outcome.review_refused, "overrides_version": task.overrides_version},
        )
        return

    views = outcome.views
    assert views is not None
    for rendered in views.files:
        await client.upload(task, rendered.name, rendered.body, rendered.content_type)
    has_state = views.job["status"] in OUTPUT_READY_STATUSES
    if has_state:
        archive = input_path(settings, task.job_id).with_suffix(".state.tar.gz")
        await asyncio.to_thread(pack_state, settings, task.job_id, archive)
        await client.upload(task, "state.tar.gz", archive, "application/gzip")
    await client.complete(
        task,
        {
            "job": views.job,
            "figures": views.figures,
            "previews": views.previews,
            "files": [rendered.name for rendered in views.files],
            "has_state": has_state,
            "overrides_version": task.overrides_version,
        },
    )
