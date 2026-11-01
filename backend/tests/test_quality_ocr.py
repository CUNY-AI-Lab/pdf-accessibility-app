"""Selective OCR recognizes vector ink without rasterizing native-text pages."""

import shutil
from types import SimpleNamespace

import pikepdf
import pytest

from app.pipeline import classify, ocr
from app.pipeline.fonts.encoding import _render_tounicode_cmap
from app.pipeline.fonts.ocr import unmapped_type3_pages
from tests.pdf_fixtures import helvetica


def outline_document(path, *, raster=False, nested=False):
    pdf = pikepdf.new()
    font = helvetica(pdf)
    for index in range(4):
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        # The last page has only a few strokes: a diagram is not outlined text.
        count = 110 if index in (1, 2) else 4
        paths = b" ".join(
            f"{20 + n % 20 * 20} {20 + n // 20 * 20} m ".encode() + b"1 1 l " * 12 + b"f"
            for n in range(count)
        )
        if index in (0, 2):
            paths += b" BT /F1 12 Tf 72 720 Td (Native text must stay native) Tj ET"
        if nested:
            form = pdf.make_stream(paths)
            form.Type, form.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
            form.BBox, form.Resources = [0, 0, 612, 792], page.Resources
            page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm=form))
            paths = b"/Fm Do"
        if raster and index == 1:
            image = pdf.make_stream(b"\xff" * 100)
            image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
            image.Width, image.Height = 10, 10
            image.ColorSpace, image.BitsPerComponent = pikepdf.Name.DeviceGray, 8
            page.Resources.XObject = pikepdf.Dictionary(Im=image)
            paths += b" q 612 0 0 792 0 0 cm /Im Do Q"
        page.Contents = pdf.make_stream(paths)
    pdf.save(path)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("raster", [False, True])
async def test_outline_selection_excludes_native_text_images_and_sparse_paths(
    tmp_path, monkeypatch, nested, raster
):
    source = tmp_path / "input.pdf"
    outline_document(source, raster=raster, nested=nested)
    assert classify._vector_outline_pages(source) == ([] if raster else [1])
    monkeypatch.setattr(classify, "_detect_language_from_text", lambda _: "en")
    result = await classify.classify_pdf(source)
    assert result.force_ocr_pages == ([] if raster else [1])
    if not raster:
        assert result.type == "mixed"
        assert set(result.ocr_pages) == {1, 3}


@pytest.mark.parametrize("failure", [False, True])
async def test_force_ocr_is_page_limited_and_stops_on_failure(tmp_path, monkeypatch, failure):
    source, output = tmp_path / "input.pdf", tmp_path / "ocr.pdf"
    outline_document(source)
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        shutil.copyfile(args[-2], args[-1])
        return SimpleNamespace(returncode=1 if failure else 0)

    async def communicate(*args, **kwargs):
        return b"", b""

    monkeypatch.setattr(ocr.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ocr, "communicate_with_timeout", communicate)
    result = await ocr.run_ocr(
        source, output, pages=[1, 3], force_pages=[1], rotate_pages=False, deskew=False
    )
    assert result.success is not failure
    assert len(calls) == (1 if failure else 2)
    first = calls[0]
    assert "--force-ocr" in first
    assert first[first.index("--pages") + 1] == "2"
    assert first[first.index("--optimize") + 1] == "0"
    if not failure:
        second = calls[1]
        assert "--skip-text" in second
        assert second[second.index("--pages") + 1] == "4"
        assert second[-2] == first[-1]


def type3_document(path, *, mapped=False, known=False, unused=False, nested=False):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    name = "/A" if known else "/0"
    charproc = pdf.make_stream(b"600 0 0 0 600 700 d1 0 0 400 650 re f")
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type3,
            FontBBox=[0, 0, 600, 700],
            FontMatrix=[0.001, 0, 0, 0.001, 0, 0],
            FirstChar=65,
            LastChar=65,
            Widths=[600],
            CharProcs=pikepdf.Dictionary({name: charproc}),
            Encoding=pikepdf.Dictionary(Differences=[65, pikepdf.Name(name)]),
        )
    )
    if mapped:
        font.ToUnicode = pdf.make_stream(_render_tounicode_cmap({65: "A"}, 1))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    content = b"" if unused else b"BT /F1 12 Tf 72 720 Td (A) Tj ET"
    if nested:
        form = pdf.make_stream(content)
        form.Subtype, form.Type = pikepdf.Name.Form, pikepdf.Name.XObject
        form.BBox, form.Resources = [0, 0, 612, 792], page.Resources
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm=form))
        content = b"/Fm Do"
    page.Contents = pdf.make_stream(content)
    pdf.save(path)


@pytest.mark.parametrize(
    "mapped,known,unused",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
@pytest.mark.parametrize("nested", [False, True])
def test_only_used_undecodable_type3_text_requires_ocr(tmp_path, mapped, known, unused, nested):
    source = tmp_path / "input.pdf"
    type3_document(source, mapped=mapped, known=known, unused=unused, nested=nested)
    assert unmapped_type3_pages(source) == ([0] if not (mapped or known or unused) else [])


async def test_mixed_pipeline_sends_only_selected_pages_to_ocr_and_extracts_its_result(
    tmp_path, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import Settings
    from app.models import Base, Job, JobStep
    from app.pipeline import orchestrator
    from tests.test_orchestrator_release_decision import _RecordingJobManager

    source = tmp_path / "source.pdf"
    outline_document(source)
    classification = classify.ClassificationResult(
        type="mixed",
        confidence=0.5,
        pages_with_text=2,
        total_pages=4,
        detected_language="en",
        ocr_pages=[1, 3],
        force_ocr_pages=[1],
    )
    calls = []

    async def classify_source(path):
        return classification

    async def recognize(input_path, output_path, language, **kwargs):
        assert input_path == source
        assert kwargs["pages"] == [1, 3]
        assert kwargs["force_pages"] == [1]
        shutil.copyfile(input_path, output_path)
        calls.append(output_path)
        return ocr.OcrResult(success=True, output_path=output_path)

    class StopAfterExtraction(Exception):
        pass

    async def extract(path, *args, **kwargs):
        assert path == calls[0]
        assert path != source
        calls.append("extracted")
        raise StopAfterExtraction()

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
            )
        )
        for step in (
            "classify",
            "ocr",
            "structure",
            "alt_text",
            "tagging",
            "validation",
            "fidelity",
        ):
            db.add(JobStep(job_id="job", step_name=step))
        await db.commit()
    await orchestrator._run_pipeline(
        "job", maker, Settings(llm_api_key="check-only"), _RecordingJobManager()
    )
    assert calls == [tmp_path / "ocred.pdf", "extracted"]
    await engine.dispose()
