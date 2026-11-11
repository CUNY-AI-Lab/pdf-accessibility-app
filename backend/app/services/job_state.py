from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStep
from app.services.job_transitions import set_step_status

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
        set_step_status(step, "pending")
