"""On OCR text, the document language stays among the languages OCR read it in.

Lingua reads noisy OCR output as unrelated languages (English zines came out
Yoruba), and that detection became the document's /Lang.
"""

from types import SimpleNamespace

import pikepdf
import pytest

from app.pipeline import classify, language, orchestrator, structure
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


@pytest.mark.parametrize(
    ("detected", "ocr_languages", "classified", "language", "reason"),
    [
        # Lingua agrees with an OCR language.
        ("en", ["ja", "en"], None, "en", "detection_matches_ocr_language"),
        ("zh", ["zh-Hans"], None, "zh", "detection_matches_ocr_language"),
        # A Dutch scan OCR'd with the eng pack: classify said nl.
        ("nl", ["en"], "nl", "nl", "detection_matches_classify_language"),
        # Yoruba on English OCR text, with and without a classify detection.
        ("yo", ["en"], "en", "en", "classify_language"),
        ("yo", ["en"], "nl", "nl", "classify_language"),
        ("yo", ["en"], None, "en", "primary_ocr_language"),
        (None, ["ja", "en"], None, "ja", "primary_ocr_language"),
        (None, ["en"], "nl", "nl", "classify_language"),
        # OCR text whose Tesseract spec names no language (osd only).
        ("yo", [], "en", "en", "classify_language"),
    ],
)
def test_ocr_text_language_needs_a_second_signal(
    monkeypatch, detected, ocr_languages, classified, language, reason
):
    monkeypatch.setattr(structure, "_detect_language", lambda _text: detected)

    decision = structure._infer_document_language(
        _elements(), ocr_languages=ocr_languages, classified_language=classified
    )

    assert decision["language"] == language
    assert decision["reason"] == reason
    assert decision["detected"] == detected


def test_digital_text_keeps_whatever_lingua_detects(monkeypatch):
    monkeypatch.setattr(structure, "_detect_language", lambda _text: "yo")

    assert structure._infer_document_language(_elements())["language"] == "yo"


def test_escalation_plan_records_the_rejected_detection():
    plan = docling_structure_escalation_plan(
        {
            "language": "en",
            "language_evidence": {
                "language": "en",
                "reason": "classify_language",
                "detected": "yo",
                "ocr_languages": ["en"],
                "classify_language": "en",
            },
        }
    )

    assert plan["language"]["signal"] == "en"
    assert plan["language"]["reason"] == "classify_language"
    assert plan["language"]["detected"] == "yo"
    assert plan["language"]["ocr_languages"] == ["en"]
    assert plan["language"]["classify_language"] == "en"


def test_escalation_plan_does_not_claim_a_reason_for_a_language_set_later():
    plan = docling_structure_escalation_plan(
        {
            "language": "fr",
            "language_evidence": {"language": None, "reason": "no_language_signal"},
        }
    )

    assert plan["language"]["signal"] == "fr"
    assert plan["language"]["reason"] != "no_language_signal"


DUTCH = [
    "De gemeente publiceert elk jaar een verslag over de woningmarkt in de stad.",
    "Dit rapport beschrijft hoe het aantal nieuwe woningen zich heeft ontwikkeld.",
    "Ook laten we zien welke wijken het snelst groeien en waar nog ruimte is.",
]


def _text_pdf(path, pages, *, pictured_page=False):
    """One page per list of lines; with ``pictured_page``, a last page that is
    a picture of a table with a one-line caption."""
    pdf = pikepdf.new()
    font = helvetica(pdf)
    for lines in pages:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        shown = " ".join(f"({line}) Tj 0 -14 Td" for line in lines)
        page.Contents = pdf.make_stream(f"BT /F1 10 Tf 72 700 Td {shown} ET".encode())
    if pictured_page:
        page = pdf.add_blank_page(page_size=(612, 792))
        image = pdf.make_stream(b"\x00")
        image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        image.Width, image.Height = 2000, 3000
        image.ColorSpace, image.BitsPerComponent = pikepdf.Name.DeviceGray, 8
        page.Resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(F1=font), XObject=pikepdf.Dictionary(Im0=image)
        )
        page.Contents = pdf.make_stream(
            b"q 612 0 0 792 0 0 cm /Im0 Do Q BT /F1 10 Tf 72 40 Td (Tabel 1) Tj ET"
        )
    pdf.save(path)


def _one_line_pdf(path):
    _text_pdf(path, [[TEXT]])


def _fake_docling(monkeypatch, pages):
    """Docling returns each page's lines as one paragraph."""

    async def convert(pdf_path, job_dir, *args, **kwargs):
        texts = [
            {
                "label": "text",
                "text": " ".join(lines),
                "prov": [{"page_no": index + 1, "bbox": {"l": 72, "t": 712, "r": 540, "b": 660,
                                                         "coord_origin": "BOTTOMLEFT"}}],
            }
            for index, lines in enumerate(pages)
        ]
        doc = {
            "body": {"children": [{"$ref": f"#/texts/{index}"} for index in range(len(texts))]},
            "texts": texts,
            "pages": {
                str(index + 1): {"size": {"width": 612, "height": 792}}
                for index in range(len(pages))
            },
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


async def _tagged_lang(source, job_dir, **structure_kwargs):
    """The tagged PDF's /Lang and the structure step's recorded reason."""
    result = await structure.extract_structure(source, job_dir, **structure_kwargs)
    output = job_dir.parent / "tagged.pdf"
    await tag_pdf(source, output, result.document_json, [])
    with pikepdf.open(output) as pdf:
        return str(pdf.Root.Lang), result.document_json["language_evidence"]["reason"]


@pytest.mark.parametrize(("ocr_languages", "lang"), [(["en"], "en"), ((), "yo")])
async def test_document_lang_follows_the_ocr_language_on_ocr_text(
    tmp_path, monkeypatch, ocr_languages, lang
):
    source = tmp_path / "zine.pdf"
    _one_line_pdf(source)
    _fake_docling(monkeypatch, [[TEXT]])
    monkeypatch.setattr(structure, "_detect_language", lambda _text: "yo")

    assert (await _tagged_lang(source, tmp_path / "job", ocr_languages=ocr_languages))[0] == lang


class _StopAfterExtraction(Exception):
    pass


async def _structure_kwargs_from_pipeline(tmp_path, monkeypatch, source, classify_source, ocr_language):
    """Run the pipeline to the structure step; return what it passes to
    extract_structure and the job's OCR language by then."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import Settings
    from app.models import Base, Job, JobStep
    from tests.test_orchestrator_release_decision import _RecordingJobManager

    seen = {}

    async def recognize(input_path, output_path, language, **kwargs):
        output_path.write_bytes(input_path.read_bytes())
        return SimpleNamespace(success=True, output_path=output_path, skipped=False, message="")

    async def extract(path, *args, **kwargs):
        seen.update(kwargs)
        raise _StopAfterExtraction()

    if classify_source is not None:
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
                ocr_language=ocr_language,
            )
        )
        for step in ("classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"):
            db.add(JobStep(job_id="job", step_name=step))
        await db.commit()
    await orchestrator._run_pipeline(
        "job", maker, Settings(llm_api_key="check-only"), _RecordingJobManager()
    )
    async with maker() as db:
        job_ocr_language = (await db.get(Job, "job")).ocr_language
    await engine.dispose()
    return {key: seen[key] for key in ("ocr_languages", "classified_language")}, job_ocr_language


@pytest.mark.parametrize(
    ("classification", "ocr_languages", "classified"),
    [("ocr_scan", ["ja", "en"], "ja"), ("mixed", ["ja", "en"], "ja"), ("digital", [], None)],
)
async def test_pipeline_passes_language_signals_only_for_ocr_text(
    tmp_path, monkeypatch, classification, ocr_languages, classified
):
    source = tmp_path / "source.pdf"
    _one_line_pdf(source)

    async def classify_source(path):
        return ClassificationResult(
            type=classification, confidence=1.0, pages_with_text=1, total_pages=1,
            detected_language="ja",
        )

    kwargs, _ = await _structure_kwargs_from_pipeline(
        tmp_path, monkeypatch, source, classify_source, "jpn+eng"
    )

    assert list(kwargs["ocr_languages"]) == ocr_languages
    assert kwargs["classified_language"] == classified


async def test_dutch_scan_ocrd_with_the_eng_pack_keeps_dutch_lang(tmp_path, monkeypatch):
    monkeypatch.setattr(language, "installed_tesseract_languages", lambda: frozenset({"eng"}))
    source = tmp_path / "scan.pdf"
    _text_pdf(source, [DUTCH])

    async def classify_source(path):
        return ClassificationResult(
            type="scanned", confidence=1.0, pages_with_text=0, total_pages=1,
            detected_language="nl",
        )

    kwargs, job_ocr_language = await _structure_kwargs_from_pipeline(
        tmp_path, monkeypatch, source, classify_source, None
    )
    _fake_docling(monkeypatch, [DUTCH])

    assert job_ocr_language == "eng"
    assert await _tagged_lang(source, tmp_path / "job", **kwargs) == (
        "nl",
        "detection_matches_classify_language",
    )


async def test_mixed_dutch_report_with_a_pictured_table_keeps_dutch_lang(tmp_path, monkeypatch):
    monkeypatch.setattr(language, "installed_tesseract_languages", lambda: frozenset({"eng"}))
    source = tmp_path / "report.pdf"
    _text_pdf(source, [DUTCH, DUTCH], pictured_page=True)

    classified = await classify.classify_pdf(source)
    assert classified.type == "mixed"
    assert classified.detected_language == "nl"

    # The real classify step, as the pipeline runs it.
    kwargs, job_ocr_language = await _structure_kwargs_from_pipeline(
        tmp_path, monkeypatch, source, None, None
    )
    _fake_docling(monkeypatch, [DUTCH, DUTCH, ["Tabel 1"]])

    assert job_ocr_language == "eng"
    assert await _tagged_lang(source, tmp_path / "job", **kwargs) == (
        "nl",
        "detection_matches_classify_language",
    )
