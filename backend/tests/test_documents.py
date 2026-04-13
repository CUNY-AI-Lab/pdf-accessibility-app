from pathlib import Path

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import documents
from app.config import get_settings
from app.models import Base, Job, ReviewTask
from app.schemas import ValidationReportResponse
from app.services.anonymous_sessions import AnonymousSession, hash_session_token


def _session(token: str) -> AnonymousSession:
    return AnonymousSession(token=token, session_hash=hash_session_token(token))


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
                owner_session_hash=_session("session-1-token-value").session_hash,
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

        tasks = await documents.list_review_tasks(
            job_id="job-doc-1",
            db=db,
            session=_session("session-1-token-value"),
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
async def test_list_review_tasks_404s_for_missing_job():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        with pytest.raises(HTTPException) as exc_info:
            await documents.list_review_tasks(
                job_id="missing-job",
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Job not found"

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_review_tasks_404s_for_job_owned_by_another_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-2",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="complete",
                input_path="/tmp/sample.pdf",
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await documents.list_review_tasks(
                job_id="job-doc-2",
                db=db,
                session=_session("session-2-token-value"),
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Job not found"

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_review_tasks_404s_for_failed_job():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-failed-tasks",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="failed",
                input_path="/tmp/sample.pdf",
            )
        )
        db.add(
            ReviewTask(
                job_id="job-doc-failed-tasks",
                task_type="annotation_description",
                title="Review links",
                detail="Review the affected links before release.",
                severity="medium",
                blocking=False,
                source="fidelity",
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await documents.list_review_tasks(
                job_id="job-doc-failed-tasks",
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Current output is not available for inspection"

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_page_preview_404s_for_failed_job(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    pdf_path = tmp_path / "failed-preview.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")

    render_called = False

    def _unexpected_render(_path: Path, _page_number: int, *, timeout: int) -> bytes:
        nonlocal render_called
        render_called = True
        raise AssertionError("preview rendering should not be called for failed jobs")

    monkeypatch.setattr(documents, "render_page_png_bytes", _unexpected_render)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-failed-preview",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="failed",
                input_path=str(pdf_path),
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await documents.get_page_preview(
                job_id="job-doc-failed-preview",
                page_number=1,
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Current output is not available for inspection"
        assert render_called is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_download_pdf_sets_no_store_headers(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = get_settings()
    pdf_path = settings.output_dir / "job-doc-3" / "accessible.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-3",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="complete",
                input_path="/tmp/sample.pdf",
                output_path=str(pdf_path),
            )
        )
        await db.commit()

        response = await documents.download_pdf(
            job_id="job-doc-3",
            db=db,
            session=_session("session-1-token-value"),
        )

        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["expires"] == "0"
        assert response.headers["vary"] == "Cookie"

    await engine.dispose()


@pytest.mark.asyncio
async def test_download_pdf_404s_for_failed_job_even_if_output_path_exists(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = get_settings()
    pdf_path = settings.output_dir / "job-doc-failed-pdf" / "accessible.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-failed-pdf",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="failed",
                input_path="/tmp/sample.pdf",
                output_path=str(pdf_path),
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await documents.download_pdf(
                job_id="job-doc-failed-pdf",
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Accessible PDF not yet available"

    await engine.dispose()


@pytest.mark.asyncio
async def test_download_report_sets_no_store_headers():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-4",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="complete",
                input_path="/tmp/sample.pdf",
                validation_json='{"compliant": true}',
            )
        )
        await db.commit()

        http_response = Response()
        report = await documents.download_report(
            job_id="job-doc-4",
            response=http_response,
            db=db,
            session=_session("session-1-token-value"),
        )

        assert isinstance(report, ValidationReportResponse)
        assert report.compliant is True
        assert http_response.headers["cache-control"] == "private, no-store"
        assert http_response.headers["pragma"] == "no-cache"
        assert http_response.headers["expires"] == "0"
        assert http_response.headers["vary"] == "Cookie"

    await engine.dispose()


@pytest.mark.asyncio
async def test_download_report_404s_for_failed_job_even_if_validation_exists():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(
            Job(
                id="job-doc-failed-report",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="failed",
                input_path="/tmp/sample.pdf",
                validation_json='{"compliant": true}',
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await documents.download_report(
                job_id="job-doc-failed-report",
                response=Response(),
                db=db,
                session=_session("session-1-token-value"),
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Validation report not yet available"

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
                owner_session_hash=_session("session-1-token-value").session_hash,
                status="complete",
                input_path="/tmp/sample.pdf",
                validation_json='{"summary": {"remaining_violations": 0}}',
            )
        )
        await db.commit()

        with pytest.raises(ValidationError):
            await documents.download_report(
                job_id="job-doc-5",
                response=Response(),
                db=db,
                session=_session("session-1-token-value"),
            )

    await engine.dispose()
