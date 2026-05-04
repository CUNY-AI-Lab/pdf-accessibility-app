from io import BytesIO

import pikepdf
import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import jobs
from app.config import Settings
from app.models import Base, Job, JobStep
from app.services.anonymous_sessions import AnonymousSession, hash_session_token
from app.services.job_state import CLEANUP_INTERRUPTED_ERROR


def _session(token: str) -> AnonymousSession:
    return AnonymousSession(token=token, session_hash=hash_session_token(token))


class _DummyJobManager:
    def __init__(self):
        self.cancelled_job_ids: list[str] = []

    async def submit_job(self, _job_id: str, coro):
        coro.close()
        return None

    async def cancel_job(self, job_id: str):
        self.cancelled_job_ids.append(job_id)
        return True


class _FailingSubmitJobManager(_DummyJobManager):
    def __init__(self, *, fail_on_attempt: int):
        super().__init__()
        self.fail_on_attempt = fail_on_attempt
        self.submit_attempts = 0
        self.submitted_job_ids: list[str] = []

    async def submit_job(self, job_id: str, coro):
        self.submit_attempts += 1
        if self.submit_attempts == self.fail_on_attempt:
            coro.close()
            raise RuntimeError("submit failed")
        self.submitted_job_ids.append(job_id)
        coro.close()
        return None


class _LimitSettings:
    max_files_per_upload = 5
    max_active_jobs_per_session = 3
    max_active_jobs_global = 12


def _allow_pdf_preflight(monkeypatch):
    monkeypatch.setattr(jobs, "preflight_pdf_upload", lambda *_args, **_kwargs: None)


def _write_image_pdf(path, *, image_width: int, image_height: int) -> None:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    image = pdf.make_stream(b"\x00")
    image["/Type"] = pikepdf.Name("/XObject")
    image["/Subtype"] = pikepdf.Name("/Image")
    image["/Width"] = image_width
    image["/Height"] = image_height
    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    page.obj["/Resources"] = pikepdf.Dictionary({
        "/XObject": pikepdf.Dictionary({"/Im0": image})
    })
    pdf.save(path)


@pytest.mark.asyncio
async def test_create_jobs_binds_job_to_current_anonymous_session(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    upload_path = tmp_path / "upload.pdf"
    upload_path.write_bytes(b"%PDF-1.7\n")

    async def _fake_save_upload(_file):
        return "stored.pdf", upload_path, 1234

    monkeypatch.setattr(jobs, "save_upload", _fake_save_upload)
    _allow_pdf_preflight(monkeypatch)
    monkeypatch.setattr(jobs, "get_settings", lambda: object())
    monkeypatch.setattr(jobs, "get_session_maker", lambda: None)
    monkeypatch.setattr(jobs, "get_job_manager", lambda: _DummyJobManager())

    async with session_maker() as db:
        upload = UploadFile(filename="sample.pdf", file=BytesIO(b"%PDF-1.7\n"))

        response = await jobs.create_jobs(
            files=[upload],
            db=db,
            session=_session("session-1-token-value"),
        )

        created_job = (
            await db.execute(select(Job).where(Job.original_filename == "sample.pdf"))
        ).scalar_one()
        assert created_job.owner_session_hash == _session("session-1-token-value").session_hash
        assert response.jobs[0].original_filename == "sample.pdf"

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_jobs_sanitizes_original_filename(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    upload_path = tmp_path / "upload.pdf"
    upload_path.write_bytes(b"%PDF-1.7\n")

    async def _fake_save_upload(_file):
        return "stored.pdf", upload_path, 1234

    monkeypatch.setattr(jobs, "save_upload", _fake_save_upload)
    _allow_pdf_preflight(monkeypatch)
    monkeypatch.setattr(jobs, "get_settings", lambda: object())
    monkeypatch.setattr(jobs, "get_session_maker", lambda: None)
    monkeypatch.setattr(jobs, "get_job_manager", lambda: _DummyJobManager())

    async with session_maker() as db:
        upload = UploadFile(filename="../../nested/secret.pdf", file=BytesIO(b"%PDF-1.7\n"))

        response = await jobs.create_jobs(
            files=[upload],
            db=db,
            session=_session("session-1-token-value"),
        )

        created_job = (await db.execute(select(Job))).scalar_one()
        assert created_job.original_filename == "secret.pdf"
        assert response.jobs[0].original_filename == "secret.pdf"

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_jobs_compensates_if_background_submission_fails(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    upload_paths = [tmp_path / "upload-1.pdf", tmp_path / "upload-2.pdf"]
    for path in upload_paths:
        path.write_bytes(b"%PDF-1.7\n")

    save_calls = 0

    async def _fake_save_upload(_file):
        nonlocal save_calls
        path = upload_paths[save_calls]
        save_calls += 1
        return path.name, path, 1234

    job_manager = _FailingSubmitJobManager(fail_on_attempt=2)

    monkeypatch.setattr(jobs, "save_upload", _fake_save_upload)
    _allow_pdf_preflight(monkeypatch)
    monkeypatch.setattr(jobs, "get_settings", lambda: object())
    monkeypatch.setattr(jobs, "get_session_maker", lambda: None)
    monkeypatch.setattr(jobs, "get_job_manager", lambda: job_manager)

    async with session_maker() as db:
        uploads = [
            UploadFile(filename="first.pdf", file=BytesIO(b"%PDF-1.7\n")),
            UploadFile(filename="second.pdf", file=BytesIO(b"%PDF-1.7\n")),
        ]

        with pytest.raises(HTTPException) as exc_info:
            await jobs.create_jobs(
                files=uploads,
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == "Failed to start accessibility processing. Please upload again."
        assert job_manager.submitted_job_ids and job_manager.cancelled_job_ids == job_manager.submitted_job_ids
        assert (await db.execute(select(Job))).scalars().all() == []
        assert all(not path.exists() for path in upload_paths)

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_jobs_rejects_image_heavy_pdf_before_creating_job(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    upload_path = tmp_path / "huge-scan.pdf"
    _write_image_pdf(upload_path, image_width=10_000, image_height=10_000)

    async def _fake_save_upload(_file):
        return "stored.pdf", upload_path, upload_path.stat().st_size

    class _Settings(_LimitSettings):
        max_upload_image_pixels = 10_000_000
        max_upload_total_image_pixels = 1_000_000_000
        max_upload_page_render_pixels = 75_000_000
        max_upload_image_heavy_pages = 75
        max_upload_pages = 300
        upload_preflight_render_dpi = 300
        upload_image_heavy_page_min_pixels = 4_000_000

    job_manager = _DummyJobManager()
    monkeypatch.setattr(jobs, "save_upload", _fake_save_upload)
    monkeypatch.setattr(jobs, "get_settings", lambda: _Settings())
    monkeypatch.setattr(jobs, "get_session_maker", lambda: None)
    monkeypatch.setattr(jobs, "get_job_manager", lambda: job_manager)

    async with session_maker() as db:
        upload = UploadFile(filename="huge-scan.pdf", file=BytesIO(b"%PDF-1.7\n"))

        with pytest.raises(HTTPException) as exc_info:
            await jobs.create_jobs(
                files=[upload],
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 413
        assert "too large or image-heavy" in exc_info.value.detail
        assert "downsample scan images" in exc_info.value.detail
        assert (await db.execute(select(Job))).scalars().all() == []
        assert not upload_path.exists()

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_and_get_jobs_are_scoped_to_current_anonymous_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_one = _session("session-1-token-value")
    session_two = _session("session-2-token-value")

    async with session_maker() as db:
        db.add_all(
            [
                Job(
                    id="job-1",
                    filename="job-1.pdf",
                    original_filename="job-1.pdf",
                    owner_session_hash=session_one.session_hash,
                    status="complete",
                    input_path="/tmp/job-1.pdf",
                    validation_json='{"compliant": true}',
                ),
                Job(
                    id="job-2",
                    filename="job-2.pdf",
                    original_filename="job-2.pdf",
                    owner_session_hash=session_two.session_hash,
                    status="failed",
                    input_path="/tmp/job-2.pdf",
                ),
            ]
        )
        db.add_all(
            [
                JobStep(job_id="job-1", step_name="validation", status="complete"),
                JobStep(job_id="job-2", step_name="validation", status="complete"),
            ]
        )
        await db.commit()

        list_response = await jobs.list_jobs(status=None, db=db, session=session_one)
        assert list_response.total == 1
        assert [job.id for job in list_response.jobs] == ["job-1"]
        assert list_response.jobs[0].validation_compliant is True
        assert list_response.jobs[0].steps[0].result is None

        owned_job = await jobs.get_job(job_id="job-1", db=db, session=session_one)
        assert owned_job.id == "job-1"
        assert owned_job.validation_compliant is True

        with pytest.raises(HTTPException) as exc_info:
            await jobs.get_job(job_id="job-2", db=db, session=session_one)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Job not found"

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_jobs_does_not_depend_on_validation_step_result_json():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_one = _session("session-1-token-value")

    async with session_maker() as db:
        db.add(
            Job(
                id="job-step-only-validation",
                filename="job.pdf",
                original_filename="job.pdf",
                owner_session_hash=session_one.session_hash,
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

        list_response = await jobs.list_jobs(status=None, db=db, session=session_one)

        assert list_response.total == 1
        assert list_response.jobs[0].id == "job-step-only-validation"
        assert list_response.jobs[0].validation_compliant is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_jobs_rejects_too_many_files(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    class _Settings(_LimitSettings):
        max_files_per_upload = 1

    monkeypatch.setattr(jobs, "get_settings", lambda: _Settings())

    async with session_maker() as db:
        uploads = [
            UploadFile(filename="first.pdf", file=BytesIO(b"%PDF-1.7\n")),
            UploadFile(filename="second.pdf", file=BytesIO(b"%PDF-1.7\n")),
        ]

        with pytest.raises(HTTPException) as exc_info:
            await jobs.create_jobs(
                files=uploads,
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 400
        assert "Limit: 1" in exc_info.value.detail

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_jobs_rejects_when_session_active_job_limit_is_reached(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    session = _session("session-1-token-value")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    class _Settings(_LimitSettings):
        max_active_jobs_per_session = 1

    monkeypatch.setattr(jobs, "get_settings", lambda: _Settings())

    async with session_maker() as db:
        db.add(
            Job(
                id="existing-active-job",
                filename="active.pdf",
                original_filename="active.pdf",
                owner_session_hash=session.session_hash,
                status="processing",
                input_path="/tmp/active.pdf",
            )
        )
        await db.commit()

        upload = UploadFile(filename="next.pdf", file=BytesIO(b"%PDF-1.7\n"))
        with pytest.raises(HTTPException) as exc_info:
            await jobs.create_jobs(files=[upload], db=db, session=session)

        assert exc_info.value.status_code == 429
        assert "too many queued or processing jobs" in exc_info.value.detail

    await engine.dispose()


def test_settings_default_job_ttl_is_12_hours():
    settings = Settings(_env_file=None, llm_base_url="http://localhost:11434/v1")
    assert settings.job_ttl_hours == 12
    assert settings.max_files_per_upload == 5
    assert settings.max_active_jobs_per_session == 3
    assert settings.max_active_jobs_global == 12
    assert settings.max_concurrent_jobs == 2


@pytest.mark.asyncio
async def test_delete_job_cancels_running_job_before_cleanup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    cleanup_calls: list[tuple[str, str | None]] = []
    job_manager = _DummyJobManager()

    def _fake_cleanup(job_id: str, input_path: str | None = None):
        cleanup_calls.append((job_id, input_path))

    monkeypatch.setattr(jobs, "cleanup_job_files", _fake_cleanup)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-delete-1",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="processing",
                input_path="/tmp/sample.pdf",
            )
        )
        await db.commit()

        await jobs.delete_job(
            job_id="job-delete-1",
            db=db,
            session=_session("session-1-token-value"),
            job_manager=job_manager,
        )

        remaining_job = await db.get(Job, "job-delete-1")
        assert remaining_job is None
        assert job_manager.cancelled_job_ids == ["job-delete-1"]
        assert cleanup_calls == [("job-delete-1", "/tmp/sample.pdf")]

    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_job_preserves_row_when_cleanup_fails(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    job_manager = _DummyJobManager()

    def _failing_cleanup(_job_id: str, _input_path: str | None = None):
        raise OSError("disk error")

    monkeypatch.setattr(jobs, "cleanup_job_files", _failing_cleanup)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-delete-2",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="processing",
                input_path="/tmp/sample.pdf",
                output_path="/tmp/output.pdf",
                validation_json='{"compliant": true}',
                fidelity_json='{"passed": true}',
            )
        )
        db.add(JobStep(job_id="job-delete-2", step_name="tagging", status="running"))
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await jobs.delete_job(
                job_id="job-delete-2",
                db=db,
                session=_session("session-1-token-value"),
                job_manager=job_manager,
            )

        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == "Failed to delete job files. Please try again."
        remaining_job = await db.get(Job, "job-delete-2")
        assert remaining_job is not None
        assert remaining_job.status == "failed"
        assert remaining_job.error == CLEANUP_INTERRUPTED_ERROR
        assert remaining_job.output_path is None
        assert remaining_job.validation_json is None
        assert remaining_job.fidelity_json is None
        running_step = (
            await db.execute(
                select(JobStep).where(
                    JobStep.job_id == "job-delete-2",
                    JobStep.step_name == "tagging",
                )
            )
        ).scalar_one()
        assert running_step.status == "failed"
        assert running_step.error == CLEANUP_INTERRUPTED_ERROR
        assert job_manager.cancelled_job_ids == ["job-delete-2"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_job_marks_terminal_job_failed_when_cleanup_fails(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    job_manager = _DummyJobManager()

    def _failing_cleanup(_job_id: str, _input_path: str | None = None):
        raise OSError("disk error")

    monkeypatch.setattr(jobs, "cleanup_job_files", _failing_cleanup)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-delete-3",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="complete",
                input_path="/tmp/sample.pdf",
                output_path="/tmp/output.pdf",
                validation_json='{"compliant": true}',
                fidelity_json='{"passed": true}',
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await jobs.delete_job(
                job_id="job-delete-3",
                db=db,
                session=_session("session-1-token-value"),
                job_manager=job_manager,
            )

        assert exc_info.value.status_code == 500
        remaining_job = await db.get(Job, "job-delete-3")
        assert remaining_job is not None
        assert remaining_job.status == "failed"
        assert remaining_job.error == CLEANUP_INTERRUPTED_ERROR
        assert remaining_job.output_path is None
        assert remaining_job.validation_json is None
        assert remaining_job.fidelity_json is None
        assert job_manager.cancelled_job_ids == ["job-delete-3"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_job_progress_sse_releases_db_session_before_stream(monkeypatch):
    closed = False

    class _ScalarResult:
        def scalar_one_or_none(self):
            return "job-progress-1"

    class _SessionContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            nonlocal closed
            closed = True

        async def execute(self, _query):
            return _ScalarResult()

    class _SessionMaker:
        def __call__(self):
            return _SessionContext()

    class _StreamingJobManager:
        def subscribe(self, _job_id: str):
            return jobs.asyncio.Queue()

        def unsubscribe(self, _job_id: str, _queue):
            return None

    monkeypatch.setattr(jobs, "get_session_maker", lambda: _SessionMaker())

    response = await jobs.job_progress_sse(
        job_id="job-progress-1",
        job_manager=_StreamingJobManager(),
        session=_session("session-1-token-value"),
    )

    assert closed is True
    assert response is not None
