import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AppliedChange, Base, Job
from scripts import corpus_benchmark


def test_benchmark_runner_for_full_profile_returns_workflow_runner():
    runner = corpus_benchmark.benchmark_runner_for_profile(corpus_benchmark.BENCHMARK_PROFILE_FULL)
    assert runner is corpus_benchmark.benchmark_one_workflow


def test_benchmark_runner_for_assistive_core_profile_dispatches(monkeypatch):
    captured = {}

    async def _fake_runner(pdf_path, run_dir, settings, session_maker, job_manager):
        captured["args"] = (pdf_path, run_dir, settings, session_maker, job_manager)
        return "assistive-core"

    class _Settings:
        def model_copy(self, *, update):
            captured["update"] = update
            return f"settings:{update['skip_alt_text_generation']}"

    monkeypatch.setattr(corpus_benchmark, "benchmark_one_workflow", _fake_runner)
    runner = corpus_benchmark.benchmark_runner_for_profile(
        corpus_benchmark.BENCHMARK_PROFILE_ASSISTIVE_CORE
    )

    result = asyncio.run(runner("a.pdf", "out", _Settings(), "db", "jobs"))

    assert result == "assistive-core"
    assert captured["update"] == {"skip_alt_text_generation": True}
    assert captured["args"] == ("a.pdf", "out", "settings:True", "db", "jobs")


def test_benchmark_runner_for_deterministic_profile_dispatches(monkeypatch):
    captured = {}

    async def _fake_runner(pdf_path, run_dir, settings):
        captured["args"] = (pdf_path, run_dir, settings)
        return "deterministic"

    monkeypatch.setattr(corpus_benchmark, "benchmark_one", _fake_runner)
    runner = corpus_benchmark.benchmark_runner_for_profile(
        corpus_benchmark.BENCHMARK_PROFILE_DETERMINISTIC
    )

    result = asyncio.run(runner("a.pdf", "out", "settings", None, None))

    assert result == "deterministic"
    assert captured["args"] == ("a.pdf", "out", "settings")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["complete", "manual_remediation", "processing", "failed"])
async def test_benchmark_finalization_acknowledges_only_finished_reviewable_changes(status):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_maker() as db:
            job = Job(
                id="benchmark",
                filename="sample.pdf",
                original_filename="sample.pdf",
                owner_session_hash="benchmark-session",
                status=status,
                input_path="sample.pdf",
            )
            pending = AppliedChange(
                job_id=job.id,
                change_type="figure_semantics",
                title="Figure",
                detail="Existing description",
                reviewable=True,
            )
            hidden = AppliedChange(
                job_id=job.id,
                change_type="reading_order",
                title="Order",
                detail="Applied ordering",
                reviewable=False,
            )
            db.add_all([job, pending, hidden])
            await db.commit()
        await corpus_benchmark._finalize_review_if_needed(job.id, session_maker)
        # A repeat is a no-op, including after the change becomes kept.
        await corpus_benchmark._finalize_review_if_needed(job.id, session_maker)
        async with session_maker() as db:
            expected = "kept" if status in {"complete", "manual_remediation"} else "pending_review"
            assert (await db.get(AppliedChange, pending.id)).review_status == expected
            assert (await db.get(AppliedChange, hidden.id)).review_status == "pending_review"
            assert (await db.get(Job, job.id)).status == status
    finally:
        await engine.dispose()
