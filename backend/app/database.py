from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine = None
_async_session = None

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"


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


def _create_schema_migrations_table(sync_conn) -> None:
    sync_conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
    )


def _applied_migration_ids(sync_conn) -> set[str]:
    result = sync_conn.execute(text(f"SELECT id FROM {SCHEMA_MIGRATIONS_TABLE}"))
    return {str(row[0]) for row in result.fetchall()}


def _record_migration(sync_conn, migration_id: str) -> None:
    sync_conn.execute(
        text(
            f"""
            INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (id, applied_at)
            VALUES (:id, :applied_at)
            """
        ),
        {
            "id": migration_id,
            "applied_at": datetime.now(UTC).isoformat(),
        },
    )


def _run_migrations(sync_conn, migrations: list[tuple[str, Callable[[], None]]]) -> None:
    _create_schema_migrations_table(sync_conn)
    applied = _applied_migration_ids(sync_conn)
    for migration_id, migrate in migrations:
        if migration_id in applied:
            continue
        migrate()
        _record_migration(sync_conn, migration_id)


def _ensure_schema(sync_conn, review_tasks_table, applied_changes_table) -> None:
    inspector = inspect(sync_conn)

    def _refresh_table_names() -> set[str]:
        inspector.info_cache.clear()
        return set(inspector.get_table_names())

    def _ensure_job_columns() -> None:
        table_names = _refresh_table_names()
        if "jobs" not in table_names:
            return
        _ensure_column("jobs", "classification", "classification VARCHAR")
        _ensure_column("jobs", "output_path", "output_path VARCHAR")
        _ensure_column("jobs", "structure_json", "structure_json TEXT")
        _ensure_column("jobs", "validation_json", "validation_json TEXT")
        _ensure_column("jobs", "fidelity_json", "fidelity_json TEXT")
        _ensure_column("jobs", "error", "error TEXT")
        _ensure_column("jobs", "page_count", "page_count INTEGER")
        _ensure_column("jobs", "file_size_bytes", "file_size_bytes INTEGER")
        _ensure_column("jobs", "owner_session_hash", "owner_session_hash TEXT")
        _ensure_column("jobs", "ocr_language", "ocr_language TEXT")
        _ensure_column("jobs", "created_at", "created_at DATETIME")
        _ensure_column("jobs", "updated_at", "updated_at DATETIME")
        sync_conn.execute(
            text(
                """
                UPDATE jobs
                SET owner_session_hash = 'legacy:' || id
                WHERE owner_session_hash IS NULL OR owner_session_hash = ''
                """
            )
        )

    def _ensure_review_tables() -> None:
        table_names = _refresh_table_names()
        if "review_tasks" not in table_names:
            review_tasks_table.create(bind=sync_conn, checkfirst=True)
        if "applied_changes" not in table_names:
            applied_changes_table.create(bind=sync_conn, checkfirst=True)

    def _ensure_column(table_name: str, column_name: str, column_sql: str) -> None:
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
            inspector.info_cache.clear()

    def _ensure_core_table_columns() -> None:
        table_names = _refresh_table_names()
        if "job_steps" in table_names:
            _ensure_column("job_steps", "started_at", "started_at DATETIME")
            _ensure_column("job_steps", "completed_at", "completed_at DATETIME")
            _ensure_column("job_steps", "result_json", "result_json TEXT")
            _ensure_column("job_steps", "error", "error TEXT")

        if "alt_texts" in table_names:
            _ensure_column("alt_texts", "generated_text", "generated_text TEXT")
            _ensure_column("alt_texts", "edited_text", "edited_text TEXT")
            _ensure_column(
                "alt_texts",
                "status",
                "status VARCHAR NOT NULL DEFAULT 'pending_review'",
            )
            _ensure_column("alt_texts", "created_at", "created_at DATETIME")
            _ensure_column("alt_texts", "updated_at", "updated_at DATETIME")

        if "review_tasks" in table_names:
            _ensure_column(
                "review_tasks",
                "task_type",
                "task_type VARCHAR NOT NULL DEFAULT 'review_task'",
            )
            _ensure_column(
                "review_tasks",
                "title",
                "title VARCHAR NOT NULL DEFAULT 'Review task'",
            )
            _ensure_column("review_tasks", "detail", "detail TEXT NOT NULL DEFAULT ''")
            _ensure_column(
                "review_tasks",
                "severity",
                "severity VARCHAR NOT NULL DEFAULT 'medium'",
            )
            _ensure_column(
                "review_tasks",
                "blocking",
                "blocking BOOLEAN NOT NULL DEFAULT 1",
            )
            _ensure_column(
                "review_tasks",
                "status",
                "status VARCHAR NOT NULL DEFAULT 'pending_review'",
            )
            _ensure_column(
                "review_tasks",
                "source",
                "source VARCHAR NOT NULL DEFAULT 'fidelity'",
            )
            _ensure_column("review_tasks", "metadata_json", "metadata_json TEXT")
            _ensure_column("review_tasks", "created_at", "created_at DATETIME")
            _ensure_column("review_tasks", "updated_at", "updated_at DATETIME")

        if "applied_changes" in table_names:
            _ensure_column(
                "applied_changes",
                "change_type",
                "change_type VARCHAR NOT NULL DEFAULT 'review_change'",
            )
            _ensure_column(
                "applied_changes",
                "title",
                "title VARCHAR NOT NULL DEFAULT 'Applied change'",
            )
            _ensure_column("applied_changes", "detail", "detail TEXT NOT NULL DEFAULT ''")
            _ensure_column(
                "applied_changes",
                "importance",
                "importance VARCHAR NOT NULL DEFAULT 'medium'",
            )
            _ensure_column(
                "applied_changes",
                "review_status",
                "review_status VARCHAR NOT NULL DEFAULT 'pending_review'",
            )
            _ensure_column(
                "applied_changes",
                "reviewable",
                "reviewable BOOLEAN NOT NULL DEFAULT 1",
            )
            _ensure_column("applied_changes", "metadata_json", "metadata_json TEXT")
            _ensure_column("applied_changes", "before_json", "before_json TEXT")
            _ensure_column("applied_changes", "after_json", "after_json TEXT")
            _ensure_column("applied_changes", "undo_payload_json", "undo_payload_json TEXT")
            _ensure_column("applied_changes", "created_at", "created_at DATETIME")
            _ensure_column("applied_changes", "updated_at", "updated_at DATETIME")

    def _ensure_indexes_and_unique_constraints() -> None:
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

    _run_migrations(
        sync_conn,
        [
            ("20260423_0001_jobs_session_fidelity_columns", _ensure_job_columns),
            ("20260423_0002_review_tables", _ensure_review_tables),
            ("20260423_0003_core_table_columns", _ensure_core_table_columns),
            ("20260423_0004_indexes_and_uniques", _ensure_indexes_and_unique_constraints),
        ],
    )
    _verify_required_schema(sync_conn)


def _verify_required_schema(sync_conn) -> None:
    inspector = inspect(sync_conn)
    table_names = set(inspector.get_table_names())
    required_tables = {"jobs", "job_steps", "alt_texts", "review_tasks", "applied_changes"}
    missing_tables = sorted(required_tables - table_names)
    if missing_tables:
        raise RuntimeError(f"Database schema is missing tables: {', '.join(missing_tables)}")

    jobs_columns = {column["name"] for column in inspector.get_columns("jobs")}
    required_job_columns = {"owner_session_hash", "fidelity_json", "ocr_language"}
    missing_job_columns = sorted(required_job_columns - jobs_columns)
    if missing_job_columns:
        raise RuntimeError(
            f"Database schema is missing jobs columns: {', '.join(missing_job_columns)}"
        )

    result = sync_conn.execute(
        text(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE owner_session_hash IS NULL OR owner_session_hash = ''
            """
        )
    )
    if int(result.scalar_one() or 0) > 0:
        raise RuntimeError("Database schema migration left jobs without owner_session_hash")
