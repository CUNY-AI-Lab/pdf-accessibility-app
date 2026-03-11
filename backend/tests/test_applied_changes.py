import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import review
from app.models import AppliedChange, Base, Job


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
async def test_revise_applied_change_rejects_non_figure_changes():
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
            metadata_json=json.dumps({}),
            undo_payload_json=json.dumps({}),
        )
        db.add(job)
        db.add(change)
        await db.commit()

        with pytest.raises(review.HTTPException) as exc_info:
            await review.revise_applied_change(
                job_id="job-change-2",
                change_id=1,
                request=None,
                db=db,
            )

        await db.refresh(change)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "This change type cannot be revised in the app."
        assert change.review_status == "pending_review"

    await engine.dispose()
