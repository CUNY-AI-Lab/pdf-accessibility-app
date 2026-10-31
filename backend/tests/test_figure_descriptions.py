"""A figure the app cannot describe ships without alt text and holds the document.

The alt-text step used to fill such figures with "[Could not determine figure
semantics]" or "[Generation failed: ...]" and approve it, so a screen reader
announced the placeholder and the document could be released.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AltTextEntry, Base, Job, JobStep, ReviewTask
from app.pipeline import alt_text, orchestrator
from app.pipeline.fidelity import assess_fidelity
from app.pipeline.structure import FigureInfo
from app.pipeline.tagger import tag_pdf

PLACEHOLDERS = ["[Could not determine figure semantics]", "[Generation failed: timeout]", ""]


def _figure_pdf(path: Path) -> Path:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    image = pdf.make_stream(bytes([90]))
    image["/Type"] = pikepdf.Name("/XObject")
    image["/Subtype"] = pikepdf.Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": image})})
    page["/Contents"] = pdf.make_stream(b"q 80 0 0 80 60 60 cm /Im0 Do Q\n")
    pdf.save(path)
    return path


FIGURE_STRUCTURE = {
    "title": "Figure test",
    "elements": [
        {
            "type": "figure",
            "figure_index": 0,
            "page": 0,
            "bbox": {"l": 60, "b": 60, "r": 140, "t": 140},
        }
    ],
}


def _figure_alts(pdf_path: Path) -> list[str | None]:
    """The /Alt of every Figure in the tagged PDF (None when absent)."""
    alts: list[str | None] = []
    with pikepdf.open(pdf_path) as pdf:

        def walk(node):
            if isinstance(node, pikepdf.Array):
                for child in node:
                    walk(child)
            elif isinstance(node, pikepdf.Dictionary):
                if node.get("/S") == pikepdf.Name("/Figure"):
                    alt = node.get("/Alt")
                    alts.append(str(alt) if alt is not None else None)
                walk(node.get("/K"))

        walk(pdf.Root["/StructTreeRoot"].get("/K"))
    return alts


# ── The alt-text step ────────────────────────────────────────────────────


def _png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (4, 4), "white").save(path)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["empty_model_text", "image_missing", "unreadable_decision", "no_decision"],
)
async def test_undescribed_figure_gets_no_text_and_waits_for_a_person(tmp_path, monkeypatch, case):
    image = tmp_path / "figure.png"
    if case != "image_missing":
        _png(image)
    figure = FigureInfo(index=0, path=image, caption=None, page=0, bbox=None)

    async def fake_intelligence(*, figures, **_kwargs):
        if case == "empty_model_text":
            return [{"suggested_action": "set_alt_text", "alt_text": "", "confidence": "low"}]
        if case == "unreadable_decision":
            return [None]
        return []

    monkeypatch.setattr(alt_text, "generate_figures_intelligence", fake_intelligence)

    [result] = await alt_text.generate_alt_text([figure], llm_client=None)

    assert result.status == "pending_review"
    assert result.generated_text == ""
    [spec] = alt_text.figure_applied_change_specs(figures=[figure], alt_texts=[result])
    assert spec["after"]["generated_text"] == ""
    assert spec["reviewable"] is True


@pytest.mark.asyncio
async def test_undescribed_figure_with_a_caption_uses_the_caption(tmp_path, monkeypatch):
    figure = FigureInfo(index=0, path=_png(tmp_path / "f.png"), caption="Figure 2. Enrollment by year")

    async def no_text(*, figures, **_kwargs):
        return [{"suggested_action": "set_alt_text", "alt_text": "", "confidence": "low"}]

    monkeypatch.setattr(alt_text, "generate_figures_intelligence", no_text)

    [result] = await alt_text.generate_alt_text([figure], llm_client=None)

    assert result.status == "approved"
    assert result.generated_text == "Figure 2. Enrollment by year"


@pytest.mark.parametrize("auto_approve", [True, False])
@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_placeholder_alt_is_never_approved(placeholder, auto_approve):
    alt = SimpleNamespace(generated_text=placeholder, status="approved")

    status, text = orchestrator._alt_text_entry_state(alt, caption=None, auto_approve=auto_approve)

    assert (status, text) == ("pending_review", "")


@pytest.mark.parametrize(
    ("generated", "caption", "expected"),
    [
        ("[Could not determine figure semantics]", "Map of campus", ("approved", "Map of campus")),
        ("A bar chart of enrollment by year", None, ("approved", "A bar chart of enrollment by year")),
        ("decorative", None, ("rejected", "")),
    ],
)
def test_described_and_decorative_figures_keep_their_decision(generated, caption, expected):
    alt = SimpleNamespace(
        generated_text=generated,
        status="rejected" if generated == "decorative" else "approved",
    )

    assert orchestrator._alt_text_entry_state(alt, caption=caption, auto_approve=True) == expected


# ── The tagger ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", PLACEHOLDERS[:2])
async def test_tag_pdf_never_writes_a_placeholder_as_alt(tmp_path, placeholder):
    output = tmp_path / "tagged.pdf"

    await tag_pdf(
        input_path=_figure_pdf(tmp_path / "in.pdf"),
        output_path=output,
        structure_json=FIGURE_STRUCTURE,
        alt_texts=[{"figure_index": 0, "text": placeholder, "status": "approved"}],
        original_filename="in.pdf",
    )

    assert _figure_alts(output) == [None]


@pytest.mark.asyncio
async def test_tag_pdf_writes_a_real_description_as_alt(tmp_path):
    output = tmp_path / "tagged.pdf"

    await tag_pdf(
        input_path=_figure_pdf(tmp_path / "in.pdf"),
        output_path=output,
        structure_json=FIGURE_STRUCTURE,
        alt_texts=[{"figure_index": 0, "text": "A gray square", "status": "approved"}],
        original_filename="in.pdf",
    )

    assert _figure_alts(output) == ["A gray square"]


# ── Fidelity ─────────────────────────────────────────────────────────────


def _pdf_with_figure_alt(path: Path, alt: str | None) -> Path:
    pdf = pikepdf.open(_figure_pdf(path), allow_overwriting_input=True)
    page = pdf.pages[0]
    figure = pikepdf.Dictionary(
        Type=pikepdf.Name.StructElem,
        S=pikepdf.Name.Figure,
        Pg=page.obj,
        K=0,
    )
    if alt is not None:
        figure.Alt = pikepdf.String(alt)
    figure = pdf.make_indirect(figure)
    root = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot, K=pikepdf.Array([figure])))
    figure.P = root
    pdf.Root.StructTreeRoot = root
    pdf.save(path)
    return path


def _assess(output: Path, alt_entries: list[dict]):
    return assess_fidelity(
        input_pdf=output,
        output_pdf=output,
        structure_json=FIGURE_STRUCTURE,
        alt_entries=alt_entries,
        validation_report={"compliant": True, "violations": [], "summary": {}, "remediation": {}},
        tagging_metrics={"figures_tagged": 1},
        classification="digital",
    )


@pytest.mark.parametrize("alt", ["[Could not determine figure semantics]", None, "  "])
def test_fidelity_holds_a_figure_a_screen_reader_cannot_describe(tmp_path, alt):
    output = _pdf_with_figure_alt(tmp_path / "out.pdf", alt)

    report, tasks = _assess(
        output,
        [{"figure_index": 0, "generated_text": None, "edited_text": None, "status": "pending_review"}],
    )

    assert report["passed"] is False
    [task] = [t for t in tasks if t["task_type"] == "alt_text" and t["blocking"]]
    assert task["metadata"]["pages_to_check"] == [1]
    assert task["metadata"]["figures_awaiting_description"] == [{"figure_index": 0, "page": 1}]


def test_fidelity_passes_a_described_figure(tmp_path):
    output = _pdf_with_figure_alt(tmp_path / "out.pdf", "A gray square")

    report, tasks = _assess(
        output,
        [{"figure_index": 0, "generated_text": None, "edited_text": "A gray square", "status": "approved"}],
    )

    assert not [t for t in tasks if t["task_type"] == "alt_text" and t["blocking"]]
    check = next(c for c in report["checks"] if c["check"] == "figure_descriptions")
    assert check["status"] == "pass"


# ── The release decision ─────────────────────────────────────────────────


async def _tag_and_release(tmp_path: Path, monkeypatch, entry: dict) -> tuple[Job, list, Path]:
    """Run tagging, fidelity and the release decision with the real tagger and
    fidelity; veraPDF and the model-backed pretag steps are stand-ins."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    tmp_path.mkdir()
    input_pdf = _figure_pdf(tmp_path / "input.pdf")

    async def compliant(**_kwargs):
        # veraPDF is not installed in CI; a pass here shows fidelity holds the
        # document on its own.
        return SimpleNamespace(compliant=True, violations=[], raw_report={"report": {"jobs": []}})

    async def same_structure(**kwargs):
        return kwargs["structure_json"], {"applied": False, "llm_usage": orchestrator._empty_llm_usage()}

    async def same_pdf(**kwargs):
        return kwargs["working_pdf"], {"applied": False, "llm_usage": orchestrator._empty_llm_usage()}

    monkeypatch.setattr(orchestrator, "validate_pdf", compliant)
    monkeypatch.setattr(orchestrator, "get_output_path", lambda _job_id, name: tmp_path / name)
    monkeypatch.setattr(orchestrator, "_apply_pretag_grounded_text_resolutions", same_structure)
    monkeypatch.setattr(orchestrator, "_apply_pretag_table_intelligence", same_structure)
    monkeypatch.setattr(orchestrator, "_apply_pretag_widget_rationalization", same_pdf)
    monkeypatch.setattr(orchestrator, "_apply_pretag_form_intelligence", same_pdf)
    monkeypatch.setattr(
        orchestrator,
        "docling_pretag_ambiguity_router",
        lambda **_kwargs: {"plan": {}, "table_targets": [], "widget_targets": [], "form_targets": []},
    )

    async def no_structure_changes(*, review_tasks, structure_json, **_kwargs):
        return review_tasks, structure_json, []

    monkeypatch.setattr(orchestrator, "auto_apply_structure_tasks", no_structure_changes)
    settings = SimpleNamespace(
        verapdf_path="verapdf",
        verapdf_flavour="ua1",
        subprocess_timeout_validation=30,
        auto_apply_llm_font_map=False,
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
                classification="digital",
                structure_json=json.dumps(FIGURE_STRUCTURE),
            )
        )
        db.add(AltTextEntry(job_id="job-1", figure_index=0, image_path="figure.png", **entry))
        for step_name in ("tagging", "validation", "fidelity"):
            db.add(JobStep(job_id="job-1", step_name=step_name))
        await db.commit()

        await orchestrator.run_tagging_and_validation(
            "job-1",
            db,
            settings,
            SimpleNamespace(emit_progress=lambda *_args, **_kwargs: None),
            working_pdf=input_pdf,
            structure_json=json.loads(json.dumps(FIGURE_STRUCTURE)),
        )
        job = await db.get(Job, "job-1")
        rows = (await db.execute(select(ReviewTask).where(ReviewTask.job_id == "job-1"))).scalars().all()

    await engine.dispose()
    return job, rows, Path(job.output_path)


@pytest.mark.asyncio
async def test_undescribed_figure_holds_the_document_until_a_person_describes_it(tmp_path, monkeypatch):
    job, rows, output = await _tag_and_release(
        tmp_path / "first",
        monkeypatch,
        {"generated_text": None, "status": "pending_review"},
    )

    assert job.status == "manual_remediation"
    assert _figure_alts(output) == [None]
    assert any(row.task_type == "alt_text" and row.blocking for row in rows)

    job, rows, output = await _tag_and_release(
        tmp_path / "second",
        monkeypatch,
        {"generated_text": None, "edited_text": "A gray square", "status": "approved"},
    )

    assert job.status == "complete"
    assert _figure_alts(output) == ["A gray square"]
    assert not [row for row in rows if row.blocking]
