import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import review
from app.models import AppliedChange, Base, Job, JobStep, ReviewTask
from app.services.anonymous_sessions import AnonymousSession, hash_session_token


def _session(token: str) -> AnonymousSession:
    return AnonymousSession(token=token, session_hash=hash_session_token(token))


class _DummyRestartJobManager:
    def __init__(self, *, running_job_ids: set[str] | None = None):
        self.submitted_job_ids: list[str] = []
        self.running_job_ids = running_job_ids or set()

    def is_running(self, job_id: str) -> bool:
        return job_id in self.running_job_ids

    async def submit_job(self, job_id: str, coro):
        self.submitted_job_ids.append(job_id)
        coro.close()
        return None


class _FailingRestartJobManager(_DummyRestartJobManager):
    async def submit_job(self, job_id: str, coro):
        self.submitted_job_ids.append(job_id)
        coro.close()
        raise RuntimeError("submit failed")


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
            owner_session_hash=_session("session-1-token-value").session_hash,
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

        response = await review.keep_applied_change(
            job_id="job-change-1",
            change_id=1,
            db=db,
            session=_session("session-1-token-value"),
        )

        await db.refresh(change)
        assert response.status == "kept"
        assert response.job_status == "complete"
        assert change.review_status == "kept"

    await engine.dispose()


@pytest.mark.asyncio
async def test_resolving_review_tasks_does_not_promote_manual_job_to_complete():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-review-resolution",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
            status="manual_remediation",
            input_path="/tmp/sample.pdf",
            validation_json='{"compliant": true}',
        )
        task = ReviewTask(
            job_id="job-review-resolution",
            task_type="content_fidelity",
            title="Review content fidelity",
            detail="Review manually.",
            severity="high",
            blocking=True,
            status="pending_review",
        )
        db.add_all([job, task])
        await db.commit()

        response = await review.resolve_review_task(
            job_id="job-review-resolution",
            task_id=1,
            db=db,
            session=_session("session-1-token-value"),
        )

        await db.refresh(job)
        await db.refresh(task)
        assert response.status == "resolved"
        assert response.job_status == "manual_remediation"
        assert job.status == "manual_remediation"
        assert task.status == "resolved"

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_applied_changes_rejects_failed_jobs():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-failed-list",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
            status="failed",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-failed-list",
            change_type="figure_semantics",
            title="Updated figure 1",
            detail="The app revised the figure decision.",
            importance="medium",
            review_status="pending_review",
            reviewable=True,
        )
        db.add_all([job, change])
        await db.commit()

        with pytest.raises(review.HTTPException) as exc_info:
            await review.list_applied_changes(
                job_id="job-change-failed-list",
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 409
        assert (
            exc_info.value.detail
            == "In-app review is only available after the app reaches a complete or manual-remediation output."
        )

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
            owner_session_hash=_session("session-1-token-value").session_hash,
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
                session=_session("session-1-token-value"),
            )

        await db.refresh(change)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "This change type cannot be revised in the app."
        assert change.review_status == "pending_review"

    await engine.dispose()


@pytest.mark.asyncio
async def test_restart_tagging_clears_stale_artifacts_and_rerun_steps(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    job_manager = _DummyRestartJobManager()
    monkeypatch.setattr(review, "get_settings", lambda: object())
    monkeypatch.setattr(review, "get_session_maker", lambda: None)
    monkeypatch.setattr(review, "get_job_manager", lambda: job_manager)

    async with session_maker() as db:
        job = Job(
            id="job-change-3",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
            status="manual_remediation",
            input_path="/tmp/sample.pdf",
            output_path="/tmp/output.pdf",
            validation_json='{"compliant": false}',
            fidelity_json='{"passed": false}',
            error="Old error",
        )
        db.add(job)
        db.add_all(
            [
                JobStep(
                    job_id="job-change-3",
                    step_name="tagging",
                    status="complete",
                    result_json='{"tags_added": 3}',
                ),
                JobStep(
                    job_id="job-change-3",
                    step_name="validation",
                    status="failed",
                    error="Old validation failure",
                    result_json='{"compliant": false}',
                ),
                JobStep(
                    job_id="job-change-3",
                    step_name="fidelity",
                    status="complete",
                    result_json='{"passed": false}',
                ),
            ]
        )
        await db.commit()

        await review._restart_tagging_with_current_state(job=job, db=db)

        await db.refresh(job)
        steps = (
            await db.execute(
                review.select(JobStep).where(JobStep.job_id == "job-change-3")
            )
        ).scalars().all()

        assert job.status == "processing"
        assert job.error is None
        assert job.output_path is None
        assert job.validation_json is None
        assert job.fidelity_json is None
        assert job_manager.submitted_job_ids == ["job-change-3"]

        by_name = {step.step_name: step for step in steps}
        assert by_name["tagging"].status == "pending"
        assert by_name["tagging"].result_json is None
        assert by_name["validation"].status == "pending"
        assert by_name["validation"].error is None
        assert by_name["validation"].result_json is None
        assert by_name["fidelity"].status == "pending"
        assert by_name["fidelity"].result_json is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_restart_tagging_rejects_stale_job_object_after_another_request_claims_rerun(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    job_manager = _DummyRestartJobManager()
    monkeypatch.setattr(review, "get_settings", lambda: object())
    monkeypatch.setattr(review, "get_session_maker", lambda: None)
    monkeypatch.setattr(review, "get_job_manager", lambda: job_manager)

    async with session_maker() as db:
        job = Job(
            id="job-change-stale-1",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
            status="manual_remediation",
            input_path="/tmp/sample.pdf",
            output_path="/tmp/output.pdf",
            validation_json='{"compliant": false}',
            fidelity_json='{"passed": false}',
        )
        db.add(job)
        db.add(JobStep(job_id="job-change-stale-1", step_name="tagging", status="complete"))
        await db.commit()

        stale_job = await review._load_job(
            job_id="job-change-stale-1",
            session_hash=_session("session-1-token-value").session_hash,
            db=db,
        )

    async with session_maker() as db:
        current_job = await db.get(Job, "job-change-stale-1")
        assert current_job is not None
        current_job.status = "processing"
        await db.commit()

    async with session_maker() as db:
        with pytest.raises(review.HTTPException) as exc_info:
            await review._restart_tagging_with_current_state(job=stale_job, db=db)

        assert exc_info.value.status_code == 409
        assert job_manager.submitted_job_ids == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_restart_tagging_restores_previous_output_when_submission_fails(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    job_manager = _FailingRestartJobManager()
    monkeypatch.setattr(review, "get_settings", lambda: object())
    monkeypatch.setattr(review, "get_session_maker", lambda: None)
    monkeypatch.setattr(review, "get_job_manager", lambda: job_manager)

    async with session_maker() as db:
        job = Job(
            id="job-change-restore-1",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
            status="complete",
            input_path="/tmp/sample.pdf",
            output_path="/tmp/accessible.pdf",
            validation_json='{"compliant": true}',
            fidelity_json='{"passed": true}',
        )
        db.add(job)
        db.add_all(
            [
                JobStep(
                    job_id="job-change-restore-1",
                    step_name="tagging",
                    status="complete",
                    result_json='{"tags_added": 3}',
                ),
                JobStep(
                    job_id="job-change-restore-1",
                    step_name="validation",
                    status="complete",
                    result_json='{"compliant": true}',
                ),
                JobStep(
                    job_id="job-change-restore-1",
                    step_name="fidelity",
                    status="complete",
                    result_json='{"passed": true}',
                ),
            ]
        )
        await db.commit()

        with pytest.raises(RuntimeError, match="submit failed"):
            await review._restart_tagging_with_current_state(job=job, db=db)

        await db.refresh(job)
        steps = (
            await db.execute(
                review.select(JobStep).where(JobStep.job_id == "job-change-restore-1")
            )
        ).scalars().all()

        assert job.status == "complete"
        assert job.output_path == "/tmp/accessible.pdf"
        assert job.validation_json == '{"compliant": true}'
        assert job.fidelity_json == '{"passed": true}'
        assert job_manager.submitted_job_ids == ["job-change-restore-1"]

        by_name = {step.step_name: step for step in steps}
        assert by_name["tagging"].status == "complete"
        assert by_name["tagging"].result_json == '{"tags_added": 3}'
        assert by_name["validation"].status == "complete"
        assert by_name["validation"].result_json == '{"compliant": true}'
        assert by_name["fidelity"].status == "complete"
        assert by_name["fidelity"].result_json == '{"passed": true}'

    await engine.dispose()


@pytest.mark.asyncio
async def test_keep_applied_change_rejects_non_pending_review_change():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-4",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
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

        with pytest.raises(review.HTTPException) as exc_info:
            await review.keep_applied_change(
                job_id="job-change-4",
                change_id=1,
                db=db,
                session=_session("session-1-token-value"),
            )

        await db.refresh(change)
        assert exc_info.value.status_code == 409
        assert change.review_status == "kept"

    await engine.dispose()


@pytest.mark.asyncio
async def test_undo_applied_change_rejects_when_job_is_already_processing(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        review,
        "get_job_manager",
        lambda: _DummyRestartJobManager(running_job_ids={"job-change-5"}),
    )

    async with session_maker() as db:
        job = Job(
            id="job-change-5",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
            status="manual_remediation",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-5",
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

        with pytest.raises(review.HTTPException) as exc_info:
            await review.undo_applied_change(
                job_id="job-change-5",
                change_id=1,
                db=db,
                session=_session("session-1-token-value"),
            )

        await db.refresh(change)
        assert exc_info.value.status_code == 409
        assert change.review_status == "pending_review"

    await engine.dispose()


@pytest.mark.asyncio
async def test_revise_applied_change_rejects_when_job_is_already_processing(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        review,
        "get_job_manager",
        lambda: _DummyRestartJobManager(running_job_ids={"job-change-6"}),
    )

    llm_called = False

    async def _unexpected_llm_call(**_kwargs):
        nonlocal llm_called
        llm_called = True
        raise AssertionError("revise should not call the LLM when a rerun is already active")

    monkeypatch.setattr(review, "generate_figure_intelligence", _unexpected_llm_call)

    async with session_maker() as db:
        job = Job(
            id="job-change-6",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
            status="manual_remediation",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-6",
            change_type="figure_semantics",
            title="Updated figure 1",
            detail="The app revised the figure decision.",
            importance="medium",
            review_status="pending_review",
            reviewable=True,
            metadata_json=json.dumps({}),
            undo_payload_json=json.dumps({}),
        )
        db.add_all([job, change])
        await db.commit()

        with pytest.raises(review.HTTPException) as exc_info:
            await review.revise_applied_change(
                job_id="job-change-6",
                change_id=1,
                request=None,
                db=db,
                session=_session("session-1-token-value"),
            )

        await db.refresh(change)
        assert exc_info.value.status_code == 409
        assert change.review_status == "pending_review"
        assert llm_called is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_undo_applied_change_rejects_failed_jobs(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(review, "get_job_manager", lambda: _DummyRestartJobManager())

    async with session_maker() as db:
        job = Job(
            id="job-change-failed-undo",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash=_session("session-1-token-value").session_hash,
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

        with pytest.raises(review.HTTPException) as exc_info:
            await review.undo_applied_change(
                job_id="job-change-failed-undo",
                change_id=1,
                db=db,
                session=_session("session-1-token-value"),
            )

        await db.refresh(change)
        assert exc_info.value.status_code == 409
        assert (
            exc_info.value.detail
            == "In-app review is only available after the app reaches a complete or manual-remediation output."
        )
        assert change.review_status == "pending_review"

    await engine.dispose()
