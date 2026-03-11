from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import documents
from app.models import AltTextEntry, Base, Job


class _FakeJobManager:
    def __init__(self) -> None:
        self.submissions: list[str] = []

    async def submit_job(self, job_id: str, coro):
        self.submissions.append(job_id)
        coro.close()
        return None


@pytest.mark.asyncio
async def test_accept_alt_text_recommendation_resumes_after_last_pending(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-alt-1",
            filename="sample.pdf",
            original_filename="sample.pdf",
            status="awaiting_recommendation_review",
            input_path="/tmp/sample.pdf",
        )
        entry = AltTextEntry(
            job_id="job-alt-1",
            figure_index=0,
            image_path="/tmp/figure.png",
            generated_text="Short figure description",
            status="pending_review",
        )
        db.add(job)
        db.add(entry)
        await db.commit()

        fake_manager = _FakeJobManager()

        async def _fake_run_tagging_and_validation(*args, **kwargs):
            return None

        monkeypatch.setattr(documents, "get_settings", lambda: SimpleNamespace())
        monkeypatch.setattr(documents, "get_session_maker", lambda: session_maker)
        monkeypatch.setattr(documents, "get_job_manager", lambda: fake_manager)
        monkeypatch.setattr(documents, "run_tagging_and_validation", _fake_run_tagging_and_validation)

        response = await documents.accept_alt_text_recommendation(
            job_id="job-alt-1",
            figure_index=0,
            db=db,
        )

        await db.refresh(job)
        await db.refresh(entry)

        assert response.status == "accepted"
        assert response.job_status == "processing"
        assert entry.status == "approved"
        assert entry.edited_text == "Short figure description"
        assert job.status == "processing"
        assert fake_manager.submissions == ["job-alt-1"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_accept_alt_text_recommendation_marks_decorative_without_resuming(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-alt-2",
            filename="sample.pdf",
            original_filename="sample.pdf",
            status="awaiting_recommendation_review",
            input_path="/tmp/sample.pdf",
        )
        first = AltTextEntry(
            job_id="job-alt-2",
            figure_index=0,
            image_path="/tmp/figure-0.png",
            edited_text="decorative",
            status="pending_review",
        )
        second = AltTextEntry(
            job_id="job-alt-2",
            figure_index=1,
            image_path="/tmp/figure-1.png",
            generated_text="Another figure",
            status="pending_review",
        )
        db.add(job)
        db.add_all([first, second])
        await db.commit()

        fake_manager = _FakeJobManager()
        monkeypatch.setattr(documents, "get_job_manager", lambda: fake_manager)

        response = await documents.accept_alt_text_recommendation(
            job_id="job-alt-2",
            figure_index=0,
            db=db,
        )

        await db.refresh(job)
        await db.refresh(first)

        assert response.status == "accepted"
        assert response.job_status == "awaiting_recommendation_review"
        assert first.status == "rejected"
        assert first.edited_text == "decorative"
        assert job.status == "awaiting_recommendation_review"
        assert fake_manager.submissions == []

    await engine.dispose()
