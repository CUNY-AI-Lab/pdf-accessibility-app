import json
import logging
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, Job, ReviewTask
from app.pipeline.orchestrator import run_tagging_and_validation
from app.schemas import (
    AltTextResponse,
    AltTextUpdateRequest,
    ReviewTaskResponse,
    StructureUpdateRequest,
    ValidationReportResponse,
)
from app.services.job_manager import get_job_manager
from app.services.path_safety import safe_filename, validate_path_within_allowed_roots
from app.services.pdf_preview import render_page_png_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs/{job_id}", tags=["documents"])
ALLOWED_STRUCTURE_TYPES = {
    "heading",
    "paragraph",
    "figure",
    "table",
    "list_item",
    "code",
    "formula",
    "artifact",
}


def _job_pdf_path(job: Job) -> Path:
    candidate = validate_path_within_allowed_roots(Path(job.output_path or job.input_path))
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")
    return candidate


def _sanitize_structure_payload(structure: dict) -> dict:
    sanitized = dict(structure)
    raw_elements = structure.get("elements", [])
    if not isinstance(raw_elements, list):
        raise HTTPException(status_code=400, detail="Structure must include an elements list")

    cleaned_elements: list[dict] = []
    for index, raw_element in enumerate(raw_elements):
        if not isinstance(raw_element, dict):
            raise HTTPException(status_code=400, detail="All structure elements must be objects")

        element = {
            key: value
            for key, value in raw_element.items()
            if key not in {"review_id", "_manual_original_type"}
        }

        element_type = str(element.get("type") or "").strip()
        if element_type and element_type not in ALLOWED_STRUCTURE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported structure element type at index {index}: {element_type}",
            )

        page_value = element.get("page")
        if page_value is not None and not isinstance(page_value, int):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid page value at element index {index}",
            )

        if element_type == "heading":
            level = element.get("level", 1)
            if isinstance(level, bool) or not isinstance(level, int):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid heading level at element index {index}",
                )
            element["level"] = max(1, min(6, level))

        cleaned_elements.append(element)

    sanitized["elements"] = cleaned_elements
    return sanitized


@router.get("/structure")
async def get_structure(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.structure_json:
        raise HTTPException(status_code=404, detail="Structure not yet extracted")
    return json.loads(job.structure_json)


@router.put("/structure")
async def update_structure(
    job_id: str,
    update: StructureUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in {"needs_manual_review", "complete"}:
        raise HTTPException(
            status_code=400,
            detail=f"Structure edits are not allowed while job status is {job.status}",
        )

    structure = _sanitize_structure_payload(update.structure)

    job.structure_json = json.dumps(structure)
    job.status = "processing"
    await db.commit()

    settings = get_settings()
    session_maker = get_session_maker()
    job_manager = get_job_manager()

    async def _resume(jid, sm, s, jm, structure_payload):
        async with sm() as resume_db:
            await run_tagging_and_validation(
                jid,
                resume_db,
                s,
                jm,
                structure_json=structure_payload,
            )

    await job_manager.submit_job(
        job_id,
        _resume(job_id, session_maker, settings, job_manager, structure),
    )

    return {
        "status": "accepted",
        "message": "Structure updated. Tagging and validation restarted.",
    }


@router.get("/alt-texts", response_model=list[AltTextResponse])
async def list_alt_texts(job_id: str, db: AsyncSession = Depends(get_db)):
    job_result = await db.execute(select(Job.id).where(Job.id == job_id))
    if not job_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Job not found")

    result = await db.execute(
        select(AltTextEntry)
        .where(AltTextEntry.job_id == job_id)
        .order_by(AltTextEntry.figure_index)
    )
    entries = result.scalars().all()
    return [
        AltTextResponse(
            id=e.id,
            figure_index=e.figure_index,
            image_url=f"/api/jobs/{job_id}/figures/{e.figure_index}/image",
            generated_text=e.generated_text,
            edited_text=e.edited_text,
            status=e.status,
        )
        for e in entries
    ]


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
    ]


@router.put("/alt-texts/{figure_index}", response_model=AltTextResponse)
async def update_alt_text(
    job_id: str,
    figure_index: int,
    update: AltTextUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id, AltTextEntry.figure_index == figure_index
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Alt text entry not found")

    if update.edited_text is not None:
        entry.edited_text = update.edited_text
    if update.status is not None:
        entry.status = update.status

    await db.commit()
    await db.refresh(entry)

    return AltTextResponse(
        id=entry.id,
        figure_index=entry.figure_index,
        image_url=f"/api/jobs/{job_id}/figures/{entry.figure_index}/image",
        generated_text=entry.generated_text,
        edited_text=entry.edited_text,
        status=entry.status,
    )


@router.get("/figures/{figure_index}/image")
async def get_figure_image(
    job_id: str, figure_index: int, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id, AltTextEntry.figure_index == figure_index
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Figure not found")

    image_path = Path(entry.image_path)
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image file not found")

    return FileResponse(image_path, media_type="image/png")


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
        preview_bytes = render_page_png_bytes(_job_pdf_path(job), page_number, timeout=get_settings().subprocess_timeout_preview)
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
