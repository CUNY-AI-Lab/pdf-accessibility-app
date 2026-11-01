import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.models import Base, Job, JobStep, ReviewTask
from app.schemas import ValidationReportResponse
from app.services.job_views import job_to_response, visible_review_tasks


@pytest.mark.asyncio
async def test_list_review_tasks_returns_only_user_visible_follow_up_tasks():
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
                owner_session_hash="test-owner",
                status="manual_remediation",
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
        db.add(
            ReviewTask(
                job_id="job-doc-1",
                task_type="annotation_description",
                title="Review non-descriptive link text",
                detail="Review the affected links before release.",
                severity="medium",
                blocking=False,
                source="fidelity",
                metadata_json=(
                    '{"pages_to_check":[3],"poor_links":[{"page":3,"text":"Click here"}]}'
                ),
            )
        )
        await db.commit()

        tasks = visible_review_tasks(
            list(
                (
                    await db.execute(
                        select(ReviewTask)
                        .where(ReviewTask.job_id == "job-doc-1")
                        .order_by(ReviewTask.blocking.desc(), ReviewTask.created_at.asc())
                    )
                ).scalars()
            )
        )

        assert [task.task_type for task in tasks] == [
            "table_semantics",
            "annotation_description",
        ]
        assert tasks[0].blocking is True
        assert tasks[0].metadata["table_review_targets"] == [{"page": 8}]
        assert tasks[1].blocking is False
        assert tasks[1].metadata["pages_to_check"] == [3]

    await engine.dispose()


@pytest.mark.asyncio
async def test_download_report_validates_payload_before_returning():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-5",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash="test-owner",
                status="complete",
                input_path="/tmp/sample.pdf",
                validation_json='{"summary": {"remaining_violations": 0}}',
            )
        )
        await db.commit()

        with pytest.raises(ValidationError):
            job = await db.get(Job, "job-doc-5")
            ValidationReportResponse.model_validate_json(job.validation_json)

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_jobs_does_not_depend_on_validation_step_result_json():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-step-only-validation",
                filename="job.pdf",
                original_filename="job.pdf",
                owner_session_hash="test-owner",
                status="failed",
                input_path="/tmp/job.pdf",
            )
        )
        db.add(
            JobStep(
                job_id="job-step-only-validation",
                step_name="validation",
                status="complete",
                result_json='{"compliant": true}',
            )
        )
        await db.commit()

        job = (await db.execute(select(Job).options(selectinload(Job.steps)))).scalar_one()
        response = job_to_response(job)
        assert response.id == "job-step-only-validation"
        assert response.validation_compliant is None

    await engine.dispose()
