from __future__ import annotations

from subprocess import CompletedProcess

from app.services import text_grounding


def test_extract_ocr_text_from_bbox_returns_normalized_tesseract_output(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    monkeypatch.setattr(text_grounding, "_tesseract_path", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(
        text_grounding,
        "render_bbox_preview_png_bytes",
        lambda pdf_path, page_number, bbox, crop_margin_points=8.0, highlight=False: b"png-bytes",
    )

    def _fake_run(cmd, capture_output, check, timeout, text, env):
        assert cmd[:3] == ["/usr/bin/tesseract", cmd[1], "stdout"]
        assert capture_output is True
        assert "--psm" in cmd
        assert "-l" in cmd
        assert "/opt/homebrew/bin" in env["PATH"]
        return CompletedProcess(cmd, 0, stdout="Data   Book\n", stderr="")

    monkeypatch.setattr(text_grounding.subprocess, "run", _fake_run)

    result = text_grounding.extract_ocr_text_from_bbox(
        pdf_path,
        page_number=1,
        bbox={"l": 10, "t": 20, "r": 100, "b": 40},
    )

    assert result == "Data Book"


def test_extract_ocr_text_from_bbox_returns_empty_when_tesseract_missing(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    monkeypatch.setattr(text_grounding, "_tesseract_path", lambda: None)

    result = text_grounding.extract_ocr_text_from_bbox(
        pdf_path,
        page_number=1,
        bbox={"l": 10, "t": 20, "r": 100, "b": 40},
    )

    assert result == ""
