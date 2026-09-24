"""The Gateway OCR engine turns a vision model's line boxes into a text layer
that OCRmyPDF can render and a screen reader can read in order."""

import base64
import io
import json

import httpx
import pytest
from ocrmypdf.font import MultiFontManager
from ocrmypdf.fpdf_renderer.renderer import Fpdf2PdfRenderer
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextLine
from PIL import Image

from app.pipeline import ocr_vlm_plugin
from app.pipeline.ocr_vlm_plugin import GatewayOcrError, GatewayVisionOcrEngine, parse_lines

MODEL_LINES = [
    {"bbox_2d": [100, 100, 900, 150], "text": "Dear Sir:-"},
    {
        "bbox_2d": [100, 200, 900, 300],
        "text": "Indiana County Progressives\nsend you congratulations",
    },
    {"bbox_2d": [100, 400, 500, 450], "text": "I am,"},
]


def _fenced(items) -> str:
    return "```json\n" + json.dumps(items) + "\n```"


def test_parse_lines_accepts_a_fenced_array_and_drops_unusable_items():
    content = _fenced(
        [
            MODEL_LINES[0],
            {"bbox_2d": [1, 2, 3], "text": "three numbers"},
            {"bbox_2d": [1, 2, 3, 4], "text": "   "},
            {"text": "no box"},
            "not an object",
        ]
    )
    assert parse_lines(content) == [{"bbox": [100.0, 100.0, 900.0, 150.0], "text": "Dear Sir:-"}]


def _page_image(tmp_path, size=(1000, 800), dpi=100):
    path = tmp_path / "page.png"
    Image.new("L", size, 255).save(path, dpi=(dpi, dpi))
    return path


def _answer(monkeypatch, status=200, items=MODEL_LINES):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        body = {"choices": [{"message": {"content": _fenced(items)}}]}
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setenv("CAIL_OCR_BASE_URL", "https://gateway.test/v1")
    monkeypatch.setenv("CAIL_OCR_API_KEY", "sk-test")
    monkeypatch.setenv("CAIL_OCR_MODEL", "vision-model")
    monkeypatch.setattr(ocr_vlm_plugin.httpx, "post", post)
    return calls


def test_rendered_text_layer_reads_every_line_once_in_order(tmp_path, monkeypatch):
    _answer(monkeypatch)
    page, text = GatewayVisionOcrEngine.generate_ocr(_page_image(tmp_path), options=None)
    assert text.split("\n") == [
        "Dear Sir:-",
        "Indiana County Progressives",
        "send you congratulations",
        "I am,",
    ]
    output = tmp_path / "layer.pdf"
    Fpdf2PdfRenderer(page=page, dpi=page.dpi, multi_font_manager=MultiFontManager()).render(output)

    lines = [
        line
        for pdf_page in extract_pages(output)
        for box in pdf_page
        if isinstance(box, LTTextContainer)
        for line in box
        if isinstance(line, LTTextLine)
    ]
    lines.sort(key=lambda line: -line.y1)
    assert [" ".join(line.get_text().split()) for line in lines] == text.split("\n")
    # 1000 x 800 px at 100 dpi is a 576 pt tall page. Each line is centered
    # where the model boxed it: "Dear Sir:-" 12.5% down, "I am," 42.5% down.
    page_height = 576
    for line, fraction_down in ((lines[0], 0.125), (lines[-1], 0.425)):
        center = (line.y0 + line.y1) / 2
        assert abs(center - page_height * (1 - fraction_down)) < page_height * 0.02


def test_a_failed_gateway_call_is_not_repeated(tmp_path, monkeypatch):
    calls = _answer(monkeypatch, status=429)
    with pytest.raises(GatewayOcrError, match="429"):
        GatewayVisionOcrEngine.generate_ocr(_page_image(tmp_path), options=None)
    assert len(calls) == 1


def test_the_page_image_is_sent_within_the_size_limit(tmp_path, monkeypatch):
    calls = _answer(monkeypatch)
    GatewayVisionOcrEngine.generate_ocr(_page_image(tmp_path, size=(3200, 4000)), options=None)
    image_url = calls[0]["messages"][1]["content"][1]["image_url"]["url"]
    encoded = image_url.split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as sent:
        assert max(sent.size) == ocr_vlm_plugin.MAX_SIDE
