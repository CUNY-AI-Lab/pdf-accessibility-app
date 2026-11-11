"""Document title evidence shared by extraction, AI assistance, and tag writing.

Only first-page typography is authoritative. Structural headings are fallbacks;
their level alone does not make a later section the document's title.
"""

from __future__ import annotations

import ctypes
import math
import re
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from statistics import median
from typing import Any

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as raw

OFFICE_TITLE_PREFIX_RE = re.compile(r"^microsoft\s+(?:office\s+)?(?:word|powerpoint|excel)\s*-", re.I)
FILE_NAME_TITLE_RE = re.compile(r"\.(?:pdf|docx?|pptx?|xlsx?|odt|odp|rtf|txt|indd|pages|key|tex)$", re.I)
PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:untitled(?:[\s_-]*\d+)?|untitled document|document\s*\d*|presentation\s*\d*|title)$", re.I,
)
DATE_OR_CODE_RE = re.compile(
    r"^(?:(?:(?:spring|summer|fall|autumn|winter|term)\s+)?\d{4}(?:\s+term)?|"
    r"(?:[A-Z]{2,6}[- ]?\d{3,5})(?:[- ]?[A-Z0-9]{1,5})?|"
    r"(?:\d{1,2}[/-]){1,2}\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(?:\d{4}|\d{1,2}(?:,?\s+\d{4})?)|\d+\s+credits?)$", re.I,
)
BOILERPLATE_RE = re.compile(
    r"^(?:city university of new york.*|cuny academic works|"
    r".*\bcollege\b.*\b(?:cuny|department)|how does access to this work.*|"
    r"(?:volume|vol\.|nr)\s*\d.*|.*@.*|https?://.*|"
    r"(?:course/section|modality|section|instructor|professor|office hours):.*)$", re.I,
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00ad", "").split())


def _key(value: Any) -> str:
    return "".join(c for c in _clean(value).casefold() if c.isalnum())


def _is_placeholder_document_title(title: str, original_filename: str) -> bool:
    """Keep the #21 junk rules, including file-like names without an extension."""
    text = _clean(title)
    name = Path(original_filename).name.casefold() if original_filename else ""
    return bool(
        not text or OFFICE_TITLE_PREFIX_RE.match(text) or FILE_NAME_TITLE_RE.search(text)
        or PLACEHOLDER_TITLE_RE.fullmatch(text)
        or (name and text.casefold() in {name, Path(name).stem})
        or ("_" in text and not re.search(r"\s", text))
    )


def _eligible(text: str, filename: str) -> bool:
    text = re.sub(r"^(?:course|date):\s*", "", text, flags=re.I)
    return bool(
        3 <= len(text) <= 400 and any(c.isalpha() for c in text)
        and not _is_placeholder_document_title(text, filename)
        and not DATE_OR_CODE_RE.fullmatch(text) and not BOILERPLATE_RE.fullmatch(text)
    )


def _page_lines(page) -> list[dict[str, Any]]:
    """Read PDFium's lines, splitting widely separated columns on the same line.

    Font size includes the text matrix: many Office PDFs use a nominal 1pt
    font and scale it. Glyph heights alone confuse capitals with lowercase.
    """
    lines: list[dict[str, Any]] = []
    with closing(page.get_textpage()) as text_page:
        chars: list[str] = []
        boxes: list[tuple[float, float, float, float]] = []
        sizes: list[float] = []
        weights: list[int] = []
        seen: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
        advances: list[tuple[float, float, float, float]] = []

        def flush() -> None:
            text = _clean("".join(chars))
            if text and boxes:
                lines.append({
                    "text": text, "size": round(median(sizes), 2),
                    "bold": median(weights) >= 600,
                    "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                             max(b[2] for b in boxes), max(b[3] for b in boxes)],
                })
            chars.clear()
            boxes.clear()
            sizes.clear()
            weights.clear()
            advances.clear()

        for i in range(text_page.count_chars()):
            char = chr(raw.FPDFText_GetUnicode(text_page, i))
            if char in {"\r", "\n"}:
                flush()
                continue
            box = text_page.get_charbox(i)
            advance = text_page.get_charbox(i, loose=True)
            if not char.isspace():
                # Synthetic bold sometimes redraws entire substrings. Read
                # each visible glyph once, including across text objects.
                if any(abs(box[0] - b[0]) < .6 and abs(box[1] - b[1]) < .6
                       and abs(box[3] - b[3]) < .6 for b in seen[char]):
                    continue
                seen[char].append(box)
            matrix = raw.FS_MATRIX()
            raw.FPDFText_GetMatrix(text_page, i, matrix)
            size = raw.FPDFText_GetFontSize(text_page, i) * math.hypot(matrix.c, matrix.d)
            if (char.isdigit() and sizes and size < .75 * median(sizes)
                    and box[1] - boxes[-1][1] > .25 * median(sizes)):
                continue  # A raised, smaller footnote marker is not title text.
            if boxes and not char.isspace() and box[0] - boxes[-1][2] > 2 * max(size, 1):
                flush()
            if char.isspace() and raw.FPDFText_IsGenerated(text_page, i):
                continue
            if (advances and char.isalnum() and chars and not chars[-1].isspace()
                    and advance[0] - advances[-1][2] > .15 * size):
                chars.append(" ")
            chars.append(char)
            if char.isalnum() and box[3] > box[1] and size > 0:
                boxes.append(box)
                advances.append(advance)
                sizes.append(size)
                flags = ctypes.c_int()
                length = raw.FPDFText_GetFontInfo(text_page, i, None, 0, flags)
                buffer = ctypes.create_string_buffer(length)
                raw.FPDFText_GetFontInfo(text_page, i, buffer, length, flags)
                bold = b"bold" in buffer.value.lower()
                weights.append(max(raw.FPDFText_GetFontWeight(text_page, i), 700 if bold else 0))
        flush()
    return lines


def _first_page_evidence(pdf_path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        with pdfium.PdfDocument(pdf_path) as document:
            if not len(document):
                return [], ""
            page = document[0]
            height = page.get_height()
            lines = _page_lines(page)
            repeated: set[str] = set()
            for index in range(1, min(3, len(document))):
                other = document[index]
                repeated.update(
                    _key(line["text"]) for line in _page_lines(other)
                    if line["bbox"][3] > other.get_height() * .88
                )
            for line in lines:
                line["top_fraction"] = 1 - line["bbox"][3] / height
                line["running_head"] = (
                    line["top_fraction"] < .12 and _key(line["text"]) in repeated
                )
            return lines, " ".join(line["text"] for line in lines)
    except Exception:
        # Broken or absent text must not prevent the metadata/H1 fallback.
        return [], ""


def _prominent_block(lines: list[dict[str, Any]], filename: str) -> dict[str, Any] | None:
    candidates = [line for line in lines if line["top_fraction"] < .55
                  and not line["running_head"] and _eligible(line["text"], "")]
    if not candidates:
        return None
    # A modest bold advantage resolves uniform-size syllabus title blocks.
    best = max(candidates, key=lambda line: (line["size"] * (1.08 if line["bold"] else 1),
                                            -line["top_fraction"]))
    block = [best]
    for line in sorted(candidates, key=lambda line: (-line["bbox"][3], line["bbox"][0])):
        previous = block[-1]
        a, b = previous["bbox"], line["bbox"]
        if line is best or b[3] >= a[3]:
            continue
        aligned = abs(a[0] - b[0]) < best["size"] or abs(a[0] + a[2] - b[0] - b[2]) < 2 * best["size"]
        if (aligned and -.2 * best["size"] <= a[1] - b[3] <= 1.5 * best["size"]
                and .95 <= line["size"] / best["size"] <= 1.05
                and line["bold"] == best["bold"]):
            block.append(line)
        if len(block) >= 4:
            break
    text = " ".join(line["text"] for line in block)
    text = re.sub(r"^course:\s*", "", text, flags=re.I)
    # A title must stand out from body type, or be the first remaining text
    # block. Otherwise this is only a candidate for model adjudication.
    body_sizes = [line["size"] for line in lines if line["top_fraction"] > .55]
    first = min(candidates, key=lambda line: line["top_fraction"])
    course_title = bool(re.match(r"^(?:course:|[A-Z]{2,6}[- ]?\d{3,5}\s*[:–-])", best["text"]))
    clear = (best is first and (best["bold"] or course_title)) or bool(
        body_sizes and best["size"] >= 1.2 * median(body_sizes)
    )
    if re.search(r"\b(?:inc\.|ltd\.?|llc|corp\.)\s*$", best["text"], re.I):
        clear = False
    return {"title": text, "source": "first_page", "clear": clear,
            "size": best["size"], "bbox": best["bbox"]}


def select_document_title(
    pdf_path: Path | None, structure: dict[str, Any], original_filename: str = "",
    *, source_pdf: pikepdf.Pdf | None = None,
) -> dict[str, Any]:
    """Rank first-page block, usable XMP/Info, first H1, then real text.

    A structural/AI title can retain a fuller, visibly grounded version of
    the same block. A filename is permitted only when there is no real text.
    """
    candidates: list[dict[str, Any]] = []
    lines, visible = _first_page_evidence(pdf_path) if pdf_path else ([], "")
    furniture = {_key(e.get("text")) for e in structure.get("elements", [])
                 if e.get("type") in {"page_header", "page_footer"}}
    for line in lines:
        line["running_head"] = line["running_head"] or _key(line["text"]) in furniture
    prominent = _prominent_block(lines, original_filename)
    if prominent:
        candidates.append(prominent)
    metadata: list[tuple[str, str]] = []

    def read_metadata(pdf: pikepdf.Pdf) -> None:
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
                metadata.append(("xmp", _clean(meta.get("dc:title"))))
        except Exception:
            pass
        metadata.append(("info", _clean(pdf.docinfo.get("/Title"))))

    if source_pdf is not None:
        read_metadata(source_pdf)
    elif pdf_path:
        try:
            with pikepdf.open(pdf_path) as pdf:
                read_metadata(pdf)
        except Exception:
            pass
    for source, text in metadata:
        if _eligible(text, original_filename):
            candidates.append({"title": text, "source": source, "clear": True})
    elements = structure.get("elements") or []
    if prominent:
        # Fake-bold redraws may leave spurious word breaks in PDFium. An
        # exact alphanumeric match can repair spacing without changing words.
        spellings = [text for _, text in metadata] + [
            _clean(e.get("text")) for e in elements if e.get("page") == 0
        ]
        for text in spellings:
            if _key(text) == _key(prominent["title"]):
                prominent["title"] = text
                break
    for element in elements:
        if element.get("type") == "heading" and element.get("level") == 1:
            text = _clean(element.get("text"))
            if _eligible(text, ""):
                candidates.append({"title": text, "source": "first_h1", "clear": False})
                break
    existing = _clean(structure.get("title"))
    if _eligible(existing, original_filename):
        candidates.append({"title": existing, "source": "structure", "clear": False})
    selected = next((c for c in candidates if c["clear"]), None)
    if selected is prominent and prominent:
        # Retain a longer title only if all its words occur on page one and
        # it contains the chosen block. Never preserve a later section title.
        if (_key(prominent["title"]) in _key(existing)
                and _key(existing) in _key(visible)):
            selected = {**prominent, "title": existing}
    if selected is None:
        selected = next((c for c in candidates if c["source"] != "first_page"), prominent)
    if selected is None:
        text = next((_clean(e.get("text")) for e in elements if _clean(e.get("text"))), "")
        text = text or visible
        selected = {"title": text[:400] if text else Path(original_filename).stem.replace("_", " ").replace("-", " "),
                    "source": "text" if text else "filename", "clear": False}
    return {**selected, "title": selected["title"] or "Untitled Document", "candidates": candidates}
