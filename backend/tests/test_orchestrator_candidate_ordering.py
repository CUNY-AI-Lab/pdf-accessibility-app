"""Candidate content and applied-change records must follow the selected output."""

import json
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AppliedChange, Base, Job, JobStep, ReviewTask
from app.pipeline import orchestrator
from app.pipeline.tagger import TaggingResult
from app.pipeline.validator import ValidationResult, Violation
from tests.test_orchestrator_release_decision import (
    _audit,
    _blocking,
    _fidelity,
    _RecordingJobManager,
)


async def _run_ordering_flow(
    tmp_path, monkeypatch, *, font_lane=False, accept_structure=True, font_map_rejection=None,
):
    input_pdf = tmp_path / "input.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(input_pdf)
    tag_inputs = []
    fidelity_sources = []
    font_map_inputs = []

    async def tag(*, input_path, output_path, structure_json, **_kwargs):
        tag_inputs.append(Path(input_path))
        with pikepdf.open(input_path) as pdf:
            if structure_json.get("auto_applied"):
                pdf.docinfo["/AutoStructureApplied"] = "yes"
            pdf.save(output_path)
        return TaggingResult(output_path=Path(output_path), tags_added=1)

    async def validate(*, pdf_path, **_kwargs):
        name = Path(pdf_path).name
        compliant = name != "accessible_sample.pdf" or not font_lane
        if name == "font_map_candidate.pdf" and font_map_rejection == "noncompliant_candidate":
            compliant = False
        return ValidationResult(
            compliant=compliant,
            violations=[] if compliant else [Violation("ISO 14289-1:2014-7.21.4.1-1", "Embed fonts", "error")],
            raw_report={"report": {"jobs": []}},
        )

    def fidelity(*, output_pdf, comparison_source_pdf, **_kwargs):
        fidelity_sources.append(Path(comparison_source_pdf))
        with pikepdf.open(output_pdf) as pdf:
            auto_applied = str(pdf.docinfo.get("/AutoStructureApplied", "")) == "yes"
            font_mapped = str(pdf.docinfo.get("/FontMapApplied", "")) == "yes"
        tasks = [] if (auto_applied and accept_structure) or font_mapped else [_blocking()]
        if font_map_rejection:
            tasks = [
                {**_blocking("content_fidelity"), "source": source}
                for source in ("validation", "fidelity")
            ]
            if font_mapped:
                tasks = (
                    [*tasks, _blocking("content_fidelity")]
                    if font_map_rejection == "fidelity_regression"
                    else []
                )
        return _fidelity(tasks), tasks

    async def grounded(**kwargs):
        return kwargs["structure_json"], _audit(applied_code_text_count=0)

    async def tables(**kwargs):
        return kwargs["structure_json"], _audit(confirmed_count=0, set_headers_count=0)

    async def forms(**kwargs):
        return kwargs["working_pdf"], _audit()

    async def adjudicate(**kwargs):
        return kwargs["review_tasks"], kwargs["fidelity_report"], {}

    async def lane(**kwargs):
        preprocessed = tmp_path / "lane_preprocessed.pdf"
        with pikepdf.open(kwargs["working_pdf"]) as pdf:
            pdf.docinfo["/FontLaneApplied"] = "yes"
            pdf.save(preprocessed)
        output = tmp_path / "lane_tagged.pdf"
        tagging = await tag(
            input_path=preprocessed, output_path=output, structure_json=kwargs["structure_json"]
        )
        return {
            "success": True,
            "validation": await validate(pdf_path=output),
            "tagging_result": tagging,
            "preprocessed_path": str(preprocessed),
            "requires_retag": True,
            "output_path": output,
            "structure_json": kwargs["structure_json"],
        }

    async def auto_structure(**kwargs):
        if font_map_rejection:
            return kwargs["review_tasks"], kwargs["structure_json"], []
        return (
            kwargs["review_tasks"],
            {**kwargs["structure_json"], "auto_applied": True},
            [{"task_type": "table_semantics", "title": "Set table headers"}],
        )

    async def font_map(*, output_pdf, **_kwargs):
        audit = {"enabled": not font_lane, "attempted": not font_lane, "applied": False}
        if font_lane:
            return audit, None, None, {}
        font_map_inputs.append(Path(output_pdf))
        output = tmp_path / "font_map_candidate.pdf"
        with pikepdf.open(output_pdf) as pdf:
            pdf.docinfo["/FontMapApplied"] = "yes"
            pdf.save(output)
        audit["applied"] = True
        metadata_overrides = {
            ("content_fidelity" if font_map_rejection else "font_text_fidelity", source): {
                "remediation_intelligence": {"summary": "Repair the font mapping"},
                "llm_auto_font_map": {
                    "attempted": True,
                    "applied": True,
                    "reason": "applied",
                    "font_code_hex": "41",
                },
            }
            for source in ("validation", "fidelity")
        }
        return audit, await validate(pdf_path=output), output, metadata_overrides

    monkeypatch.setattr(orchestrator, "tag_pdf", tag)
    monkeypatch.setattr(orchestrator, "validate_pdf", validate)
    monkeypatch.setattr(orchestrator, "assess_fidelity", fidelity)
    monkeypatch.setattr(orchestrator, "get_output_path", lambda _id, name: tmp_path / name)
    monkeypatch.setattr(orchestrator, "_apply_pretag_grounded_text_resolutions", grounded)
    monkeypatch.setattr(orchestrator, "_apply_pretag_table_intelligence", tables)
    monkeypatch.setattr(orchestrator, "_apply_pretag_widget_rationalization", forms)
    monkeypatch.setattr(orchestrator, "_apply_pretag_form_intelligence", forms)
    monkeypatch.setattr(orchestrator, "_adjudicate_grounded_text_candidates", adjudicate)
    monkeypatch.setattr(orchestrator, "docling_pretag_ambiguity_router", lambda **_kw: {})
    monkeypatch.setattr(orchestrator, "_font_remediation_lanes", lambda *_a, **_kw: (["embed_fonts"], []))
    monkeypatch.setattr(orchestrator, "_attempt_font_lane", lane)
    monkeypatch.setattr(orchestrator, "auto_apply_structure_tasks", auto_structure)
    monkeypatch.setattr(orchestrator, "_attempt_auto_llm_font_map", font_map)
    settings = SimpleNamespace(
        verapdf_path="verapdf", verapdf_flavour="ua1", subprocess_timeout_validation=30,
        auto_apply_llm_font_map=not font_lane,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            structure = {"elements": [{"type": "paragraph", "text": "Body", "page": 0}]}
            job = Job(
                id="job-1", filename="stored.pdf", original_filename="sample.pdf",
                owner_session_hash="owner", input_path=str(input_pdf), status="processing",
                classification="digital", structure_json=json.dumps(structure),
            )
            db.add(job)
            for name in ("tagging", "validation", "fidelity"):
                db.add(JobStep(job_id=job.id, step_name=name))
            await db.commit()
            await orchestrator.run_tagging_and_validation(
                job.id, db, settings, _RecordingJobManager(),
                working_pdf=input_pdf, structure_json=structure,
            )
            changes = (await db.execute(select(AppliedChange))).scalars().all()
            review_rows = (await db.execute(select(ReviewTask))).scalars().all()
            return job, changes, tag_inputs, fidelity_sources, font_map_inputs, review_rows
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_structure_retry_preserves_selected_font_lane_output(tmp_path, monkeypatch):
    job, _, tag_inputs, fidelity_sources, _, _ = await _run_ordering_flow(
        tmp_path, monkeypatch, font_lane=True
    )

    assert job.status == "complete"
    assert json.loads(job.validation_json)["remediation"]["font_remediation"]["applied"] is True
    assert tag_inputs[-1] in (tmp_path / "lane_preprocessed.pdf", tmp_path / "lane_tagged.pdf")
    assert fidelity_sources[-1] == tmp_path / "lane_preprocessed.pdf"
    with pikepdf.open(job.output_path) as pdf:
        assert str(pdf.docinfo.get("/FontLaneApplied", "")) == "yes"
        assert str(pdf.docinfo.get("/AutoStructureApplied", "")) == "yes"


@pytest.mark.asyncio
@pytest.mark.parametrize("accept_structure", [True, False])
async def test_font_map_uses_current_candidate_and_records_only_shipped_changes(
    tmp_path, monkeypatch, accept_structure
):
    job, changes, _, _, font_map_inputs, _ = await _run_ordering_flow(
        tmp_path, monkeypatch, accept_structure=accept_structure
    )

    assert job.status == "complete"
    expected_input = "accessible_auto_review_sample.pdf" if accept_structure else "accessible_sample.pdf"
    assert font_map_inputs == [tmp_path / expected_input]
    assert bool(json.loads(job.structure_json).get("auto_applied")) is accept_structure
    assert [change.title for change in changes] == (["Set table headers"] if accept_structure else [])
    with pikepdf.open(job.output_path) as pdf:
        assert (str(pdf.docinfo.get("/AutoStructureApplied", "")) == "yes") is accept_structure
        assert str(pdf.docinfo.get("/FontMapApplied", "")) == "yes"


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection_reason", ["fidelity_regression", "noncompliant_candidate"])
async def test_rejected_font_map_marks_every_task_metadata_as_not_applied(
    tmp_path, monkeypatch, rejection_reason
):
    job, changes, _, _, _, review_rows = await _run_ordering_flow(
        tmp_path, monkeypatch, font_map_rejection=rejection_reason,
    )

    assert job.status == "manual_remediation"
    assert Path(job.output_path).name == "accessible_sample.pdf"
    assert changes == []
    with pikepdf.open(job.output_path) as pdf:
        assert "/FontMapApplied" not in pdf.docinfo
    assert {row.source for row in review_rows} == {"validation", "fidelity"}
    for row in review_rows:
        metadata = json.loads(row.metadata_json)
        font_map = metadata["llm_auto_font_map"]
        assert font_map["applied"] is False
        assert font_map["reason"] == rejection_reason
        assert font_map["attempted"] is True
        assert font_map["font_code_hex"] == "41"
        assert metadata["remediation_intelligence"]["summary"] == "Repair the font mapping"
    audit = json.loads(job.validation_json)["remediation"]["llm_font_map_auto"]
    assert audit["applied"] is False
    assert audit["reason"] == rejection_reason
