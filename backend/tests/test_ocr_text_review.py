"""OCR text gets checked: crops are read in the job's language, text in any
script is compared, and pages OCR'd from letter outlines get a spot-check."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.pipeline import classify, fidelity, orchestrator
from app.pipeline.fidelity import assess_fidelity
from app.services import remediation_intelligence
from app.services.candidate_state import CandidatePhase
from app.services.page_intelligence import (
    _normalize_dense_text,
    _normalize_text,
    collect_grounded_text_candidates,
)
from app.services.release_policy import release_decision
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


# ── Pages OCR'd from letter outlines get an advisory spot-check ──


async def test_classification_names_outline_pages_but_not_type3_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(classify, "_detect_language_from_text", lambda _: "en")
    outlined = tmp_path / "outlined.pdf"
    outline_document(outlined)
    type3 = tmp_path / "type3.pdf"
    type3_document(type3)

    outlined_result = await classify.classify_pdf(outlined)
    type3_result = await classify.classify_pdf(type3)

    assert outlined_result.outline_pages == [1]
    assert type3_result.force_ocr_pages == [0]
    assert type3_result.outline_pages == []


def _outline_task(tasks):
    return [
        task for task in tasks
        if isinstance(task.get("metadata"), dict)
        and task["metadata"].get("check") == "ocr_outline_text"
    ]


@pytest.mark.parametrize("compliant", [True, False])
def test_outline_pages_get_an_advisory_task_that_never_holds_release(monkeypatch, compliant):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)
    without_report, without_tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=compliant),
        tagging_metrics={"tables_tagged": 0},
        classification="mixed",
    )
    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": [], "ocr_outline_pages": [3, 2, 0, "x"]},
        alt_entries=[],
        validation_report=_validation_report(compliant=compliant),
        tagging_metrics={"tables_tagged": 0},
        classification="mixed",
    )

    (task,) = _outline_task(tasks)
    assert task["blocking"] is False
    assert task["metadata"]["pages_to_check"] == [2, 3]
    assert not _outline_task(without_tasks)
    # Release, pass/fail and the blocking count are what they were without it.
    assert report["passed"] == without_report["passed"]
    assert report["summary"]["blocking_tasks"] == without_report["summary"]["blocking_tasks"]
    assert release_decision(compliant=compliant, checks=report["checks"]) == release_decision(
        compliant=compliant, checks=without_report["checks"]
    )


class _StopAfterStructure(Exception):
    pass


@pytest.mark.parametrize(("outline_pages", "recorded"), [([1], [2]), ([], None)])
async def test_pipeline_records_outline_pages_after_ocr(tmp_path, monkeypatch, outline_pages, recorded):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, Job, JobStep
    from app.pipeline.structure import StructureResult
    from tests.test_orchestrator_release_decision import _RecordingJobManager

    source = tmp_path / "source.pdf"
    outline_document(source)
    stored = {}

    async def classify_source(path):
        return classify.ClassificationResult(
            type="mixed", confidence=0.5, pages_with_text=2, total_pages=4,
            detected_language="en", ocr_pages=[1, 3], force_ocr_pages=[1],
            outline_pages=outline_pages,
        )

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

    monkeypatch.setattr(orchestrator, "classify_pdf", classify_source)
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
    assert stored.get("ocr_outline_pages") == recorded
