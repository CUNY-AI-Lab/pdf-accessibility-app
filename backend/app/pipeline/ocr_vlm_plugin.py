"""OCRmyPDF engine plugin: recognize text with a vision model on the CAIL Gateway.

The OCR step loads it with ``--plugin app.pipeline.ocr_vlm_plugin`` when
``OCR_ENGINE=gateway``. Each page image is sent to the Gateway's
OpenAI-compatible chat endpoint, and the vision model returns every text line
with a bounding box (normalized 0-1000). The lines become an
``OcrElement`` tree and OCRmyPDF writes the invisible text layer as usual, so
Docling, the tagger, and validation are unchanged. Word boxes are laid out
across each line in proportion to word length: the renderer needs words, and a
screen reader needs only the line's text and position.

Page orientation and skew still come from Tesseract, and so does the text of
any page the Gateway cannot read. A call is repeated unchanged only after 429,
502, 503 or 504, which carry no answer; a timeout on our side is not repeated,
since the Gateway may already have metered an answer that arrived too late. An
answer that is cut off (usually a repetition loop) or has no readable lines is
asked for again at a higher temperature, up to three answers, within a time
limit per page; after that, or on any other failure, the page gets
Tesseract's text.

The OCR step passes the connection and the page time limit in the
environment:
    CAIL_OCR_BASE_URL       Gateway base URL
    CAIL_OCR_API_KEY        Gateway credential
    CAIL_OCR_MODEL          vision model
    CAIL_OCR_PAGE_SECONDS   time for all of a page's Gateway calls
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from ocrmypdf import hookimpl
from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine
from ocrmypdf.hocrtransform import BoundingBox, HocrParser, OcrClass, OcrElement
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence
from PIL import Image

if TYPE_CHECKING:
    from ocrmypdf._options import OcrOptions

logger = logging.getLogger(__name__)

# The long side sent to the model; larger images exceed provider body limits.
MAX_SIDE = 1600
# Well above the densest page measured, so a normal page is never cut short;
# a repetition loop stops here.
MAX_OUTPUT_TOKENS = 12000
# An answer that is cut off or has no readable lines is asked for again at a
# higher temperature, which usually breaks a repetition loop (olmOCR's
# pipeline does the same, from 0.1 up).
TEMPERATURES = (0.1, 0.4, 0.7)
# Statuses that carry no answer, so the same request is sent again.
NO_ANSWER_STATUSES = {429, 502, 503, 504}
RETRY_DELAYS_SECONDS = (5, 20)

SYSTEM_PROMPT = (
    "You read scanned document pages. Return every line of text on the page as "
    "JSON, in natural reading order (top to bottom; column by column for "
    "multi-column layouts). Output a JSON array only, no prose. Each item is "
    '{"bbox_2d": [x1, y1, x2, y2], "text": "..."} where the box tightly '
    "encloses that one line, in coordinates from 0 to 1000 across the page's "
    "width and down its height, and the text is exactly as written: keep original "
    "spelling, capitalization, and punctuation; do not correct or modernize. "
    "Include headings, running heads, page numbers, marginal notes, captions, "
    "and footnotes. One item per printed or handwritten line. Write a row of "
    "dot leaders as a few dots, not every dot. If the page has no text, return []."
)


class GatewayOcrError(RuntimeError):
    """The Gateway did not return usable lines for a page."""


def _encoded_page(image: Image.Image) -> str:
    scaled = image.convert("RGB")
    scaled.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    scaled.save(buffer, format="JPEG", quality=88)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _complete(base_url: str, api_key: str, body: dict, deadline: float) -> dict:
    """One answer, sending the request again only after a status that carries
    no answer, and giving up at the page's deadline."""
    for delay in (*RETRY_DELAYS_SECONDS, None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayOcrError("page time limit reached")
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=remaining,
        )
        if response.status_code not in NO_ANSWER_STATUSES or delay is None:
            break
        time.sleep(min(delay, max(deadline - time.monotonic(), 0)))
    if response.status_code != 200:
        raise GatewayOcrError(f"Gateway returned {response.status_code}")
    answer = response.json()
    logger.info("Gateway OCR usage: %s", answer.get("usage"))
    return answer["choices"][0]


def _request_lines(image: Image.Image) -> list[dict]:
    base_url, api_key, model, page_seconds = (
        os.environ.get(name, "").strip()
        for name in (
            "CAIL_OCR_BASE_URL",
            "CAIL_OCR_API_KEY",
            "CAIL_OCR_MODEL",
            "CAIL_OCR_PAGE_SECONDS",
        )
    )
    if not (base_url and api_key and model and page_seconds):
        raise GatewayOcrError(
            "CAIL_OCR_BASE_URL, CAIL_OCR_API_KEY, CAIL_OCR_MODEL and "
            "CAIL_OCR_PAGE_SECONDS are required"
        )
    messages = [
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
    ]
    problems = []
    # A non-streamed answer arrives all at once, so one call may use all of it.
    deadline = time.monotonic() + float(page_seconds)
    for temperature in TEMPERATURES:
        body = {
            "model": model,
            "temperature": temperature,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": messages,
        }
        choice = _complete(base_url, api_key, body, deadline)
        content = choice["message"]["content"] or ""
        finish = choice.get("finish_reason")
        lines = parse_lines(content)
        if finish == "stop" and (lines or _is_empty_array(content)):
            return lines
        problems.append(f"{temperature}: finish_reason {finish}, {len(lines)} lines")
    raise GatewayOcrError("no usable answer (" + "; ".join(problems) + ")")


def _is_empty_array(content: str) -> bool:
    return re.sub(r"```(?:json)?|\s", "", content) == "[]"


def parse_lines(content: str) -> list[dict]:
    """Every readable line item in the answer. Items are decoded one at a
    time, so a malformed item or a cut-off answer loses only the items it
    touches."""
    decoder = json.JSONDecoder()
    lines = []
    position = content.find("{")
    while position != -1:
        try:
            item, end = decoder.raw_decode(content, position)
        except json.JSONDecodeError:
            position = content.find("{", position + 1)
            continue
        position = content.find("{", end)
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


def _tesseract_page(input_file: Path, options: OcrOptions) -> tuple[OcrElement, str]:
    with tempfile.TemporaryDirectory() as tmp:
        hocr, text = Path(tmp) / "page.hocr", Path(tmp) / "page.txt"
        TesseractOcrEngine.generate_hocr(input_file, hocr, text, options)
        return HocrParser(hocr).parse(), text.read_text(encoding="utf-8")


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
            try:
                lines = _request_lines(image)
            except Exception as exc:  # noqa: BLE001 - any failure falls back to Tesseract
                logger.warning(
                    "page %s: Gateway OCR failed (%s); using Tesseract", page_number + 1, exc
                )
                return _tesseract_page(input_file, options)
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
