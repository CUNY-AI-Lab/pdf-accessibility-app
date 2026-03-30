from collections.abc import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine = None
_async_session = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, echo=settings.debug)
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _async_session
    if _async_session is None:
        _async_session = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _async_session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    session_maker = get_session_maker()
    async with session_maker() as session:
        yield session


async def init_db():
    from app.models import AppliedChange, Base, ReviewTask

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_schema, ReviewTask.__table__, AppliedChange.__table__)


def _ensure_schema(sync_conn, review_tasks_table, applied_changes_table) -> None:
    inspector = inspect(sync_conn)
    table_names = set(inspector.get_table_names())

    if "jobs" in table_names:
        columns = {column["name"] for column in inspector.get_columns("jobs")}
        if "fidelity_json" not in columns:
            sync_conn.execute(text("ALTER TABLE jobs ADD COLUMN fidelity_json TEXT"))
        if "owner_session_hash" not in columns:
            sync_conn.execute(text("ALTER TABLE jobs ADD COLUMN owner_session_hash TEXT"))
        if "ocr_language" not in columns:
            sync_conn.execute(text("ALTER TABLE jobs ADD COLUMN ocr_language TEXT"))

    if "review_tasks" not in table_names:
        review_tasks_table.create(bind=sync_conn, checkfirst=True)
    if "applied_changes" not in table_names:
        applied_changes_table.create(bind=sync_conn, checkfirst=True)

    # Ensure indexes exist for databases created before index definitions were added
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)"))
    sync_conn.execute(
        text("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at)")
    )
    sync_conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_jobs_owner_session_hash ON jobs (owner_session_hash)"
        )
    )
    sync_conn.execute(
        text("CREATE INDEX IF NOT EXISTS idx_job_steps_job_id ON job_steps (job_id)")
    )
    sync_conn.execute(
        text("CREATE INDEX IF NOT EXISTS idx_alt_texts_job_id ON alt_texts (job_id)")
    )
    sync_conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_review_tasks_job_id ON review_tasks (job_id)"
        )
    )
    sync_conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_applied_changes_job_id ON applied_changes (job_id)"
        )
    )
    sync_conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_applied_changes_review_status ON applied_changes (review_status)"
        )
    )
    # Unique constraints for data integrity
    sync_conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_steps_job_step ON job_steps (job_id, step_name)"
        )
    )
    sync_conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_alt_texts_job_figure ON alt_texts (job_id, figure_index)"
        )
    )
