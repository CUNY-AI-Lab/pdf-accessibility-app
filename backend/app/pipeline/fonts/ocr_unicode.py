"""Recover Unicode for glyphs whose fonts carry none, from OCR of the page.

Some fonts say nothing about what their glyphs are: an empty ToUnicode map,
no glyph names, codes numbered in drawing order (33, 34, ...), as some
print-to-PDF drivers write them. Only the glyph shapes remain. This lane
renders each page that draws such a glyph, reads it with Tesseract (hOCR with
character boxes), and lets every occurrence of a code vote for the text OCR
read at its position. A code is mapped only when its occurrences agree;
otherwise its font gets no mapping for it and the document stays held.

PDFix's Font Fix recovers unmapped glyphs with OCR one glyph at a time.
Reading whole pages keeps word context, which settles look-alikes (l, I, 1;
o, O, 0) that a lone glyph cannot. A glyph that draws nothing maps to a
space, or to a zero-width space when it does not advance. One outline in
several subsets of the same font is one character, so its occurrences vote
together.
"""

from __future__ import annotations

import hashlib
import logging
import math
import subprocess
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import pikepdf
import pypdfium2 as pdfium
from fontTools.ttLib import TTFont
from pdfminer.converter import PDFLayoutAnalyzer
from pdfminer.layout import LTChar
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser

from app.config import get_settings
from app.pipeline.fonts.encoding import (
    _collect_gid_to_unicode,
    _is_valid_unicode_text,
    _parse_tounicode_map,
    _render_tounicode_cmap,
    _simple_font_auto_unicode_policy,
    _simple_font_unicode_map,
)
from app.pipeline.fonts.resources import _font_stream_bytes, _resolve_dictionary
from app.pipeline.fonts.symbols import _symbol_family
from app.pipeline.page_frame import PageFrame
from app.pipeline.pdf_repair import pdfminer_readable
from app.services.runtime_paths import enriched_subprocess_env, resolve_binary
from app.services.thread_work import to_thread

logger = logging.getLogger(__name__)

OCR_DPI = 300
# A page rendered for OCR stays under this many pixels (a poster drops dpi).
MAX_RENDER_PIXELS = 60_000_000
# A code maps when its leading reading has at least this share of its
# readings, at least two of them, and LEAD_RATIO times the runner-up; a code
# seen once needs MIN_SINGLE_CONFIDENCE.
MIN_AGREEMENT = 0.6
LEAD_RATIO = 3
MIN_SINGLE_CONFIDENCE = 90.0
TESSERACT_TIMEOUT_SECONDS = 180
ZERO_WIDTH_SPACE = "\u200b"
SIMPLE_SUBTYPES = ("/TrueType", "/Type1", "/MMType1")


@dataclass
class _Target:
    """A font whose used codes need Unicode it does not provide."""

    font: pikepdf.Dictionary
    code_bytes: int
    known: dict[int, str]
    program: bytes | None
    descendant: pikepdf.Dictionary | None = None
    used: set[int] = field(default_factory=set)
    parsed: TTFont | bool | None = None  # the parsed TrueType program; False if unreadable


@dataclass(frozen=True)
class _OcrChar:
    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.left + self.right) / 2, (self.top + self.bottom) / 2


def _valid_entries(font: pikepdf.Dictionary) -> dict[int, str]:
    stream = font.get("/ToUnicode")
    if not isinstance(stream, pikepdf.Stream):
        return {}
    try:
        mapping = _parse_tounicode_map(stream)
    except Exception:
        return {}
    return {code: text for code, text in mapping.items() if _is_valid_unicode_text(text)}


def _cid_to_gid(descendant: pikepdf.Dictionary) -> dict[int, int] | None:
    """The CIDToGIDMap as a table, or None for /Identity (or absent)."""
    value = descendant.get("/CIDToGIDMap")
    if isinstance(value, pikepdf.Stream):
        data = value.read_bytes()
        return {cid: int.from_bytes(data[2 * cid: 2 * cid + 2], "big") for cid in range(len(data) // 2)}
    return None


def _target_for(font: pikepdf.Dictionary) -> _Target | None:
    if _symbol_family(font):
        return None  # OCR reads a dingbat as the nearest letter or mark.
    subtype = font.get("/Subtype")
    if subtype in tuple(pikepdf.Name(s) for s in SIMPLE_SUBTYPES):
        descriptor = _resolve_dictionary(font.get("/FontDescriptor"))
        program = _font_stream_bytes(descriptor) if descriptor is not None else None
        if not program:
            return None  # Unembedded: the page shows a substitute's shapes.
        known = _valid_entries(font)
        if _simple_font_auto_unicode_policy(font, program) != "blocked":
            try:
                derived = _simple_font_unicode_map(font, program)
            except Exception:
                derived = {}
            known = {**derived, **known}
        return _Target(font=font, code_bytes=1, known=known, program=program)
    if subtype == pikepdf.Name("/Type0"):
        encoding = font.get("/Encoding")
        if encoding not in (pikepdf.Name("/Identity-H"), pikepdf.Name("/Identity-V")):
            return None  # Codes are CIDs only under Identity encodings.
        descendants = font.get("/DescendantFonts")
        if not isinstance(descendants, pikepdf.Array) or not len(descendants):
            return None
        descendant = _resolve_dictionary(descendants[0])
        descriptor = _resolve_dictionary(descendant.get("/FontDescriptor")) if descendant is not None else None
        program = _font_stream_bytes(descriptor) if descriptor is not None else None
        if descendant is None or not program:
            return None
        known = _valid_entries(font)
        if descendant.get("/Subtype") == pikepdf.Name("/CIDFontType2"):
            try:
                by_gid = _collect_gid_to_unicode(program)
            except Exception:
                by_gid = {}
            table = _cid_to_gid(descendant)
            derived = by_gid if table is None else {cid: by_gid[gid] for cid, gid in table.items() if gid in by_gid}
            known = {**derived, **known}
        return _Target(font=font, code_bytes=2, known=known, program=program, descendant=descendant)
    return None


def _family(font: pikepdf.Dictionary) -> str:
    """The font's name without its subset prefix (ABCDEF+)."""
    name = str(font.get("/BaseFont") or "")
    return name.split("+", 1)[1] if len(name) > 8 and name[7] == "+" else name


class _Fonts(PDFResourceManager):
    """Remembers which PDF object each font came from."""

    def __init__(self) -> None:
        super().__init__()
        self.sources: dict[int, int] = {}

    def get_font(self, objid, spec):
        font = super().get_font(objid, spec)
        if objid is not None:
            self.sources[id(font)] = objid
        return font


class _GlyphRecorder(PDFLayoutAnalyzer):
    """Records (font object, code, user-space box) for every glyph on a page
    that uses a target font; other fonts' glyphs are recorded with object -1
    so that OCR words can be aligned with all the glyphs they cover."""

    def __init__(self, manager: _Fonts, targets: set[int]) -> None:
        super().__init__(manager, pageno=1, laparams=None)
        self.sources = manager.sources
        self.targets = targets
        self.glyphs: list[tuple[int, int, tuple[float, float, float, float]]] = []

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        advance = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        objid = self.sources.get(id(font))
        char = self.cur_item._objs[-1] if self.cur_item._objs else None
        if isinstance(char, LTChar):
            self.glyphs.append((objid if objid in self.targets else -1, int(cid), (char.x0, char.y0, char.x1, char.y1)))
        return advance

    def receive_layout(self, ltpage) -> None:
        return None


def _page_glyphs(pdf_path: Path, targets: set[int]) -> dict[int, list[tuple[int, int, tuple[float, float, float, float]]]]:
    """Each page's glyphs drawn with a target font, by 0-based page index."""
    found: dict[int, list] = {}
    with pdfminer_readable(pdf_path) as readable, readable.open("rb") as handle:
        document = PDFDocument(PDFParser(handle))
        manager = _Fonts()
        for index, page in enumerate(PDFPage.create_pages(document)):
            device = _GlyphRecorder(manager, targets)
            try:
                PDFPageInterpreter(manager, device).process_page(page)
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
                logger.warning("Could not read glyphs on page %s: %s", index + 1, exc)
                continue
            if any(objid != -1 for objid, _, _ in device.glyphs):
                found[index] = device.glyphs
    return found


def _title_box(title: str, key: str) -> list[float] | None:
    for part in title.split(";"):
        fields = part.split()
        if fields[:1] == [key] and len(fields) == 5:
            return [float(v) for v in fields[1:]]
    return None


def _title_number(title: str, key: str) -> float | None:
    for part in title.split(";"):
        fields = part.split()
        if fields[:1] == [key] and len(fields) == 2:
            return float(fields[1])
    return None


def _parse_hocr_words(hocr: bytes) -> list[_OcrChar]:
    """Each OCR word as text with its box and confidence."""
    words: list[_OcrChar] = []
    for element in ElementTree.fromstring(hocr).iter():
        if "ocrx_word" not in (element.get("class") or ""):
            continue
        # hOCR indents the character spans inside a word; that whitespace is markup.
        text = unicodedata.normalize("NFC", "".join(piece.strip() for piece in element.itertext()))
        title = element.get("title") or ""
        box = _title_box(title, "bbox")
        if text and box is not None and not any(ch.isspace() for ch in text):
            words.append(_OcrChar(text, box[0], box[1], box[2], box[3], _title_number(title, "x_wconf") or 0.0))
    return words


def _parse_hocr_chars(hocr: bytes) -> list[_OcrChar]:
    chars: list[_OcrChar] = []
    root = ElementTree.fromstring(hocr)
    for element in root.iter():
        if "ocrx_cinfo" not in (element.get("class") or ""):
            continue
        text = unicodedata.normalize("NFC", "".join(element.itertext())).strip()
        title = element.get("title") or ""
        box = confidence = None
        for part in title.split(";"):
            fields = part.split()
            if fields[:1] == ["x_bboxes"] and len(fields) == 5:
                box = [float(v) for v in fields[1:]]
            elif fields[:1] == ["x_conf"] and len(fields) == 2:
                confidence = float(fields[1])
        if text and box is not None:
            chars.append(_OcrChar(text, box[0], box[1], box[2], box[3], confidence if confidence is not None else 0.0))
    return chars


def _ocr_page(page: pdfium.PdfPage, languages: str, tesseract: str) -> tuple[list[_OcrChar], list[_OcrChar], float, PageFrame, object]:
    frame = PageFrame.of(page)
    width, height = page.get_size()
    scale = min(OCR_DPI / 72, math.sqrt(MAX_RENDER_PIXELS / max(width * height, 1.0)))
    image = page.render(scale=scale).to_pil()
    with tempfile.TemporaryDirectory(prefix="ocr-unicode-") as tmp:
        png = Path(tmp) / "page.png"
        image.save(png)
        base = Path(tmp) / "page"
        subprocess.run(
            [tesseract, str(png), str(base), "-l", languages, "--psm", "3", "-c", "hocr_char_boxes=1", "hocr"],
            check=True,
            capture_output=True,
            timeout=TESSERACT_TIMEOUT_SECONDS,
            env=enriched_subprocess_env(),
        )
        hocr = base.with_suffix(".hocr").read_bytes()
    return _parse_hocr_chars(hocr), _parse_hocr_words(hocr), scale, frame, image.convert("L")


def _image_box(frame: PageFrame, box: tuple[float, float, float, float], scale: float) -> tuple[float, float, float, float]:
    """A user-space box as (left, top, right, bottom) pixels of the rendered page."""
    shown = frame.to_docling({"l": box[0], "b": box[1], "r": box[2], "t": box[3]})
    height = (frame.top - frame.bottom) if frame.rotation in (0, 180) else (frame.right - frame.left)
    return shown["l"] * scale, (height - shown["t"]) * scale, shown["r"] * scale, (height - shown["b"]) * scale


@dataclass(frozen=True)
class _Reading:
    """What OCR read at one occurrence of a code."""

    nearest: str  # the character whose center is nearest the glyph's
    whole: str  # every character centered in the glyph, left to right (ligatures)
    confidence: float


def _read_at(box: tuple[float, float, float, float], rows: dict[int, list[_OcrChar]], row_px: int) -> _Reading | None:
    left, top, right, bottom = box
    pad = (bottom - top) * 0.2
    inside = []
    for row in range(int((top - pad) // row_px), int((bottom + pad) // row_px) + 1):
        for char in rows.get(row, ()):
            x, y = char.center
            if left <= x <= right and top - pad <= y <= bottom + pad:
                inside.append(char)
    if not inside:
        return None
    middle = (left + right) / 2
    nearest = min(inside, key=lambda c: abs(c.center[0] - middle))
    inside.sort(key=lambda c: c.left)
    return _Reading(nearest.text, "".join(c.text for c in inside), nearest.confidence)


def _inked(gray, box: tuple[float, float, float, float]) -> bool:
    """Whether the rendered page has any dark pixels inside the box."""
    left, top, right, bottom = (int(round(v)) for v in box)
    left, top = max(left, 0), max(top, 0)
    right, bottom = min(right, gray.width), min(bottom, gray.height)
    if right <= left or bottom <= top:
        return True
    region = gray.crop((left, top, right, bottom))
    dark = sum(region.histogram()[:128])
    return dark > max(1, 0.002 * (right - left) * (bottom - top))


# Readings OCR confuses within a class: quotes by style, and Tesseract's bar
# for the vertical stroke of I or l in running text.
QUOTE_CLASSES = ({'"', "\u201c", "\u201d"}, {"'", "\u2018", "\u2019"})
VERTICAL_STROKE_LETTERS = {"I", "l"}


def _merge_confusions(votes: Counter[str]) -> Counter[str]:
    merged = Counter(votes)
    for quotes in QUOTE_CLASSES:
        present = [q for q in quotes if merged.get(q)]
        if len(present) > 1:
            keep = max(present, key=lambda q: merged[q])
            for q in present:
                if q != keep:
                    merged[keep] += merged.pop(q)
    letters = [letter for letter in VERTICAL_STROKE_LETTERS if merged.get(letter)]
    if merged.get("|") and len(letters) == 1:
        merged[letters[0]] += merged.pop("|")
    return merged


def _leader(votes: Counter[str]) -> tuple[str, float] | None:
    """The leading reading and its share, if it leads clearly."""
    votes = _merge_confusions(votes)
    if not votes:
        return None
    ranked = votes.most_common(2)
    text, count = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    share = count / sum(votes.values())
    if count >= 2 and share >= MIN_AGREEMENT and count >= LEAD_RATIO * runner_up:
        return text, share
    return None


def _decide(readings: list[_Reading], unread: int) -> tuple[str, float] | None:
    """The code's text and the share of readings behind it, or None."""
    if not readings:
        return None
    # A ligature reads as several characters every time; prefer that only
    # when it is the consistent whole reading.
    whole = _leader(Counter(r.whole for r in readings))
    decided = whole if whole is not None and len(whole[0]) > 1 else _leader(Counter(r.nearest for r in readings))
    if decided is None and len(readings) == 1 and unread == 0 and readings[0].confidence >= MIN_SINGLE_CONFIDENCE:
        decided = (readings[0].nearest, 1.0)
    if decided is None:
        return None
    text = decided[0]
    if any(ch.isspace() for ch in text) or not _is_valid_unicode_text(text):
        return None
    return decided


def _program_font(target: _Target) -> TTFont | None:
    """The target's embedded TrueType program, parsed once; None for others."""
    if target.parsed is None and target.program:
        try:
            font = TTFont(BytesIO(target.program), lazy=True)
            target.parsed = font if "glyf" in font else False
        except Exception:
            target.parsed = False
    return target.parsed if isinstance(target.parsed, TTFont) else None


def _glyph_name(target: _Target, code: int) -> str | None:
    font = _program_font(target)
    if font is None:
        return None
    try:
        if target.descendant is not None:
            table = _cid_to_gid(target.descendant)
            gid = code if table is None else table.get(code)
            order = font.getGlyphOrder()
            return order[gid] if gid is not None and gid < len(order) else None
        for subtable in font["cmap"].tables if "cmap" in font else []:
            cmap = getattr(subtable, "cmap", None) or {}
            name = cmap.get(code) or cmap.get(0xF000 + code)
            if name:
                return name
    except Exception:
        return None
    return None


def _outline(target: _Target, code: int) -> str | None:
    """A digest of the glyph's outline: the same shape in another subset of
    the same font gives the same digest. None for composite or non-TrueType
    glyphs, which keep their own votes."""
    name = _glyph_name(target, code)
    font = _program_font(target)
    if name is None or font is None:
        return None
    try:
        glyph = font["glyf"][name]
        if glyph.isComposite():
            return None
        coordinates, ends, flags = glyph.getCoordinates(font["glyf"])
        shape = (tuple(coordinates), tuple(ends), bytes(f & 1 for f in flags))
        return hashlib.sha256(repr(shape).encode()).hexdigest()[:16]
    except Exception:
        return None


def _blank_glyph_text(target: _Target, code: int) -> str | None:
    """A space for a code whose glyph draws nothing and advances, a zero-width
    space when it does not advance; None if it draws something or cannot be told."""
    name = _glyph_name(target, code)
    font = _program_font(target)
    if name is None or font is None:
        return None
    try:
        if getattr(font["glyf"][name], "numberOfContours", 1) != 0:
            return None
    except Exception:
        return None
    return " " if _advance(target, code) > 0 else ZERO_WIDTH_SPACE


def _advance(target: _Target, code: int) -> float:
    if target.descendant is not None:
        return _cid_width(target.descendant, code)
    return _simple_width(target.font, code)


def _simple_width(font: pikepdf.Dictionary, code: int) -> float:
    widths = font.get("/Widths")
    first = int(font.get("/FirstChar", 0))
    if isinstance(widths, pikepdf.Array) and 0 <= code - first < len(widths):
        return float(widths[code - first])
    return 0.0


def _cid_width(descendant: pikepdf.Dictionary, cid: int) -> float:
    widths = descendant.get("/W")
    if isinstance(widths, pikepdf.Array):
        items = list(widths)
        i = 0
        while i < len(items):
            start = int(items[i])
            if i + 1 < len(items) and isinstance(items[i + 1], pikepdf.Array):
                run = items[i + 1]
                if start <= cid < start + len(run):
                    return float(run[cid - start])
                i += 2
            elif i + 2 < len(items):
                if start <= cid <= int(items[i + 1]):
                    return float(items[i + 2])
                i += 3
            else:
                break
    return float(descendant.get("/DW", 1000))


def _recover_sync(input_path: Path, output_path: Path, languages: str, max_pages: int) -> tuple[bool, str, dict[str, object]]:
    stats: dict[str, object] = {
        "fonts_targeted": 0, "codes_targeted": 0, "codes_mapped": 0, "codes_unresolved": 0,
        "pages_read": 0, "unresolved_examples": [],
    }
    tesseract = resolve_binary("tesseract", explicit=get_settings().tesseract_path)
    if not tesseract:
        return False, "Tesseract is not available for OCR font mapping", stats
    with pikepdf.open(input_path) as pdf:
        candidates: dict[int, _Target] = {}
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == pikepdf.Name("/Font") and obj.objgen != (0, 0):
                target = _target_for(obj)
                if target is not None:
                    candidates[obj.objgen[0]] = target
        if not candidates:
            return False, "No embedded fonts the OCR mapping can repair", stats
        page_glyphs = _page_glyphs(input_path, set(candidates))
        for glyphs in page_glyphs.values():
            for objid, code, _ in glyphs:
                if objid != -1:
                    candidates[objid].used.add(code)
        targets = {objid: t for objid, t in candidates.items() if t.used - set(t.known)}
        if not targets:
            return False, "Every used code already has Unicode", stats
        missing = {objid: t.used - set(t.known) for objid, t in targets.items()}
        stats["fonts_targeted"] = len(targets)
        stats["codes_targeted"] = sum(len(codes) for codes in missing.values())
        pages = sorted(
            index for index, glyphs in page_glyphs.items()
            if any(objid in missing and code in missing[objid] for objid, code, _ in glyphs)
        )
        if len(pages) > max_pages:
            return False, f"OCR font mapping skipped: {len(pages)} pages to read > limit {max_pages}", stats

        readings: dict[tuple[int, int], list[_Reading]] = defaultdict(list)
        # Votes from OCR words with exactly as many letters as glyphs under them.
        word_readings: dict[tuple[int, int], list[_Reading]] = defaultdict(list)
        unread: Counter[tuple[int, int]] = Counter()
        # Occurrences with no OCR text and no ink: the glyph draws nothing.
        blank_seen: Counter[tuple[int, int]] = Counter()
        document = pdfium.PdfDocument(str(input_path))
        try:
            for index in pages:
                page = document[index]
                try:
                    chars, words, scale, frame, gray = _ocr_page(page, languages, tesseract)
                finally:
                    page.close()
                stats["pages_read"] = int(stats["pages_read"]) + 1
                row_px = 32
                rows: dict[int, list[_OcrChar]] = defaultdict(list)
                for char in chars:
                    rows[int(char.center[1] // row_px)].append(char)
                placed = [(objid, code, _image_box(frame, box, scale)) for objid, code, box in page_glyphs[index]]
                for word in words:
                    under = sorted(
                        (g for g in placed if word.left <= (g[2][0] + g[2][2]) / 2 <= word.right
                         and word.top <= (g[2][1] + g[2][3]) / 2 <= word.bottom),
                        key=lambda g: g[2][0],
                    )
                    if len(under) != len(word.text):
                        continue
                    for (objid, code, _), letter in zip(under, word.text, strict=True):
                        if objid in missing and code in missing[objid]:
                            word_readings[(objid, code)].append(_Reading(letter, letter, word.confidence))
                for objid, code, box in page_glyphs[index]:
                    if objid not in missing or code not in missing[objid]:
                        continue
                    pixels = _image_box(frame, box, scale)
                    seen = _read_at(pixels, rows, row_px)
                    if seen is None:
                        unread[(objid, code)] += 1
                        if not _inked(gray, pixels):
                            blank_seen[(objid, code)] += 1
                    else:
                        readings[(objid, code)].append(seen)
        finally:
            document.close()

        # Votes are pooled by glyph: one outline across subsets of the same
        # font is one character, so its occurrences in every subset vote together.
        groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
        for objid, codes in missing.items():
            family = _family(targets[objid].font)
            for code in codes:
                shape = _outline(targets[objid], code)
                groups[(family, shape) if shape else (objid, code)].append((objid, code))
        decided: dict[tuple, tuple[str, float]] = {}
        for key, members in groups.items():
            objid, code = members[0]
            # An outline with no contours draws nothing, whatever OCR read
            # beneath it from other text.
            result = None
            blank = _blank_glyph_text(targets[objid], code)
            if blank is not None:
                result = (blank, 1.0)
            pooled = [r for m in members for r in readings.get(m, [])]
            aligned = [r for m in members for r in word_readings.get(m, [])]
            if result is None and aligned:
                result = _decide(aligned, 0)
            if result is None:
                result = _decide(pooled, sum(unread[m] for m in members))
            unread_total = sum(unread[m] for m in members)
            if result is None and not pooled and unread_total >= 2 and sum(blank_seen[m] for m in members) >= 0.9 * unread_total:
                advance = _advance(targets[objid], code)
                result = (" " if advance > 0 else ZERO_WIDTH_SPACE, 1.0)
            if result is not None:
                decided[key] = result
        # A font draws a character with one glyph: two different glyphs of a
        # font reading as the same character means one is misread, so only the
        # better-supported glyph keeps it.
        claims: dict[tuple[int, str], list[tuple]] = defaultdict(list)
        for key, (text, _) in decided.items():
            if text in (" ", ZERO_WIDTH_SPACE):
                continue
            for objid in {objid for objid, _ in groups[key]}:
                claims[(objid, text)].append(key)
        for keys in claims.values():
            if len(set(keys)) > 1:
                keep = max(set(keys), key=lambda k: (decided[k][1], sum(len(readings.get(m, [])) for m in groups[k])))
                for key in set(keys) - {keep}:
                    decided.pop(key, None)
        resolved: dict[int, dict[int, str]] = defaultdict(dict)
        unresolved: list[tuple[int, int]] = []
        for key, members in groups.items():
            for objid, code in members:
                if key in decided:
                    resolved[objid][code] = decided[key][0]
                else:
                    unresolved.append((objid, code))
        stats["codes_mapped"] = sum(len(m) for m in resolved.values())
        stats["codes_unresolved"] = len(unresolved)
        examples = stats["unresolved_examples"]
        for objid, code in unresolved[:8]:
            if isinstance(examples, list):
                examples.append({"font": str(targets[objid].font.get("/BaseFont")), "code": code,
                                 "readings": dict(Counter(r.nearest for r in readings.get((objid, code), [])).most_common(4))})

        if not resolved:
            return False, f"OCR font mapping could not map any of {stats['codes_targeted']} codes", stats
        # Confident codes are written even when others stay unmapped: validation
        # keeps a repair that removes failures without adding any, and the
        # unmapped codes keep the document held.
        for objid, mapping in resolved.items():
            target = targets[objid]
            full = {**target.known, **mapping}
            target.font["/ToUnicode"] = pdf.make_stream(_render_tounicode_cmap(full, target.code_bytes))
        pdf.save(output_path)
    message = f"OCR font mapping mapped {stats['codes_mapped']} of {stats['codes_targeted']} codes in {stats['fonts_targeted']} fonts"
    if stats["codes_unresolved"]:
        message += f"; {stats['codes_unresolved']} stay unmapped"
    return True, message, stats


async def _recover_tounicode_by_ocr(
    input_path: Path,
    output_path: Path,
    *,
    languages: str,
    max_pages: int,
) -> tuple[bool, str, dict[str, object]]:
    """Map the used codes no font data explains, from OCR of their pages."""
    try:
        return await to_thread(_recover_sync, input_path, output_path, languages, max_pages)
    except Exception as exc:  # noqa: BLE001 - a failed lane leaves the document as it was
        logger.warning("OCR font mapping failed for %s: %s", input_path.name, exc)
        return False, f"OCR font mapping failed: {exc}", {}
