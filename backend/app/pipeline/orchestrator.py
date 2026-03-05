"""Pipeline orchestrator: runs all steps in sequence with progress events."""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AltTextEntry, Job, JobStep
from app.pipeline.alt_text import generate_alt_text
from app.pipeline.classify import classify_pdf
from app.pipeline.ocr import run_ocr
from app.pipeline.structure import extract_structure
from app.pipeline.tagger import tag_pdf
from app.pipeline.validator import validate_pdf
from app.services.file_storage import create_job_dir, get_output_path
from app.services.job_manager import JobManager
from app.services.llm_client import LlmClient

logger = logging.getLogger(__name__)


FONT_RULE_FRAGMENT = "-7.21."


def _aggregate_violations(violations) -> dict[str, dict]:
    """Aggregate violations by rule_id while preserving key display fields."""
    aggregated: dict[str, dict] = {}
    for v in violations:
        rule_id = str(getattr(v, "rule_id", "")).strip()
        if not rule_id:
            continue

        count = getattr(v, "count", 1)
        if not isinstance(count, int) or count < 1:
            count = 1

        if rule_id not in aggregated:
            aggregated[rule_id] = {
                "rule_id": rule_id,
                "description": getattr(v, "description", "Unknown violation"),
                "severity": getattr(v, "severity", "error"),
                "location": getattr(v, "location", None),
                "category": getattr(v, "category", None),
                "fix_hint": getattr(v, "fix_hint", None),
                "count": 0,
            }

        entry = aggregated[rule_id]
        entry["count"] += count
        if entry.get("severity") != "error" and getattr(v, "severity", "") == "error":
            entry["severity"] = "error"
        if not entry.get("location") and getattr(v, "location", None):
            entry["location"] = getattr(v, "location")
        if not entry.get("category") and getattr(v, "category", None):
            entry["category"] = getattr(v, "category")
        if not entry.get("fix_hint") and getattr(v, "fix_hint", None):
            entry["fix_hint"] = getattr(v, "fix_hint")
    return aggregated


def _build_validation_changes(
    baseline_violations,
    post_violations,
) -> tuple[list[dict], dict[str, str]]:
    """Build per-rule remediation lifecycle entries."""
    baseline_map = _aggregate_violations(baseline_violations)
    post_map = _aggregate_violations(post_violations)
    all_rule_ids = sorted(set(baseline_map) | set(post_map))

    changes: list[dict] = []
    status_by_rule: dict[str, str] = {}
    for rule_id in all_rule_ids:
        before = baseline_map.get(rule_id)
        after = post_map.get(rule_id)
        remediation_status = "needs_remediation" if after else "auto_remediated"
        status_by_rule[rule_id] = remediation_status
        source = after or before or {}

        changes.append({
            "rule_id": rule_id,
            "description": source.get("description", "Unknown violation"),
            "severity": source.get("severity", "error"),
            "location": source.get("location"),
            "category": source.get("category"),
            "fix_hint": source.get("fix_hint"),
            "baseline_count": before.get("count", 0) if before else 0,
            "post_count": after.get("count", 0) if after else 0,
            "remediation_status": remediation_status,
        })

    return changes, status_by_rule


def _violation_weight(violation) -> int:
    count = getattr(violation, "count", 1)
    if isinstance(count, int) and count > 0:
        return count
    return 1


def _error_count(validation) -> int:
    return sum(_violation_weight(v) for v in validation.violations if v.severity == "error")


def _warning_count(validation) -> int:
    return sum(_violation_weight(v) for v in validation.violations if v.severity != "error")


def _font_only_errors(violations) -> bool:
    errors = [v for v in violations if v.severity == "error"]
    if not errors:
        return False
    return all(FONT_RULE_FRAGMENT in str(v.rule_id) for v in errors)


async def _attempt_font_remediation(
    job_id: str,
    job: Job,
    settings: Settings,
    working_pdf: Path,
    structure_json: dict,
    reviewed_alts: list[dict],
):
    """Run OCR redo + tagging + validation as a targeted font remediation lane."""
    job_dir = create_job_dir(job_id)
    fontfix_ocr_output = job_dir / "fontfix_ocred.pdf"
    ocr_result = await run_ocr(
        input_path=working_pdf,
        output_path=fontfix_ocr_output,
        language=settings.ocr_language,
        mode="redo",
    )
    if not ocr_result.success:
        return {
            "attempted": True,
            "success": False,
            "error": ocr_result.message,
            "ocr_skipped": ocr_result.skipped,
            "ocr_message": ocr_result.message,
        }

    remediation_input = ocr_result.output_path
    remediation_output = get_output_path(job_id, f"accessible_fontfix_{job.original_filename}")
    tagging_result = await tag_pdf(
        input_path=remediation_input,
        output_path=remediation_output,
        structure_json=structure_json,
        alt_texts=reviewed_alts,
        original_filename=job.original_filename or "",
    )
    validation = await validate_pdf(
        pdf_path=tagging_result.output_path,
        verapdf_path=settings.verapdf_path,
        flavour=settings.verapdf_flavour,
    )

    return {
        "attempted": True,
        "success": True,
        "ocr_skipped": ocr_result.skipped,
        "ocr_message": ocr_result.message,
        "input_path": str(remediation_input),
        "output_path": tagging_result.output_path,
        "tagging_result": tagging_result,
        "validation": validation,
    }


async def _update_step(
    db: AsyncSession,
    job_id: str,
    step_name: str,
    status: str,
    result: dict | None = None,
    error: str | None = None,
):
    """Update a job step's status in the database."""
    stmt = select(JobStep).where(
        JobStep.job_id == job_id, JobStep.step_name == step_name
    )
    row = await db.execute(stmt)
    step = row.scalar_one()

    step.status = status
    if status == "running":
        step.started_at = datetime.now(timezone.utc)
    if status in ("complete", "failed", "skipped"):
        step.completed_at = datetime.now(timezone.utc)
    if result:
        step.result_json = json.dumps(result)
    if error:
        step.error = error

    await db.commit()


async def run_pipeline(
    job_id: str,
    db_session_maker,
    settings: Settings,
    job_manager: JobManager,
):
    """Execute the full PDF accessibility pipeline for a job."""
    async with db_session_maker() as db:
        job = await db.get(Job, job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        input_path = Path(job.input_path)
        job_dir = create_job_dir(job_id)
        current_step = None

        try:
            job.status = "processing"
            await db.commit()

            # ── Step 1: Classify ──
            current_step = "classify"
            await _update_step(db, job_id, "classify", "running")
            job_manager.emit_progress(job_id, step="classify", status="running")

            classification = await classify_pdf(input_path)

            job.classification = classification.type
            job.page_count = classification.total_pages
            await _update_step(db, job_id, "classify", "complete", result={
                "type": classification.type,
                "confidence": classification.confidence,
                "pages_with_text": classification.pages_with_text,
                "total_pages": classification.total_pages,
            })
            job_manager.emit_progress(
                job_id, step="classify", status="complete",
                result={"type": classification.type},
            )

            # ── Step 2: OCR (conditional) ──
            current_step = "ocr"
            working_pdf = input_path

            if classification.type in ("scanned", "mixed"):
                await _update_step(db, job_id, "ocr", "running")
                job_manager.emit_progress(job_id, step="ocr", status="running")

                ocr_output = job_dir / "ocred.pdf"
                ocr_result = await run_ocr(input_path, ocr_output, settings.ocr_language)

                if ocr_result.success:
                    working_pdf = ocr_result.output_path
                    await _update_step(db, job_id, "ocr", "complete", result={
                        "skipped": ocr_result.skipped,
                        "message": ocr_result.message,
                    })
                    job_manager.emit_progress(job_id, step="ocr", status="complete")
                else:
                    await _update_step(db, job_id, "ocr", "failed", error=ocr_result.message)
                    job_manager.emit_progress(
                        job_id, step="ocr", status="failed", message=ocr_result.message,
                    )
                    # Continue anyway with original PDF
                    working_pdf = input_path
            else:
                await _update_step(db, job_id, "ocr", "skipped")
                job_manager.emit_progress(job_id, step="ocr", status="skipped")

            # ── Step 3: Structure Extraction ──
            current_step = "structure"
            await _update_step(db, job_id, "structure", "running")
            job_manager.emit_progress(job_id, step="structure", status="running")

            structure = await extract_structure(working_pdf, job_dir)
            if structure.processed_pdf_path:
                working_pdf = structure.processed_pdf_path

            job.structure_json = json.dumps(structure.document_json)
            await _update_step(db, job_id, "structure", "complete", result={
                "page_count": structure.page_count,
                "headings": structure.headings_count,
                "tables": structure.tables_count,
                "figures": structure.figures_count,
            })
            job_manager.emit_progress(
                job_id, step="structure", status="complete",
                result={"figures_found": structure.figures_count},
            )

            # ── Step 4: Alt Text Generation ──
            current_step = "alt_text"
            if structure.figures:
                await _update_step(db, job_id, "alt_text", "running")
                job_manager.emit_progress(job_id, step="alt_text", status="running")

                llm_client = LlmClient(
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                    timeout=settings.llm_timeout,
                )

                try:
                    alt_texts = await generate_alt_text(structure.figures, llm_client)
                finally:
                    await llm_client.close()

                # Save alt text entries to database
                for alt in alt_texts:
                    fig = structure.figures[alt.figure_index]
                    db.add(AltTextEntry(
                        job_id=job_id,
                        figure_index=alt.figure_index,
                        image_path=str(fig.path),
                        generated_text=alt.generated_text,
                        status=alt.status,
                    ))
                await db.commit()

                await _update_step(db, job_id, "alt_text", "complete", result={
                    "count": len(alt_texts),
                })
                job_manager.emit_progress(
                    job_id, step="alt_text", status="complete",
                    result={"count": len(alt_texts)},
                )

                # Pause for review
                job.status = "awaiting_review"
                await db.commit()
                job_manager.emit_progress(job_id, step="review", status="awaiting_review")
                return  # Pipeline resumes after user approval

            else:
                await _update_step(db, job_id, "alt_text", "skipped")
                job_manager.emit_progress(job_id, step="alt_text", status="skipped")

            # No figures = skip review, go straight to tagging
            await run_tagging_and_validation(
                job_id, db, settings, job_manager, working_pdf, structure.document_json
            )

        except Exception as e:
            logger.exception(f"Pipeline failed for job {job_id}")
            await db.rollback()
            # Sanitize error: strip server paths
            user_error = re.sub(r"/\S*", "", str(e)).strip(": ")
            if current_step:
                await _update_step(
                    db,
                    job_id,
                    current_step,
                    "failed",
                    error=user_error or str(e),
                )
            job = await db.get(Job, job_id)
            if job:
                job.status = "failed"
                job.error = user_error or f"Pipeline failed at step: {current_step}"
                await db.commit()
            job_manager.emit_progress(
                job_id, step=current_step or "error", status="failed", message=user_error,
            )


async def run_tagging_and_validation(
    job_id: str,
    db: AsyncSession,
    settings: Settings,
    job_manager: JobManager,
    working_pdf: Path | None = None,
    structure_json: dict | None = None,
):
    """Run steps 5-6 (tagging + validation). Called after review approval."""
    job = await db.get(Job, job_id)
    if not job:
        return

    if working_pdf is None:
        # Check if an OCR'd version exists in the processing directory
        ocred_path = settings.processing_dir / job_id / "ocred.pdf"
        working_pdf = ocred_path if ocred_path.exists() else Path(job.input_path)
    if structure_json is None:
        structure_json = json.loads(job.structure_json) if job.structure_json else {}

    try:
        job.status = "processing"
        await db.commit()

        baseline_validation = await validate_pdf(
            pdf_path=working_pdf,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
        )

        # ── Step 5: Tagging ──
        await _update_step(db, job_id, "tagging", "running")
        job_manager.emit_progress(job_id, step="tagging", status="running")

        output_path = get_output_path(job_id, f"accessible_{job.original_filename}")

        # Gather approved alt texts
        result = await db.execute(
            select(AltTextEntry).where(
                AltTextEntry.job_id == job_id,
                AltTextEntry.status.in_(("approved", "rejected")),
            )
        )
        reviewed_alts = [
            {
                "figure_index": a.figure_index,
                "text": a.edited_text or a.generated_text,
                "status": a.status,
                "decorative": a.status == "rejected",
            }
            for a in result.scalars().all()
        ]

        tagging_result = await tag_pdf(
            input_path=working_pdf,
            output_path=output_path,
            structure_json=structure_json,
            alt_texts=reviewed_alts,
            original_filename=job.original_filename or "",
        )

        job.output_path = str(tagging_result.output_path)
        await _update_step(db, job_id, "tagging", "complete", result={
            "tags_added": tagging_result.tags_added,
            "lang_set": tagging_result.lang_set,
            "struct_elems": tagging_result.struct_elems_created,
            "headings_tagged": tagging_result.headings_tagged,
            "figures_tagged": tagging_result.figures_tagged,
            "decorative_figures_artifacted": tagging_result.decorative_figures_artifacted,
            "tables_tagged": tagging_result.tables_tagged,
            "lists_tagged": tagging_result.lists_tagged,
            "links_tagged": tagging_result.links_tagged,
            "bookmarks_added": tagging_result.bookmarks_added,
            "title_set": tagging_result.title_set,
        })
        job_manager.emit_progress(job_id, step="tagging", status="complete")

        # ── Step 6: Validation ──
        await _update_step(db, job_id, "validation", "running")
        job_manager.emit_progress(job_id, step="validation", status="running")

        validation = await validate_pdf(
            pdf_path=tagging_result.output_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
        )

        selected_tagging_result = tagging_result
        selected_validation = validation
        font_remediation = {
            "attempted": False,
            "eligible": False,
            "applied": False,
            "first_pass_errors": _error_count(validation),
            "first_pass_warnings": _warning_count(validation),
            "second_pass_errors": None,
            "second_pass_warnings": None,
            "error": None,
            "ocr_message": "",
            "ocr_skipped": False,
        }

        if not validation.compliant and _font_only_errors(validation.violations):
            font_remediation["attempted"] = True
            font_remediation["eligible"] = True
            try:
                attempt = await _attempt_font_remediation(
                    job_id=job_id,
                    job=job,
                    settings=settings,
                    working_pdf=working_pdf,
                    structure_json=structure_json,
                    reviewed_alts=reviewed_alts,
                )
                font_remediation["ocr_message"] = str(attempt.get("ocr_message", ""))
                font_remediation["ocr_skipped"] = bool(attempt.get("ocr_skipped", False))
                font_remediation["error"] = attempt.get("error")

                if attempt.get("success"):
                    candidate_validation = attempt["validation"]
                    candidate_errors = _error_count(candidate_validation)
                    candidate_warnings = _warning_count(candidate_validation)
                    font_remediation["second_pass_errors"] = candidate_errors
                    font_remediation["second_pass_warnings"] = candidate_warnings

                    first_errors = font_remediation["first_pass_errors"]
                    first_warnings = font_remediation["first_pass_warnings"]
                    improved = (
                        candidate_validation.compliant
                        or candidate_errors < first_errors
                        or (
                            candidate_errors == first_errors
                            and candidate_warnings < first_warnings
                        )
                    )

                    if improved:
                        selected_validation = candidate_validation
                        selected_tagging_result = attempt["tagging_result"]
                        job.output_path = str(attempt["output_path"])
                        font_remediation["applied"] = True
            except Exception as exc:
                logger.exception(f"Font remediation fallback failed for job {job_id}")
                font_remediation["error"] = str(exc)

        baseline_has_verapdf_report = bool(baseline_validation.raw_report.get("report"))
        baseline_validator_name = (
            "veraPDF"
            if baseline_has_verapdf_report
            else baseline_validation.raw_report.get("validator", "unknown")
        )
        baseline_errors = _error_count(baseline_validation)
        baseline_warnings = _warning_count(baseline_validation)

        has_verapdf_report = bool(selected_validation.raw_report.get("report"))
        validator_name = (
            "veraPDF"
            if has_verapdf_report
            else selected_validation.raw_report.get("validator", "unknown")
        )
        post_errors = _error_count(selected_validation)
        post_warnings = _warning_count(selected_validation)

        changes, status_by_rule = _build_validation_changes(
            baseline_validation.violations,
            selected_validation.violations,
        )
        needs_remediation = len(
            [c for c in changes if c["remediation_status"] == "needs_remediation"]
        )
        auto_remediated = len(
            [c for c in changes if c["remediation_status"] == "auto_remediated"]
        )
        manual_remediated = len(
            [c for c in changes if c["remediation_status"] == "manual_remediated"]
        )

        job.validation_json = json.dumps({
            "compliant": selected_validation.compliant,
            "profile": settings.verapdf_flavour,
            "standard": "PDF/UA",
            "validator": validator_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "baseline": {
                "compliant": baseline_validation.compliant,
                "validator": baseline_validator_name,
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
                "font_remediation": font_remediation,
            },
            "tagging": {
                "headings_tagged": selected_tagging_result.headings_tagged,
                "figures_tagged": selected_tagging_result.figures_tagged,
                "decorative_figures_artifacted": selected_tagging_result.decorative_figures_artifacted,
                "tables_tagged": selected_tagging_result.tables_tagged,
                "lists_tagged": selected_tagging_result.lists_tagged,
                "links_tagged": selected_tagging_result.links_tagged,
                "bookmarks_added": selected_tagging_result.bookmarks_added,
                "title_set": selected_tagging_result.title_set,
                "lang_set": selected_tagging_result.lang_set,
            },
            "claims": {
                "automated_validation_only": True,
                "requires_manual_check_for_reading_experience": True,
            },
        })

        await _update_step(db, job_id, "validation", "complete", result={
            "compliant": selected_validation.compliant,
            "violations_count": len(selected_validation.violations),
            "font_remediation_attempted": bool(font_remediation["attempted"]),
            "font_remediation_applied": bool(font_remediation["applied"]),
        })
        job_manager.emit_progress(
            job_id, step="validation", status="complete",
            result={
                "compliant": selected_validation.compliant,
                "font_remediation_attempted": bool(font_remediation["attempted"]),
                "font_remediation_applied": bool(font_remediation["applied"]),
            },
        )

        # Done!
        final_status = "complete" if selected_validation.compliant else "needs_manual_review"
        job.status = final_status
        await db.commit()
        job_manager.emit_progress(job_id, step="review", status=final_status)
        logger.info(f"Pipeline complete for job {job_id} with status={final_status}")

    except Exception as e:
        logger.exception(f"Tagging/validation failed for job {job_id}")
        await db.rollback()
        user_error = re.sub(r"/\S*", "", str(e)).strip(": ")
        job = await db.get(Job, job_id)
        if job:
            job.status = "failed"
            job.error = user_error or "Tagging/validation failed"
            await db.commit()
        job_manager.emit_progress(
            job_id, step="error", status="failed", message=user_error,
        )
