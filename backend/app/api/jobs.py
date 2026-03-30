import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, Job, JobStep, ReviewTask
from app.pipeline.orchestrator import run_pipeline
from app.schemas import JobCreateResponse, JobListResponse, JobResponse, JobStepResponse
from app.services.anonymous_sessions import AnonymousSession, get_anonymous_session
from app.services.file_storage import cleanup_job_files, save_upload
from app.services.html_report import render_batch_html_report
from app.services.job_manager import _DONE, JobManager, get_job_manager
from app.services.job_state import (
    CLEANUP_INTERRUPTED_ERROR,
    mark_job_failed,
)
from app.services.path_safety import safe_filename

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger(__name__)

PIPELINE_STEPS = ["classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"]
PIPELINE_STEP_ORDER = {name: idx for idx, name in enumerate(PIPELINE_STEPS)}
JOB_START_FAILURE_ERROR = "Accessibility processing could not start. Please upload the PDF again."
JOB_START_FAILURE_DETAIL = "Failed to start accessibility processing. Please upload again."


def _parse_result_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _validation_compliant(job: Job) -> bool | None:
    report = _parse_result_json(job.validation_json)
    if isinstance(report, dict) and isinstance(report.get("compliant"), bool):
        return report["compliant"]

    return None


def job_to_response(job: Job, *, include_step_results: bool = True) -> JobResponse:
    return JobResponse(
        id=job.id,
        filename=job.filename,
        original_filename=job.original_filename,
        status=job.status,
        classification=job.classification,
        ocr_language=job.ocr_language,
        page_count=job.page_count,
        file_size_bytes=job.file_size_bytes,
        error=job.error,
        validation_compliant=_validation_compliant(job),
        created_at=job.created_at,
        updated_at=job.updated_at,
        steps=[
            JobStepResponse(
                step_name=s.step_name,
                status=s.status,
                started_at=s.started_at,
                completed_at=s.completed_at,
                error=s.error,
                result=_parse_result_json(s.result_json) if include_step_results else None,
            )
            for s in sorted(
                job.steps,
                key=lambda s: PIPELINE_STEP_ORDER.get(s.step_name, len(PIPELINE_STEPS)),
            )
        ],
    )


async def _compensate_failed_job_creation(
    *,
    db: AsyncSession,
    created_jobs: list[Job],
    submitted_job_ids: set[str],
    job_manager: JobManager,
    uploaded_paths: list[Path],
) -> None:
    for job_id in submitted_job_ids:
        try:
            await job_manager.cancel_job(job_id)
        except Exception:
            logger.exception("Failed to cancel partially started job %s", job_id)

    for job in created_jobs:
        try:
            await asyncio.to_thread(cleanup_job_files, job.id, job.input_path)
        except Exception:
            logger.exception("Failed to clean files for job %s after start failure", job.id)
            await mark_job_failed(db, job, error=JOB_START_FAILURE_ERROR)
        else:
            await db.delete(job)

    for uploaded_path in uploaded_paths:
        try:
            uploaded_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to delete uploaded file after start failure: %s", uploaded_path)

    await db.commit()


@router.post("", response_model=JobCreateResponse, status_code=201)
async def create_jobs(
    files: list[UploadFile],
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    for file in files:
        raw_filename = str(file.filename or "").strip()
        original_filename = safe_filename(raw_filename)
        if not raw_filename or not original_filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail=f"File '{original_filename or file.filename}' is not a PDF",
            )

    created_jobs = []
    uploaded_paths: list[Path] = []
    try:
        for file in files:
            stored_name, path, size = await save_upload(file)
            uploaded_paths.append(path)
            original_filename = safe_filename(str(file.filename or "").strip())

            job = Job(
                filename=stored_name,
                original_filename=original_filename,
                owner_session_hash=session.session_hash,
                input_path=str(path),
                file_size_bytes=size,
            )
            db.add(job)
            await db.flush()

            # Create pipeline step records
            for step_name in PIPELINE_STEPS:
                db.add(JobStep(job_id=job.id, step_name=step_name))

            await db.flush()
            # Refresh to load steps relationship
            await db.refresh(job, ["steps"])
            created_jobs.append(job)

        await db.commit()
    except Exception:
        await db.rollback()
        for uploaded_path in uploaded_paths:
            try:
                uploaded_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    # Submit each job to the pipeline
    settings = get_settings()
    session_maker = get_session_maker()
    job_manager = get_job_manager()
    submitted_job_ids: set[str] = set()
    try:
        for j in created_jobs:
            await job_manager.submit_job(
                j.id,
                run_pipeline(j.id, session_maker, settings, job_manager),
            )
            submitted_job_ids.add(j.id)
    except Exception as exc:
        logger.exception("Failed to start accessibility processing for uploaded jobs")
        await _compensate_failed_job_creation(
            db=db,
            created_jobs=created_jobs,
            submitted_job_ids=submitted_job_ids,
            job_manager=job_manager,
            uploaded_paths=uploaded_paths,
        )
        raise HTTPException(status_code=500, detail=JOB_START_FAILURE_DETAIL) from exc

    return JobCreateResponse(jobs=[job_to_response(j) for j in created_jobs])


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    query = (
        select(Job)
        .options(
            load_only(
                Job.id,
                Job.filename,
                Job.original_filename,
                Job.status,
                Job.classification,
                Job.ocr_language,
                Job.page_count,
                Job.file_size_bytes,
                Job.error,
                Job.validation_json,
                Job.created_at,
                Job.updated_at,
            ),
            selectinload(Job.steps).load_only(
                JobStep.step_name,
                JobStep.status,
                JobStep.started_at,
                JobStep.completed_at,
                JobStep.error,
            )
        )
        .where(Job.owner_session_hash == session.session_hash)
        .order_by(Job.created_at.desc())
    )
    if status:
        query = query.where(Job.status == status)

    result = await db.execute(query)
    jobs = result.scalars().all()
    return JobListResponse(
        jobs=[job_to_response(j, include_step_results=False) for j in jobs],
        total=len(jobs),
    )


_BATCH_REPORT_STATUSES = frozenset({"complete", "manual_remediation"})
_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "Cookie",
}


@router.get("/download/batch-report.html")
async def download_batch_report(
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    """Download a combined HTML report for all completed jobs in this session."""
    result = await db.execute(
        select(Job)
        .where(
            Job.owner_session_hash == session.session_hash,
            Job.status.in_(_BATCH_REPORT_STATUSES),
        )
        .order_by(Job.created_at.desc())
    )
    jobs = result.scalars().all()

    if not jobs:
        raise HTTPException(status_code=404, detail="No completed jobs to report on")

    job_reports: list[dict] = []
    for job in jobs:
        if not job.validation_json:
            continue
        try:
            validation = json.loads(job.validation_json)
        except json.JSONDecodeError:
            logger.error("Skipping job %s in batch report: corrupt validation_json (BUG)", job.id)
            continue
        alt_texts = (
            await db.execute(
                select(AltTextEntry)
                .where(AltTextEntry.job_id == job.id)
                .order_by(AltTextEntry.figure_index)
            )
        ).scalars().all()
        review_tasks = (
            await db.execute(
                select(ReviewTask)
                .where(ReviewTask.job_id == job.id)
                .order_by(ReviewTask.blocking.desc(), ReviewTask.created_at.asc())
            )
        ).scalars().all()
        job_reports.append({
            "job": job,
            "validation": validation,
            "alt_texts": list(alt_texts),
            "review_tasks": list(review_tasks),
        })

    if not job_reports:
        raise HTTPException(status_code=404, detail="No completed jobs with reports")

    html_content = render_batch_html_report(job_reports)
    return Response(
        content=html_content,
        media_type="text/html",
        headers={
            **_NO_STORE_HEADERS,
            "Content-Disposition": 'attachment; filename="batch_accessibility_report.html"',
        },
    )


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    result = await db.execute(
        select(Job)
        .options(selectinload(Job.steps))
        .where(Job.id == job_id, Job.owner_session_hash == session.session_hash)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_response(job)


@router.get("/{job_id}/progress")
async def job_progress_sse(
    job_id: str,
    job_manager: JobManager = Depends(get_job_manager),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    # Verify ownership before the stream starts, then release the DB session.
    session_maker = get_session_maker()
    async with session_maker() as db:
        result = await db.execute(
            select(Job.id).where(
                Job.id == job_id,
                Job.owner_session_hash == session.session_hash,
            )
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Job not found")

    queue = job_manager.subscribe(job_id)

    async def event_generator():
        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                    if event_data is _DONE:
                        return
                    yield {"event": "progress", "data": event_data}
                except TimeoutError:
                    yield {"event": "ping", "data": "keepalive"}
        except asyncio.CancelledError:
            pass
        finally:
            job_manager.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())


@router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
    job_manager: JobManager = Depends(get_job_manager),
):
    result = await db.execute(
        select(Job).where(
            Job.id == job_id,
            Job.owner_session_hash == session.session_hash,
        )
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    await job_manager.cancel_job(job_id)
    input_path = job.input_path
    try:
        await asyncio.to_thread(cleanup_job_files, job_id, input_path)
    except Exception as exc:
        logger.exception(f"Failed to clean deleted job files for {job_id}")
        await mark_job_failed(
            db,
            job,
            error=CLEANUP_INTERRUPTED_ERROR,
        )
        await db.commit()
        raise HTTPException(
            status_code=500,
            detail="Failed to delete job files. Please try again.",
        ) from exc
    await db.delete(job)
    await db.commit()
