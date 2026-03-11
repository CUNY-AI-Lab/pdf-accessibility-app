import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import Job, JobStep
from app.pipeline.orchestrator import run_pipeline
from app.schemas import JobCreateResponse, JobListResponse, JobResponse, JobStepResponse
from app.services.file_storage import cleanup_job_files, save_upload
from app.services.job_manager import _DONE, JobManager, get_job_manager

router = APIRouter(prefix="/jobs", tags=["jobs"])

PIPELINE_STEPS = ["classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"]
PIPELINE_STEP_ORDER = {name: idx for idx, name in enumerate(PIPELINE_STEPS)}


def _parse_result_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def job_to_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        filename=job.filename,
        original_filename=job.original_filename,
        status=job.status,
        classification=job.classification,
        page_count=job.page_count,
        file_size_bytes=job.file_size_bytes,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        steps=[
            JobStepResponse(
                step_name=s.step_name,
                status=s.status,
                started_at=s.started_at,
                completed_at=s.completed_at,
                error=s.error,
                result=_parse_result_json(s.result_json),
            )
            for s in sorted(
                job.steps,
                key=lambda s: PIPELINE_STEP_ORDER.get(s.step_name, len(PIPELINE_STEPS)),
            )
        ],
    )


@router.post("", response_model=JobCreateResponse, status_code=201)
async def create_jobs(
    files: list[UploadFile],
    db: AsyncSession = Depends(get_db),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    for file in files:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail=f"File '{file.filename}' is not a PDF",
            )

    created_jobs = []
    uploaded_paths: list[Path] = []
    try:
        for file in files:
            stored_name, path, size = await save_upload(file)
            uploaded_paths.append(path)

            job = Job(
                filename=stored_name,
                original_filename=file.filename,
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
    for j in created_jobs:
        await job_manager.submit_job(
            j.id,
            run_pipeline(j.id, session_maker, settings, job_manager),
        )

    return JobCreateResponse(jobs=[job_to_response(j) for j in created_jobs])


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    query = select(Job).options(selectinload(Job.steps)).order_by(Job.created_at.desc())
    if status:
        query = query.where(Job.status == status)

    result = await db.execute(query)
    jobs = result.scalars().all()
    return JobListResponse(jobs=[job_to_response(j) for j in jobs], total=len(jobs))


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Job).options(selectinload(Job.steps)).where(Job.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_response(job)


@router.get("/{job_id}/progress")
async def job_progress_sse(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    job_manager: JobManager = Depends(get_job_manager),
):
    # Verify job exists
    result = await db.execute(select(Job.id).where(Job.id == job_id))
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
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    input_path = job.input_path
    await db.delete(job)
    await db.commit()
    cleanup_job_files(job_id, input_path)
