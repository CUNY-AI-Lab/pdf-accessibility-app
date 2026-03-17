from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.models import Base, Job, JobStep
from app.services.job_state import CLEANUP_INTERRUPTED_ERROR, RESTART_INTERRUPTED_ERROR


class _DummyJobManager:
    def __init__(self):
        self.cancelled_job_ids: list[str] = []

    def is_running(self, _job_id: str) -> bool:
        return False

    async def cancel_job(self, job_id: str):
        self.cancelled_job_ids.append(job_id)
        return True

    async def shutdown(self):
        return None


async def _noop_async():
    return None


def test_create_app_serves_built_frontend(tmp_path, monkeypatch):
    dist_dir = tmp_path / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    (dist_dir / "vite.svg").write_text("<svg></svg>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")

    monkeypatch.setattr(main, "ensure_dirs", lambda: None)
    monkeypatch.setattr(main, "init_db", _noop_async)
    monkeypatch.setattr(main, "_fail_abandoned_jobs_once", _noop_async)
    monkeypatch.setattr(main, "get_job_manager", lambda: _DummyJobManager())

    app = main.create_app(frontend_dist_dir=dist_dir)

    with TestClient(app) as client:
        root_response = client.get("/")
        assert root_response.status_code == 200
        assert "div id='root'" in root_response.text
        assert "anon_session=" in root_response.headers["set-cookie"]
        assert "HttpOnly" in root_response.headers["set-cookie"]

        root_head_response = client.head("/")
        assert root_head_response.status_code == 200

        asset_response = client.get("/assets/app.js")
        assert asset_response.status_code == 200
        assert asset_response.text == "console.log('ok');"

        missing_asset_response = client.get("/assets/missing.js")
        assert missing_asset_response.status_code == 404

        spa_response = client.get("/review/123")
        assert spa_response.status_code == 200
        assert "div id='root'" in spa_response.text

        health_response = client.get("/health")
        assert health_response.status_code == 200


@pytest.mark.asyncio
async def test_cleanup_expired_jobs_once_marks_terminal_jobs_failed_and_continues_batch(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="expired-job-1",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash="session-hash",
                status="complete",
                input_path="/tmp/sample.pdf",
                output_path="/tmp/output.pdf",
                validation_json='{"compliant": true}',
                fidelity_json='{"passed": true}',
                created_at=datetime.now(UTC) - timedelta(hours=24),
            )
        )
        db.add(
            Job(
                id="expired-job-3",
                filename="other.pdf",
                original_filename="other.pdf",
                owner_session_hash="session-hash",
                status="complete",
                input_path="/tmp/other.pdf",
                created_at=datetime.now(UTC) - timedelta(hours=24),
            )
        )
        await db.commit()

    job_manager = _DummyJobManager()

    cleanup_calls: list[str] = []

    def _cleanup(job_id: str, _input_path: str | None = None):
        cleanup_calls.append(job_id)
        if job_id == "expired-job-1":
            raise OSError("permission denied")

    monkeypatch.setattr(main, "get_session_maker", lambda: session_maker)
    monkeypatch.setattr(main, "get_job_manager", lambda: job_manager)
    monkeypatch.setattr(main, "cleanup_job_files", _cleanup)

    removed = await main._cleanup_expired_jobs_once(
        cutoff=datetime.now(UTC) - timedelta(hours=12),
        batch_size=1,
    )

    assert removed == 1
    assert cleanup_calls == ["expired-job-1", "expired-job-3"]

    async with session_maker() as db:
        remaining = await db.get(Job, "expired-job-1")
        assert remaining is not None
        assert remaining.status == "failed"
        assert remaining.error == CLEANUP_INTERRUPTED_ERROR
        assert remaining.output_path is None
        assert remaining.validation_json is None
        assert remaining.fidelity_json is None
        assert await db.get(Job, "expired-job-3") is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_fail_abandoned_jobs_once_marks_active_jobs_failed(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add_all(
            [
                Job(
                    id="queued-job",
                    filename="queued.pdf",
                    original_filename="queued.pdf",
                    owner_session_hash="session-hash",
                    status="queued",
                    input_path="/tmp/queued.pdf",
                    output_path="/tmp/old-output.pdf",
                    validation_json='{"compliant": true}',
                    fidelity_json='{"passed": true}',
                ),
                Job(
                    id="processing-job",
                    filename="processing.pdf",
                    original_filename="processing.pdf",
                    owner_session_hash="session-hash",
                    status="processing",
                    input_path="/tmp/processing.pdf",
                    output_path="/tmp/old-output-2.pdf",
                    validation_json='{"compliant": false}',
                    fidelity_json='{"passed": false}',
                ),
                Job(
                    id="complete-job",
                    filename="complete.pdf",
                    original_filename="complete.pdf",
                    owner_session_hash="session-hash",
                    status="complete",
                    input_path="/tmp/complete.pdf",
                ),
                JobStep(job_id="queued-job", step_name="classify", status="pending"),
                JobStep(job_id="processing-job", step_name="tagging", status="running"),
                JobStep(job_id="processing-job", step_name="validation", status="pending"),
            ]
        )
        await db.commit()

    monkeypatch.setattr(main, "get_session_maker", lambda: session_maker)

    failed_count = await main._fail_abandoned_jobs_once()

    assert failed_count == 2

    async with session_maker() as db:
        queued_job = await db.get(Job, "queued-job")
        processing_job = await db.get(Job, "processing-job")
        complete_job = await db.get(Job, "complete-job")
        running_step = (
            await db.execute(
                select(JobStep).where(
                    JobStep.job_id == "processing-job",
                    JobStep.step_name == "tagging",
                )
            )
        ).scalar_one()
        queued_step = (
            await db.execute(
                select(JobStep).where(
                    JobStep.job_id == "queued-job",
                    JobStep.step_name == "classify",
                )
            )
        ).scalar_one()
        pending_processing_step = (
            await db.execute(
                select(JobStep).where(
                    JobStep.job_id == "processing-job",
                    JobStep.step_name == "validation",
                )
            )
        ).scalar_one()

        assert queued_job is not None
        assert queued_job.status == "failed"
        assert queued_job.error == RESTART_INTERRUPTED_ERROR
        assert queued_job.output_path is None
        assert queued_job.validation_json is None
        assert queued_job.fidelity_json is None
        assert queued_step.status == "failed"
        assert queued_step.error == RESTART_INTERRUPTED_ERROR
        assert queued_step.completed_at is not None

        assert processing_job is not None
        assert processing_job.status == "failed"
        assert processing_job.error == RESTART_INTERRUPTED_ERROR
        assert processing_job.output_path is None
        assert processing_job.validation_json is None
        assert processing_job.fidelity_json is None

        assert running_step.status == "failed"
        assert running_step.error == RESTART_INTERRUPTED_ERROR
        assert running_step.completed_at is not None
        assert pending_processing_step.status == "failed"
        assert pending_processing_step.error == RESTART_INTERRUPTED_ERROR
        assert pending_processing_step.completed_at is not None

        assert complete_job is not None
        assert complete_job.status == "complete"

    await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_expired_jobs_once_marks_active_jobs_failed_when_cleanup_fails(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add_all(
            [
                Job(
                    id="expired-job-2",
                    filename="sample.pdf",
                    original_filename="sample.pdf",
                    owner_session_hash="session-hash",
                    status="processing",
                    input_path="/tmp/sample.pdf",
                    output_path="/tmp/output.pdf",
                    validation_json='{"compliant": true}',
                    fidelity_json='{"passed": true}',
                    created_at=datetime.now(UTC) - timedelta(hours=24),
                    updated_at=datetime.now(UTC) - timedelta(hours=24),
                ),
                JobStep(job_id="expired-job-2", step_name="tagging", status="running"),
            ]
        )
        await db.commit()

    job_manager = _DummyJobManager()
    monkeypatch.setattr(job_manager, "is_running", lambda _job_id: True)

    def _failing_cleanup(_job_id: str, _input_path: str | None = None):
        raise OSError("permission denied")

    monkeypatch.setattr(main, "get_session_maker", lambda: session_maker)
    monkeypatch.setattr(main, "get_job_manager", lambda: job_manager)
    monkeypatch.setattr(main, "cleanup_job_files", _failing_cleanup)

    removed = await main._cleanup_expired_jobs_once(
        cutoff=datetime.now(UTC) - timedelta(hours=12),
        batch_size=100,
    )

    assert removed == 0

    async with session_maker() as db:
        remaining = await db.get(Job, "expired-job-2")
        running_step = (
            await db.execute(
                select(JobStep).where(
                    JobStep.job_id == "expired-job-2",
                    JobStep.step_name == "tagging",
                )
            )
        ).scalar_one()

        assert remaining is not None
        assert remaining.status == "failed"
        assert remaining.error == CLEANUP_INTERRUPTED_ERROR
        assert remaining.output_path is None
        assert remaining.validation_json is None
        assert remaining.fidelity_json is None
        assert running_step.status == "failed"
        assert running_step.error == CLEANUP_INTERRUPTED_ERROR
        assert job_manager.cancelled_job_ids == ["expired-job-2"]

    monkeypatch.setattr(job_manager, "is_running", lambda _job_id: False)
    monkeypatch.setattr(main, "cleanup_job_files", lambda *_args, **_kwargs: None)

    removed_on_retry = await main._cleanup_expired_jobs_once(
        cutoff=datetime.now(UTC) - timedelta(hours=12),
        batch_size=100,
    )

    assert removed_on_retry == 1

    async with session_maker() as db:
        assert await db.get(Job, "expired-job-2") is None

    await engine.dispose()
