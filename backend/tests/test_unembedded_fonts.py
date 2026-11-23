"""A font the app cannot embed is named, with where to fix it (#87).

78 East Bay Punk zines stamped every page in Avenir: TrueType, no FontFile2,
MacRomanEncoding with Differences. The runner has no Avenir program and no
approved substitute, so the job was held with only veraPDF rule numbers.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AltTextEntry, Base, Job, JobStep, ReviewTask
from app.pipeline import orchestrator
from app.pipeline.fonts.unembedded import unembedded_fonts
from app.pipeline.validator import ValidationResult, Violation
from app.services.html_report import render_html_report

NOT_EMBEDDED = "ISO 14289-1:2014-7.21.4.1-1"
STAMP = b"(EBP-DA | www.eastbaypunkda.com) Tj"
AVENIR = {
    "base_font": "Avenir",
    "subtype": "TrueType",
    "encoding": "MacRomanEncoding",
    "differences": True,
}


def _font(pdf, name: str, *, embedded: bool = False) -> pikepdf.Object:
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name(f"/{name}"), Flags=32,
        FontBBox=[-167, -288, 1000, 932], Ascent=1000, Descent=-366, CapHeight=708,
        ItalicAngle=0, StemV=80,
    )
    if embedded:
        descriptor.FontFile2 = pdf.make_stream(b"program")
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name(f"/{name}"),
        FirstChar=32, LastChar=126, Widths=[556] * 95,
        FontDescriptor=pdf.make_indirect(descriptor),
        Encoding=pikepdf.Dictionary(
            Type=pikepdf.Name.Encoding, BaseEncoding=pikepdf.Name.MacRomanEncoding,
            Differences=[39, pikepdf.Name("/quotesingle")],
        ) if name == "Avenir" else pikepdf.Name.WinAnsiEncoding,
    ))


def _stamped_pdf(path: Path) -> Path:
    """Page 1 shows the stamp directly; pages 2 and 3 draw one shared Form that
    shows it. Page 2 also has OCR-style text (mode 3) in an unembedded font,
    which 7.21.4.1 exempts, visible text in an embedded font, and a Type 3
    font, which has no program to embed."""
    with pikepdf.new() as pdf:
        avenir = _font(pdf, "Avenir")
        hidden = _font(pdf, "TimesNewRomanPSMT")
        embedded = _font(pdf, "Embedded", embedded=True)
        type3 = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type3, FontBBox=[0, 0, 1, 1],
            FontMatrix=[0.001, 0, 0, 0.001, 0, 0], CharProcs=pikepdf.Dictionary(),
            Encoding=pikepdf.Dictionary(Differences=[65, pikepdf.Name("/A")]),
            FirstChar=65, LastChar=65, Widths=[500],
        ))
        stamp = pdf.make_stream(b"BT /Av 8 Tf 20 8 Td " + STAMP + b" ET")
        stamp.Type, stamp.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
        stamp.BBox = [0, 0, 396, 30]
        stamp.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(Av=avenir))
        contents = [
            b"BT /Av 8 Tf 20 8 Td " + STAMP + b" ET",
            # Drawn as on page 3, in the same state: walked again for this page.
            b"q /Stamp Do Q BT 3 Tr /Hid 10 Tf 40 300 Td (scanned words) Tj ET "
            b"BT 0 Tr /Emb 10 Tf 40 200 Td (body) Tj /T3 10 Tf (A) Tj ET",
            b"q /Stamp Do Q",
        ]
        for content in contents:
            page = pdf.add_blank_page(page_size=(396, 612))
            page.Resources = pikepdf.Dictionary(
                Font=pikepdf.Dictionary(Av=avenir, Hid=hidden, Emb=embedded, T3=type3),
                XObject=pikepdf.Dictionary(Stamp=stamp),
            )
            page.Contents = pdf.make_stream(content)
        pdf.save(path)
    return path


def test_names_each_unembedded_font_with_its_encoding_and_pages(tmp_path):
    assert unembedded_fonts(_stamped_pdf(tmp_path / "zine.pdf")) == [{**AVENIR, "pages": [1, 2, 3]}]


def test_a_font_used_only_in_annotation_appearances_is_named_on_that_page(tmp_path):
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        page = pdf.add_blank_page()
        appearance = pdf.make_stream(b"BT /Av 8 Tf 2 2 Td " + STAMP + b" ET")
        appearance.Type, appearance.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
        appearance.BBox = [0, 0, 200, 20]
        appearance.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(Av=_font(pdf, "Avenir")))
        page.Annots = pdf.make_indirect([pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.FreeText, Rect=[0, 0, 200, 20],
            AP=pikepdf.Dictionary(N=appearance),
        ))])
        pdf.save(tmp_path / "annotated.pdf")
    assert unembedded_fonts(tmp_path / "annotated.pdf") == [{**AVENIR, "pages": [2]}]


async def _process(tmp_path: Path, monkeypatch) -> tuple[Job, list[ReviewTask], dict]:
    """Tagging, validation and fidelity with the real tagger; veraPDF reports
    7.21.4.1-1 and the embed lane finds no program, as on the runner."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    source = _stamped_pdf(tmp_path / "zine.pdf")

    async def held(**_kwargs):
        return ValidationResult(
            compliant=False,
            violations=[Violation(
                rule_id=NOT_EMBEDDED, description="Font programs shall be embedded",
                severity="error", count=3, category="fonts",
            )],
            raw_report={"report": {"jobs": []}},
        )

    async def no_program(**kwargs):
        return {"lane": kwargs["lane"], "attempted": True, "success": False,
                "error": "No embeddable local fonts were found"}

    async def same_structure(**kwargs):
        return kwargs["structure_json"], {"applied": False, "llm_usage": orchestrator._empty_llm_usage()}

    async def same_pdf(**kwargs):
        return kwargs["working_pdf"], {"applied": False, "llm_usage": orchestrator._empty_llm_usage()}

    async def no_structure_changes(*, review_tasks, structure_json, **_kwargs):
        return review_tasks, structure_json, []

    monkeypatch.setattr(orchestrator, "validate_pdf", held)
    monkeypatch.setattr(orchestrator, "_attempt_font_lane", no_program)
    monkeypatch.setattr(orchestrator, "get_output_path", lambda _job_id, name: tmp_path / name)
    monkeypatch.setattr(orchestrator, "_apply_pretag_grounded_text_resolutions", same_structure)
    monkeypatch.setattr(orchestrator, "_apply_pretag_table_intelligence", same_structure)
    monkeypatch.setattr(orchestrator, "_apply_pretag_widget_rationalization", same_pdf)
    monkeypatch.setattr(orchestrator, "_apply_pretag_form_intelligence", same_pdf)
    monkeypatch.setattr(orchestrator, "auto_apply_structure_tasks", no_structure_changes)
    monkeypatch.setattr(
        orchestrator,
        "docling_pretag_ambiguity_router",
        lambda **_kwargs: {"plan": {}, "table_targets": [], "widget_targets": [], "form_targets": []},
    )
    settings = SimpleNamespace(
        verapdf_path="verapdf", verapdf_flavour="ua1", subprocess_timeout_validation=30,
        auto_apply_llm_font_map=False,
    )
    structure = {"title": "Zine", "elements": [
        {"type": "paragraph", "text": "EBP-DA | www.eastbaypunkda.com", "page": page}
        for page in range(3)
    ]}

    async with session_maker() as db:
        db.add(Job(
            id="job-1", filename="stored.pdf", original_filename="zine.pdf",
            owner_session_hash="owner", input_path=str(source), status="processing",
            classification="digital", structure_json=json.dumps(structure),
        ))
        for step_name in ("tagging", "validation", "fidelity"):
            db.add(JobStep(job_id="job-1", step_name=step_name))
        await db.commit()
        await orchestrator.run_tagging_and_validation(
            "job-1", db, settings, SimpleNamespace(emit_progress=lambda *_a, **_k: None),
            working_pdf=source, structure_json=structure,
        )
        job = await db.get(Job, "job-1")
        tasks = list((await db.scalars(select(ReviewTask).where(ReviewTask.job_id == "job-1"))).all())
        step = await db.scalar(
            select(JobStep).where(JobStep.job_id == "job-1", JobStep.step_name == "validation")
        )
        alt_texts = list((await db.scalars(select(AltTextEntry))).all())
        report = render_html_report(job, json.loads(job.validation_json), alt_texts, tasks)
    await engine.dispose()
    return job, tasks, {"step": json.loads(step.result_json), "report": report}


@pytest.mark.asyncio
async def test_held_document_names_the_font_to_embed_in_the_source(tmp_path, monkeypatch):
    job, tasks, seen = await _process(tmp_path, monkeypatch)
    named = [{**AVENIR, "pages": [1, 2, 3]}]

    assert job.status == "manual_remediation"
    assert seen["step"]["font_remediation_applied"] is False
    assert seen["step"]["unembedded_fonts"] == named
    assert json.loads(job.validation_json)["unembedded_fonts"] == named
    [task] = [task for task in tasks if task.task_type == "font_text_fidelity" and task.blocking]
    metadata = json.loads(task.metadata_json)
    assert metadata["unembedded_fonts"] == named
    assert metadata["pages_to_check"] == [1, 2, 3]
    assert NOT_EMBEDDED in metadata["rules"]
    # The person reads the font's name, not only a rule number.
    assert "Avenir" in task.detail
    assert "Avenir" in seen["report"]
    assert "TimesNewRomanPSMT" not in task.detail
