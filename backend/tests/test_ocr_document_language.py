"""On OCR text, the document language stays among the languages OCR read it in.

Lingua reads noisy OCR output as unrelated languages (English zines came out
Yoruba), and that detection became the document's /Lang.
"""

from types import SimpleNamespace

import pikepdf
import pytest

from app.pipeline import orchestrator, structure
from app.pipeline.classify import ClassificationResult
from app.pipeline.language import tesseract_languages_to_bcp47
from app.pipeline.tagger import tag_pdf
from app.services.docling_escalation import docling_structure_escalation_plan
from tests.pdf_fixtures import helvetica

TEXT = "The zine collective met on Tuesday to plan the next issue of the newsletter."


def _elements():
    return [{"type": "paragraph", "text": TEXT, "page": 0}]


@pytest.mark.parametrize(
    ("spec", "tags"),
    [("eng", ["en"]), ("jpn+eng", ["ja", "en"]), ("chi_sim+osd", ["zh-Hans"]), ("", []), (None, [])],
)
def test_tesseract_language_specs_become_tags_primary_first(spec, tags):
    assert tesseract_languages_to_bcp47(spec) == tags


def test_ocr_text_rejects_a_detection_outside_its_ocr_languages(monkeypatch):
    monkeypatch.setattr(structure, "_detect_language", lambda _text: "yo")

    decision = structure._infer_document_language(_elements(), ocr_languages=["en"])

    assert decision["language"] == "en"
    assert decision["detected"] == "yo"
    assert decision["reason"] != "detection_is_an_ocr_language"


def test_ocr_text_keeps_a_detection_among_its_ocr_languages(monkeypatch):
    monkeypatch.setattr(structure, "_detect_language", lambda _text: "en")

    decision = structure._infer_document_language(_elements(), ocr_languages=["ja", "en"])

    assert decision["language"] == "en"


def test_ocr_text_matches_a_detection_by_primary_subtag(monkeypatch):
    monkeypatch.setattr(structure, "_detect_language", lambda _text: "zh")

    decision = structure._infer_document_language(_elements(), ocr_languages=["zh-Hans"])

    assert decision["language"] == "zh"


def test_ocr_text_without_a_detection_uses_the_primary_ocr_language(monkeypatch):
    monkeypatch.setattr(structure, "_detect_language", lambda _text: None)

    decision = structure._infer_document_language(_elements(), ocr_languages=["ja", "en"])

    assert decision["language"] == "ja"


def test_digital_text_keeps_whatever_lingua_detects(monkeypatch):
    monkeypatch.setattr(structure, "_detect_language", lambda _text: "yo")

    assert structure._infer_document_language(_elements())["language"] == "yo"


def test_escalation_plan_records_the_rejected_detection():
    plan = docling_structure_escalation_plan(
        {
            "language": "en",
            "language_evidence": {
                "language": "en",
                "reason": "detection_not_an_ocr_language",
                "detected": "yo",
                "ocr_languages": ["en"],
            },
        }
    )

    assert plan["language"]["signal"] == "en"
    assert plan["language"]["reason"] == "detection_not_an_ocr_language"
    assert plan["language"]["detected"] == "yo"
    assert plan["language"]["ocr_languages"] == ["en"]


def test_escalation_plan_does_not_claim_a_reason_for_a_language_set_later():
    plan = docling_structure_escalation_plan(
        {
            "language": "fr",
            "language_evidence": {"language": None, "reason": "no_language_signal"},
        }
    )

    assert plan["language"]["signal"] == "fr"
    assert plan["language"]["reason"] != "no_language_signal"


def _one_line_pdf(path):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(f"BT /F1 10 Tf 72 700 Td ({TEXT}) Tj ET".encode())
    pdf.save(path)


@pytest.mark.parametrize(("ocr_languages", "lang"), [(["en"], "en"), ((), "yo")])
async def test_document_lang_follows_the_ocr_language_on_ocr_text(
    tmp_path, monkeypatch, ocr_languages, lang
):
    source = tmp_path / "zine.pdf"
    _one_line_pdf(source)

    async def convert(pdf_path, job_dir, *args, **kwargs):
        doc = {
            "body": {"children": [{"$ref": "#/texts/0"}]},
            "texts": [
                {
                    "label": "text",
                    "text": TEXT,
                    "prov": [{"page_no": 1, "bbox": {"l": 72, "t": 712, "r": 540, "b": 698,
                                                     "coord_origin": "BOTTOMLEFT"}}],
                }
            ],
            "pages": {"1": {"size": {"width": 612, "height": 792}}},
        }
        return doc, []

    monkeypatch.setattr(structure, "_convert_via_docling_serve", convert)
    monkeypatch.setattr(
        structure,
        "get_settings",
        lambda: SimpleNamespace(
            docling_serve_url="http://docling",
            docling_serve_timeout=60,
            docling_serve_ocr_engine="rapidocr",
            docling_serve_token="",
        ),
    )
    monkeypatch.setattr(structure, "_detect_language", lambda _text: "yo")

    result = await structure.extract_structure(
        source, tmp_path / "job", ocr_languages=ocr_languages
    )
    output = tmp_path / "tagged.pdf"
    await tag_pdf(source, output, result.document_json, [])

    with pikepdf.open(output) as pdf:
        assert str(pdf.Root.Lang) == lang


class _StopAfterExtraction(Exception):
    pass


@pytest.mark.parametrize(
    ("classification", "ocr_languages"),
    [("ocr_scan", ["ja", "en"]), ("mixed", ["ja", "en"]), ("digital", [])],
)
async def test_pipeline_passes_the_jobs_ocr_languages_for_ocr_text(
    tmp_path, monkeypatch, classification, ocr_languages
):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import Settings
    from app.models import Base, Job, JobStep
    from tests.test_orchestrator_release_decision import _RecordingJobManager

    source = tmp_path / "source.pdf"
    _one_line_pdf(source)
    seen = {}

    async def classify_source(path):
        return ClassificationResult(
            type=classification, confidence=1.0, pages_with_text=1, total_pages=1,
        )

    async def recognize(input_path, output_path, language, **kwargs):
        output_path.write_bytes(input_path.read_bytes())
        return SimpleNamespace(success=True, output_path=output_path, skipped=False, message="")

    async def extract(path, *args, ocr_languages=(), **kwargs):
        seen["ocr_languages"] = list(ocr_languages)
        raise _StopAfterExtraction()

    monkeypatch.setattr(orchestrator, "classify_pdf", classify_source)
    monkeypatch.setattr(orchestrator, "run_ocr", recognize)
    monkeypatch.setattr(orchestrator, "extract_structure", extract)
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
                ocr_language="jpn+eng",
            )
        )
        for step in ("classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"):
            db.add(JobStep(job_id="job", step_name=step))
        await db.commit()
    await orchestrator._run_pipeline(
        "job", maker, Settings(llm_api_key="check-only"), _RecordingJobManager()
    )
    await engine.dispose()

    assert seen["ocr_languages"] == ocr_languages
