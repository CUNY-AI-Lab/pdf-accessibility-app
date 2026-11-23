"""OCR text gets checked: crops are read in the job's language, text in any
script is compared, and pages force-OCR'd from drawn letters get a spot-check."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from app.config import Settings
from app.pipeline import classify, fidelity, orchestrator
from app.pipeline.fidelity import assess_fidelity
from app.pipeline.fonts import lanes
from app.pipeline.fonts.rules import FONT_LANE_OCR_REDO
from app.pipeline.ocr import FORCED_OCR_PAGES_KEY
from app.services import remediation_intelligence
from app.services.candidate_state import CandidatePhase
from app.services.page_intelligence import (
    _normalize_dense_text,
    _normalize_text,
    collect_grounded_text_candidates,
)
from app.services.release_policy import release_decision
from tests.pdf_fixtures import helvetica
from tests.test_quality_ocr import outline_document, type3_document
from tests.test_remediation_intelligence import (
    _document_model,
    _job,
    _page_blocks_for_review_from_document,
)

SPACED_BLOCK = {
    "review_id": "review-1",
    "type": "paragraph",
    "page": 0,
    "text": "D a t a  B o o k",
    "bbox": {"l": 72, "t": 700, "r": 240, "b": 660},
}


def _capture_crop_language(monkeypatch, target, text="Data Book"):
    seen = []

    def ocr(pdf_path, *, page_number, bbox, language=None, **_kwargs):
        seen.append(language)
        return text

    monkeypatch.setattr(target, ocr)
    return seen


def _validation_report(*, compliant=True):
    return {"compliant": compliant, "violations": [], "summary": {}}


# ── The job's OCR language reaches the crop OCR ──


def test_grounded_text_crops_are_read_in_the_jobs_language(monkeypatch, tmp_path):
    seen = _capture_crop_language(
        monkeypatch, "app.services.text_grounding.extract_ocr_text_from_bbox"
    )

    collect_grounded_text_candidates(
        tmp_path / "out.pdf", {"elements": [SPACED_BLOCK]}, ocr_language="jpn+eng"
    )

    assert seen == ["jpn+eng"]


def test_fidelity_reads_crops_in_the_jobs_language(monkeypatch):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)
    seen = _capture_crop_language(
        monkeypatch, "app.services.text_grounding.extract_ocr_text_from_bbox"
    )

    assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": [SPACED_BLOCK]},
        alt_entries=[],
        validation_report=_validation_report(),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
        ocr_language="rus",
    )

    assert seen == ["rus"]


def test_reading_review_reads_crops_in_the_jobs_language(monkeypatch, tmp_path):
    seen = _capture_crop_language(
        monkeypatch, "app.services.remediation_intelligence.extract_ocr_text_from_bbox"
    )
    job = _job(tmp_path, structure={"elements": [SPACED_BLOCK]}, ocr_language="kor")
    page_blocks = _page_blocks_for_review_from_document(_document_model(job), [1])

    remediation_intelligence._suspicious_reading_blocks_from_page_blocks(job, page_blocks)

    assert seen == ["kor"]


async def test_pretag_grounding_passes_the_jobs_language(monkeypatch, tmp_path):
    seen = {}

    def collect(pdf_path, structure_json, **kwargs):
        seen.update(kwargs)
        return {"targets": []}

    monkeypatch.setattr(orchestrator, "collect_grounded_text_candidates", collect)

    _, audit = await orchestrator._apply_pretag_grounded_text_resolutions(
        job=SimpleNamespace(ocr_language="ara"),
        settings=Settings(llm_api_key="check-only", auto_apply_grounded_text=True),
        working_pdf=tmp_path / "working.pdf",
        structure_json={"elements": []},
    )

    assert audit["reason"] == "no_candidates"
    assert seen["ocr_language"] == "ara"


async def test_candidate_fidelity_passes_the_jobs_language(monkeypatch, tmp_path):
    seen = {}

    def assess(**kwargs):
        seen.update(kwargs)
        return {"checks": []}, []

    monkeypatch.setattr(orchestrator, "assess_fidelity", assess)
    candidate = SimpleNamespace(
        phase=CandidatePhase.VALIDATED,
        output_path=tmp_path / "out.pdf",
        fidelity_source_pdf=tmp_path / "in.pdf",
        structure_json={"elements": []},
        alt_entries=[],
        validation_report={"tagging": {}},
        validation=SimpleNamespace(compliant=True, raw_report={}),
    )

    await orchestrator._assess_candidate_fidelity(
        candidate,
        job=SimpleNamespace(input_path=str(tmp_path / "in.pdf"), classification="mixed",
                            ocr_language="heb"),
        settings=Settings(llm_api_key="check-only"),
        adjudicate=False,
    )

    assert seen["ocr_language"] == "heb"


# ── Text in any script is compared ──


def _ascii_reference(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


@pytest.mark.parametrize(
    "text",
    ["Hello, World! 2024", "  O-683  item\tl1st  ", "D a t a  B o o k", "", "e-mail: a_b@c.org"],
)
def test_ascii_text_normalizes_as_before(text):
    assert _normalize_text(text) == _ascii_reference(text)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("人口推計", "入口推討"),  # kanji swap
        ("ひらがな", "ひらカな"),  # kana swap
        ("Москва", "Мосвка"),  # Cyrillic transposition
        ("résumé", "resume"),  # accents
        ("किताब", "कताब"),  # a Devanagari vowel sign
    ],
)
def test_differences_in_any_script_survive_normalization(left, right):
    assert _normalize_dense_text(left)
    assert _normalize_dense_text(left) != _normalize_dense_text(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [("２０２４年", "2024年"), ("ＯＣＲ", "ocr"), ("ﬁnance", "finance"), ("STRASSE", "straße")],
)
def test_compatibility_forms_and_case_fold_together(left, right):
    assert _normalize_text(left) == _normalize_text(right)


def test_a_kanji_swap_in_a_mixed_script_block_becomes_a_review_target(monkeypatch, tmp_path):
    native = "人口推計 Population Estimates 2024年"
    _capture_crop_language(
        monkeypatch,
        "app.services.text_grounding.extract_ocr_text_from_bbox",
        text="入口推討 Population Estimates 2024年",
    )

    result = collect_grounded_text_candidates(
        tmp_path / "out.pdf",
        {"elements": [{**SPACED_BLOCK, "text": native}]},
        ocr_language="jpn+eng",
    )

    assert result["target_count"] == 1
    assert result["targets"][0]["issue_type"] == "encoding_problem"


# ── Pages force-OCR'd from drawn letters get an advisory spot-check ──


def _forced_ocr_task(tasks):
    return [
        task for task in tasks
        if isinstance(task.get("metadata"), dict)
        and task["metadata"].get("check") == "forced_ocr_text"
    ]


@pytest.mark.parametrize("compliant", [True, False])
def test_forced_ocr_pages_get_an_advisory_task_that_never_holds_release(monkeypatch, compliant):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)

    def assess(structure_json):
        return assess_fidelity(
            input_pdf=Path("in.pdf"),
            output_pdf=Path("out.pdf"),
            structure_json=structure_json,
            alt_entries=[],
            validation_report=_validation_report(compliant=compliant),
            tagging_metrics={"tables_tagged": 0},
            classification="mixed",
        )

    without_report, without_tasks = assess({"elements": []})
    report, tasks = assess({"elements": [], FORCED_OCR_PAGES_KEY: [3, 2, 0, "x"]})

    (task,) = _forced_ocr_task(tasks)
    assert task["blocking"] is False
    assert task["metadata"]["pages_to_check"] == [2, 3]
    assert not _forced_ocr_task(without_tasks)
    # Release, pass/fail and the blocking count are what they were without it.
    assert report["passed"] == without_report["passed"]
    assert report["summary"]["blocking_tasks"] == without_report["summary"]["blocking_tasks"]
    assert release_decision(compliant=compliant, checks=report["checks"]) == release_decision(
        compliant=compliant, checks=without_report["checks"]
    )


class _StopAfterStructure(Exception):
    pass


def _plain_text_document(path):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(b"BT /F1 12 Tf 72 720 Td (Native text stays native) Tj ET")
    pdf.save(path)


@pytest.mark.parametrize(
    ("build", "recorded"),
    [
        (outline_document, [2]),  # page 2 draws its letters as outlines
        (type3_document, [1]),  # page 1 shows undecodable Type3 codes
        (_plain_text_document, None),
    ],
)
async def test_pipeline_records_force_ocrd_pages(tmp_path, monkeypatch, build, recorded):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, Job, JobStep
    from app.pipeline.structure import StructureResult
    from tests.test_orchestrator_release_decision import _RecordingJobManager

    source = tmp_path / "source.pdf"
    build(source)
    stored = {}
    monkeypatch.setattr(classify, "_detect_language_from_text", lambda _: "en")

    async def recognize(input_path, output_path, language, **kwargs):
        output_path.write_bytes(input_path.read_bytes())
        return SimpleNamespace(success=True, output_path=output_path, skipped=False, message="")

    async def extract(path, *args, **kwargs):
        return StructureResult(document_json={"title": "Statement", "elements": []}, figures=[])

    real_update_step = orchestrator._update_step

    async def update_step(db, job_id, step, status, **kwargs):
        if (step, status) == ("structure", "complete"):
            job = await db.get(Job, job_id)
            stored.update(json.loads(job.structure_json))
            raise _StopAfterStructure()
        await real_update_step(db, job_id, step, status, **kwargs)

    monkeypatch.setattr(orchestrator, "run_ocr", recognize)
    monkeypatch.setattr(orchestrator, "extract_structure", extract)
    monkeypatch.setattr(orchestrator, "ensure_figure_images", lambda _pdf, _structure, figures, _dir: figures)
    monkeypatch.setattr(
        orchestrator,
        "synthesize_missing_visual_figures",
        lambda *, structure_json, figures, **_kw: (structure_json, figures, {"applied_count": 0}),
    )

    async def no_table_model(_pdf, structure_json, _settings):
        return structure_json, {}

    monkeypatch.setattr(orchestrator, "_apply_table_model", no_table_model)
    monkeypatch.setattr(orchestrator, "_update_step", update_step)
    monkeypatch.setattr(orchestrator, "create_job_dir", lambda _: tmp_path)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(
            Job(
                id="job",
                input_path=str(source),
                filename="source.pdf",
                original_filename="source.pdf",
                owner_session_hash="owner",
                status="queued",
            )
        )
        for step in ("classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"):
            db.add(JobStep(job_id="job", step_name=step))
        await db.commit()
    settings = Settings(
        llm_api_key="check-only",
        assist_toc_with_llm=False,
        assist_title_with_llm=False,
        assist_bookmarks_with_llm=False,
    )
    await orchestrator._run_pipeline("job", maker, settings, _RecordingJobManager())
    await engine.dispose()

    assert stored, "the pipeline did not reach the end of the structure step"
    assert stored.get(FORCED_OCR_PAGES_KEY) == recorded


async def test_font_ocr_lane_candidate_keeps_the_forced_ocr_advisory(tmp_path, monkeypatch):
    working_pdf = tmp_path / "working.pdf"
    working_pdf.write_bytes(b"%PDF-1.7\n")
    tagged_pdf = tmp_path / "tagged.pdf"
    tagged_pdf.write_bytes(b"%PDF-1.7\n")

    async def fake_run_ocr(*args, **kwargs):
        return SimpleNamespace(success=True, output_path=working_pdf, skipped=False, message="")

    async def rebuilt_structure(input_path, job_dir, **_kwargs):
        return SimpleNamespace(
            document_json={"elements": [{"type": "paragraph", "text": "fresh", "page": 0}]}
        )

    async def fake_tag_pdf(*args, **kwargs):
        output = kwargs["output_path"]
        output.write_bytes(b"%PDF-1.7\n")
        return SimpleNamespace(output_path=output, struct_elems_created=1)

    async def fake_validate(pdf_path, verapdf_path, flavour, **kwargs):
        return SimpleNamespace(compliant=True, violations=[], raw_report={}, validated_path=Path(pdf_path))

    monkeypatch.setattr(lanes, "create_job_dir", lambda job_id: tmp_path)
    monkeypatch.setattr(lanes, "run_ocr", fake_run_ocr)
    monkeypatch.setattr(lanes, "extract_structure", rebuilt_structure)
    monkeypatch.setattr(lanes, "tag_pdf", fake_tag_pdf)
    monkeypatch.setattr(lanes, "validate_pdf", fake_validate)

    result = await lanes._attempt_font_lane(
        job_id="job-ocr",
        job=SimpleNamespace(original_filename="sample.pdf", ocr_language=None),
        settings=Settings(llm_api_key="check-only"),
        working_pdf=working_pdf,
        tagged_pdf=tagged_pdf,
        structure_json={
            "elements": [{"type": "paragraph", "text": "stale", "page": 0}],
            FORCED_OCR_PAGES_KEY: [2],
        },
        reviewed_alts=[],
        lane=FONT_LANE_OCR_REDO,
        current_tagging_result=SimpleNamespace(),
    )
    assert result["details"]["structure_refreshed"] is True

    # Fidelity on the lane's candidate still asks for the spot-check.
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)
    _, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json=result["structure_json"],
        alt_entries=[],
        validation_report=_validation_report(),
        tagging_metrics={"tables_tagged": 0},
        classification="mixed",
    )
    (task,) = _forced_ocr_task(tasks)
    assert task["metadata"]["pages_to_check"] == [2]
