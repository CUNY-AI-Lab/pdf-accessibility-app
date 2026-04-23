import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import database
from app.models import AppliedChange, ReviewTask


@pytest.mark.asyncio
async def test_schema_migration_backfills_owner_session_hash_and_records_versions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE jobs (
                    id VARCHAR PRIMARY KEY,
                    filename VARCHAR NOT NULL,
                    original_filename VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    input_path VARCHAR NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO jobs (id, filename, original_filename, status, input_path)
                VALUES ('legacy-job', 'legacy.pdf', 'legacy.pdf', 'complete', '/tmp/legacy.pdf')
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE job_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id VARCHAR NOT NULL,
                    step_name VARCHAR NOT NULL,
                    status VARCHAR NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE alt_texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id VARCHAR NOT NULL,
                    figure_index INTEGER NOT NULL,
                    image_path VARCHAR NOT NULL,
                    status VARCHAR NOT NULL
                )
                """
            )
        )

        await conn.run_sync(
            database._ensure_schema,
            ReviewTask.__table__,
            AppliedChange.__table__,
        )

        def _inspect(sync_conn):
            inspector = inspect(sync_conn)
            jobs_columns = {column["name"] for column in inspector.get_columns("jobs")}
            alt_text_columns = {
                column["name"] for column in inspector.get_columns("alt_texts")
            }
            tables = set(inspector.get_table_names())
            owner_hash = sync_conn.execute(
                text("SELECT owner_session_hash FROM jobs WHERE id = 'legacy-job'")
            ).scalar_one()
            migrations = {
                row[0]
                for row in sync_conn.execute(
                    text("SELECT id FROM schema_migrations")
                ).fetchall()
            }
            return jobs_columns, alt_text_columns, tables, owner_hash, migrations

        jobs_columns, alt_text_columns, tables, owner_hash, migrations = await conn.run_sync(
            _inspect
        )

    assert {"owner_session_hash", "fidelity_json", "ocr_language"} <= jobs_columns
    assert {"generated_text", "edited_text", "created_at", "updated_at"} <= alt_text_columns
    assert {"review_tasks", "applied_changes", "schema_migrations"} <= tables
    assert owner_hash == "legacy:legacy-job"
    assert {
        "20260423_0001_jobs_session_fidelity_columns",
        "20260423_0002_review_tables",
        "20260423_0003_core_table_columns",
        "20260423_0004_indexes_and_uniques",
    } <= migrations

    await engine.dispose()
