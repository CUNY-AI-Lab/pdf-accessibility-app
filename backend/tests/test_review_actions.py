import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AppliedChange, Base, Job
from app.services import review_actions as review


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
            owner_session_hash="test-owner",
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

        with pytest.raises(review.ReviewRefusal) as exc_info:
            await review.apply_review_action(
                action="revise",
                job=job,
                change_id=1,
                db=db,
            )

        await db.refresh(change)
        assert exc_info.value.code == 400
        assert exc_info.value.detail == "This change type cannot be revised in the app."
        assert change.review_status == "pending_review"

    await engine.dispose()


@pytest.mark.asyncio
async def test_undo_rejects_non_pending_review_change():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-4",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash="test-owner",
            status="complete",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-4",
            change_type="figure_semantics",
            title="Updated figure 1",
            detail="The app revised the figure decision.",
            importance="medium",
            review_status="kept",
            reviewable=True,
        )
        db.add_all([job, change])
        await db.commit()

        with pytest.raises(review.ReviewRefusal) as exc_info:
            await review.apply_review_action(
                action="undo",
                job=job,
                change_id=1,
                db=db,
            )

        await db.refresh(change)
        assert exc_info.value.code == 409
        assert change.review_status == "kept"

    await engine.dispose()


@pytest.mark.asyncio
async def test_undo_applied_change_rejects_failed_jobs():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-failed-undo",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash="test-owner",
            status="failed",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-failed-undo",
            change_type="figure_semantics",
            title="Updated figure 1",
            detail="The app revised the figure decision.",
            importance="medium",
            review_status="pending_review",
            reviewable=True,
            undo_payload_json=json.dumps({"kind": "alt_text_entry", "entry_id": 1}),
        )
        db.add_all([job, change])
        await db.commit()

        with pytest.raises(review.ReviewRefusal) as exc_info:
            await review.apply_review_action(
                action="undo",
                job=job,
                change_id=1,
                db=db,
            )

        await db.refresh(change)
        assert exc_info.value.code == 409
        assert (
            exc_info.value.detail
            == "In-app review is only available after the app reaches a complete or manual-remediation output."
        )
        assert change.review_status == "pending_review"

    await engine.dispose()
