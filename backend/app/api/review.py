import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, Job, ReviewTask
from app.pipeline.orchestrator import run_tagging_and_validation
from app.schemas import ReviewTaskResponse, ReviewTaskUpdateRequest
from app.services.job_manager import get_job_manager

router = APIRouter(prefix="/jobs/{job_id}", tags=["review"])

TASK_EVIDENCE_REQUIREMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "reading_order": (
        ("verification_method", "verification method"),
        ("pages_checked", "pages checked"),
    ),
    "font_text_fidelity": (
        ("assistive_tech", "assistive technology"),
        ("sample_scope", "sample scope"),
    ),
    "table_semantics": (
        ("tables_checked", "tables checked"),
        ("verification_method", "verification method"),
    ),
    "content_fidelity": (
        ("comparison_method", "comparison method"),
        ("pages_checked", "pages checked"),
    ),
    "alt_text": (
        ("figures_checked", "figures checked"),
    ),
}


def _parse_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _manual_review_completion_state(
    validation_payload: dict,
    tasks: list[ReviewTask],
) -> tuple[bool, str | None]:
    if not bool(validation_payload.get("compliant", False)):
        return False, "Validation still reports unresolved PDF/UA errors"

    blocking_validation = [
        task for task in tasks if bool(task.blocking) and task.source == "validation"
    ]
    if blocking_validation:
        return False, "Validation-derived remediation tasks cannot be cleared in-app"

    pending_blocking = [
        task
        for task in tasks
        if bool(task.blocking)
        and task.source != "validation"
        and task.status == "pending_review"
    ]
    if pending_blocking:
        return False, f"{len(pending_blocking)} blocking review task(s) still need review"

    return True, None


def _task_to_response(task: ReviewTask) -> ReviewTaskResponse:
    return ReviewTaskResponse(
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


def _validated_task_metadata(
    task: ReviewTask,
    update: ReviewTaskUpdateRequest,
) -> dict:
    task_type = getattr(task, "task_type", "")
    metadata = _parse_json(task.metadata_json)
    if update.resolution_note is not None:
        normalized = update.resolution_note.strip()
        if normalized:
            metadata["resolution_note"] = normalized
        else:
            metadata.pop("resolution_note", None)
    if update.evidence is not None:
        normalized_evidence = {
            str(key): str(value).strip()
            for key, value in update.evidence.items()
            if str(key).strip()
        }
        if normalized_evidence:
            metadata["evidence"] = normalized_evidence
        else:
            metadata.pop("evidence", None)

    next_status = update.status or task.status
    if next_status == "resolved" and not str(metadata.get("resolution_note") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Resolution note is required before marking a review task resolved",
        )
    if next_status == "resolved":
        evidence = metadata.get("evidence")
        evidence_dict = evidence if isinstance(evidence, dict) else {}
        missing_fields = [
            label
            for key, label in TASK_EVIDENCE_REQUIREMENTS.get(task_type, ())
            if not str(evidence_dict.get(key) or "").strip()
        ]
        if missing_fields:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Missing required review evidence: "
                    + ", ".join(missing_fields)
                ),
            )
    return metadata


@router.post("/approve", status_code=200)
async def approve_review(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == "awaiting_review":
        # Check that all alt texts have been reviewed
        result = await db.execute(
            select(AltTextEntry).where(
                AltTextEntry.job_id == job_id,
                AltTextEntry.status == "pending_review",
            )
        )
        pending = result.scalars().all()
        if pending:
            raise HTTPException(
                status_code=400,
                detail=f"{len(pending)} figure(s) still need review",
            )

        # Update status and trigger remaining pipeline steps
        job.status = "processing"
        await db.commit()

        # Submit tagging + validation to run in background
        settings = get_settings()
        session_maker = get_session_maker()
        job_manager = get_job_manager()

        async def _resume(jid, sm, s, jm):
            async with sm() as resume_db:
                await run_tagging_and_validation(jid, resume_db, s, jm)

        await job_manager.submit_job(
            job_id,
            _resume(job_id, session_maker, settings, job_manager),
        )

        return {"status": "approved", "message": "Tagging and validation started"}

    if job.status != "needs_manual_review":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not awaiting review (current status: {job.status})",
        )

    result = await db.execute(
        select(ReviewTask).where(ReviewTask.job_id == job_id).order_by(ReviewTask.id)
    )
    tasks = result.scalars().all()
    ok_to_complete, reason = _manual_review_completion_state(_parse_json(job.validation_json), tasks)
    if not ok_to_complete:
        raise HTTPException(status_code=400, detail=reason or "Manual review is not complete")

    job.status = "complete"
    await db.commit()
    return {"status": "approved", "message": "Manual fidelity review recorded"}


@router.put("/review-tasks/{task_id}", response_model=ReviewTaskResponse)
async def update_review_task(
    job_id: str,
    task_id: int,
    update: ReviewTaskUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.id == task_id,
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found")
    if task.source == "validation":
        raise HTTPException(
            status_code=400,
            detail="Validation-derived tasks cannot be resolved in-app",
        )

    metadata = _validated_task_metadata(task, update)
    if update.status is not None:
        task.status = update.status
    task.metadata_json = json.dumps(metadata) if metadata else None

    await db.commit()
    await db.refresh(task)
    return _task_to_response(task)
