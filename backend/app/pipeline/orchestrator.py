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
                await _update_step(db, job_id, current_step, "failed", error=str(e))
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

        # ── Step 5: Tagging ──
        await _update_step(db, job_id, "tagging", "running")
        job_manager.emit_progress(job_id, step="tagging", status="running")

        output_path = get_output_path(job_id, f"accessible_{job.original_filename}")

        # Gather approved alt texts
        result = await db.execute(
            select(AltTextEntry).where(
                AltTextEntry.job_id == job_id,
                AltTextEntry.status == "approved",
            )
        )
        approved_alts = [
            {
                "figure_index": a.figure_index,
                "text": a.edited_text or a.generated_text,
            }
            for a in result.scalars().all()
        ]

        tagging_result = await tag_pdf(
            input_path=working_pdf,
            output_path=output_path,
            structure_json=structure_json,
            alt_texts=approved_alts,
        )

        job.output_path = str(tagging_result.output_path)
        await _update_step(db, job_id, "tagging", "complete", result={
            "tags_added": tagging_result.tags_added,
            "lang_set": tagging_result.lang_set,
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

        job.validation_json = json.dumps({
            "compliant": validation.compliant,
            "violations": [
                {
                    "rule_id": v.rule_id,
                    "description": v.description,
                    "severity": v.severity,
                    "location": v.location,
                    "count": v.count,
                }
                for v in validation.violations
            ],
            "summary": {
                "passed": len([v for v in validation.violations if v.severity != "error"]),
                "failed": len([v for v in validation.violations if v.severity == "error"]),
            },
        })

        await _update_step(db, job_id, "validation", "complete", result={
            "compliant": validation.compliant,
            "violations_count": len(validation.violations),
        })
        job_manager.emit_progress(
            job_id, step="validation", status="complete",
            result={"compliant": validation.compliant},
        )

        # Done!
        job.status = "complete"
        await db.commit()
        logger.info(f"Pipeline complete for job {job_id}")

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
