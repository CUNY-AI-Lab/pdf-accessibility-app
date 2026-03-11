import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import review
from app.models import AppliedChange, Base, Job, ReviewTask


@pytest.mark.asyncio
async def test_keep_applied_change_returns_current_job_status():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-1",
            filename="sample.pdf",
            original_filename="sample.pdf",
            status="complete",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-1",
            change_type="reading_order",
            title="Updated reading order",
            detail="The app reordered one page.",
            importance="high",
            review_status="pending_review",
            reviewable=True,
        )
        db.add(job)
        db.add(change)
        await db.commit()

        response = await review.keep_applied_change(job_id="job-change-1", change_id=1, db=db)

        await db.refresh(change)
        assert response.status == "kept"
        assert response.job_status == "complete"
        assert change.review_status == "kept"

    await engine.dispose()


@pytest.mark.asyncio
async def test_suggest_applied_change_reopens_review_task(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-2",
            filename="sample.pdf",
            original_filename="sample.pdf",
            status="complete",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-2",
            change_type="table_semantics",
            title="Updated table interpretation",
            detail="The app changed a table header model.",
            importance="high",
            review_status="pending_review",
            reviewable=True,
            metadata_json=json.dumps(
                {
                    "reopen_task": {
                        "task_type": "table_semantics",
                        "title": "Review complex table semantics",
                        "detail": "Check whether this is really a table.",
                        "severity": "high",
                        "blocking": True,
                        "source": "fidelity",
                        "metadata": {"table_review_targets": []},
                    }
                }
            ),
            undo_payload_json=json.dumps(
                {
                    "kind": "structure_json",
                    "structure_json": {"elements": [{"type": "paragraph", "text": "before"}]},
                }
            ),
        )
        db.add(job)
        db.add(change)
        await db.commit()

        async def _fake_restart_tagging_with_structure_recommendation(*, job, db, structure_payload):
            job.status = "processing"
            await db.commit()

        monkeypatch.setattr(
            review,
            "_restart_tagging_with_structure_recommendation",
            _fake_restart_tagging_with_structure_recommendation,
        )

        response = await review.suggest_applied_change(
            job_id="job-change-2",
            change_id=1,
            request=None,
            db=db,
        )

        await db.refresh(job)
        await db.refresh(change)
        result = await db.execute(
            select(ReviewTask).where(ReviewTask.job_id == "job-change-2")
        )
        tasks = result.scalars().all()

        assert response.status == "reopened"
        assert response.job_status == "awaiting_recommendation_review"
        assert job.status == "awaiting_recommendation_review"
        assert change.review_status == "undone"
        assert len(tasks) == 1
        assert tasks[0].task_type == "table_semantics"
    await engine.dispose()
