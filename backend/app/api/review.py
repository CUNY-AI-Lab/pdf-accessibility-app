from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, get_session_maker
from app.models import AltTextEntry, Job
from app.pipeline.orchestrator import run_tagging_and_validation
from app.services.job_manager import get_job_manager

router = APIRouter(prefix="/jobs/{job_id}", tags=["review"])


@router.post("/approve", status_code=200)
async def approve_review(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "awaiting_review":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not awaiting review (current status: {job.status})",
        )

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
