import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.health import router as health_router
from app.api.router import api_router
from app.config import get_settings
from app.database import get_session_maker, init_db
from app.models import Job
from app.services.file_storage import cleanup_job_files, ensure_dirs
from app.services.job_manager import get_job_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _periodic_cleanup():
    """Delete expired jobs (files + DB rows) based on job_ttl_hours."""
    settings = get_settings()
    session_maker = get_session_maker()
    batch_size = 100

    while True:
        try:
            await asyncio.sleep(3600)  # Run every hour
            cutoff = datetime.now(UTC) - timedelta(hours=settings.job_ttl_hours)
            total_removed = 0

            while True:
                # Fetch a batch of expired jobs (IDs + input paths only)
                async with session_maker() as session:
                    result = await session.execute(
                        select(Job.id, Job.input_path)
                        .where(Job.created_at < cutoff)
                        .limit(batch_size)
                    )
                    expired = result.all()
                    if not expired:
                        break

                    # Clean files outside the session (sync I/O in thread)
                    for job_id, input_path in expired:
                        try:
                            await asyncio.to_thread(cleanup_job_files, job_id, input_path)
                        except Exception:
                            logger.exception(f"Failed to clean files for job {job_id}")

                    # Delete DB rows
                    for job_id, _ in expired:
                        job = await session.get(Job, job_id)
                        if job:
                            await session.delete(job)
                    await session.commit()
                    total_removed += len(expired)

            if total_removed:
                logger.info(f"Expired job cleanup complete ({total_removed} removed)")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Periodic cleanup failed, will retry next cycle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting PDF Accessibility app...")
    ensure_dirs()
    await init_db()
    logger.info("Database initialized")

    cleanup_task = asyncio.create_task(_periodic_cleanup(), name="periodic-cleanup")

    yield

    logger.info("Shutting down...")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    job_manager = get_job_manager()
    await job_manager.shutdown()
    logger.info("Shutdown complete")


app = FastAPI(
    title="PDF Accessibility",
    description="PDF accessibility remediation pipeline",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(api_router)
