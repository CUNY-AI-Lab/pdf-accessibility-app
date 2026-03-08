from pathlib import Path

import pikepdf
import pytest

from tests.fixtures import TEST_SAMPLE_PDF

from app.pipeline.tagger import tag_pdf


@pytest.mark.asyncio
async def test_tag_pdf_sets_markinfo_suspects_false(tmp_path):
    input_pdf = TEST_SAMPLE_PDF
    output_pdf = tmp_path / "tagged.pdf"

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "Test Sample"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        mark_info = pdf.Root["/MarkInfo"]
        assert mark_info["/Marked"] is True
        assert mark_info["/Suspects"] is False


def _build_blank_image_only_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    empty_form = pdf.make_stream(b"")
    empty_form["/Type"] = pikepdf.Name("/XObject")
    empty_form["/Subtype"] = pikepdf.Name("/Form")
    empty_form["/FormType"] = 1
    empty_form["/BBox"] = pikepdf.Array([0, 0, 200, 200])
    empty_form["/Resources"] = pikepdf.Dictionary()

    white_image = pdf.make_stream(bytes([255]))
    white_image["/Type"] = pikepdf.Name("/XObject")
    white_image["/Subtype"] = pikepdf.Name("/Image")
    white_image["/Width"] = 1
    white_image["/Height"] = 1
    white_image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    white_image["/BitsPerComponent"] = 8

    page["/Resources"] = pikepdf.Dictionary({
        "/XObject": pikepdf.Dictionary({
            "/OCR0": empty_form,
            "/Im0": white_image,
        }),
    })
    page["/Contents"] = pdf.make_stream(
        b"q 1 0 0 1 0 0 cm\n/OCR0 Do\nQ\nq\n200 0 0 200 0 0 cm\n/Im0 Do\nQ\n"
    )
    pdf.save(path)


def _build_ocr_noise_only_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    empty_form = pdf.make_stream(b"")
    empty_form["/Type"] = pikepdf.Name("/XObject")
    empty_form["/Subtype"] = pikepdf.Name("/Form")
    empty_form["/FormType"] = 1
    empty_form["/BBox"] = pikepdf.Array([0, 0, 200, 200])
    empty_form["/Resources"] = pikepdf.Dictionary()

    white_image = pdf.make_stream(bytes([255]))
    white_image["/Type"] = pikepdf.Name("/XObject")
    white_image["/Subtype"] = pikepdf.Name("/Image")
    white_image["/Width"] = 1
    white_image["/Height"] = 1
    white_image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    white_image["/BitsPerComponent"] = 8

    page["/Resources"] = pikepdf.Dictionary({
        "/XObject": pikepdf.Dictionary({
            "/OCR-noise": empty_form,
            "/Im0": white_image,
        }),
    })
    page["/Contents"] = pdf.make_stream(
        b"q 1 0 0 1 0 0 cm\n/OCR-noise Do\nQ\n"
        b"q 200 0 0 200 0 0 cm\n/Im0 Do\nQ\n"
        b"q\n0 0 200 200 re\n0 0 200 200 re\nW*\nn\n"
        b"0.239 w\nq\n1 0 0 1 10 10 cm\n0 0 m\n0 25 l\nS\nQ\nQ\n"
    )
    pdf.save(path)


@pytest.mark.asyncio
async def test_tag_pdf_artifacts_visually_blank_image_only_pages(monkeypatch, tmp_path):
    input_pdf = tmp_path / "blank_image_only.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_blank_image_only_pdf(input_pdf)

    monkeypatch.setattr(
        "app.pipeline.tagger._render_page_ink_ratio",
        lambda *_args, **_kwargs: 0.0,
    )

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "Blank OCR Page"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        stream = bytes(pdf.pages[0].Contents.read_bytes())
        assert b"/Artifact BMC" in stream
        assert b"/OCR0 Do" in stream
        assert b"/Im0 Do" in stream


@pytest.mark.asyncio
async def test_tag_pdf_artifacts_ocr_noise_only_pages(tmp_path):
    input_pdf = tmp_path / "ocr_noise_only.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_ocr_noise_only_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "OCR Noise"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        stream = bytes(pdf.pages[0].Contents.read_bytes())
        assert b"/Artifact BMC" in stream
        assert b"/Im0 Do" in stream
