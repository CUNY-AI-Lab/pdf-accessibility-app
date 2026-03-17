from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStep

ACTIVE_JOB_STATUSES = frozenset({"queued", "processing"})
RESTART_INTERRUPTED_ERROR = (
    "Processing was interrupted by an app restart. Please upload the PDF again."
)
CLEANUP_INTERRUPTED_ERROR = (
    "Job cleanup could not finish. The job was not deleted."
)
RERUN_STEP_NAMES = ("tagging", "validation", "fidelity")


def clear_terminal_artifacts(job: Job) -> None:
    job.output_path = None
    job.validation_json = None
    job.fidelity_json = None


async def reset_rerun_steps(db: AsyncSession, job_id: str) -> None:
    result = await db.execute(
        select(JobStep).where(
            JobStep.job_id == job_id,
            JobStep.step_name.in_(RERUN_STEP_NAMES),
        )
    )
    for step in result.scalars():
        step.status = "pending"
        step.started_at = None
        step.completed_at = None
        step.result_json = None
        step.error = None


async def mark_job_failed(
    db: AsyncSession,
    job: Job,
    *,
    error: str,
    clear_artifacts: bool = True,
) -> None:
    job.status = "failed"
    job.error = error
    if clear_artifacts:
        clear_terminal_artifacts(job)

    now = datetime.now(UTC)
    result = await db.execute(
        select(JobStep).where(
            JobStep.job_id == job.id,
            JobStep.status.in_(("pending", "running")),
        )
    )
    for step in result.scalars():
        step.status = "failed"
        step.completed_at = now
        step.error = error
