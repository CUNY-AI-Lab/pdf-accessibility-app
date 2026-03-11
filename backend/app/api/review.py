import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, AppliedChange, Job, ReviewTask
from app.pipeline.structure import FigureInfo
from app.pipeline.fidelity import assess_fidelity
from app.pipeline.orchestrator import (
    _build_validation_changes,
    _error_count,
    _update_step,
    _warning_count,
    run_tagging_and_validation,
)
from app.pipeline.validator import ValidationResult, Violation, validate_pdf
from app.schemas import (
    AppliedChangeActionResponse,
    AppliedChangeResponse,
    ReviewRecommendationApplyResponse,
    ReviewTaskResponse,
    ReviewSuggestionRequest,
)
from app.services.applied_changes import (
    add_applied_change,
    change_to_response_payload,
    list_pending_reviewable_changes,
    parse_json_dict,
)
from app.services.file_storage import get_output_path
from app.services.font_actualtext import (
    apply_actualtext_batch_to_contexts,
)
from app.services.font_artifact import apply_artifact_batch_to_contexts
from app.services.font_unicode_override import apply_unicode_override_to_context
from app.services.job_manager import get_job_manager
from app.services.llm_client import make_llm_client
from app.services.path_safety import safe_filename
from app.services.intelligence_gemini_figures import generate_figure_intelligence
from app.services.recommendation_apply import (
    applicable_actualtext_candidates,
    apply_reading_order_recommendation,
    apply_table_recommendation,
    can_accept_reading_order_recommendation,
    can_accept_table_recommendation,
)
from app.services.review_suggestions import (
    generate_review_suggestion,
    select_auto_font_review_resolution,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs/{job_id}", tags=["review"])

LLM_GARBLED_TEXT_FOLLOWUP_KIND = "garbled_text_hint"


def _parse_json(raw: str | None) -> dict:
    return parse_json_dict(raw)


def _allowed_font_targets(task_metadata: dict) -> dict[tuple[int, int], dict]:
    raw_targets = task_metadata.get("font_review_targets", [])
    return {
        (
            int(target.get("page")),
            int(target.get("operator_index")),
        ): target
        for target in raw_targets
        if isinstance(target, dict)
        and isinstance(target.get("page"), int)
        and isinstance(target.get("operator_index"), int)
    }


def _resolve_font_review_target(
    *,
    task_metadata: dict,
    page_number: int,
    operator_index: int,
) -> tuple[dict, str]:
    matched_target = _allowed_font_targets(task_metadata).get((page_number, operator_index))
    if matched_target is None:
        raise HTTPException(
            status_code=400,
            detail="Requested page/operator is not one of the task's flagged font targets",
        )
    context_path = str(matched_target.get("context_path") or "").strip()
    if not context_path:
        raise HTTPException(
            status_code=400,
            detail="Task target did not include a usable veraPDF context path",
        )
    return matched_target, context_path


def _post_tagging_font_remediation_preservation(
    *,
    task: ReviewTask,
    task_metadata: dict,
    actualtext_attempts: list[dict[str, object]] | None = None,
    font_mapping_attempts: list[dict[str, object]] | None = None,
) -> dict[tuple[str, str], dict]:
    preserved: dict[str, object] = {}
    llm_suggestion = task_metadata.get("llm_suggestion")
    if isinstance(llm_suggestion, dict) and llm_suggestion:
        preserved["llm_suggestion"] = llm_suggestion

    for key, new_attempts in (
        ("post_tagging_actualtext_attempts", actualtext_attempts),
        ("post_tagging_font_mapping_attempts", font_mapping_attempts),
    ):
        merged_attempts: list[dict[str, object]] = []
        existing_attempts = task_metadata.get(key)
        if isinstance(existing_attempts, list):
            merged_attempts.extend(
                attempt for attempt in existing_attempts if isinstance(attempt, dict)
            )
        if isinstance(new_attempts, list):
            merged_attempts.extend(
                attempt for attempt in new_attempts if isinstance(attempt, dict)
            )
        if merged_attempts:
            preserved[key] = merged_attempts

    return {
        (task.task_type, task.source): preserved,
    }


async def _load_job(
    *,
    job_id: str,
    db: AsyncSession,
) -> Job:
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


async def _load_review_task(
    *,
    job_id: str,
    task_id: int,
    db: AsyncSession,
    expected_task_type: str | None = None,
    invalid_type_detail: str | None = None,
) -> ReviewTask:
    task_result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.id == task_id,
        )
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found")
    if expected_task_type is not None and task.task_type != expected_task_type:
        raise HTTPException(
            status_code=400,
            detail=invalid_type_detail or "Unsupported review task type",
        )
    return task


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


def _applied_change_to_response(change: AppliedChange) -> AppliedChangeResponse:
    return AppliedChangeResponse(**change_to_response_payload(change))


async def _restart_tagging_with_current_state(
    *,
    job: Job,
    db: AsyncSession,
) -> None:
    job.status = "processing"
    await db.commit()

    settings = get_settings()
    session_maker = get_session_maker()
    job_manager = get_job_manager()

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
        raise HTTPException(status_code=400, detail="Applied figure change is missing figure context")

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
        raise HTTPException(status_code=404, detail="Figure recommendation context is no longer available")

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
            previous_suggestion=metadata.get("llm_suggestion") if isinstance(metadata.get("llm_suggestion"), dict) else None,
        )
    finally:
        await llm_client.close()

    suggested_action = str(decision.get("suggested_action") or "").strip()
    if suggested_action not in {"set_alt_text", "mark_decorative"}:
        raise HTTPException(
            status_code=409,
            detail="The model could not produce a direct figure fix. Download the PDF and do external QA for this case.",
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
            raise HTTPException(status_code=502, detail="The model did not return a revised figure description")
        entry.edited_text = revised_text
        entry.status = "approved"
        detail = str(decision.get("summary") or "The model revised this figure description.").strip()

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
            "llm_suggestion": dict(decision),
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
        message="Revised the figure change and restarted accessibility processing.",
        job_status="processing",
    )


def _garbled_text_followup_spec(*, parent_task: ReviewTask, suggestion: dict) -> dict | None:
    if getattr(parent_task, "task_type", "") != "reading_order":
        return None

    raw_hints = suggestion.get("readable_text_hints", [])
    if not isinstance(raw_hints, list):
        return None

    normalized_hints: list[dict[str, object]] = []
    seen_pairs: set[tuple[int, str]] = set()
    for raw_hint in raw_hints:
        if not isinstance(raw_hint, dict):
            continue
        if not bool(raw_hint.get("should_block_accessibility", False)):
            continue
        page = raw_hint.get("page")
        review_id = str(raw_hint.get("review_id") or "").strip()
        if not isinstance(page, int) or page < 1 or not review_id:
            continue
        key = (page, review_id)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        normalized_hints.append(
            {
                "page": page,
                "review_id": review_id,
                "extracted_text": str(raw_hint.get("extracted_text") or "").strip(),
                "native_text_candidate": str(raw_hint.get("native_text_candidate") or "").strip(),
                "ocr_text_candidate": str(raw_hint.get("ocr_text_candidate") or "").strip(),
                "readable_text_hint": str(raw_hint.get("readable_text_hint") or "").strip(),
                "chosen_source": str(raw_hint.get("chosen_source") or "").strip(),
                "issue_type": str(raw_hint.get("issue_type") or "uncertain").strip() or "uncertain",
                "confidence": str(raw_hint.get("confidence") or "low").strip() or "low",
                "reason": str(raw_hint.get("reason") or "").strip(),
            }
        )

    if not normalized_hints:
        return None

    normalized_hints.sort(key=lambda item: (int(item["page"]), str(item["review_id"])))
    pages_to_check = sorted({int(item["page"]) for item in normalized_hints})
    issue_types = sorted({str(item["issue_type"]) for item in normalized_hints if str(item["issue_type"]).strip()})

    page_label = ", ".join(str(page) for page in pages_to_check[:6])
    if len(pages_to_check) > 6:
        page_label += ", ..."

    detail = (
        f"Gemini flagged {len(normalized_hints)} text block(s) whose extracted text may not match "
        f"what appears on the page. Review pages {page_label} and confirm what assistive technology "
        "should announce before release."
    )

    return {
        "task_type": "content_fidelity",
        "title": "Verify readable text on flagged blocks",
        "detail": detail,
        "severity": "high",
        "blocking": True,
        "source": "fidelity",
        "metadata": {
            "llm_followup_kind": LLM_GARBLED_TEXT_FOLLOWUP_KIND,
            "parent_task_id": int(parent_task.id),
            "pages_to_check": pages_to_check,
            "issue_types": issue_types,
            "flagged_blocks": normalized_hints,
            "llm_summary": str(suggestion.get("summary") or "").strip(),
        },
    }


async def _sync_llm_followup_tasks(
    *,
    db: AsyncSession,
    job_id: str,
    parent_task: ReviewTask,
    suggestion: dict,
) -> None:
    followup_spec = _garbled_text_followup_spec(parent_task=parent_task, suggestion=suggestion)

    result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.task_type == "content_fidelity",
            ReviewTask.source == "fidelity",
        )
    )
    candidate_tasks = result.scalars().all()

    matching_tasks: list[ReviewTask] = []
    for candidate in candidate_tasks:
        metadata = _parse_json(candidate.metadata_json)
        if (
            metadata.get("llm_followup_kind") == LLM_GARBLED_TEXT_FOLLOWUP_KIND
            and int(metadata.get("parent_task_id") or -1) == int(parent_task.id)
        ):
            matching_tasks.append(candidate)

    if followup_spec is None:
        for existing in matching_tasks:
            await db.delete(existing)
        return

    primary = matching_tasks[0] if matching_tasks else None
    if primary is None:
        db.add(
            ReviewTask(
                job_id=job_id,
                task_type=str(followup_spec["task_type"]),
                title=str(followup_spec["title"]),
                detail=str(followup_spec["detail"]),
                severity=str(followup_spec["severity"]),
                blocking=bool(followup_spec["blocking"]),
                status="pending_review",
                source=str(followup_spec["source"]),
                metadata_json=json.dumps(followup_spec["metadata"]),
            )
        )
    else:
        primary.title = str(followup_spec["title"])
        primary.detail = str(followup_spec["detail"])
        primary.severity = str(followup_spec["severity"])
        primary.blocking = bool(followup_spec["blocking"])
        primary.status = "pending_review"
        primary.metadata_json = json.dumps(followup_spec["metadata"])

    for duplicate in matching_tasks[1:]:
        await db.delete(duplicate)


def _accepted_recommendation_metadata(
    task_metadata: dict,
    *,
    suggested_action: str,
) -> dict:
    metadata = dict(task_metadata)
    metadata["accepted_recommendation"] = {
        "suggested_action": suggested_action,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    return metadata


def _validation_result_from_payload(payload: dict) -> ValidationResult:
    violations: list[Violation] = []
    raw_violations = payload.get("violations", [])
    if isinstance(raw_violations, list):
        for item in raw_violations:
            if not isinstance(item, dict):
                continue
            violations.append(
                Violation(
                    rule_id=str(item.get("rule_id") or ""),
                    description=str(item.get("description") or "Unknown violation"),
                    severity=str(item.get("severity") or "error"),
                    location=str(item.get("location")) if item.get("location") is not None else None,
                    count=int(item.get("count", 1) or 1),
                    category=str(item.get("category")) if item.get("category") is not None else None,
                    fix_hint=str(item.get("fix_hint")) if item.get("fix_hint") is not None else None,
                )
            )
    return ValidationResult(
        compliant=bool(payload.get("compliant", False)),
        violations=violations,
        raw_report={},
    )


async def _load_font_review_context(
    *,
    job_id: str,
    task_id: int,
    db: AsyncSession,
) -> tuple[Job, ReviewTask, Path, dict, dict[tuple[int, int], dict]]:
    job = await _load_job(job_id=job_id, db=db)
    if not job.output_path:
        raise HTTPException(status_code=400, detail="Job does not have a tagged PDF output yet")
    if not job.validation_json:
        raise HTTPException(status_code=400, detail="Validation report is not available for this job")

    task = await _load_review_task(
        job_id=job_id,
        task_id=task_id,
        db=db,
        expected_task_type="font_text_fidelity",
        invalid_type_detail="This remediation is only supported for font review tasks",
    )

    output_pdf = Path(job.output_path)
    if not output_pdf.exists():
        raise HTTPException(status_code=404, detail="Tagged PDF output file not found")

    task_metadata = _parse_json(task.metadata_json)
    return job, task, output_pdf, task_metadata, _allowed_font_targets(task_metadata)


async def _refresh_after_post_tagging_remediation(
    *,
    job: Job,
    db: AsyncSession,
    output_pdf: Path,
    preserved_task_metadata: dict[tuple[str, str], dict],
    remediation_claims: dict[str, bool],
    failure_detail: str,
) -> None:
    job.status = "processing"
    await db.commit()

    settings = get_settings()
    try:
        await _refresh_post_tagging_reports(
            job=job,
            db=db,
            settings=settings,
            output_pdf=output_pdf,
            preserved_task_metadata=preserved_task_metadata,
            remediation_claims=remediation_claims,
        )
    except Exception as exc:
        job.status = "awaiting_recommendation_review"
        await db.commit()
        logger.exception(failure_detail)
        raise HTTPException(
            status_code=502,
            detail="Remediation applied but validation refresh failed",
        ) from exc


async def _refresh_recommendation_review_status(
    *,
    job: Job,
    db: AsyncSession,
) -> None:
    validation_payload = _parse_json(job.validation_json)
    result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job.id,
            ReviewTask.blocking.is_(True),
            ReviewTask.status == "pending_review",
        )
    )
    pending_blocking = result.scalars().all()
    job.status = (
        "complete"
        if bool(validation_payload.get("compliant", False)) and not pending_blocking
        else "awaiting_recommendation_review"
    )
    await db.commit()


async def _ensure_review_task_from_change(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
    feedback: str | None,
) -> ReviewTask:
    change_metadata = _parse_json(change.metadata_json)
    reopen_task = change_metadata.get("reopen_task")
    if not isinstance(reopen_task, dict):
        raise HTTPException(status_code=400, detail="This change cannot be revised automatically.")

    task = ReviewTask(
        job_id=job.id,
        task_type=str(reopen_task.get("task_type") or "review_task"),
        title=str(reopen_task.get("title") or change.title),
        detail=str(reopen_task.get("detail") or change.detail),
        severity=str(reopen_task.get("severity") or "high"),
        blocking=bool(reopen_task.get("blocking", True)),
        status="pending_review",
        source=str(reopen_task.get("source") or "fidelity"),
        metadata_json=json.dumps(reopen_task.get("metadata", {})),
    )
    db.add(task)
    await db.flush()

    if feedback:
        settings = get_settings()
        llm_client = make_llm_client(settings)
        try:
            suggestion = await generate_review_suggestion(
                job=job,
                task=task,
                llm_client=llm_client,
                reviewer_feedback=feedback,
            )
        except Exception as exc:
            logger.exception("Failed to regenerate recommendation from applied change")
            raise HTTPException(status_code=502, detail="Failed to revise the applied change.") from exc
        finally:
            await llm_client.close()
        metadata = _parse_json(task.metadata_json)
        metadata["llm_suggestion"] = suggestion
        task.metadata_json = json.dumps(metadata)
        await _sync_llm_followup_tasks(
            db=db,
            job_id=job.id,
            parent_task=task,
            suggestion=suggestion,
        )

    job.status = "awaiting_recommendation_review"
    change.review_status = "undone"
    await db.commit()
    await db.refresh(task)
    return task


async def _undo_applied_change(
    *,
    job: Job,
    change: AppliedChange,
    db: AsyncSession,
) -> AppliedChangeActionResponse:
    undo_payload = _parse_json(change.undo_payload_json)
    kind = str(undo_payload.get("kind") or "").strip()
    if kind == "structure_json":
        structure_payload = undo_payload.get("structure_json")
        if not isinstance(structure_payload, dict):
            raise HTTPException(status_code=400, detail="This change cannot be undone.")
        change.review_status = "undone"
        await db.flush()
        await _restart_tagging_with_structure_recommendation(
            job=job,
            db=db,
            structure_payload=structure_payload,
        )
        return AppliedChangeActionResponse(
            status="undone",
            message="Undid the applied change and restarted validation.",
            job_status="processing",
        )
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
            message="Undid the applied figure change and restarted accessibility processing.",
            job_status="processing",
        )
    raise HTTPException(status_code=400, detail="This change cannot be undone.")


async def _accept_recommendation_without_changes(
    *,
    job: Job,
    task: ReviewTask,
    task_metadata: dict,
    suggested_action: str,
    db: AsyncSession,
) -> ReviewRecommendationApplyResponse:
    task.status = "resolved"
    task.metadata_json = json.dumps(
        _accepted_recommendation_metadata(
            task_metadata,
            suggested_action=suggested_action,
        )
    )
    await db.commit()
    await _refresh_recommendation_review_status(job=job, db=db)
    return ReviewRecommendationApplyResponse(
        status="accepted",
        message="Accepted the recommendation. No PDF changes were needed.",
    )


async def _restart_tagging_with_structure_recommendation(
    *,
    job: Job,
    db: AsyncSession,
    structure_payload: dict,
) -> None:
    job.structure_json = json.dumps(structure_payload)
    job.status = "processing"
    await db.commit()

    settings = get_settings()
    session_maker = get_session_maker()
    job_manager = get_job_manager()

    async def _resume(jid, sm, s, jm, structure_json):
        async with sm() as resume_db:
            await run_tagging_and_validation(
                jid,
                resume_db,
                s,
                jm,
                structure_json=structure_json,
            )

    await job_manager.submit_job(
        job.id,
        _resume(job.id, session_maker, settings, job_manager, structure_payload),
    )


async def _refresh_post_tagging_reports(
    *,
    job: Job,
    db: AsyncSession,
    settings,
    output_pdf: Path,
    preserved_task_metadata: dict[tuple[str, str], dict] | None = None,
    remediation_claims: dict[str, bool] | None = None,
) -> None:
    previous_payload = _parse_json(job.validation_json)
    baseline_validation = _validation_result_from_payload(previous_payload)
    tagging_metrics = previous_payload.get("tagging", {})
    if not isinstance(tagging_metrics, dict):
        tagging_metrics = {}
    previous_remediation = previous_payload.get("remediation", {})
    if not isinstance(previous_remediation, dict):
        previous_remediation = {}

    await _update_step(db, job.id, "validation", "running")
    await _update_step(db, job.id, "fidelity", "running")

    selected_validation = await validate_pdf(
        pdf_path=output_pdf,
        verapdf_path=settings.verapdf_path,
        flavour=settings.verapdf_flavour,
        timeout_seconds=settings.subprocess_timeout_validation,
    )

    changes, status_by_rule = _build_validation_changes(
        baseline_validation.violations,
        selected_validation.violations,
    )
    for change in changes:
        if int(change.get("baseline_count", 0) or 0) > 0 and int(change.get("post_count", 0) or 0) == 0:
            change["remediation_status"] = "manual_remediated"
            status_by_rule[str(change.get("rule_id") or "")] = "manual_remediated"

    baseline_errors = _error_count(baseline_validation)
    baseline_warnings = _warning_count(baseline_validation)
    post_errors = _error_count(selected_validation)
    post_warnings = _warning_count(selected_validation)
    needs_remediation = len(
        [c for c in changes if c["remediation_status"] == "needs_remediation"]
    )
    auto_remediated = len(
        [c for c in changes if c["remediation_status"] == "auto_remediated"]
    )
    manual_remediated = len(
        [c for c in changes if c["remediation_status"] == "manual_remediated"]
    )

    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job.id,
            AltTextEntry.status.in_(("approved", "rejected")),
        )
    )
    reviewed_alt_entries = result.scalars().all()
    structure_json = _parse_json(job.structure_json)

    normalized_remediation_claims = {"post_tagging_review_edit": True}
    if isinstance(remediation_claims, dict):
        normalized_remediation_claims.update({
            str(key): bool(value)
            for key, value in remediation_claims.items()
            if str(key).strip()
        })

    validation_payload = {
        "compliant": selected_validation.compliant,
        "profile": settings.verapdf_flavour,
        "standard": "PDF/UA",
        "validator": "veraPDF",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline": {
            "compliant": baseline_validation.compliant,
            "validator": str(previous_payload.get("validator") or "veraPDF"),
            "violations_count": len(baseline_validation.violations),
            "summary": {
                "errors": baseline_errors,
                "warnings": baseline_warnings,
            },
        },
        "violations": [
            {
                "rule_id": v.rule_id,
                "description": v.description,
                "severity": v.severity,
                "location": v.location,
                "count": v.count,
                "category": v.category,
                "fix_hint": v.fix_hint,
                "remediation_status": status_by_rule.get(v.rule_id, "needs_remediation"),
            }
            for v in selected_validation.violations
        ],
        "summary": {
            "passed": len([v for v in selected_validation.violations if v.severity != "error"]),
            "failed": len([v for v in selected_validation.violations if v.severity == "error"]),
            "errors": post_errors,
            "warnings": post_warnings,
        },
        "changes": changes,
        "remediation": {
            "needs_remediation": needs_remediation,
            "auto_remediated": auto_remediated,
            "manual_remediated": manual_remediated,
            "baseline_errors": baseline_errors,
            "baseline_warnings": baseline_warnings,
            "post_errors": post_errors,
            "post_warnings": post_warnings,
            "errors_reduced": baseline_errors - post_errors,
            "warnings_reduced": baseline_warnings - post_warnings,
            "font_remediation": previous_remediation.get("font_remediation", {}),
            **normalized_remediation_claims,
        },
        "tagging": tagging_metrics,
        "claims": {
            **(previous_payload.get("claims", {}) if isinstance(previous_payload.get("claims"), dict) else {}),
            **normalized_remediation_claims,
        },
    }

    fidelity_report, review_tasks = assess_fidelity(
        input_pdf=Path(job.input_path),
        output_pdf=output_pdf,
        structure_json=structure_json,
        alt_entries=[
            {
                "figure_index": entry.figure_index,
                "generated_text": entry.generated_text,
                "edited_text": entry.edited_text,
                "status": entry.status,
            }
            for entry in reviewed_alt_entries
        ],
        validation_report=validation_payload,
        raw_validation_report=selected_validation.raw_report,
        tagging_metrics=tagging_metrics,
        classification=job.classification,
    )

    validation_payload["fidelity"] = fidelity_report
    job.output_path = str(output_pdf)
    job.validation_json = json.dumps(validation_payload)
    job.fidelity_json = json.dumps(fidelity_report)

    await db.execute(delete(ReviewTask).where(ReviewTask.job_id == job.id))
    for task in review_tasks:
        metadata = task.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        key = (
            str(task.get("task_type") or "review_task"),
            str(task.get("source") or "fidelity"),
        )
        preserved = (
            preserved_task_metadata.get(key, {})
            if preserved_task_metadata is not None
            else {}
        )
        if isinstance(preserved, dict) and preserved:
            metadata = {**preserved, **metadata}
        db.add(ReviewTask(
            job_id=job.id,
            task_type=str(task.get("task_type") or "review_task"),
            title=str(task.get("title") or "Recommendation review required"),
            detail=str(task.get("detail") or ""),
            severity=str(task.get("severity") or "medium"),
            blocking=bool(task.get("blocking", True)),
            status=str(task.get("status") or "pending_review"),
            source=str(task.get("source") or "fidelity"),
            metadata_json=json.dumps(metadata),
        ))

    blocking_task_count = len([task for task in review_tasks if bool(task.get("blocking"))])
    await _update_step(db, job.id, "validation", "complete", result={
        "compliant": selected_validation.compliant,
        "violations_count": len(selected_validation.violations),
        "post_tagging_actualtext": True,
    })
    await _update_step(db, job.id, "fidelity", "complete", result={
        "passed": bool(fidelity_report.get("passed", False)),
        "blocking_tasks": blocking_task_count,
        "advisory_tasks": len(review_tasks) - blocking_task_count,
    })

    job.status = "complete" if selected_validation.compliant and not blocking_task_count else "awaiting_recommendation_review"
    await db.commit()


@router.post("/approve", status_code=200)
async def approve_review(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == "awaiting_recommendation_review":
        raise HTTPException(
            status_code=400,
            detail=(
                "Recommendation review completes automatically when the remaining "
                "blocking recommendations are accepted."
            ),
        )

    raise HTTPException(
        status_code=400,
        detail=f"Job is not awaiting review (current status: {job.status})",
    )


@router.post("/review-tasks/{task_id}/suggest", response_model=ReviewTaskResponse)
async def suggest_review_task(
    job_id: str,
    task_id: int,
    request: ReviewSuggestionRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(job_id=job_id, db=db)
    task = await _load_review_task(job_id=job_id, task_id=task_id, db=db)

    settings = get_settings()
    llm_client = make_llm_client(settings)

    metadata = _parse_json(task.metadata_json)
    try:
        suggestion = await generate_review_suggestion(
            job=job,
            task=task,
            llm_client=llm_client,
            reviewer_feedback=(request.feedback.strip() if request and request.feedback else None),
        )
    except ValueError as exc:
        logger.exception("Invalid review suggestion request")
        raise HTTPException(status_code=400, detail="Invalid suggestion request") from exc
    except Exception as exc:
        logger.exception("Failed to generate LLM suggestion")
        raise HTTPException(
            status_code=502,
            detail="Failed to generate suggestion",
        ) from exc
    finally:
        await llm_client.close()

    metadata["llm_suggestion"] = suggestion
    task.metadata_json = json.dumps(metadata) if metadata else None
    await _sync_llm_followup_tasks(
        db=db,
        job_id=job_id,
        parent_task=task,
        suggestion=suggestion,
    )
    await db.commit()
    await db.refresh(task)
    return _task_to_response(task)


@router.get("/applied-changes", response_model=list[AppliedChangeResponse])
async def list_applied_changes(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(job_id=job_id, db=db)
    changes = await list_pending_reviewable_changes(db=db, job_id=job.id)
    return [_applied_change_to_response(change) for change in changes]


@router.post("/applied-changes/{change_id}/keep", response_model=AppliedChangeActionResponse)
async def keep_applied_change(
    job_id: str,
    change_id: int,
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(job_id=job_id, db=db)
    change = await _load_applied_change(job_id=job_id, change_id=change_id, db=db)
    change.review_status = "kept"
    await db.commit()
    return AppliedChangeActionResponse(
        status="kept",
        message="Kept this applied change.",
        job_status=str(job.status),
    )


@router.post("/applied-changes/{change_id}/undo", response_model=AppliedChangeActionResponse)
async def undo_applied_change(
    job_id: str,
    change_id: int,
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(job_id=job_id, db=db)
    change = await _load_applied_change(job_id=job_id, change_id=change_id, db=db)
    return await _undo_applied_change(job=job, change=change, db=db)


@router.post("/applied-changes/{change_id}/suggest", response_model=AppliedChangeActionResponse)
async def suggest_applied_change(
    job_id: str,
    change_id: int,
    request: ReviewSuggestionRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(job_id=job_id, db=db)
    change = await _load_applied_change(job_id=job_id, change_id=change_id, db=db)
    if change.change_type == "figure_semantics":
        return await _apply_revised_figure_change(
            job=job,
            change=change,
            db=db,
            reviewer_feedback=(request.feedback.strip() if request and request.feedback else None),
        )
    await _undo_applied_change(job=job, change=change, db=db)
    await _ensure_review_task_from_change(
        job=job,
        change=change,
        db=db,
        feedback=(request.feedback.strip() if request and request.feedback else None),
    )
    return AppliedChangeActionResponse(
        status="reopened",
        message="Undid this change and reopened it for a revised recommendation.",
        job_status="awaiting_recommendation_review",
    )


@router.post(
    "/review-tasks/{task_id}/apply-recommendation",
    response_model=ReviewRecommendationApplyResponse,
)
async def apply_review_recommendation(
    job_id: str,
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(job_id=job_id, db=db)
    task = await _load_review_task(job_id=job_id, task_id=task_id, db=db)

    task_metadata = _parse_json(task.metadata_json)
    suggestion = task_metadata.get("llm_suggestion")
    if not isinstance(suggestion, dict) or not suggestion:
        raise HTTPException(status_code=400, detail="No recommendation is available for this task")
    suggested_action = str(suggestion.get("suggested_action") or "").strip()

    if task.task_type == "reading_order":
        structure_payload = _parse_json(job.structure_json)
        if suggested_action == "confirm_current_order":
            return await _accept_recommendation_without_changes(
                job=job,
                task=task,
                task_metadata=task_metadata,
                suggested_action=suggested_action,
                db=db,
            )
        if not can_accept_reading_order_recommendation(structure_payload, suggestion):
            raise HTTPException(
                status_code=400,
                detail="This recommendation is not ready to apply automatically yet. Suggest an alternative instead.",
            )
        next_structure = apply_reading_order_recommendation(structure_payload, suggestion)
        if not next_structure:
            raise HTTPException(
                status_code=400,
                detail="This recommendation is not ready to apply automatically yet. Suggest an alternative instead.",
            )
        await add_applied_change(
            db=db,
            job=job,
            change_type="reading_order",
            title="Updated reading order",
            detail=str(suggestion.get("summary") or "The model reordered content to improve assistive-tech reading order."),
            importance="high",
            reviewable=True,
            metadata={
                "suggested_action": suggested_action,
                "llm_suggestion": suggestion,
                "reopen_task": {
                    "task_type": task.task_type,
                    "title": task.title,
                    "detail": task.detail,
                    "severity": task.severity,
                    "blocking": task.blocking,
                    "source": task.source,
                    "metadata": {
                        **task_metadata,
                        "llm_suggestion": suggestion,
                    },
                },
            },
            before={"structure_json": structure_payload},
            after={"structure_json": next_structure},
            undo_payload={"kind": "structure_json", "structure_json": structure_payload},
        )
        await _restart_tagging_with_structure_recommendation(
            job=job,
            db=db,
            structure_payload=next_structure,
        )
        return ReviewRecommendationApplyResponse(
            status="accepted",
            message="Applied the recommendation and restarted tagging.",
        )

    if task.task_type == "table_semantics":
        structure_payload = _parse_json(job.structure_json)
        if suggested_action == "confirm_current_headers":
            return await _accept_recommendation_without_changes(
                job=job,
                task=task,
                task_metadata=task_metadata,
                suggested_action=suggested_action,
                db=db,
            )
        if not can_accept_table_recommendation(structure_payload, suggestion):
            raise HTTPException(
                status_code=400,
                detail="This recommendation is not ready to apply automatically yet. Suggest an alternative instead.",
            )
        next_structure = apply_table_recommendation(structure_payload, suggestion)
        if not next_structure:
            raise HTTPException(
                status_code=400,
                detail="This recommendation is not ready to apply automatically yet. Suggest an alternative instead.",
            )
        await add_applied_change(
            db=db,
            job=job,
            change_type="table_semantics",
            title="Updated table interpretation",
            detail=str(suggestion.get("summary") or "The model updated table semantics to improve accessible reading."),
            importance="high",
            reviewable=True,
            metadata={
                "suggested_action": suggested_action,
                "llm_suggestion": suggestion,
                "reopen_task": {
                    "task_type": task.task_type,
                    "title": task.title,
                    "detail": task.detail,
                    "severity": task.severity,
                    "blocking": task.blocking,
                    "source": task.source,
                    "metadata": {
                        **task_metadata,
                        "llm_suggestion": suggestion,
                    },
                },
            },
            before={"structure_json": structure_payload},
            after={"structure_json": next_structure},
            undo_payload={"kind": "structure_json", "structure_json": structure_payload},
        )
        await _restart_tagging_with_structure_recommendation(
            job=job,
            db=db,
            structure_payload=next_structure,
        )
        return ReviewRecommendationApplyResponse(
            status="accepted",
            message="Applied the recommendation and restarted tagging.",
        )

    if task.task_type == "font_text_fidelity":
        _, _, output_pdf, _, _ = await _load_font_review_context(
            job_id=job_id,
            task_id=task_id,
            db=db,
        )
        if suggested_action == "artifact_if_decorative":
            selected = select_auto_font_review_resolution(
                job=job,
                task=task,
                suggestion=suggestion,
            )
            if not isinstance(selected, dict) or str(selected.get("resolution_type") or "") != "artifact":
                raise HTTPException(
                    status_code=400,
                    detail="This recommendation is not ready to apply automatically yet. Suggest an alternative instead.",
                )
            targets = selected.get("targets")
            if not isinstance(targets, list) or not targets:
                raise HTTPException(status_code=400, detail="Invalid recommendation request")
            context_paths = [
                str(target.get("context_path") or "").strip()
                for target in targets
                if isinstance(target, dict)
            ]
            if any(not context_path for context_path in context_paths):
                raise HTTPException(status_code=400, detail="Invalid recommendation request")
            patched_output = get_output_path(
                job_id,
                f"accessible_recommendation_artifact_{task_id}_{safe_filename(job.original_filename)}",
            )
            try:
                apply_artifact_batch_to_contexts(
                    input_pdf=output_pdf,
                    output_pdf=patched_output,
                    context_paths=context_paths,
                )
            except ValueError as exc:
                logger.exception("Invalid recommendation remediation request")
                raise HTTPException(status_code=400, detail="Invalid recommendation request") from exc
            except Exception as exc:
                logger.exception("Failed to apply recommendation remediation")
                raise HTTPException(status_code=502, detail="Internal processing error") from exc
            preserved_task_metadata = _post_tagging_font_remediation_preservation(
                task=task,
                task_metadata=task_metadata,
            )
            await _refresh_after_post_tagging_remediation(
                job=job,
                db=db,
                output_pdf=patched_output,
                preserved_task_metadata=preserved_task_metadata,
                remediation_claims={"post_tagging_artifact": True},
                failure_detail="Validation refresh failed after accepted recommendation remediation",
            )
            return ReviewRecommendationApplyResponse(
                status="accepted",
                message="Accepted the recommendation and refreshed validation.",
            )

        if suggested_action == "font_map_candidate":
            selected = select_auto_font_review_resolution(
                job=job,
                task=task,
                suggestion=suggestion,
            )
            if not isinstance(selected, dict) or str(selected.get("resolution_type") or "") != "font_map":
                raise HTTPException(
                    status_code=400,
                    detail="This recommendation is not ready to apply automatically yet. Suggest an alternative instead.",
                )
            _, context_path = _resolve_font_review_target(
                task_metadata=task_metadata,
                page_number=int(selected["page_number"]),
                operator_index=int(selected["operator_index"]),
            )
            patched_output = get_output_path(
                job_id,
                f"accessible_recommendation_fontmap_{task_id}_{safe_filename(job.original_filename)}",
            )
            try:
                applied = apply_unicode_override_to_context(
                    input_pdf=output_pdf,
                    output_pdf=patched_output,
                    context_path=context_path,
                    unicode_text=str(selected["unicode_text"]),
                )
            except ValueError as exc:
                logger.exception("Invalid recommendation remediation request")
                raise HTTPException(status_code=400, detail="Invalid recommendation request") from exc
            except Exception as exc:
                logger.exception("Failed to apply recommendation remediation")
                raise HTTPException(status_code=502, detail="Internal processing error") from exc
            preserved_task_metadata = _post_tagging_font_remediation_preservation(
                task=task,
                task_metadata=task_metadata,
                font_mapping_attempts=[
                    {
                        "page_number": int(selected["page_number"]),
                        "operator_index": int(selected["operator_index"]),
                        "unicode_text": str(selected["unicode_text"]),
                        "font_base_name": applied.get("font_base_name"),
                        "font_code_hex": f"{int(applied.get('font_code', 0)):02X}",
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                        "mode": "accepted_recommendation",
                    }
                ],
            )
            await _refresh_after_post_tagging_remediation(
                job=job,
                db=db,
                output_pdf=patched_output,
                preserved_task_metadata=preserved_task_metadata,
                remediation_claims={"post_tagging_font_map": True},
                failure_detail="Validation refresh failed after accepted recommendation remediation",
            )
            return ReviewRecommendationApplyResponse(
                status="accepted",
                message="Accepted the recommendation and refreshed validation.",
            )

        candidates = applicable_actualtext_candidates(suggestion, task_metadata)
        if not candidates:
            raise HTTPException(
                status_code=400,
                detail="This recommendation is not ready to apply automatically yet. Suggest an alternative instead.",
            )

        batch_patches: list[dict[str, str]] = []
        for candidate in candidates:
            _, context_path = _resolve_font_review_target(
                task_metadata=task_metadata,
                page_number=int(candidate["page"]),
                operator_index=int(candidate["operator_index"]),
            )
            batch_patches.append(
                {
                    "context_path": context_path,
                    "actual_text": str(candidate["proposed_actualtext"]),
                }
            )

        patched_output = get_output_path(
            job_id,
            f"accessible_recommendation_actualtext_{task_id}_{safe_filename(job.original_filename)}",
        )
        try:
            apply_actualtext_batch_to_contexts(
                input_pdf=output_pdf,
                output_pdf=patched_output,
                patches=batch_patches,
            )
        except ValueError as exc:
            logger.exception("Invalid recommendation remediation request")
            raise HTTPException(status_code=400, detail="Invalid recommendation request") from exc
        except Exception as exc:
            logger.exception("Failed to apply recommendation remediation")
            raise HTTPException(status_code=502, detail="Internal processing error") from exc

        preserved_task_metadata = _post_tagging_font_remediation_preservation(
            task=task,
            task_metadata=task_metadata,
            actualtext_attempts=[
                {
                    "page_number": candidate["page"],
                    "operator_index": candidate["operator_index"],
                    "actual_text": candidate["proposed_actualtext"],
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                    "mode": "accepted_recommendation",
                }
                for candidate in candidates
            ],
        )
        await _refresh_after_post_tagging_remediation(
            job=job,
            db=db,
            output_pdf=patched_output,
            preserved_task_metadata=preserved_task_metadata,
            remediation_claims={"post_tagging_actualtext": True},
            failure_detail="Validation refresh failed after accepted recommendation remediation",
        )
        return ReviewRecommendationApplyResponse(
            status="accepted",
            message="Accepted the recommendation and refreshed validation.",
        )

    raise HTTPException(
        status_code=400,
        detail="This task type does not support direct recommendation application",
    )
