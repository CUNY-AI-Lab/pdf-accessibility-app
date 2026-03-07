import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, Job, ReviewTask
from app.pipeline.fidelity import assess_fidelity
from app.pipeline.orchestrator import run_tagging_and_validation
from app.pipeline.orchestrator import (
    _build_validation_changes,
    _error_count,
    _update_step,
    _warning_count,
)
from app.pipeline.validator import ValidationResult, Violation, validate_pdf
from app.schemas import (
    FontActualTextBatchRequest,
    FontActualTextRequest,
    FontUnicodeOverrideRequest,
    ReviewTaskResponse,
    ReviewTaskUpdateRequest,
)
from app.services.job_manager import get_job_manager
from app.services.llm_client import LlmClient
from app.services.file_storage import get_output_path
from app.services.font_actualtext import (
    apply_actualtext_batch_to_contexts,
    apply_actualtext_to_context,
)
from app.services.font_unicode_override import apply_unicode_override_to_context
from app.services.pdf_preview import render_target_preview_png_bytes
from app.services.review_suggestions import generate_review_suggestion

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


def _manual_font_remediation_preservation(
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
        ("manual_actualtext_attempts", actualtext_attempts),
        ("manual_font_mapping_attempts", font_mapping_attempts),
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


def _existing_job_pdf_path(job: Job) -> Path:
    candidates = []
    if job.output_path:
        candidates.append(Path(job.output_path))
    if job.input_path:
        candidates.append(Path(job.input_path))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise HTTPException(status_code=404, detail="PDF file not found for review preview")


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


async def _refresh_post_tagging_reports(
    *,
    job: Job,
    db: AsyncSession,
    settings,
    output_pdf: Path,
    preserved_task_metadata: dict[tuple[str, str], dict] | None = None,
    manual_claims: dict[str, bool] | None = None,
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

    normalized_manual_claims = {"manual_post_tagging_review_edit": True}
    if isinstance(manual_claims, dict):
        normalized_manual_claims.update({
            str(key): bool(value)
            for key, value in manual_claims.items()
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
            **normalized_manual_claims,
        },
        "tagging": tagging_metrics,
        "claims": {
            **(previous_payload.get("claims", {}) if isinstance(previous_payload.get("claims"), dict) else {}),
            **normalized_manual_claims,
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
            str(task.get("task_type") or "manual_review"),
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
            task_type=str(task.get("task_type") or "manual_review"),
            title=str(task.get("title") or "Manual review required"),
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
        "manual_post_tagging_actualtext": True,
    })
    await _update_step(db, job.id, "fidelity", "complete", result={
        "passed": bool(fidelity_report.get("passed", False)),
        "blocking_tasks": blocking_task_count,
        "advisory_tasks": len(review_tasks) - blocking_task_count,
    })

    job.status = "complete" if selected_validation.compliant and not blocking_task_count else "needs_manual_review"
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


@router.post("/review-tasks/{task_id}/suggest", response_model=ReviewTaskResponse)
async def suggest_review_task(
    job_id: str,
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    task_result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.id == task_id,
        )
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found")

    settings = get_settings()
    llm_client = LlmClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
    )

    metadata = _parse_json(task.metadata_json)
    try:
        suggestion = await generate_review_suggestion(job=job, task=task, llm_client=llm_client)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to generate LLM suggestion: {exc}",
        ) from exc
    finally:
        await llm_client.close()

    metadata["llm_suggestion"] = suggestion
    task.metadata_json = json.dumps(metadata) if metadata else None
    await db.commit()
    await db.refresh(task)
    return _task_to_response(task)


@router.get("/review-tasks/{task_id}/font-target-preview")
async def get_font_target_preview(
    job_id: str,
    task_id: int,
    page_number: int = Query(..., ge=1),
    operator_index: int = Query(..., ge=0),
    db: AsyncSession = Depends(get_db),
):
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    task_result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.id == task_id,
        )
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found")
    if task.task_type != "font_text_fidelity":
        raise HTTPException(status_code=400, detail="Font target previews are only supported for font review tasks")

    task_metadata = _parse_json(task.metadata_json)
    allowed_targets = _allowed_font_targets(task_metadata)
    matched_target = allowed_targets.get((page_number, operator_index))
    if matched_target is None:
        raise HTTPException(
            status_code=400,
            detail="Requested page/operator is not one of the task's flagged font targets",
        )

    context_path = str(matched_target.get("context_path") or "").strip()
    if not context_path:
        raise HTTPException(status_code=400, detail="Task target did not include a usable veraPDF context path")

    pdf_path = _existing_job_pdf_path(job)
    try:
        image_bytes = render_target_preview_png_bytes(pdf_path, context_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to render font target preview: {exc}") from exc

    return StreamingResponse(BytesIO(image_bytes), media_type="image/png")


@router.post("/review-tasks/{task_id}/actualtext", status_code=200)
async def apply_font_actualtext(
    job_id: str,
    task_id: int,
    request: FontActualTextRequest,
    db: AsyncSession = Depends(get_db),
):
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.output_path:
        raise HTTPException(status_code=400, detail="Job does not have a tagged PDF output yet")
    if not job.validation_json:
        raise HTTPException(status_code=400, detail="Validation report is not available for this job")

    task_result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.id == task_id,
        )
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found")
    if task.task_type != "font_text_fidelity":
        raise HTTPException(status_code=400, detail="ActualText remediation is only supported for font review tasks")

    output_pdf = Path(job.output_path)
    if not output_pdf.exists():
        raise HTTPException(status_code=404, detail="Tagged PDF output file not found")

    task_metadata = _parse_json(task.metadata_json)
    allowed_targets = _allowed_font_targets(task_metadata)
    matched_target = allowed_targets.get((request.page_number, request.operator_index))
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

    patched_output = get_output_path(
        job_id,
        f"accessible_manual_actualtext_{task_id}_{job.original_filename}",
    )

    try:
        apply_actualtext_to_context(
            input_pdf=output_pdf,
            output_pdf=patched_output,
            context_path=context_path,
            actual_text=request.actual_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to apply ActualText remediation: {exc}") from exc

    job.status = "processing"
    await db.commit()

    settings = get_settings()
    preserved_task_metadata = _manual_font_remediation_preservation(
        task=task,
        task_metadata=task_metadata,
        actualtext_attempts=[
            {
                "page_number": request.page_number,
                "operator_index": request.operator_index,
                "actual_text": request.actual_text,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "mode": "single",
            }
        ],
    )
    try:
        await _refresh_post_tagging_reports(
            job=job,
            db=db,
            settings=settings,
            output_pdf=patched_output,
            preserved_task_metadata=preserved_task_metadata,
            manual_claims={"manual_post_tagging_actualtext": True},
        )
    except Exception as exc:
        job.status = "needs_manual_review"
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"ActualText remediation was applied, but validation refresh failed: {exc}",
        ) from exc

    return {
        "status": "ok",
        "message": "Applied ActualText remediation and refreshed validation.",
    }


@router.post("/review-tasks/{task_id}/actualtext/batch", status_code=200)
async def apply_font_actualtext_batch(
    job_id: str,
    task_id: int,
    request: FontActualTextBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.output_path:
        raise HTTPException(status_code=400, detail="Job does not have a tagged PDF output yet")
    if not job.validation_json:
        raise HTTPException(status_code=400, detail="Validation report is not available for this job")

    task_result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.id == task_id,
        )
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found")
    if task.task_type != "font_text_fidelity":
        raise HTTPException(status_code=400, detail="ActualText remediation is only supported for font review tasks")
    if not request.targets:
        raise HTTPException(status_code=400, detail="At least one batch target is required")

    output_pdf = Path(job.output_path)
    if not output_pdf.exists():
        raise HTTPException(status_code=404, detail="Tagged PDF output file not found")

    task_metadata = _parse_json(task.metadata_json)
    allowed_targets = _allowed_font_targets(task_metadata)

    batch_patches: list[dict[str, str]] = []
    seen_pairs: set[tuple[int, int]] = set()
    for target_request in request.targets:
        pair = (target_request.page_number, target_request.operator_index)
        if pair in seen_pairs:
            raise HTTPException(
                status_code=400,
                detail="Duplicate page/operator pair provided in batch request",
            )
        seen_pairs.add(pair)

        matched_target = allowed_targets.get(pair)
        if matched_target is None:
            raise HTTPException(
                status_code=400,
                detail="One or more requested page/operator pairs are not flagged font targets",
            )
        context_path = str(matched_target.get("context_path") or "").strip()
        if not context_path:
            raise HTTPException(
                status_code=400,
                detail="A flagged target did not include a usable veraPDF context path",
            )
        batch_patches.append({
            "context_path": context_path,
            "actual_text": target_request.actual_text,
        })

    patched_output = get_output_path(
        job_id,
        f"accessible_manual_actualtext_batch_{task_id}_{job.original_filename}",
    )

    try:
        apply_actualtext_batch_to_contexts(
            input_pdf=output_pdf,
            output_pdf=patched_output,
            patches=batch_patches,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to apply ActualText batch remediation: {exc}") from exc

    job.status = "processing"
    await db.commit()

    settings = get_settings()
    preserved_task_metadata = _manual_font_remediation_preservation(
        task=task,
        task_metadata=task_metadata,
        actualtext_attempts=[
            {
                "page_number": target_request.page_number,
                "operator_index": target_request.operator_index,
                "actual_text": target_request.actual_text,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "mode": "batch",
            }
            for target_request in request.targets
        ],
    )
    try:
        await _refresh_post_tagging_reports(
            job=job,
            db=db,
            settings=settings,
            output_pdf=patched_output,
            preserved_task_metadata=preserved_task_metadata,
            manual_claims={"manual_post_tagging_actualtext": True},
        )
    except Exception as exc:
        job.status = "needs_manual_review"
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"ActualText batch remediation was applied, but validation refresh failed: {exc}",
        ) from exc

    return {
        "status": "ok",
        "message": "Applied ActualText batch remediation and refreshed validation.",
    }


@router.post("/review-tasks/{task_id}/font-map", status_code=200)
async def apply_font_unicode_override(
    job_id: str,
    task_id: int,
    request: FontUnicodeOverrideRequest,
    db: AsyncSession = Depends(get_db),
):
    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.output_path:
        raise HTTPException(status_code=400, detail="Job does not have a tagged PDF output yet")
    if not job.validation_json:
        raise HTTPException(status_code=400, detail="Validation report is not available for this job")

    task_result = await db.execute(
        select(ReviewTask).where(
            ReviewTask.job_id == job_id,
            ReviewTask.id == task_id,
        )
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found")
    if task.task_type != "font_text_fidelity":
        raise HTTPException(status_code=400, detail="Font-map remediation is only supported for font review tasks")

    output_pdf = Path(job.output_path)
    if not output_pdf.exists():
        raise HTTPException(status_code=404, detail="Tagged PDF output file not found")

    task_metadata = _parse_json(task.metadata_json)
    allowed_targets = _allowed_font_targets(task_metadata)
    matched_target = allowed_targets.get((request.page_number, request.operator_index))
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

    patched_output = get_output_path(
        job_id,
        f"accessible_manual_fontmap_{task_id}_{job.original_filename}",
    )

    try:
        applied = apply_unicode_override_to_context(
            input_pdf=output_pdf,
            output_pdf=patched_output,
            context_path=context_path,
            unicode_text=request.unicode_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to apply font-map remediation: {exc}") from exc

    job.status = "processing"
    await db.commit()

    settings = get_settings()
    preserved_task_metadata = _manual_font_remediation_preservation(
        task=task,
        task_metadata=task_metadata,
        font_mapping_attempts=[
            {
                "page_number": request.page_number,
                "operator_index": request.operator_index,
                "unicode_text": request.unicode_text,
                "font_base_name": applied.get("font_base_name"),
                "font_code_hex": f"{int(applied.get('font_code', 0)):02X}",
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
    try:
        await _refresh_post_tagging_reports(
            job=job,
            db=db,
            settings=settings,
            output_pdf=patched_output,
            preserved_task_metadata=preserved_task_metadata,
            manual_claims={"manual_post_tagging_font_map": True},
        )
    except Exception as exc:
        job.status = "needs_manual_review"
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Font-map remediation was applied, but validation refresh failed: {exc}",
        ) from exc

    return {
        "status": "ok",
        "message": "Applied font-map remediation and refreshed validation.",
    }
