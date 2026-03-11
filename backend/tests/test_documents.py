import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import documents
from app.models import Base, Job, ReviewTask


@pytest.mark.asyncio
async def test_list_review_tasks_returns_current_task_payload():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-1",
                filename="sample.pdf",
                original_filename="sample.pdf",
                status="awaiting_recommendation_review",
                input_path="/tmp/sample.pdf",
            )
        )
        db.add(
            ReviewTask(
                job_id="job-doc-1",
                task_type="table_semantics",
                title="Review complex table semantics",
                detail="Check whether this region is really a table.",
                severity="high",
                blocking=True,
                source="fidelity",
                metadata_json='{"table_review_targets":[{"page":8}]}',
            )
        )
        await db.commit()

        tasks = await documents.list_review_tasks(job_id="job-doc-1", db=db)

        assert len(tasks) == 1
        assert tasks[0].task_type == "table_semantics"
        assert tasks[0].blocking is True
        assert tasks[0].metadata["table_review_targets"][0]["page"] == 8

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_review_tasks_404s_for_missing_job():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        with pytest.raises(HTTPException) as exc_info:
            await documents.list_review_tasks(job_id="missing-job", db=db)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Job not found"

    await engine.dispose()
