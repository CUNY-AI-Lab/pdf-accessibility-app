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
from app.pipeline.structure import FigureInfo
from app.schemas import (
    AltTextResponse,
    AltTextRecommendationApplyResponse,
    AltTextSuggestionRequest,
    ReviewTaskResponse,
    ValidationReportResponse,
)
from app.services.intelligence_gemini_figures import generate_figure_intelligence
from app.services.job_manager import get_job_manager
from app.services.llm_client import make_llm_client
from app.services.path_safety import safe_filename, validate_path_within_allowed_roots
from app.services.pdf_preview import render_page_png_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs/{job_id}", tags=["documents"])


def _job_pdf_path(job: Job) -> Path:
    candidate = validate_path_within_allowed_roots(Path(job.output_path or job.input_path))
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")
    return candidate


def _figure_info_for_alt_entry(job: Job, entry: AltTextEntry) -> FigureInfo:
    image_path = validate_path_within_allowed_roots(Path(entry.image_path))
    caption: str | None = None
    page: int | None = None
    bbox: dict | None = None

    if job.structure_json:
        try:
            structure = json.loads(job.structure_json)
        except json.JSONDecodeError:
            structure = {}
        elements = structure.get("elements", []) if isinstance(structure, dict) else []
        if isinstance(elements, list):
            for element in elements:
                if not isinstance(element, dict):
                    continue
                if element.get("type") != "figure":
                    continue
                if element.get("figure_index") != entry.figure_index:
                    continue
                raw_caption = element.get("caption")
                raw_page = element.get("page")
                raw_bbox = element.get("bbox")
                caption = str(raw_caption).strip() if isinstance(raw_caption, str) and raw_caption.strip() else None
                page = raw_page if isinstance(raw_page, int) else None
                bbox = raw_bbox if isinstance(raw_bbox, dict) else None
                break

    return FigureInfo(
        index=entry.figure_index,
        path=image_path,
        caption=caption,
        page=page,
        bbox=bbox,
    )


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


@router.post("/alt-texts/{figure_index}/suggest", response_model=AltTextResponse)
async def suggest_alt_text(
    job_id: str,
    figure_index: int,
    request: AltTextSuggestionRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "awaiting_recommendation_review":
        raise HTTPException(status_code=400, detail="This job is not awaiting figure recommendation review")

    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id,
            AltTextEntry.figure_index == figure_index,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Alt text entry not found")

    settings = get_settings()
    llm_client = make_llm_client(settings)
    try:
        decision = await generate_figure_intelligence(
            figure=_figure_info_for_alt_entry(job, entry),
            llm_client=llm_client,
            job=job,
            original_filename=job.original_filename,
            reviewer_feedback=(request.feedback.strip() if request and request.feedback else None),
        )
    finally:
        await llm_client.close()

    suggested_action = str(decision.get("suggested_action") or "").strip()
    if suggested_action == "set_alt_text":
        revised_text = str(decision.get("alt_text") or "").strip()
        if not revised_text:
            raise HTTPException(status_code=502, detail="Model did not return a revised figure description")
        entry.edited_text = revised_text
        entry.status = "pending_review"
    elif suggested_action == "mark_decorative" or bool(decision.get("is_decorative", False)):
        entry.edited_text = "decorative"
        entry.status = "pending_review"
    else:
        raise HTTPException(
            status_code=409,
            detail="This figure needs a different kind of review. Try another instruction or download the PDF for external QA.",
        )

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


@router.post(
    "/alt-texts/{figure_index}/accept-recommendation",
    response_model=AltTextRecommendationApplyResponse,
)
async def accept_alt_text_recommendation(
    job_id: str,
    figure_index: int,
    db: AsyncSession = Depends(get_db),
):
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "awaiting_recommendation_review":
        raise HTTPException(status_code=400, detail="This job is not awaiting figure recommendation review")

    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id,
            AltTextEntry.figure_index == figure_index,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Alt text entry not found")

    recommendation = str(entry.edited_text or entry.generated_text or "").strip()
    if not recommendation:
        raise HTTPException(status_code=400, detail="No recommendation is available for this figure")

    if recommendation.lower() == "decorative":
        entry.edited_text = "decorative"
        entry.status = "rejected"
        message = "Accepted the recommendation and marked this figure decorative."
    else:
        entry.edited_text = recommendation
        entry.status = "approved"
        message = "Accepted the recommendation for this figure."

    await db.commit()
    await db.refresh(entry)

    pending_result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id,
            AltTextEntry.status == "pending_review",
        )
    )
    pending = pending_result.scalars().all()

    if not pending:
        job.status = "processing"
        await db.commit()

        settings = get_settings()
        session_maker = get_session_maker()
        job_manager = get_job_manager()

        async def _resume(jid: str) -> None:
            async with session_maker() as resume_db:
                await run_tagging_and_validation(jid, resume_db, settings, job_manager)

        await job_manager.submit_job(job_id, _resume(job_id))
        return AltTextRecommendationApplyResponse(
            status="accepted",
            message="Accepted the recommendation and resumed accessibility processing.",
            job_status="processing",
            alt_text=AltTextResponse(
                id=entry.id,
                figure_index=entry.figure_index,
                image_url=f"/api/jobs/{job_id}/figures/{entry.figure_index}/image",
                generated_text=entry.generated_text,
                edited_text=entry.edited_text,
                status=entry.status,
            ),
        )

    return AltTextRecommendationApplyResponse(
        status="accepted",
        message=message,
        job_status=str(job.status),
        alt_text=AltTextResponse(
            id=entry.id,
            figure_index=entry.figure_index,
            image_url=f"/api/jobs/{job_id}/figures/{entry.figure_index}/image",
            generated_text=entry.generated_text,
            edited_text=entry.edited_text,
            status=entry.status,
        ),
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
