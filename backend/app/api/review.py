import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, AppliedChange, Job
from app.pipeline.orchestrator import run_tagging_and_validation
from app.pipeline.structure import FigureInfo
from app.schemas import (
    AppliedChangeActionResponse,
    AppliedChangeResponse,
    ReviewEditRequest,
    ReviewFeedbackRequest,
)
from app.services.anonymous_sessions import AnonymousSession, get_anonymous_session
from app.services.applied_changes import (
    add_applied_change,
    change_to_response_payload,
    list_pending_reviewable_changes,
    parse_json_dict,
)
from app.services.intelligence_gemini_figures import generate_figure_intelligence
from app.services.job_manager import get_job_manager
from app.services.job_state import (
    ACTIVE_JOB_STATUSES,
    clear_terminal_artifacts,
    reset_rerun_steps,
)
from app.services.llm_client import make_llm_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs/{job_id}", tags=["review"])

_REVIEW_ACTION_STALE_DETAIL = (
    "This figure decision is no longer pending review. Refresh to see the latest job state."
)
_REVIEW_ACTION_IN_PROGRESS_DETAIL = (
    "Accessibility processing is already running for this PDF. Refresh to see the latest status."
)
_REVIEW_SURFACE_UNAVAILABLE_DETAIL = (
    "In-app review is only available after the app reaches a complete or manual-remediation output."
)
_REVIEWABLE_JOB_STATUSES = frozenset({"complete", "manual_remediation"})


def _parse_json(raw: str | None) -> dict:
    return parse_json_dict(raw)


async def _load_job(
    *,
    job_id: str,
    session_hash: str,
    db: AsyncSession,
) -> Job:
    job_result = await db.execute(
        select(Job).where(
            Job.id == job_id,
            Job.owner_session_hash == session_hash,
        )
    )
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


async def _load_applied_change(
    *,
    job_id: str,
    change_id: int,
    db: AsyncSession,
) -> AppliedChange:
    result = await db.execute(
        select(AppliedChange).where(
            AppliedChange.job_id == job_id,
            AppliedChange.id == change_id,
        )
    )
    change = result.scalar_one_or_none()
    if not change:
        raise HTTPException(status_code=404, detail="Applied change not found")
    return change


def _ensure_change_is_pending_review(change: AppliedChange) -> None:
    if change.review_status != "pending_review":
        raise HTTPException(status_code=409, detail=_REVIEW_ACTION_STALE_DETAIL)


def _ensure_job_is_not_processing(job: Job) -> None:
    if job.status in ACTIVE_JOB_STATUSES or get_job_manager().is_running(job.id):
        raise HTTPException(status_code=409, detail=_REVIEW_ACTION_IN_PROGRESS_DETAIL)


def _ensure_job_supports_in_app_review(job: Job) -> None:
    if job.status not in _REVIEWABLE_JOB_STATUSES:
        raise HTTPException(status_code=409, detail=_REVIEW_SURFACE_UNAVAILABLE_DETAIL)


def _applied_change_to_response(change: AppliedChange) -> AppliedChangeResponse:
    return AppliedChangeResponse(**change_to_response_payload(change))


async def _restart_tagging_with_current_state(
    *,
    job: Job,
    db: AsyncSession,
) -> None:
    _ensure_job_supports_in_app_review(job)

    job_manager = get_job_manager()
    if job_manager.is_running(job.id):
        raise HTTPException(status_code=409, detail=_REVIEW_ACTION_IN_PROGRESS_DETAIL)

    active_statuses = tuple(ACTIVE_JOB_STATUSES)
    claim_result = await db.execute(
        update(Job)
        .where(
            Job.id == job.id,
            ~Job.status.in_(active_statuses),
        )
        .values(
            status="processing",
            error=None,
            output_path=None,
            validation_json=None,
            fidelity_json=None,
            updated_at=datetime.now(UTC),
        )
    )
    if claim_result.rowcount != 1:
        raise HTTPException(status_code=409, detail=_REVIEW_ACTION_IN_PROGRESS_DETAIL)

    job.status = "processing"
    job.error = None
    clear_terminal_artifacts(job)

    await reset_rerun_steps(db, job.id)
    await db.commit()

    settings = get_settings()
    session_maker = get_session_maker()

    async def _resume(jid, sm, s, jm):
        async with sm() as resume_db:
            await run_tagging_and_validation(
                jid,
                resume_db,
                s,
                jm,
            )

    await job_manager.submit_job(
        job.id,
        _resume(job.id, session_maker, settings, job_manager),
    )


async def _load_figure_change_context(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
) -> tuple[AltTextEntry, dict]:
    metadata = _parse_json(change.metadata_json)
    undo_payload = _parse_json(change.undo_payload_json)

    figure_index_raw = metadata.get("figure_index", undo_payload.get("figure_index"))
    try:
        figure_index = int(figure_index_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Applied figure change is missing figure context",
        )

    entry_id_raw = undo_payload.get("entry_id")
    entry = None
    if entry_id_raw is not None:
        try:
            entry_id = int(entry_id_raw)
        except (TypeError, ValueError):
            entry_id = 0
        if entry_id > 0:
            result = await db.execute(
                select(AltTextEntry).where(
                    AltTextEntry.job_id == job.id,
                    AltTextEntry.id == entry_id,
                )
            )
            entry = result.scalar_one_or_none()

    if entry is None:
        result = await db.execute(
            select(AltTextEntry).where(
                AltTextEntry.job_id == job.id,
                AltTextEntry.figure_index == figure_index,
            )
        )
        entry = result.scalar_one_or_none()

    if entry is None:
        raise HTTPException(status_code=404, detail="Figure review context is no longer available")

    return entry, metadata


def _figure_info_from_change_metadata(
    *,
    entry: AltTextEntry,
    metadata: dict,
) -> FigureInfo:
    image_path = Path(entry.image_path)
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Figure image file not found")
    page_raw = metadata.get("page")
    page = None
    if isinstance(page_raw, int) and page_raw >= 1:
        page = page_raw - 1
    bbox = metadata.get("bbox") if isinstance(metadata.get("bbox"), dict) else None
    caption = str(metadata.get("caption") or "").strip() or None
    return FigureInfo(
        index=entry.figure_index,
        path=image_path,
        caption=caption,
        page=page,
        bbox=bbox,
    )


async def _apply_revised_figure_change(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
    reviewer_feedback: str | None,
) -> AppliedChangeActionResponse:
    entry, metadata = await _load_figure_change_context(job=job, change=change, db=db)
    settings = get_settings()
    llm_client = make_llm_client(settings)
    try:
        decision = await generate_figure_intelligence(
            figure=_figure_info_from_change_metadata(entry=entry, metadata=metadata),
            llm_client=llm_client,
            job=job,
            original_filename=job.original_filename,
            reviewer_feedback=reviewer_feedback,
            previous_intelligence=metadata.get("remediation_intelligence")
            if isinstance(metadata.get("remediation_intelligence"), dict)
            else None,
        )
    finally:
        await llm_client.close()

    suggested_action = str(decision.get("suggested_action") or "").strip()
    if suggested_action not in {"set_alt_text", "mark_decorative"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "The model could not produce a direct figure fix. "
                "Download the PDF and do external QA for this case."
            ),
        )

    previous_state = {
        "generated_text": entry.generated_text,
        "edited_text": entry.edited_text,
        "status": entry.status,
    }
    if suggested_action == "mark_decorative" or bool(decision.get("is_decorative", False)):
        entry.edited_text = "decorative"
        entry.status = "rejected"
        detail = str(decision.get("summary") or "The model marked this figure decorative.").strip()
    else:
        revised_text = str(decision.get("alt_text") or "").strip()
        if not revised_text:
            raise HTTPException(
                status_code=502,
                detail="The model did not return a revised figure description",
            )
        entry.edited_text = revised_text
        entry.status = "approved"
        detail = str(
            decision.get("summary") or "The model revised this figure description."
        ).strip()

    change.review_status = "undone"
    await add_applied_change(
        db=db,
        job=job,
        change_type="figure_semantics",
        title=f"Updated figure {entry.figure_index + 1}",
        detail=detail,
        importance=str(change.importance or "medium"),
        reviewable=True,
        metadata={
            **metadata,
            "remediation_intelligence": dict(decision),
            "figure_index": entry.figure_index,
        },
        before=previous_state,
        after={
            "generated_text": entry.generated_text,
            "edited_text": entry.edited_text,
            "status": entry.status,
        },
        undo_payload={
            "kind": "alt_text_entry",
            "entry_id": entry.id,
            "figure_index": entry.figure_index,
            **previous_state,
        },
    )
    await _restart_tagging_with_current_state(job=job, db=db)
    return AppliedChangeActionResponse(
        status="reopened",
        message="Retried the figure decision and restarted accessibility processing.",
        job_status="processing",
    )


async def _undo_applied_change(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
) -> AppliedChangeActionResponse:
    undo_payload = _parse_json(change.undo_payload_json)
    kind = str(undo_payload.get("kind") or "").strip()
    if kind == "alt_text_entry":
        entry_id_raw = undo_payload.get("entry_id")
        figure_index_raw = undo_payload.get("figure_index")
        entry = None
        if entry_id_raw is not None:
            try:
                entry_id = int(entry_id_raw)
            except (TypeError, ValueError):
                entry_id = 0
            if entry_id > 0:
                entry_result = await db.execute(
                    select(AltTextEntry).where(
                        AltTextEntry.job_id == job.id,
                        AltTextEntry.id == entry_id,
                    )
                )
                entry = entry_result.scalar_one_or_none()
        if entry is None and figure_index_raw is not None:
            try:
                figure_index = int(figure_index_raw)
            except (TypeError, ValueError):
                figure_index = -1
            if figure_index >= 0:
                entry_result = await db.execute(
                    select(AltTextEntry).where(
                        AltTextEntry.job_id == job.id,
                        AltTextEntry.figure_index == figure_index,
                    )
                )
                entry = entry_result.scalar_one_or_none()
        if entry is None and not bool(undo_payload.get("delete_if_absent", False)):
            raise HTTPException(status_code=404, detail="Alt text entry not found for undo.")
        if bool(undo_payload.get("delete_if_absent", False)):
            if entry is not None:
                await db.delete(entry)
        elif entry is not None:
            entry.generated_text = undo_payload.get("generated_text")
            entry.edited_text = undo_payload.get("edited_text")
            entry.status = str(undo_payload.get("status") or "pending_review")
        change.review_status = "undone"
        await _restart_tagging_with_current_state(job=job, db=db)
        return AppliedChangeActionResponse(
            status="undone",
            message="Undid the applied figure decision and restarted accessibility processing.",
            job_status="processing",
        )
    raise HTTPException(status_code=400, detail="This change cannot be undone.")


@router.get("/applied-changes", response_model=list[AppliedChangeResponse])
async def list_applied_changes(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_job(job_id=job_id, session_hash=session.session_hash, db=db)
    _ensure_job_supports_in_app_review(job)
    changes = await list_pending_reviewable_changes(db=db, job_id=job.id)
    return [_applied_change_to_response(change) for change in changes]


@router.post("/applied-changes/{change_id}/keep", response_model=AppliedChangeActionResponse)
async def keep_applied_change(
    job_id: str,
    change_id: int,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_job(job_id=job_id, session_hash=session.session_hash, db=db)
    _ensure_job_supports_in_app_review(job)
    change = await _load_applied_change(job_id=job_id, change_id=change_id, db=db)
    _ensure_change_is_pending_review(change)
    _ensure_job_is_not_processing(job)
    change.review_status = "kept"
    job.updated_at = datetime.now(UTC)
    await db.commit()
    return AppliedChangeActionResponse(
        status="kept",
        message="Kept this figure decision.",
        job_status=str(job.status),
    )


@router.post("/applied-changes/{change_id}/undo", response_model=AppliedChangeActionResponse)
async def undo_applied_change(
    job_id: str,
    change_id: int,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_job(job_id=job_id, session_hash=session.session_hash, db=db)
    _ensure_job_supports_in_app_review(job)
    change = await _load_applied_change(job_id=job_id, change_id=change_id, db=db)
    _ensure_change_is_pending_review(change)
    _ensure_job_is_not_processing(job)
    return await _undo_applied_change(job=job, change=change, db=db)


@router.post("/applied-changes/{change_id}/revise", response_model=AppliedChangeActionResponse)
async def revise_applied_change(
    job_id: str,
    change_id: int,
    request: ReviewFeedbackRequest | None = None,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    job = await _load_job(job_id=job_id, session_hash=session.session_hash, db=db)
    _ensure_job_supports_in_app_review(job)
    change = await _load_applied_change(job_id=job_id, change_id=change_id, db=db)
    _ensure_change_is_pending_review(change)
    _ensure_job_is_not_processing(job)
    if change.change_type != "figure_semantics":
        raise HTTPException(
            status_code=400,
            detail="This change type cannot be revised in the app.",
        )
    return await _apply_revised_figure_change(
        job=job,
        change=change,
        db=db,
        reviewer_feedback=(request.feedback.strip() if request and request.feedback else None),
    )


@router.post("/applied-changes/{change_id}/edit", response_model=AppliedChangeActionResponse)
async def edit_applied_change(
    job_id: str,
    change_id: int,
    request: ReviewEditRequest,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    """Directly edit the alt text for a figure decision."""
    job = await _load_job(job_id=job_id, session_hash=session.session_hash, db=db)
    _ensure_job_supports_in_app_review(job)
    change = await _load_applied_change(job_id=job_id, change_id=change_id, db=db)
    _ensure_change_is_pending_review(change)
    _ensure_job_is_not_processing(job)
    if change.change_type != "figure_semantics":
        raise HTTPException(
            status_code=400,
            detail="This change type cannot be edited in the app.",
        )

    entry, metadata = await _load_figure_change_context(job=job, change=change, db=db)
    edited_text = request.text.strip()
    if not edited_text:
        raise HTTPException(status_code=400, detail="Alt text cannot be empty.")

    previous_state = {
        "generated_text": entry.generated_text,
        "edited_text": entry.edited_text,
        "status": entry.status,
    }

    entry.edited_text = edited_text
    entry.status = "approved"

    change.review_status = "undone"
    await add_applied_change(
        db=db,
        job=job,
        change_type="figure_semantics",
        title=f"Updated figure {entry.figure_index + 1}",
        detail="Manually edited figure description.",
        importance=str(change.importance or "medium"),
        reviewable=True,
        metadata={
            **metadata,
            "figure_index": entry.figure_index,
        },
        before=previous_state,
        after={
            "generated_text": entry.generated_text,
            "edited_text": entry.edited_text,
            "status": entry.status,
        },
        undo_payload={
            "kind": "alt_text_entry",
            "entry_id": entry.id,
            "figure_index": entry.figure_index,
            **previous_state,
        },
    )
    await _restart_tagging_with_current_state(job=job, db=db)
    return AppliedChangeActionResponse(
        status="edited",
        message="Saved the edited figure description and restarted accessibility processing.",
        job_status="processing",
    )


@router.get("/figures/{figure_index}/image")
async def get_figure_image(
    job_id: str,
    figure_index: int,
    db: AsyncSession = Depends(get_db),
    session: AnonymousSession = Depends(get_anonymous_session),
):
    """Serve the extracted figure image for review."""
    await _load_job(job_id=job_id, session_hash=session.session_hash, db=db)
    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id,
            AltTextEntry.figure_index == figure_index,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Figure not found")
    image_path = Path(entry.image_path)
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Figure image file not found")
    suffix = image_path.suffix.lower()
    media_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    media_type = media_types.get(suffix, "image/png")
    return FileResponse(image_path, media_type=media_type)
