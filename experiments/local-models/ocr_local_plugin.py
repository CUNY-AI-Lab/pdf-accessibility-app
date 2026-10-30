"""Experimental OCRmyPDF engine: a local OCR model's text placed on Tesseract's words.

Mounted over ``app/pipeline/ocr_vlm_plugin.py`` for bench runs
(``run_pipeline_bench.sh --ocr-plugin``), so the OCR step loads it exactly as
it loads the Gateway engine. Connection settings come from the OCR step as
for the Gateway engine (CAIL_OCR_BASE_URL, CAIL_OCR_HEADERS, CAIL_OCR_MODEL,
CAIL_OCR_PAGE_SECONDS). The model is asked with olmOCR's own prompt and
image size, and returns plain text without positions; positions come from
Tesseract, which reads every page as well:

- ``LOCAL_OCR_MODE=tesseract``: Tesseract alone, through this engine (control).
- ``LOCAL_OCR_MODE=model``: all of the model's words. The model's words and
  Tesseract's are aligned (edit distance, in order); each model word takes the
  box of the Tesseract word it aligns with, a model word with no counterpart
  joins its predecessor's box, and Tesseract words with no counterpart go.
- ``LOCAL_OCR_MODE=guard``: Tesseract's words, each replaced by the model word
  aligned with it when Tesseract was unsure of it (confidence below
  LOCAL_OCR_GUARD_CONF) or the two agree closely (similarity at least
  LOCAL_OCR_GUARD_SIM). Model words with no Tesseract word are dropped.

Each page's Tesseract words, model answer and decisions are written to
LOCAL_OCR_SIDECAR/<BENCH_PAGE>.json.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from ocrmypdf import hookimpl
from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine
from ocrmypdf.hocrtransform import BoundingBox, HocrParser, OcrClass, OcrElement
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence
from PIL import Image
from rapidfuzz.distance import Levenshtein
from rapidfuzz.process import cdist

if TYPE_CHECKING:
    from ocrmypdf._options import OcrOptions

logger = logging.getLogger(__name__)

# olmOCR's own request: its prompt, a PNG whose long side is 1288 pixels, and
# its retry temperatures for an answer that is cut off.
OLMOCR_PROMPT = (
    "Attached is one page of a document that you must process. "
    "Just return the plain text representation of this document as if you were reading it "
    "naturally. Convert equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for "
    "the primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram "
    "parameters."
)
LONG_SIDE = 1288
MAX_TOKENS = 8000
TEMPERATURES = (0.1, 0.2, 0.3)
GAP = 0.6


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def png_page(image: Image.Image) -> str:
    scaled = image.convert("RGB")
    scale = LONG_SIDE / max(scaled.size)
    scaled = scaled.resize(
        (round(scaled.width * scale), round(scaled.height * scale)), Image.Resampling.LANCZOS
    )
    buffer = io.BytesIO()
    scaled.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def model_answer(image: Image.Image, record: dict) -> str:
    """olmOCR's natural text for the page, or raise."""
    base_url, headers = env("CAIL_OCR_BASE_URL"), json.loads(env("CAIL_OCR_HEADERS", "{}"))
    headers["X-Bench-Page"] = env("BENCH_PAGE")
    deadline = time.monotonic() + float(env("CAIL_OCR_PAGE_SECONDS", "1500"))
    encoded = png_page(image)
    attempts = record.setdefault("attempts", [])
    for temperature in TEMPERATURES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        started = time.monotonic()
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": env("CAIL_OCR_MODEL"),
                "temperature": temperature,
                "max_tokens": MAX_TOKENS,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": OLMOCR_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{encoded}"},
                            },
                        ],
                    }
                ],
            },
            timeout=remaining,
        )
        attempt = {"temperature": temperature, "status": response.status_code,
                   "seconds": round(time.monotonic() - started, 1)}
        attempts.append(attempt)
        if response.status_code != 200:
            continue
        payload = response.json()
        choice = payload["choices"][0]
        content = choice["message"]["content"] or ""
        attempt.update(finish=choice.get("finish_reason"), usage=payload.get("usage"),
                       timing=payload.get("timing"))
        record["model_raw"] = content
        if choice.get("finish_reason") == "stop":
            return natural_text(content)
    raise RuntimeError(f"no usable answer: {attempts}")


def natural_text(content: str) -> str:
    """The page text of an olmOCR answer: front matter and Markdown markup
    removed, table cells as text."""
    text = content.strip()
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if match:
        text = text[match.end():]
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"</t[dh]>", " ", text)
    text = re.sub(r"</tr>|<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = []
    for line in text.splitlines():
        if re.fullmatch(r"\s*([-*_])\1{2,}\s*", line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", line)
        line = re.sub(r"\\([\\`*_{}\[\]()#+\-.!])", r"\1", line)
        lines.append(line)
    return "\n".join(lines).strip()


def fold(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    letters = "".join(c for c in decomposed if c.isalnum() and not unicodedata.combining(c))
    return letters.lower() or word


def align(ocr_words: list[str], model_words: list[str]) -> list[tuple[int | None, int | None]]:
    """Global edit-distance alignment of two word sequences: pairs (i, j),
    with None on the side that has no counterpart."""
    n, m = len(ocr_words), len(model_words)
    if not n or not m:
        return [(i, None) for i in range(n)] + [(None, j) for j in range(m)]
    cost = cdist([fold(w) for w in ocr_words], [fold(w) for w in model_words],
                 scorer=Levenshtein.normalized_distance)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = i * GAP
    for j in range(1, m + 1):
        score[0][j] = j * GAP
    for i in range(1, n + 1):
        row, prev, costs = score[i], score[i - 1], cost[i - 1]
        for j in range(1, m + 1):
            row[j] = min(prev[j - 1] + costs[j - 1], prev[j] + GAP, row[j - 1] + GAP)
    pairs = []
    i, j = n, m
    while i or j:
        if i and j and abs(score[i][j] - (score[i - 1][j - 1] + cost[i - 1][j - 1])) < 1e-9:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and abs(score[i][j] - (score[i - 1][j] + GAP)) < 1e-9:
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return pairs


def words_of(element: OcrElement) -> list[OcrElement]:
    if element.ocr_class == OcrClass.WORD:
        return [element]
    found = []
    for child in element.children or []:
        found.extend(words_of(child))
    return found


def prune(element: OcrElement) -> bool:
    """Drop words left without text and containers left without words; true
    when ``element`` keeps something."""
    if element.ocr_class == OcrClass.WORD:
        return bool((element.text or "").strip())
    element.children = [child for child in (element.children or []) if prune(child)]
    if element.ocr_class in OcrClass.LINE_TYPES:
        element.text = " ".join(word.text for word in words_of(element))
    return bool(element.children)


def page_text(page: OcrElement) -> str:
    lines = []

    def walk(element: OcrElement) -> None:
        if element.ocr_class in OcrClass.LINE_TYPES:
            lines.append(" ".join(word.text for word in words_of(element)))
            return
        for child in element.children or []:
            walk(child)

    walk(page)
    return "\n".join(lines)


def spread_lines(text: str, page: OcrElement) -> OcrElement:
    """Model lines laid evenly down the page, for a page where Tesseract found
    no words to place them on."""
    box = page.bbox
    lines = [line for line in text.splitlines() if line.strip()]
    top, height = box.top + 0.05 * (box.bottom - box.top), 0.9 * (box.bottom - box.top)
    left, right = box.left + 0.05 * (box.right - box.left), box.right - 0.05 * (box.right - box.left)
    step = height / max(len(lines), 1)
    children = []
    for index, line in enumerate(lines):
        y0, y1 = top + index * step, top + (index + 0.8) * step
        words = line.split()
        unit = (right - left) / max(sum(len(w) for w in words) + len(words) - 1, 1)
        x, elements = left, []
        for word in words:
            elements.append(OcrElement(ocr_class=OcrClass.WORD, text=word, bbox=BoundingBox(
                left=x, top=y0, right=min(x + len(word) * unit, right), bottom=y1)))
            x += (len(word) + 1) * unit
        children.append(OcrElement(ocr_class=OcrClass.LINE, text=line, children=elements,
                                   bbox=BoundingBox(left=left, top=y0, right=right, bottom=y1)))
    page.children = children
    return page


def combine(page: OcrElement, text: str, mode: str, record: dict) -> OcrElement:
    words = words_of(page)
    model_words = text.split()
    pairs = align([w.text for w in words], model_words)
    conf_limit = float(env("LOCAL_OCR_GUARD_CONF", "0.6"))
    sim_limit = float(env("LOCAL_OCR_GUARD_SIM", "0.75"))
    counts = {"pairs": 0, "accepted": 0, "kept": 0, "ocr_only": 0, "model_only": 0}
    dropped = []
    if mode == "model":
        if not words:
            record["placement"] = "spread"
            return spread_lines(text, page)
        new_text: list[str | None] = [None] * len(words)
        pending: list[str] = []  # model words before the first aligned pair
        last = None
        for i, j in pairs:
            if i is not None and j is not None:
                counts["pairs"] += 1
                new_text[i] = " ".join([*pending, model_words[j]])
                pending = []
                last = i
            elif j is not None:
                counts["model_only"] += 1
                if last is None:
                    pending.append(model_words[j])
                else:
                    new_text[last] += " " + model_words[j]
            else:
                counts["ocr_only"] += 1
        if pending:  # no word aligned at all
            new_text[0] = " ".join(pending)
        for word, value in zip(words, new_text, strict=True):
            word.text = value or ""
    else:
        for i, j in pairs:
            if i is not None and j is not None:
                counts["pairs"] += 1
                word = words[i]
                similarity = 1 - Levenshtein.normalized_distance(fold(word.text),
                                                                 fold(model_words[j]))
                confidence = word.confidence if word.confidence is not None else 1.0
                if confidence < conf_limit or similarity >= sim_limit:
                    if word.text != model_words[j]:
                        counts["accepted"] += 1
                    word.text = model_words[j]
                else:
                    counts["kept"] += 1
            elif j is not None:
                counts["model_only"] += 1
                dropped.append(model_words[j])
            else:
                counts["ocr_only"] += 1
        record["dropped_model_words"] = " ".join(dropped)
    record["counts"] = counts
    prune(page)
    return page


def tesseract_page(input_file: Path, options: OcrOptions) -> OcrElement:
    """Tesseract's words for the page image, cached by the image's bytes."""
    cache = Path(env("LOCAL_OCR_TESS_CACHE", "/cache/ocr-tesseract"))
    key = hashlib.sha256(Path(input_file).read_bytes()).hexdigest()
    hocr = cache / f"{key}.hocr"
    if not hocr.exists():
        with tempfile.TemporaryDirectory() as tmp:
            out, txt = Path(tmp) / "page.hocr", Path(tmp) / "page.txt"
            TesseractOcrEngine.generate_hocr(input_file, out, txt, options)
            cache.mkdir(parents=True, exist_ok=True)
            partial = hocr.with_suffix(f".{os.getpid()}.tmp")
            partial.write_bytes(out.read_bytes())
            partial.replace(hocr)
    return HocrParser(hocr).parse()


class LocalModelOcrEngine(OcrEngine):
    @staticmethod
    def version() -> str:
        return env("CAIL_OCR_MODEL")

    @staticmethod
    def creator_tag(options: OcrOptions) -> str:
        return f"Local OCR experiment ({env('CAIL_OCR_MODEL')}, {env('LOCAL_OCR_MODE')})"

    def __str__(self) -> str:
        return "Local model OCR experiment"

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
    def generate_ocr(input_file: Path, options: OcrOptions, page_number: int = 0):
        mode = env("LOCAL_OCR_MODE", "guard")
        record: dict = {"page": env("BENCH_PAGE"), "mode": mode}
        started = time.monotonic()
        page = tesseract_page(input_file, options)
        record["tesseract_seconds"] = round(time.monotonic() - started, 1)
        words = words_of(page)
        record["tesseract_words"] = [
            [w.text, w.confidence, [round(v) for v in (w.bbox.left, w.bbox.top, w.bbox.right,
                                                       w.bbox.bottom)]]
            for w in words
        ]
        record["tesseract_text"] = page_text(page)
        if mode != "tesseract":
            with Image.open(input_file) as image:
                record["image_size"] = list(image.size)
                try:
                    started = time.monotonic()
                    text = model_answer(image, record)
                    record["model_seconds"] = round(time.monotonic() - started, 1)
                    record["model_text"] = text
                    page = combine(page, text, mode, record)
                except Exception as exc:  # noqa: BLE001 - the page keeps Tesseract's text
                    record["error"] = repr(exc)
                    logger.warning("page %s: local OCR failed (%s); using Tesseract",
                                   page_number + 1, exc)
        record["output_text"] = page_text(page)
        if sidecar := env("LOCAL_OCR_SIDECAR"):
            path = Path(sidecar) / f"{env('BENCH_PAGE') or 'page'}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=1))
        return page, record["output_text"]

    @staticmethod
    def generate_hocr(input_file, output_hocr, output_text, options) -> None:
        raise NotImplementedError("Use generate_ocr")

    @staticmethod
    def generate_pdf(input_file, output_pdf, output_text, options) -> None:
        raise NotImplementedError("Use generate_ocr")


@hookimpl
def get_ocr_engine(options):
    return LocalModelOcrEngine()
