import asyncio
import time
from pathlib import Path

import pikepdf
import pytest

from app.pipeline import classify


def _write_blank_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(path)


def _write_text_image_pdf(
    path: Path,
    *,
    pages: int = 1,
    image_width: int = 2000,
    image_height: int = 3000,
    creator: str | None = None,
    producer: str | None = None,
) -> None:
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Helvetica"),
            }
        )
    )
    for _ in range(pages):
        page = pdf.add_blank_page(page_size=(612, 792))
        image = pdf.make_stream(b"\x00")
        image["/Type"] = pikepdf.Name("/XObject")
        image["/Subtype"] = pikepdf.Name("/Image")
        image["/Width"] = image_width
        image["/Height"] = image_height
        image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
        image["/BitsPerComponent"] = 8
        page.obj["/Resources"] = pikepdf.Dictionary(
            {
                "/Font": pikepdf.Dictionary({"/F1": font}),
                "/XObject": pikepdf.Dictionary({"/Im0": image}),
            }
        )
        page.obj["/Contents"] = pdf.make_stream(
            b"q 612 0 0 792 0 0 cm /Im0 Do Q\n"
            b"BT /F1 12 Tf 72 720 Td (Recognized text) Tj ET\n"
        )
    if creator is not None:
        pdf.docinfo["/Creator"] = creator
    if producer is not None:
        pdf.docinfo["/Producer"] = producer
    pdf.save(path)


def test_detect_language_from_text_uses_progressive_sample_tiers(monkeypatch, tmp_path):
    pdf_path = tmp_path / "mixed.pdf"
    calls = []

    def fake_extract_text(_path, *, page_numbers, maxpages):
        calls.append((tuple(page_numbers), maxpages))
        if maxpages == 3:
            return ""
        return "Bonjour tout le monde. Ce document est redige en francais canadien."

    monkeypatch.setattr("pdfminer.high_level.extract_text", fake_extract_text)
    monkeypatch.setattr(classify, "detect_language", lambda text: "fr" if "Bonjour" in text else None)

    assert classify._detect_language_from_text(pdf_path) == "fr"
    assert calls == [
        ((0, 1, 2), 3),
        ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9), 10),
    ]


@pytest.mark.asyncio
async def test_classify_pdf_detects_ocr_scan_with_existing_text_layer(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "paper-capture.pdf"
    _write_text_image_pdf(
        pdf_path,
        pages=3,
        creator="ocrmypdf 14.4.0 / Tesseract OCR-PDF",
        producer="Adobe Acrobat Paper Capture Plug-in",
    )

    async def fake_probe_ocr(_path):
        return None

    monkeypatch.setattr(classify, "_detect_language_from_text", lambda _path: None)
    monkeypatch.setattr(classify, "_probe_ocr_detect", fake_probe_ocr)

    result = await classify.classify_pdf(pdf_path)

    assert result.type == "ocr_scan"
    assert result.pages_with_text == 3
    assert result.image_heavy_pages == 3
    assert result.total_image_pixels == 18_000_000
    assert result.ocr_scan_like is True


@pytest.mark.asyncio
async def test_classify_pdf_keeps_image_rich_text_pdf_digital_without_ocr_metadata(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "image-rich-digital.pdf"
    _write_text_image_pdf(pdf_path, pages=3, producer="Report generator")

    async def fake_probe_ocr(_path):
        return None

    monkeypatch.setattr(classify, "_detect_language_from_text", lambda _path: None)
    monkeypatch.setattr(classify, "_probe_ocr_detect", fake_probe_ocr)

    result = await classify.classify_pdf(pdf_path)

    assert result.type == "digital"
    assert result.pages_with_text == 3
    assert result.image_heavy_pages == 3
    assert result.ocr_scan_like is False


@pytest.mark.asyncio
async def test_classify_pdf_falls_back_to_probe_ocr_after_text_detection_timeout(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "digital.pdf"
    _write_blank_pdf(pdf_path)

    def slow_text_detection(_path):
        time.sleep(0.05)
        return "fr"

    async def fake_probe_ocr(_path):
        return "es"

    monkeypatch.setattr(classify, "_page_has_text", lambda _page: True)
    monkeypatch.setattr(classify, "_detect_language_from_text", slow_text_detection)
    monkeypatch.setattr(classify, "_probe_ocr_detect", fake_probe_ocr)
    monkeypatch.setattr(classify, "_TEXT_DETECT_TIMEOUT_SECONDS", 0.001)

    result = await classify.classify_pdf(pdf_path)

    assert result.type == "digital"
    assert result.pages_with_text == 1
    assert result.detected_language == "es"


@pytest.mark.asyncio
async def test_classify_pdf_continues_without_language_after_probe_ocr_timeout(
    monkeypatch, tmp_path
):
    pdf_path = tmp_path / "scanned.pdf"
    _write_blank_pdf(pdf_path)

    async def slow_probe_ocr(_path):
        await asyncio.sleep(0.05)
        return "es"

    monkeypatch.setattr(classify, "_page_has_text", lambda _page: False)
    monkeypatch.setattr(classify, "_probe_ocr_detect", slow_probe_ocr)
    monkeypatch.setattr(classify, "_PROBE_OCR_TIMEOUT_SECONDS", 0.001)

    result = await classify.classify_pdf(pdf_path)

    assert result.type == "scanned"
    assert result.pages_with_text == 0
    assert result.detected_language is None
