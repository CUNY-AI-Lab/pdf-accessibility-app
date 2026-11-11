"""Job and review-task payloads rendered for the Worker by the runner."""

import json
import logging

from app.models import Job, ReviewTask
from app.pipeline.steps import PIPELINE_STEP_ORDER, PIPELINE_STEPS
from app.schemas import JobResponse, JobStepResponse, ReviewTaskResponse
from app.services.review_surface import is_user_visible_review_task_type

logger = logging.getLogger(__name__)


def _parse_result_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _validation_compliant(job: Job) -> bool | None:
    report = _parse_result_json(job.validation_json)
    if isinstance(report, dict) and isinstance(report.get("compliant"), bool):
        return report["compliant"]

    return None


def job_to_response(job: Job, *, include_step_results: bool = True) -> JobResponse:
    return JobResponse(
        id=job.id,
        filename=job.filename,
        original_filename=job.original_filename,
        status=job.status,
        classification=job.classification,
        ocr_language=job.ocr_language,
        page_count=job.page_count,
        file_size_bytes=job.file_size_bytes,
        error=job.error,
        validation_compliant=_validation_compliant(job),
        created_at=job.created_at,
        updated_at=job.updated_at,
        steps=[
            JobStepResponse(
                step_name=s.step_name,
                status=s.status,
                started_at=s.started_at,
                completed_at=s.completed_at,
                error=s.error,
                result=_parse_result_json(s.result_json) if include_step_results else None,
            )
            for s in sorted(
                job.steps,
                key=lambda s: PIPELINE_STEP_ORDER.get(s.step_name, len(PIPELINE_STEPS)),
            )
        ],
    )


def _safe_json_loads(raw: str) -> dict:
    """Parse JSON, returning an error-indicating dict on decode errors.

    Metadata is supplementary display info on task cards — a corrupt entry
    should not block the entire review task list from loading.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Corrupt metadata_json in review task (BUG — investigate): %.200s", raw)
        return {"_error": "corrupt metadata"}


def visible_review_tasks(tasks: list[ReviewTask]) -> list[ReviewTaskResponse]:
    """The review tasks a person sees, in the order given."""
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
            metadata=_safe_json_loads(task.metadata_json) if task.metadata_json else {},
        )
        for task in tasks
        if is_user_visible_review_task_type(task.task_type)
    ]
