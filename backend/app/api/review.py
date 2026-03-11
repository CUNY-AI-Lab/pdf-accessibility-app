import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, Job, ReviewTask
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
    ReviewRecommendationApplyResponse,
    ReviewTaskResponse,
    ReviewSuggestionRequest,
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
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
