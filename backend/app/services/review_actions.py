"""Review edits applied to a runner task's saved database before re-tagging."""

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AltTextEntry, AppliedChange, Job
from app.pipeline.structure import FigureInfo
from app.schemas import AppliedChangeActionResponse
from app.services.alt_text_cache import usable_description
from app.services.applied_changes import add_applied_change, parse_json_dict
from app.services.figure_state import restore_stored_figure, set_figure_status
from app.services.intelligence_figures import generate_figure_intelligence
from app.services.llm_client import make_llm_client
from app.services.path_safety import PathAccessError, validate_path_within_allowed_roots
from app.services.review_errors import ReviewRefusal
from app.services.review_state import transition_review

_REVIEW_ACTION_STALE_DETAIL = (
    "This figure decision is no longer pending review. Refresh to see the latest job state."
)
_REVIEW_SURFACE_UNAVAILABLE_DETAIL = (
    "In-app review is only available after the app reaches a complete or manual-remediation output."
)
_REVIEWABLE_JOB_STATUSES = frozenset({"complete", "manual_remediation"})


def _parse_json(raw: str | None) -> dict:
    return parse_json_dict(raw)


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
        raise ReviewRefusal(code=404, detail="Applied change not found")
    return change


def _ensure_change_is_pending_review(change: AppliedChange) -> None:
    if not change.reviewable or change.review_status != "pending_review":
        raise ReviewRefusal(code=409, detail=_REVIEW_ACTION_STALE_DETAIL)


def _ensure_job_supports_in_app_review(job: Job) -> None:
    if job.status not in _REVIEWABLE_JOB_STATUSES:
        raise ReviewRefusal(code=409, detail=_REVIEW_SURFACE_UNAVAILABLE_DETAIL)


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
        raise ReviewRefusal(
            code=400,
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
        raise ReviewRefusal(code=404, detail="Figure review context is no longer available")

    return entry, metadata


def _figure_info_from_change_metadata(
    *,
    entry: AltTextEntry,
    metadata: dict,
) -> FigureInfo:
    try:
        image_path = validate_path_within_allowed_roots(Path(entry.image_path))
    except PathAccessError as exc:
        raise ReviewRefusal(code=403, detail=str(exc)) from exc
    if not image_path.exists():
        raise ReviewRefusal(code=404, detail="Figure image file not found")
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


async def apply_revised_figure_change(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
    reviewer_feedback: str | None,
) -> AppliedChangeActionResponse:
    """Ask the model again about one figure and record its new decision.

    The caller re-runs tagging afterwards.
    """
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
        raise ReviewRefusal(
            code=409,
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
        set_figure_status(entry, "rejected")
        detail = str(decision.get("summary") or "The model marked this figure decorative.").strip()
    else:
        revised_text = str(decision.get("alt_text") or "").strip()
        if not usable_description(revised_text):
            raise ReviewRefusal(
                code=502,
                detail="The model did not return a revised figure description",
            )
        entry.edited_text = revised_text
        set_figure_status(entry, "approved")
        detail = str(
            decision.get("summary") or "The model revised this figure description."
        ).strip()

    await transition_review(db, change, "undone")
    change.metadata_json = json.dumps({**_parse_json(change.metadata_json), "superseded": True})
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
    return AppliedChangeActionResponse(
        status="reopened",
        message="Retried the figure decision and restarted accessibility processing.",
        job_status="processing",
    )


async def apply_undo(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
) -> AppliedChangeActionResponse:
    """Restore the state an applied change replaced. The caller re-runs tagging."""
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
            raise ReviewRefusal(code=404, detail="Alt text entry not found for undo.")
        replaced_state = {
            "generated_text": entry.generated_text if entry else None,
            "edited_text": entry.edited_text if entry else None,
            "status": entry.status if entry else None,
        }
        if bool(undo_payload.get("delete_if_absent", False)):
            if entry is not None:
                await db.delete(entry)
        elif entry is not None:
            await restore_stored_figure(db, entry, undo_payload)
        await transition_review(db, change, "undone")
        if entry is not None and not bool(undo_payload.get("delete_if_absent", False)):
            # The restored semantics get their own revision; never reopen a superseded row.
            await add_applied_change(
                db=db,
                job=job,
                change_type="figure_semantics",
                title=f"Restored figure {entry.figure_index + 1}",
                detail="Restored the previous figure decision.",
                reviewable=False,
                metadata={
                    **_parse_json(change.metadata_json),
                    "figure_index": entry.figure_index,
                    "restored_by_undo_of": change.id,
                },
                before=replaced_state,
                after={
                    "generated_text": entry.generated_text,
                    "edited_text": entry.edited_text,
                    "status": entry.status,
                },
            )

        return AppliedChangeActionResponse(
            status="undone",
            message="Undid the applied figure decision and restarted accessibility processing.",
            job_status="processing",
        )
    raise ReviewRefusal(code=400, detail="This change cannot be undone.")


async def apply_review_action(
    *,
    job: Job,
    db: AsyncSession,
    action: str,
    change_id: int,
    feedback: str | None = None,
    text: str | None = None,
) -> AppliedChangeActionResponse:
    """Apply one review action with saved-state checks; the caller re-runs tagging.

    The v2 runner uses this: the Worker queued the action, and the job's
    database here still holds its finished state.
    """
    _ensure_job_supports_in_app_review(job)
    change = await _load_applied_change(job_id=job.id, change_id=change_id, db=db)
    if action == "edit":
        if change.review_status not in ("pending_review", "kept"):
            raise ReviewRefusal(code=409, detail=_REVIEW_ACTION_STALE_DETAIL)
    else:
        _ensure_change_is_pending_review(change)
    if action == "undo":
        return await apply_undo(job=job, change=change, db=db)
    if change.change_type != "figure_semantics":
        raise ReviewRefusal(
            code=400,
            detail=f"This change type cannot be {'edited' if action == 'edit' else 'revised'} in the app.",
        )
    if action == "revise":
        return await apply_revised_figure_change(
            job=job,
            change=change,
            db=db,
            reviewer_feedback=(feedback or "").strip() or None,
        )
    if action == "edit":
        return await apply_edit(job=job, change=change, db=db, text=text or "")
    raise ReviewRefusal(code=400, detail="Unknown review action.")


async def apply_edit(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
    text: str,
) -> AppliedChangeActionResponse:
    """Set a figure's description by hand. The caller re-runs tagging."""
    entry, metadata = await _load_figure_change_context(job=job, change=change, db=db)
    edited_text = text.strip()
    if not usable_description(edited_text):
        raise ReviewRefusal(code=400, detail="Alt text cannot be empty.")

    previous_state = {
        "generated_text": entry.generated_text,
        "edited_text": entry.edited_text,
        "status": entry.status,
    }

    entry.edited_text = edited_text
    set_figure_status(entry, "approved")

    await transition_review(db, change, "undone")
    change.metadata_json = json.dumps({**_parse_json(change.metadata_json), "superseded": True})
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
    return AppliedChangeActionResponse(
        status="edited",
        message="Saved the edited figure description and restarted accessibility processing.",
        job_status="processing",
    )
