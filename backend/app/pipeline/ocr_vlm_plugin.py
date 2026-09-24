"""OCRmyPDF engine plugin: recognize text with a vision model on the CAIL Gateway.

The OCR step loads it with ``--plugin app.pipeline.ocr_vlm_plugin`` when
``OCR_ENGINE=gateway``. Each page image is sent to the Gateway's
OpenAI-compatible chat endpoint, and the vision model returns every text line
with a bounding box (normalized 0-1000). The lines become an
``OcrElement`` tree and OCRmyPDF writes the invisible text layer as usual, so
Docling, the tagger, and validation are unchanged. Word boxes are laid out
across each line in proportion to word length: the renderer needs words, and a
screen reader needs only the line's text and position.

Page orientation and skew still come from Tesseract.

The OCR step passes the connection in the environment:
    CAIL_OCR_BASE_URL   Gateway base URL
    CAIL_OCR_API_KEY    Gateway credential
    CAIL_OCR_MODEL      vision model
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from ocrmypdf import hookimpl
from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine
from ocrmypdf.hocrtransform import BoundingBox, OcrClass, OcrElement
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence
from PIL import Image

if TYPE_CHECKING:
    from ocrmypdf._options import OcrOptions

# The long side sent to the model; larger images exceed provider body limits.
MAX_SIDE = 1600
REQUEST_TIMEOUT_SECONDS = 180

SYSTEM_PROMPT = (
    "You read scanned document pages. Return every line of text on the page as "
    "JSON, in natural reading order (top to bottom; column by column for "
    "multi-column layouts). Output a JSON array only, no prose. Each item is "
    '{"bbox_2d": [x1, y1, x2, y2], "text": "..."} where the box tightly '
    "encloses that one line and the text is exactly as written: keep original "
    "spelling, capitalization, and punctuation; do not correct or modernize. "
    "Include headings, handwritten text, marginal notes, captions, and "
    "footnotes. One item per printed or handwritten line."
)


class GatewayOcrError(RuntimeError):
    """The Gateway did not return usable lines for a page."""


def _encoded_page(image: Image.Image) -> str:
    scaled = image.convert("RGB")
    scaled.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    scaled.save(buffer, format="JPEG", quality=88)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _request_lines(image: Image.Image) -> list[dict]:
    base_url, api_key, model = (
        os.environ.get(name, "").strip()
        for name in ("CAIL_OCR_BASE_URL", "CAIL_OCR_API_KEY", "CAIL_OCR_MODEL")
    )
    if not (base_url and api_key and model):
        raise GatewayOcrError("CAIL_OCR_BASE_URL, CAIL_OCR_API_KEY and CAIL_OCR_MODEL are required")
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Return the lines of this page."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{_encoded_page(image)}"},
                    },
                ],
            },
        ],
    }
    # One attempt: the Gateway may already have charged for a request that
    # failed afterwards, so a paid call is never repeated.
    response = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise GatewayOcrError(f"Gateway returned {response.status_code}")
    content = response.json()["choices"][0]["message"]["content"] or ""
    return parse_lines(content)


def parse_lines(content: str) -> list[dict]:
    """The model's line list, tolerating a Markdown code fence around it."""
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip())
    items = json.loads(text)
    if not isinstance(items, list):
        raise GatewayOcrError("model did not return a list of lines")
    lines = []
    for item in items:
        box = item.get("bbox_2d") if isinstance(item, dict) else None
        line_text = item.get("text") if isinstance(item, dict) else None
        if (
            isinstance(box, list)
            and len(box) == 4
            and all(isinstance(value, int | float) for value in box)
            and isinstance(line_text, str)
            and line_text.strip()
        ):
            lines.append({"bbox": [float(value) for value in box], "text": line_text})
    return lines


def _word_elements(
    text: str, left: float, top: float, right: float, bottom: float
) -> list[OcrElement]:
    """Words spread across the line box in proportion to their length."""
    words = text.split()
    if not words:
        return []
    units = sum(len(word) for word in words) + (len(words) - 1)
    step = (right - left) / max(units, 1)
    elements = []
    x = left
    for word in words:
        width = len(word) * step
        elements.append(
            OcrElement(
                ocr_class=OcrClass.WORD,
                text=word,
                bbox=BoundingBox(left=x, top=top, right=min(x + width, right), bottom=bottom),
            )
        )
        x += width + step
    return elements


def page_from_lines(
    lines: list[dict], width: int, height: int, dpi: float | None, page_number: int
) -> OcrElement:
    """An OcrElement page from normalized (0-1000) line boxes."""
    line_elements = []
    for line in lines:
        x1, y1, x2, y2 = line["bbox"]
        left, right = sorted((x1 * width / 1000, x2 * width / 1000))
        top, bottom = sorted((y1 * height / 1000, y2 * height / 1000))
        # A model sometimes returns several lines in one item; give each its
        # own slice of the box so none is drawn over another.
        parts = [part for part in line["text"].split("\n") if part.strip()]
        slice_height = (bottom - top) / max(len(parts), 1)
        for index, part in enumerate(parts):
            part_top = top + index * slice_height
            words = _word_elements(part, left, part_top, right, part_top + slice_height)
            if words:
                line_elements.append(
                    OcrElement(
                        ocr_class=OcrClass.LINE,
                        text=part,
                        bbox=BoundingBox(
                            left=left, top=part_top, right=right, bottom=part_top + slice_height
                        ),
                        children=words,
                    )
                )
    return OcrElement(
        ocr_class=OcrClass.PAGE,
        bbox=BoundingBox(left=0, top=0, right=width, bottom=height),
        dpi=dpi,
        page_number=page_number,
        children=line_elements,
    )


class GatewayVisionOcrEngine(OcrEngine):
    """Text from a Gateway vision model; orientation and skew from Tesseract."""

    @staticmethod
    def version() -> str:
        return os.environ.get("CAIL_OCR_MODEL", "")

    @staticmethod
    def creator_tag(options: OcrOptions) -> str:
        return f"CAIL Gateway OCR ({GatewayVisionOcrEngine.version()})"

    def __str__(self) -> str:
        return "CAIL Gateway vision OCR"

    @staticmethod
    def languages(options: OcrOptions) -> set[str]:
        return TesseractOcrEngine.languages(options)

    @staticmethod
    def get_orientation(input_file: Path, options: OcrOptions) -> OrientationConfidence:
        return TesseractOcrEngine.get_orientation(input_file, options)

    @staticmethod
    def get_deskew(input_file: Path, options: OcrOptions) -> float:
        return TesseractOcrEngine.get_deskew(input_file, options)

    @staticmethod
    def supports_generate_ocr() -> bool:
        return True

    @staticmethod
    def generate_ocr(
        input_file: Path, options: OcrOptions, page_number: int = 0
    ) -> tuple[OcrElement, str]:
        with Image.open(input_file) as image:
            width, height = image.size
            # Without a recorded resolution OCRmyPDF falls back to the page's.
            dpi_info = image.info.get("dpi")
            dpi = (
                float(dpi_info[0] if isinstance(dpi_info, tuple) else dpi_info)
                if dpi_info
                else None
            )
            lines = _request_lines(image)
        page = page_from_lines(lines, width, height, dpi, page_number)
        return page, "\n".join(line.text for line in page.children)

    @staticmethod
    def generate_hocr(input_file, output_hocr, output_text, options) -> None:
        raise NotImplementedError("Use generate_ocr")

    @staticmethod
    def generate_pdf(input_file, output_pdf, output_text, options) -> None:
        raise NotImplementedError("Use generate_ocr")


@hookimpl
def get_ocr_engine(options):
    return GatewayVisionOcrEngine()
