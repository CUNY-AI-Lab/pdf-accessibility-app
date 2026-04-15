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
