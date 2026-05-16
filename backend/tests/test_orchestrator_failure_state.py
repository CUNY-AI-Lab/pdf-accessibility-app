from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Job, JobStep
from app.pipeline import orchestrator


class _RecordingJobManager:
    def __init__(self):
        self.events: list[dict[str, object]] = []

    def emit_progress(self, job_id: str, **event_data):
        self.events.append({"job_id": job_id, **event_data})


def _pretend_audit(**overrides):
    return {
        "applied": False,
        "applied_count": 0,
        "llm_usage": orchestrator._empty_llm_usage(),
        **overrides,
    }


@pytest.mark.asyncio
async def test_tagging_failure_marks_active_step_failed(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    output_pdf = tmp_path / "accessible_sample.pdf"

    async def _fake_validate_pdf(**_kwargs):
        return SimpleNamespace(compliant=True, violations=[], raw_report={"report": {}})

    async def _fake_tag_pdf(**_kwargs):
        raise RuntimeError("tagger exploded")

    async def _fake_grounded_text(**kwargs):
        return kwargs["structure_json"], _pretend_audit(applied_code_text_count=0)

    async def _fake_table_intelligence(**kwargs):
        return kwargs["structure_json"], _pretend_audit(
            confirmed_count=0,
            set_headers_count=0,
        )

    async def _fake_widget_rationalization(**kwargs):
        return kwargs["working_pdf"], _pretend_audit()

    async def _fake_form_intelligence(**kwargs):
        return kwargs["working_pdf"], _pretend_audit()

    monkeypatch.setattr(orchestrator, "validate_pdf", _fake_validate_pdf)
    monkeypatch.setattr(orchestrator, "tag_pdf", _fake_tag_pdf)
    monkeypatch.setattr(orchestrator, "get_output_path", lambda *_args: output_pdf)
    monkeypatch.setattr(
        orchestrator,
        "_apply_pretag_grounded_text_resolutions",
        _fake_grounded_text,
    )
    monkeypatch.setattr(
        orchestrator,
        "_apply_pretag_table_intelligence",
        _fake_table_intelligence,
    )
    monkeypatch.setattr(
        orchestrator,
        "_apply_pretag_widget_rationalization",
        _fake_widget_rationalization,
    )
    monkeypatch.setattr(
        orchestrator,
        "_apply_pretag_form_intelligence",
        _fake_form_intelligence,
    )
    monkeypatch.setattr(
        orchestrator,
        "docling_pretag_ambiguity_router",
        lambda **_kwargs: {
            "plan": {},
            "table_targets": [],
            "widget_targets": [],
            "form_targets": [],
        },
    )

    job_manager = _RecordingJobManager()
    settings = SimpleNamespace(
        verapdf_path="verapdf",
        verapdf_flavour="ua1",
        subprocess_timeout_validation=30,
    )

    async with session_maker() as db:
        db.add(
            Job(
                id="job-1",
                filename="stored.pdf",
                original_filename="sample.pdf",
                owner_session_hash="owner",
                input_path=str(input_pdf),
                status="processing",
                structure_json="{}",
            )
        )
        for step_name in ("tagging", "validation", "fidelity"):
            db.add(JobStep(job_id="job-1", step_name=step_name))
        await db.commit()

        await orchestrator.run_tagging_and_validation(
            "job-1",
            db,
            settings,
            job_manager,
            working_pdf=input_pdf,
            structure_json={},
        )

        failed_job = await db.get(Job, "job-1")
        tagging_step = (
            await db.execute(
                select(JobStep).where(
                    JobStep.job_id == "job-1",
                    JobStep.step_name == "tagging",
                )
            )
        ).scalar_one()

    assert failed_job is not None
    assert failed_job.status == "failed"
    assert failed_job.output_path is None
    assert failed_job.validation_json is None
    assert tagging_step.status == "failed"
    assert tagging_step.error == "tagger exploded"
    assert job_manager.events[-1] == {
        "job_id": "job-1",
        "step": "tagging",
        "status": "failed",
        "message": "tagger exploded",
    }

    await engine.dispose()
