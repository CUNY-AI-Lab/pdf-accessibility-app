import json
import logging
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import Job, ReviewTask
from app.schemas import ReviewTaskResponse, ValidationReportResponse
from app.services.path_safety import safe_filename, validate_path_within_allowed_roots
from app.services.pdf_preview import render_page_png_bytes
from app.services.review_surface import is_user_visible_review_task_type

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs/{job_id}", tags=["documents"])


def _job_pdf_path(job: Job) -> Path:
    candidate = validate_path_within_allowed_roots(Path(job.output_path or job.input_path))
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")
    return candidate


@router.get("/review-tasks", response_model=list[ReviewTaskResponse])
async def list_review_tasks(job_id: str, db: AsyncSession = Depends(get_db)):
    job_result = await db.execute(select(Job.id).where(Job.id == job_id))
    if not job_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Job not found")

    result = await db.execute(
        select(ReviewTask)
        .where(ReviewTask.job_id == job_id)
        .order_by(ReviewTask.blocking.desc(), ReviewTask.created_at.asc())
    )
    tasks = result.scalars().all()
    return [
        ReviewTaskResponse(
            id=task.id,
            task_type=task.task_type,
            title=task.title,
            detail=task.detail,
            severity=task.severity,
            blocking=bool(task.blocking),
            status=task.status,
            source=task.source,
            metadata=json.loads(task.metadata_json) if task.metadata_json else {},
        )
        for task in tasks
        if is_user_visible_review_task_type(task.task_type)
    ]


@router.get("/pages/{page_number}/preview")
async def get_page_preview(
    job_id: str,
    page_number: int,
    db: AsyncSession = Depends(get_db),
):
    if page_number < 1:
        raise HTTPException(status_code=400, detail="Page number must be 1 or greater")

    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.page_count and page_number > job.page_count:
        raise HTTPException(status_code=404, detail="Page number exceeds document length")

    try:
        preview_bytes = render_page_png_bytes(
            _job_pdf_path(job),
            page_number,
            timeout=get_settings().subprocess_timeout_preview,
        )
    except FileNotFoundError as exc:
        logger.exception("PDF file not found for page preview")
        raise HTTPException(status_code=404, detail="PDF file not found") from exc
    except ValueError as exc:
        logger.exception("Invalid page preview request")
        raise HTTPException(status_code=400, detail="Invalid preview request") from exc
    except Exception as exc:
        logger.exception("Failed to render page preview")
        raise HTTPException(status_code=502, detail="Failed to render preview") from exc

    return StreamingResponse(
        BytesIO(preview_bytes),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/download")
async def download_pdf(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.output_path:
        raise HTTPException(status_code=404, detail="Accessible PDF not yet available")

    return FileResponse(
        _job_pdf_path(job),
        media_type="application/pdf",
        filename=f"accessible_{safe_filename(job.original_filename)}",
    )


@router.get("/download/report", response_model=ValidationReportResponse)
async def download_report(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.validation_json:
        raise HTTPException(status_code=404, detail="Validation report not yet available")

    return json.loads(job.validation_json)
