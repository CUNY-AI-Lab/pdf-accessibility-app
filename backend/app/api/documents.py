import json
import logging
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import AltTextEntry, Job, ReviewTask
from app.schemas import ReviewTaskResponse, ValidationReportResponse
from app.services.anonymous_sessions import AnonymousSession, get_anonymous_session
from app.services.html_report import render_html_report
from app.services.path_safety import safe_filename, validate_path_within_allowed_roots
from app.services.pdf_preview import render_page_png_bytes
from app.services.review_surface import is_user_visible_review_task_type

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs/{job_id}", tags=["documents"])
_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "Cookie",
}
_OUTPUT_READY_STATUSES = frozenset({"complete", "manual_remediation"})
_OUTPUT_INSPECTION_UNAVAILABLE_DETAIL = "Current output is not available for inspection"


def _job_pdf_path(job: Job) -> Path:
    candidate = validate_path_within_allowed_roots(Path(job.output_path or job.input_path))
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")
    return candidate


async def _load_owned_job(
    *,
    job_id: str,
    session_hash: str,
    db: AsyncSession,
) -> Job:
    result = await db.execute(
        select(Job).where(
            Job.id == job_id,
            Job.owner_session_hash == session_hash,
        )
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _ensure_output_ready_for_inspection(job: Job) -> None:
    if job.status not in _OUTPUT_READY_STATUSES:
        raise HTTPException(status_code=404, detail=_OUTPUT_INSPECTION_UNAVAILABLE_DETAIL)


@router.get("/review-tasks", response_model=list[ReviewTaskResponse])
async def list_review_tasks(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_owned_job(job_id=job_id, session_hash=session.session_hash, db=db)
    _ensure_output_ready_for_inspection(job)

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
    session: AnonymousSession = Depends(get_anonymous_session),
):
    if page_number < 1:
        raise HTTPException(status_code=400, detail="Page number must be 1 or greater")

    job = await _load_owned_job(job_id=job_id, session_hash=session.session_hash, db=db)
    _ensure_output_ready_for_inspection(job)

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
async def download_pdf(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_owned_job(job_id=job_id, session_hash=session.session_hash, db=db)
    if job.status not in _OUTPUT_READY_STATUSES or not job.output_path:
        raise HTTPException(status_code=404, detail="Accessible PDF not yet available")

    return FileResponse(
        _job_pdf_path(job),
        media_type="application/pdf",
        filename=f"accessible_{safe_filename(job.original_filename)}",
        headers=_NO_STORE_HEADERS,
    )


@router.get("/download/report", response_model=ValidationReportResponse)
async def download_report(
    job_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_owned_job(job_id=job_id, session_hash=session.session_hash, db=db)
    if job.status not in _OUTPUT_READY_STATUSES or not job.validation_json:
        raise HTTPException(status_code=404, detail="Validation report not yet available")

    for header_name, header_value in _NO_STORE_HEADERS.items():
        response.headers[header_name] = header_value
    return ValidationReportResponse.model_validate_json(job.validation_json)


@router.get("/download/report.html")
async def download_html_report(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_owned_job(job_id=job_id, session_hash=session.session_hash, db=db)
    if job.status not in _OUTPUT_READY_STATUSES or not job.validation_json:
        raise HTTPException(status_code=404, detail="Validation report not yet available")

    validation = json.loads(job.validation_json)
    alt_texts = (
        await db.execute(
            select(AltTextEntry)
            .where(AltTextEntry.job_id == job_id)
            .order_by(AltTextEntry.figure_index)
        )
    ).scalars().all()
    review_tasks = (
        await db.execute(
            select(ReviewTask)
            .where(ReviewTask.job_id == job_id)
            .order_by(ReviewTask.blocking.desc(), ReviewTask.created_at.asc())
        )
    ).scalars().all()

    html_content = render_html_report(job, validation, list(alt_texts), list(review_tasks))
    filename = safe_filename(job.original_filename).rsplit(".", 1)[0]
    return Response(
        content=html_content,
        media_type="text/html",
        headers={
            **_NO_STORE_HEADERS,
            "Content-Disposition": f'attachment; filename="report_{filename}.html"',
        },
    )
