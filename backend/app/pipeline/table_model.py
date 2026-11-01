"""Table structure from a small vision model, with the text from the page.

Docling finds most tables, but TableFormer often gets their grid wrong: a
borderless table read as one row whose cells hold whole columns, rows run
together. A small vision model reading each table's crop gets the grid right
more often (PaddleOCR-VL-1.5, asked "Table Recognition:", answers in OTSL:
rows of cells with their spans). Its text is not the page's, though: it
straightens and rewrites what it reads, and its answer has no positions and no
header marks. So this step takes only the model's grid. Each word the page
draws in the table (the PDF's own text, or the OCR layer on a scan) goes to a
model cell by aligning the two texts character by character, cell boxes are
drawn around their words, and the tagger then tags the page's own glyphs into
the new cells as it does Docling's.

For each table, in order:

1. The region: Docling's box, widened over words it clips or that sit next
   to it and belong to no other element (the crop the model reads is this
   region).
2. The model's answer for the crop, cached by the crop's bytes, the model and
   the prompt.
3. Lines of words to the model's rows, by aligning the table's text in line
   order with the model's in row order; then each row's words to its cells,
   in column order, so a cell running over several lines stays together.
   Characters are aligned, not words, so two cells drawn as one run of glyphs
   can split between them.
4. Cell boxes from their words. Docling's header flags carry over to the
   cells whose words were in Docling's header cells; without any, the
   tagger infers headers as it does for Docling's tables.
5. Docling's table is kept when the model gave no grid, the grid has fewer
   than two rows or columns, too little of either text aligns with the
   other, the model left out numbers the page draws (a dropped column,
   whose values would otherwise join their neighbors' cells), or the model
   ran complete Docling rows together without splitting any Docling cell
   (Docling's grid is then the finer reading of the same words).

On a scan, the optional text correction lets a cell read the model's text,
as /ActualText, only where every difference from the OCR text is a known
OCR confusion (0 and O, 1 and l and I, 5 and S, 8 and B, a rule read as a
pipe, a dropped decimal point) and everything else matches.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
import pikepdf
import pypdfium2 as pdfium
from rapidfuzz.distance import Levenshtein

from app.config import DEFAULT_LLM_MAX_REQUEST_BYTES
from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import MeasuredGlyph, PdfGlyphReader, union_bbox
from app.pipeline.tagger_ownership import _measured_words, _page_has_hidden_text
from app.services.vision_requests import bound_vision_request

logger = logging.getLogger(__name__)

Box = dict[str, float]

PROMPT = "Table Recognition:"
# The crop: 144 ppi, with 4 pt around the region.
RENDER_SCALE = 2.0
CROP_PAD = 4.0
MAX_TOKENS = 4096


# --- the model's answer ---------------------------------------------------------


@dataclass
class ModelAnswer:
    text: str
    finish: str = "stop"
    seconds: float | None = None
    cached: bool = False


class TableModelUnavailable(Exception):
    """The model cannot answer now (down, busy, or too slow): the remaining
    tables of the document keep Docling's grids without asking."""


class TableReader(Protocol):
    model: str

    async def read(self, png: bytes) -> ModelAnswer | None: ...


def cache_key(model: str, png: bytes, prompt: str = PROMPT) -> str:
    """The cache key of one crop's answer: the crop's bytes, the model, the
    prompt and the token cap."""
    digest = hashlib.sha256(f"{model}\n{prompt}\n{MAX_TOKENS}\n".encode())
    digest.update(png)
    return digest.hexdigest()


class AnswerCache:
    """Answers on disk, one JSON file per crop."""

    def __init__(self, directory: Path, model: str) -> None:
        self.directory = directory
        self.model = model

    def path(self, png: bytes) -> Path:
        key = cache_key(self.model, png)
        return self.directory / key[:2] / f"{key}.json"

    def get(self, png: bytes) -> ModelAnswer | None:
        path = self.path(png)
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        return ModelAnswer(record["text"], record.get("finish", "stop"), record.get("seconds"), True)

    def put(self, png: bytes, answer: ModelAnswer, extra: dict | None = None) -> None:
        path = self.path(png)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"text": answer.text, "finish": answer.finish, "seconds": answer.seconds,
                  **(extra or {})}
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(record))
        partial.replace(path)


class CachedReader:
    """Answers only from the cache (the bench's free loop); a crop not in it
    has no answer, so its table keeps Docling's grid."""

    def __init__(self, cache: AnswerCache) -> None:
        self.cache = cache
        self.model = cache.model
        self.misses = 0

    async def read(self, png: bytes) -> ModelAnswer | None:
        answer = self.cache.get(png)
        if answer is None:
            self.misses += 1
        return answer


class HttpReader:
    """The table model behind an OpenAI-compatible chat endpoint, one
    request at a time, with an optional answer cache."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
        cache: AnswerCache | None = None,
        max_request_bytes: int = DEFAULT_LLM_MAX_REQUEST_BYTES,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout = timeout
        self.cache = cache
        self.max_request_bytes = max_request_bytes

    async def read(self, png: bytes) -> ModelAnswer | None:
        if self.cache is not None and (answer := self.cache.get(png)) is not None:
            return answer
        image = "data:image/png;base64," + base64.b64encode(png).decode()
        body = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image}},
                {"type": "text", "text": PROMPT},
            ]}],
        }
        body = await asyncio.to_thread(bound_vision_request, body, self.max_request_bytes)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.url, json=body)
        except httpx.HTTPError as exc:
            raise TableModelUnavailable(repr(exc)) from exc
        if response.status_code in (429, 503):
            raise TableModelUnavailable(f"busy ({response.status_code})")
        if response.status_code != 200:
            logger.warning("table model answered %s", response.status_code)
            return None
        payload = response.json()
        choice = payload["choices"][0]
        answer = ModelAnswer(
            str(choice["message"]["content"] or ""),
            str(choice.get("finish_reason") or "stop"),
            round(time.monotonic() - started, 3),
        )
        if self.cache is not None:
            self.cache.put(png, answer, {"usage": payload.get("usage"),
                                         "timing": payload.get("timing")})
        return answer


# --- the model's grid -----------------------------------------------------------


@dataclass
class GridCell:
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    text: str = ""


@dataclass
class Grid:
    rows: int
    cols: int
    cells: list[GridCell]
    problems: list[str] = field(default_factory=list)


OTSL_TOKEN = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|ched|rhed|srow|nl)>")
CONTINUATIONS = {"lcel", "ucel", "xcel"}


def _cell_text(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", raw)
    text = re.sub(r"</?[a-zA-Z][^>]*>", " ", text)
    return " ".join(html.unescape(text).split())


def parse_otsl(answer: str) -> Grid | None:
    """The grid of an OTSL answer: fcel/ecel start a cell (with or without
    text), lcel continues the cell to the left, ucel the one above, xcel
    both; nl ends a row. None when the answer has no cells."""
    text = re.sub(r"<loc_\d+>", "", answer)
    matches = list(OTSL_TOKEN.finditer(text))
    rows: list[list[tuple[str, str]]] = [[]]
    for index, match in enumerate(matches):
        kind = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if kind == "nl":
            rows.append([])
            continue
        rows[-1].append((kind, _cell_text(text[match.end():end])))
    rows = [row for row in rows if row]
    if not rows:
        return None
    problems: list[str] = []
    width = max(len(row) for row in rows)
    if any(len(row) != width for row in rows):
        problems.append("ragged rows")
    grid = [row + [("ecel", "")] * (width - len(row)) for row in rows]
    for r, row in enumerate(grid):
        for c, (kind, content) in enumerate(row):
            if (kind == "lcel" and c == 0) or (kind == "ucel" and r == 0) or (
                kind == "xcel" and (r == 0 or c == 0)
            ):
                row[c] = ("fcel" if content else "ecel", content)
                problems.append("span without a cell")
    claimed = [[False] * width for _ in grid]
    cells: list[GridCell] = []
    for r, row in enumerate(grid):
        for c, (kind, content) in enumerate(row):
            if kind in CONTINUATIONS:
                continue
            col_span = 1
            while c + col_span < width and grid[r][c + col_span][0] == "lcel":
                col_span += 1
            row_span = 1
            while r + row_span < len(grid) and grid[r + row_span][c][0] == "ucel" and all(
                grid[r + row_span][c + k][0] == "xcel" for k in range(1, col_span)
            ):
                row_span += 1
            for rr in range(r, r + row_span):
                for cc in range(c, c + col_span):
                    claimed[rr][cc] = True
            cells.append(GridCell(r, c, row_span, col_span, content))
    for r, row in enumerate(grid):
        for c, (kind, _) in enumerate(row):
            if not claimed[r][c]:
                problems.append("span without a cell")
                cells.append(GridCell(r, c))
    cells.sort(key=lambda cell: (cell.row, cell.col))
    return Grid(len(grid), width, cells, sorted(set(problems)))


# --- the page's words -----------------------------------------------------------


@dataclass
class Word:
    glyphs: list[MeasuredGlyph]
    box: Box  # Docling's frame
    upright: bool  # runs left to right as the page is shown

    @property
    def text(self) -> str:
        return "".join(glyph.text for glyph in self.glyphs)

    @property
    def height(self) -> float:
        return self.box["t"] - self.box["b"]


def page_words(glyph_reader: PdfGlyphReader, page_index: int, frame: PageFrame) -> list[Word]:
    """The page's words as the tagger owns them (see _measured_words), with
    boxes in Docling's frame."""
    ops, starts = [], set()
    for stream_ops in glyph_reader.page(page_index).ops.values():
        starts.add(len(ops))
        ops.extend(stream_ops)
    glyph_words, _ = _measured_words(dict(enumerate(ops)), starts)
    display_turn = frame.rotation // 90
    words = []
    for glyphs in glyph_words:
        box = union_bbox(glyph.bbox for glyph in glyphs)
        if box is None:
            continue
        words.append(Word(glyphs, frame.to_docling(box), glyphs[0].turn % 4 == display_turn))
    return words


def _holds(area: Box, box: Box) -> bool:
    """An area holds a box (both upright): the middle of its height and at
    least half its width, the tagger's ownership rule."""
    middle = (box["b"] + box["t"]) / 2
    if not area["b"] <= middle <= area["t"]:
        return False
    width = box["r"] - box["l"]
    if width <= 0:
        return area["l"] <= box["l"] <= area["r"]
    return (min(box["r"], area["r"]) - max(box["l"], area["l"])) / width >= 0.5


def _holds_word(area: Box, word: Word) -> bool:
    if word.upright:
        return _holds(area, word.box)
    x, y = (word.box["l"] + word.box["r"]) / 2, (word.box["b"] + word.box["t"]) / 2
    return area["l"] <= x <= area["r"] and area["b"] <= y <= area["t"]


def _touches(area: Box, box: Box) -> bool:
    return min(box["r"], area["r"]) > max(box["l"], area["l"]) and min(
        box["t"], area["t"]
    ) > max(box["b"], area["b"])


def _element_boxes(element: dict) -> list[Box]:
    boxes = [element.get("bbox"), *(element.get("extra_bboxes") or [])]
    return [box for box in boxes if isinstance(box, dict)]


def table_region(table: dict, others: list[dict], words: list[Word]) -> tuple[Box, list[Word]]:
    """The table's region and its words: Docling's box, widened over words it
    clips (any part inside) and then, line by line, over words next to it
    that no other element holds."""
    box = dict(table["bbox"])
    held = [b for element in others for b in _element_boxes(element)]

    def free(word: Word) -> bool:
        return not any(_holds_word(b, word) for b in held)

    inside = [w for w in words if _holds_word(box, w) or (_touches(box, w.box) and free(w))]
    region = union_bbox([box, *(w.box for w in inside)]) or box
    chosen = {id(w) for w in inside}
    candidates = [w for w in words if id(w) not in chosen and w.upright and free(w)]
    changed = True
    while changed and candidates:
        changed = False
        for word in list(candidates):
            h = max(word.height, 1.0)
            overlap_x = min(word.box["r"], region["r"]) - max(word.box["l"], region["l"])
            gap_y = max(word.box["b"] - region["t"], region["b"] - word.box["t"])
            middle = (word.box["b"] + word.box["t"]) / 2
            gap_x = max(word.box["l"] - region["r"], region["l"] - word.box["r"])
            above_below = overlap_x > 0 and gap_y <= 1.2 * h
            beside = region["b"] <= middle <= region["t"] and gap_x <= 2.0 * h
            if above_below or beside:
                inside.append(word)
                candidates.remove(word)
                region = union_bbox([region, word.box]) or region
                changed = True
    return region, inside


def render_crop(page: pdfium.PdfPage, region: Box) -> bytes:
    """The region of the page as shown, as PNG bytes, at RENDER_SCALE."""
    width, height = page.get_size()
    image = page.render(scale=RENDER_SCALE).to_pil().convert("RGB")
    left = max(0.0, (region["l"] - CROP_PAD) * RENDER_SCALE)
    top = max(0.0, (height - region["t"] - CROP_PAD) * RENDER_SCALE)
    right = min(float(image.width), (region["r"] + CROP_PAD) * RENDER_SCALE)
    bottom = min(float(image.height), (height - region["b"] + CROP_PAD) * RENDER_SCALE)
    crop = image.crop((round(left), round(top), round(right), round(bottom)))
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG")
    return buffer.getvalue()


# --- alignment ------------------------------------------------------------------

DASHES = set("-‐‑‒–—―−")
KEPT_SYMBOLS = set("%$€£¥.,+()/<>=*&#@:;?!")
RULE_STROKES = set("|¦│┃_")


def fold(text: str) -> str:
    """Characters compared: letters and digits lowercased and without accents,
    dashes as "-", the symbols that tell values apart, and any other mark
    (a check mark, a bullet, a degree sign, a quote) as "~", so marks drawn
    with different code points still align. Spaces and rule strokes are left
    out."""
    out = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch) or ch.isspace() or ch in RULE_STROKES:
            continue
        if ch.isalnum():
            out.append(ch.lower())
        elif ch in DASHES:
            out.append("-")
        elif ch in KEPT_SYMBOLS:
            out.append(ch)
        else:
            out.append("~")
    return "".join(out)


# The model writes mathematics and some symbols as LaTeX; the page draws the
# characters. Only alignment reads this; a cell's text is the page's.
LATEX_SYMBOLS = {
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "·", "circ": "°", "degree": "°",
    "infty": "∞", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "approx": "≈",
    "sim": "~", "uparrow": "↑", "downarrow": "↓", "rightarrow": "→", "leftarrow": "←",
    "to": "→", "checkmark": "✓", "%": "%", "$": "$", "&": "&", "#": "#", "_": "_",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω", "prime": "′",
}
LATEX_COMMAND = re.compile(r"\\([A-Za-z]+|[%$&#_])")


def plain_model_text(text: str) -> str:
    """A model cell's text with its LaTeX read as the characters it draws:
    math delimiters, braces, sub- and superscript marks and formatting
    commands removed, symbol commands replaced, others dropped."""
    if "\\" not in text and "$" not in text:
        return text
    text = re.sub(r"\\[()\[\]]", " ", text)
    text = text.replace("$", " ")
    text = LATEX_COMMAND.sub(lambda m: LATEX_SYMBOLS.get(m.group(1), ""), text)
    text = re.sub(r"[{}^]", "", text)
    text = re.sub(r"(?<=\w)_(?=\w)", "", text)
    return " ".join(text.split())


def _char_map(page: str, model: str) -> tuple[list[int | None], list[bool]]:
    """For each page character, the model character aligned with it (by
    edit distance), and whether the two are equal."""
    mapped: list[int | None] = [None] * len(page)
    equal = [False] * len(page)
    for tag, i1, i2, j1, j2 in Levenshtein.opcodes(page, model):
        if tag == "equal":
            for k in range(i2 - i1):
                mapped[i1 + k] = j1 + k
                equal[i1 + k] = True
        elif tag == "replace":
            for k in range(min(i2 - i1, j2 - j1)):
                mapped[i1 + k] = j1 + k
    return mapped, equal


def _lines(words: list[Word]) -> list[list[Word]]:
    """Words in lines, top to bottom, each left to right: a word joins the
    line whose first word's height holds its middle. Text that does not run
    left to right is a line of its own."""
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: -w.box["t"]):
        if not word.upright:
            lines.append([word])
            continue
        middle = (word.box["b"] + word.box["t"]) / 2
        for line in lines:
            first = line[0]
            if not first.upright:
                continue
            overlap = min(word.box["t"], first.box["t"]) - max(word.box["b"], first.box["b"])
            # A raised or lowered mark (a footnote star, a subscript) joins
            # the line it overlaps by half its own height.
            if first.box["b"] <= middle <= first.box["t"] or overlap >= 0.5 * word.height:
                line.append(word)
                break
        else:
            lines.append([word])
    lines.sort(key=lambda line: -max(w.box["t"] for w in line))
    return [sorted(line, key=lambda w: w.box["l"]) for line in lines]


@dataclass
class Piece:
    """A word, or the part of one, that goes to one cell."""

    word: Word
    glyphs: list[MeasuredGlyph]
    box: Box


@dataclass
class Assignment:
    pieces: dict[int, list[Piece]]  # by cell index
    stats: dict


def _monotone_rows(votes: list[dict[int, float]]) -> list[int | None]:
    """A row for each line, never above the previous line's, maximizing the
    alignment votes kept. A line without votes gets none."""
    rows = sorted({r for line in votes for r in line})
    if not rows:
        return [None] * len(votes)
    best = [0.0] * len(rows)
    back: list[list[int]] = []
    for line in votes:
        prefix, prefix_at, choice = -1.0, 0, []
        new = [0.0] * len(rows)
        for k, row in enumerate(rows):
            if best[k] > prefix:
                prefix, prefix_at = best[k], k
            new[k] = prefix + line.get(row, 0.0)
            choice.append(prefix_at)
        best = new
        back.append(choice)
    k = max(range(len(rows)), key=lambda i: best[i])
    chosen: list[int | None] = [None] * len(votes)
    for index in range(len(votes) - 1, -1, -1):
        chosen[index] = rows[k] if votes[index] else None
        k = back[index][k]
    return chosen


def _components(words: list[tuple[int, Word]]) -> list[list[tuple[int, Word]]]:
    """A row's words grouped into the cells they look like: words on one line
    close together, and words on consecutive lines that overlap across.
    Groups left to right, words in a group by line and then left to right."""
    parent = list(range(len(words)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_line: dict[int, list[int]] = defaultdict(list)
    for index, (line, _) in enumerate(words):
        by_line[line].append(index)
    for indices in by_line.values():
        ordered = sorted(indices, key=lambda i: words[i][1].box["l"])
        for a, b in zip(ordered, ordered[1:]):
            wa, wb = words[a][1], words[b][1]
            if wb.box["l"] - wa.box["r"] <= 0.5 * max(wa.height, wb.height, 1.0):
                parent[find(b)] = find(a)
    lines = sorted(by_line)
    for upper, lower in zip(lines, lines[1:]):
        for a in by_line[upper]:
            for b in by_line[lower]:
                wa, wb = words[a][1], words[b][1]
                if min(wa.box["r"], wb.box["r"]) > max(wa.box["l"], wb.box["l"]):
                    parent[find(b)] = find(a)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(words)):
        groups[find(index)].append(index)
    ordered_groups = sorted(groups.values(), key=lambda g: min(words[i][1].box["l"] for i in g))
    return [
        [words[i] for i in sorted(group, key=lambda i: (words[i][0], words[i][1].box["l"]))]
        for group in ordered_groups
    ]


def _sequence(words: list[Word]) -> tuple[str, list[tuple[int, int]]]:
    """The words' folded text and, for each character, its word and glyph."""
    chars: list[str] = []
    owners: list[tuple[int, int]] = []
    for w, word in enumerate(words):
        for g, glyph in enumerate(word.glyphs):
            for ch in fold(glyph.text):
                chars.append(ch)
                owners.append((w, g))
    return "".join(chars), owners


def _cells_sequence(cells: list[GridCell], order: list[int]) -> tuple[str, list[int]]:
    chars: list[str] = []
    owners: list[int] = []
    for index in order:
        for ch in fold(plain_model_text(cells[index].text)):
            chars.append(ch)
            owners.append(index)
    return "".join(chars), owners


def _align_row(
    sequences: list[list[Word]], cell_text: str, cell_owner: list[int]
) -> tuple[float, int, dict[int, list[int | None]], set[tuple[int, int]]]:
    """Each glyph's cell when each word sequence is aligned on its own with a
    row's cell text; the reading's score (equal characters, less two for each
    word split between cells), the equal count, and the model characters hit."""
    glyph_cells_by_word: dict[int, list[int | None]] = {}
    equal_count = 0
    model_hits: set[tuple[int, int]] = set()
    splits = 0
    starts: dict[int, int] = {}
    for j, cell in enumerate(cell_owner):
        starts.setdefault(cell, j)
    for sequence in sequences:
        text, owner = _sequence(sequence)
        mapped, equal = _char_map(text, cell_text)
        per_glyph: dict[tuple[int, int], Counter] = defaultdict(Counter)
        for k, j in enumerate(mapped):
            if j is None:
                continue
            per_glyph[owner[k]][cell_owner[j]] += 1.0 if equal[k] else 0.25
            if equal[k]:
                model_hits.add((cell_owner[j], j - starts[cell_owner[j]]))
        equal_count += sum(equal)
        for w, word in enumerate(sequence):
            glyph_cells: list[int | None] = []
            for g in range(len(word.glyphs)):
                counts = per_glyph.get((w, g))
                glyph_cells.append(counts.most_common(1)[0][0] if counts else None)
            if len({c for c in glyph_cells if c is not None}) > 1:
                splits += 1
            glyph_cells_by_word[id(word)] = glyph_cells
    return equal_count - 2.0 * splits, equal_count, glyph_cells_by_word, model_hits


def assign_words(grid: Grid, words: list[Word]) -> Assignment:
    """Each word of the region to a cell of the grid (see the module notes)."""
    cells = grid.cells
    lines = _lines(words)
    line_of = {id(w): n for n, line in enumerate(lines) for w in line}
    ordered = [w for line in lines for w in line]
    page_text, page_owner = _sequence(ordered)
    row_major = sorted(range(len(cells)), key=lambda i: (cells[i].row, cells[i].col))
    model_text, model_owner = _cells_sequence(cells, row_major)

    # Stage 1: lines to rows.
    mapped, equal = _char_map(page_text, model_text)
    votes: list[dict[int, float]] = [defaultdict(float) for _ in lines]
    span_votes: list[dict[int, float]] = [defaultdict(float) for _ in lines]
    for k, j in enumerate(mapped):
        if j is None:
            continue
        cell = cells[model_owner[j]]
        line = line_of[id(ordered[page_owner[k][0]])]
        weight = 1.0 if equal[k] else 0.25
        (votes if cell.row_span == 1 else span_votes)[line][cell.row] += weight
    for n in range(len(lines)):
        if not votes[n] and span_votes[n]:
            votes[n] = span_votes[n]
    # Words shared by a line and a row, each worth its length over the
    # number of rows that have it: the order-free evidence that keeps the
    # second line of a multi-line cell with its row, though the page draws it
    # after the first lines of the cells to its right.
    row_tokens: dict[int, Counter] = defaultdict(Counter)
    for cell in cells:
        tokens = [fold(t) for t in plain_model_text(cell.text).split()]
        for r in range(cell.row, cell.row + cell.row_span):
            row_tokens[r].update(t for t in tokens if t)
    rows_with: Counter = Counter()
    for counter in row_tokens.values():
        rows_with.update(counter.keys())
    for n, line in enumerate(lines):
        for word in line:
            token = fold(word.text)
            if not token or token not in rows_with:
                continue
            weight = len(token) / rows_with[token]
            for r, counter in row_tokens.items():
                if token in counter:
                    votes[n][r] += weight
    # A line the table-wide alignment left mostly unmatched (drawn out of the
    # model's row order, such as a label centered between two rows' lines)
    # is aligned alone with each row, and takes the row it matches best.
    row_texts: dict[int, str] = {}
    for r in range(grid.rows):
        covering_cells = sorted(
            (i for i, c in enumerate(cells) if c.row <= r < c.row + c.row_span),
            key=lambda i: cells[i].col,
        )
        row_texts[r] = _cells_sequence(cells, covering_cells)[0]
    for n, line in enumerate(lines):
        line_text = _sequence(line)[0]
        if not line_text or sum(votes[n].values()) >= 0.5 * len(line_text):
            continue
        scores = {
            r: sum(i2 - i1 for tag, i1, i2, _, _ in Levenshtein.opcodes(line_text, text)
                   if tag == "equal")
            for r, text in row_texts.items() if text
        }
        if scores:
            row, score = max(scores.items(), key=lambda item: (item[1], -item[0]))
            if score >= 0.5 * len(line_text):
                votes[n] = defaultdict(float, {row: float(score)})
    line_rows = _monotone_rows([dict(v) for v in votes])

    # Stage 2: each row's words to its cells.
    covering: dict[int, list[int]] = defaultdict(list)
    for index, cell in enumerate(cells):
        for r in range(cell.row, cell.row + cell.row_span):
            covering[r].append(index)
    word_cells: dict[int, list[int | None]] = {}  # word id -> cell per glyph
    word_by_id = {id(w): w for w in ordered}
    matched_page = 0
    matched_model_chars: set[tuple[int, int]] = set()
    for row in sorted({r for r in line_rows if r is not None}):
        order = sorted(covering.get(row, []), key=lambda i: (cells[i].col, cells[i].row))
        cell_text, cell_owner = _cells_sequence(cells, order)
        row_lines = [line for n, line in enumerate(lines) if line_rows[n] == row]
        row_words = [(n, w) for n, line in enumerate(lines) if line_rows[n] == row for w in line]
        # Two readings of a row's words: grouped into the cells they look
        # like (a cell over several lines read whole), and line by line (a
        # label centered across lines that belong to other cells). The one
        # that matches more characters and splits fewer words is kept.
        readings = [
            [[w for group in _components(row_words) for _, w in group]],
            row_lines,
        ]
        best = None
        for reading in readings:
            result = _align_row(reading, cell_text, cell_owner)
            if best is None or result[0] > best[0]:
                best = result
        assert best is not None
        _, equal_count, glyph_cells_by_word, model_hits = best
        matched_page += equal_count
        matched_model_chars.update(model_hits)
        word_cells.update(glyph_cells_by_word)
    matched_model = len(matched_model_chars)

    # Glyphs without a cell take their word's; words without one take the
    # nearest word's on their line.
    pieces: dict[int, list[Piece]] = defaultdict(list)
    split_words = 0
    owner_of_word: dict[int, int] = {}
    for word_id, glyph_cells in word_cells.items():
        known = [c for c in glyph_cells if c is not None]
        if not known:
            continue
        majority = Counter(known).most_common(1)[0][0]
        filled = list(glyph_cells)
        # Fill gaps from the left neighbor, then the right.
        last = None
        for i, c in enumerate(filled):
            if c is None:
                filled[i] = last
            else:
                last = c
        nxt = None
        for i in range(len(filled) - 1, -1, -1):
            if filled[i] is None:
                filled[i] = nxt
            else:
                nxt = filled[i]
        runs: list[tuple[int, list[int]]] = []
        for i, c in enumerate(filled):
            assert c is not None
            if runs and runs[-1][0] == c:
                runs[-1][1].append(i)
            else:
                runs.append((c, [i]))
        word = word_by_id[word_id]
        if len(runs) > 1 and len({c for c, _ in runs}) == len(runs):
            split_words += 1
            for c, indices in runs:
                glyphs = [word.glyphs[i] for i in indices]
                pieces[c].append(Piece(word, glyphs, _glyphs_box(word, glyphs)))
        else:
            pieces[majority].append(Piece(word, word.glyphs, word.box))
            owner_of_word[word_id] = majority
        if len(runs) > 1:
            owner_of_word.setdefault(word_id, majority)
    unmatched = [
        w for w in ordered
        if all(c is None for c in word_cells.get(id(w), [None]))
    ]
    attached = 0
    for word in unmatched:
        if not fold(word.text):
            continue  # a rule stroke: the tagger decides (see _recover_unowned_words)
        line = lines[line_of[id(word)]]
        best, distance = None, 3.0 * max(word.height, 1.0)
        for other in line:
            if id(other) not in owner_of_word:
                continue
            gap = max(other.box["l"] - word.box["r"], word.box["l"] - other.box["r"], 0.0)
            if gap < distance:
                best, distance = owner_of_word[id(other)], gap
        if best is not None:
            pieces[best].append(Piece(word, word.glyphs, word.box))
            attached += 1
    page_chars = len(page_text)
    model_chars = len(model_text)
    stats = {
        "words": len(ordered),
        "lines": len(lines),
        "lines_without_row": sum(r is None for r in line_rows),
        "page_chars": page_chars,
        "model_chars": model_chars,
        "page_coverage": round(matched_page / page_chars, 3) if page_chars else 0.0,
        "model_coverage": round(matched_model / model_chars, 3) if model_chars else 0.0,
        "split_words": split_words,
        "unmatched_words": len(unmatched),
        "unmatched_numbers": sum(any(ch.isdigit() for ch in w.text) for w in unmatched),
        "word_coverage": round(1 - len(unmatched) / len(ordered), 3) if ordered else 0.0,
        "attached_words": attached,
        "unassigned_words": len(unmatched) - attached,
    }
    return Assignment(dict(pieces), stats)


def _glyphs_box(word: Word, glyphs: list[MeasuredGlyph]) -> Box:
    """A piece's box in Docling's frame: the word's box cut to its glyphs
    along the direction the word runs."""
    whole = union_bbox(glyph.bbox for glyph in word.glyphs)
    part = union_bbox(glyph.bbox for glyph in glyphs)
    if whole is None or part is None:
        return word.box
    # Scale the user-space cut into Docling's frame along both axes.
    box = dict(word.box)
    w_user = whole["r"] - whole["l"]
    h_user = whole["t"] - whole["b"]
    if w_user >= h_user and word.box["r"] > word.box["l"]:
        start = (part["l"] - whole["l"]) / w_user if w_user else 0.0
        end = (part["r"] - whole["l"]) / w_user if w_user else 1.0
        span = word.box["r"] - word.box["l"]
        box["l"], box["r"] = word.box["l"] + start * span, word.box["l"] + end * span
    return box


# --- the new table --------------------------------------------------------------

# Characters the OCR reads for one another (OCR text -> model text).
CONFUSIONS = {
    ("O", "0"), ("o", "0"), ("0", "O"),
    ("l", "1"), ("I", "1"), ("|", "1"), ("1", "l"), ("1", "I"), ("l", "I"), ("I", "l"),
    ("S", "5"), ("s", "5"), ("5", "S"),
    ("B", "8"), ("8", "B"),
}
RULE_ARTIFACTS = set("|¦")


def ocr_correction(page_text: str, model_text: str) -> tuple[str | None, list[str]]:
    """The model's text for a cell when every difference from the OCR text is
    a known confusion, else None; and the differences found, each as
    "kind:page>model"."""
    page, model = "".join(page_text.split()), "".join(model_text.split())
    if page == model or not model:
        return None, []
    differences: list[str] = []
    allowed = True
    for tag, i1, i2, j1, j2 in Levenshtein.opcodes(page, model):
        a, b = page[i1:i2], model[j1:j2]
        if tag == "equal":
            continue
        if tag == "replace" and len(a) == len(b) and all(pair in CONFUSIONS for pair in zip(a, b)):
            differences.append(f"confusion:{a}>{b}")
        elif tag == "delete" and set(a) <= RULE_ARTIFACTS:
            differences.append(f"rule:{a}>")
        elif (
            tag == "insert" and b == "." and 0 < i1 < len(page)
            and page[i1 - 1].isdigit() and page[i1].isdigit()
        ):
            differences.append("decimal:>.")
        else:
            differences.append(f"other:{a}>{b}")
            allowed = False
    if not allowed:
        return None, differences
    return " ".join(model_text.split()), differences


def _header_flags(table: dict, words: list[Word]) -> dict[int, str]:
    """Docling's header flag for each word its header cells hold."""
    flags: dict[int, str] = {}
    for cell in table.get("cells") or []:
        if not isinstance(cell, dict) or not isinstance(cell.get("bbox"), dict):
            continue
        if not (cell.get("column_header") or cell.get("row_header") or cell.get("is_header")):
            continue
        scope = "row" if cell.get("row_header") and not cell.get("column_header") else "column"
        for word in words:
            if _holds_word(cell["bbox"], word):
                flags[id(word)] = scope
    return flags


def _band_centers(values: dict[int, list[float]], count: int, low: float, high: float,
                  descending: bool) -> list[float]:
    """Each row's (or column's) middle from its words, missing ones between
    their neighbors."""
    centers: list[float | None] = [
        sum(values[i]) / len(values[i]) if values.get(i) else None for i in range(count)
    ]
    known = [i for i, v in enumerate(centers) if v is not None]
    start, end = (high, low) if descending else (low, high)
    for i in range(count):
        if centers[i] is not None:
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        known_a = centers[before] if before is not None else None
        known_b = centers[after] if after is not None else None
        a = known_a if known_a is not None else start
        b = known_b if known_b is not None else end
        ia = before if before is not None else -1
        ib = after if after is not None else count
        centers[i] = a + (b - a) * (i - ia) / (ib - ia)
    return [float(c) for c in centers if c is not None]


def rebuild_table(table: dict, grid: Grid, assignment: Assignment, words: list[Word],
                  *, scanned: bool, correct_text: bool) -> tuple[dict, dict]:
    """A copy of the table element with the model's grid and the page's
    words in its cells."""
    flags = _header_flags(table, words)
    new_cells: list[dict] = []
    rows_y: dict[int, list[float]] = defaultdict(list)
    cols_x: dict[int, list[float]] = defaultdict(list)
    corrections: list[dict] = []
    rejected: list[dict] = []
    for index, cell in enumerate(grid.cells):
        pieces = assignment.pieces.get(index, [])
        ordered = sorted(pieces, key=lambda p: (-round(p.box["t"]), p.box["l"]))
        text = " ".join("".join(g.text for g in p.glyphs) for p in ordered)
        entry: dict = {
            "text": text,
            "row": cell.row,
            "col": cell.col,
            "row_span": cell.row_span,
            "col_span": cell.col_span,
            "column_header": False,
            "row_header": False,
            "is_header": False,
        }
        if pieces:
            box = union_bbox(p.box for p in pieces)
            assert box is not None
            entry["bbox"] = {"l": box["l"] - 0.01, "b": box["b"] - 0.01,
                             "r": box["r"] + 0.01, "t": box["t"] + 0.01}
            if cell.row_span == 1:
                rows_y[cell.row].append((box["b"] + box["t"]) / 2)
            if cell.col_span == 1:
                cols_x[cell.col].append((box["l"] + box["r"]) / 2)
            scopes = Counter(flags.get(id(p.word)) for p in pieces)
            scope, count = scopes.most_common(1)[0]
            if scope and count * 2 >= len(pieces):
                entry["is_header"] = True
                entry["column_header"] = scope == "column"
                entry["row_header"] = scope == "row"
        if scanned and pieces and cell.text:
            model_text = plain_model_text(cell.text)
            corrected, differences = ocr_correction(text, model_text)
            if differences:
                record = {"row": cell.row, "col": cell.col, "ocr": text, "model": model_text,
                          "differences": differences}
                if corrected is not None:
                    corrections.append(record)
                    if correct_text:
                        entry["actual_text"] = corrected
                else:
                    rejected.append(record)
        new_cells.append(entry)
    bbox = table["bbox"]
    row_centers = _band_centers(rows_y, grid.rows, bbox["b"], bbox["t"], descending=True)
    col_centers = _band_centers(cols_x, grid.cols, bbox["l"], bbox["r"], descending=False)
    for entry in new_cells:
        if "bbox" not in entry:
            # No word for an empty cell's box to take: a point where it sits.
            x, y = col_centers[entry["col"]], row_centers[entry["row"]]
            entry["bbox"] = {"l": x, "b": y, "r": x, "t": y}
    boxes = [bbox, *(c["bbox"] for c in new_cells if c["bbox"]["r"] > c["bbox"]["l"])]
    new_table = dict(table)
    new_table.update(
        cells=new_cells,
        num_rows=grid.rows,
        num_cols=grid.cols,
        bbox=union_bbox(boxes) or bbox,
    )
    return new_table, {"corrections": corrections, "rejected_corrections": rejected}


def compare_grids(table: dict, grid: Grid, assignment: Assignment) -> dict:
    """How the model's grid and Docling's divide the same words: Docling cells
    whose words the model splits between cells, and model cells holding
    words of two or more complete Docling rows (rows with at least half their
    cells, and two, holding text)."""
    docling_cells = [
        c for c in table.get("cells") or []
        if isinstance(c, dict) and isinstance(c.get("bbox"), dict) and str(c.get("text") or "").strip()
    ]
    num_cols = max(int(table.get("num_cols") or 0), 1)
    filled: Counter = Counter(int(c.get("row", 0)) for c in docling_cells)
    full_rows = {r for r, n in filled.items() if n >= 2 and n >= num_cols / 2}
    docling_of: dict[int, int] = {}
    for index, cell in enumerate(docling_cells):
        for pieces in assignment.pieces.values():
            for piece in pieces:
                if id(piece.word) not in docling_of and _holds_word(cell["bbox"], piece.word):
                    docling_of[id(piece.word)] = index
    model_cells_of: dict[int, set[int]] = defaultdict(set)
    docling_rows_of: dict[int, set[int]] = defaultdict(set)
    for model_index, pieces in assignment.pieces.items():
        for piece in pieces:
            d = docling_of.get(id(piece.word))
            if d is None:
                continue
            model_cells_of[d].add(model_index)
            docling_rows_of[model_index].add(int(docling_cells[d].get("row", 0)))
    return {
        "docling_cells_split": sum(len(cells) > 1 for cells in model_cells_of.values()),
        "model_cells_merging_rows": sum(
            len(rows & full_rows) > 1 for rows in docling_rows_of.values()
        ),
    }


# --- the step -------------------------------------------------------------------


@dataclass
class TablePrep:
    element_index: int
    page: int
    region: Box
    words: list[Word]
    png: bytes
    scanned: bool
    report: dict


def prepare_tables(pdf_path: Path, elements: list[dict]) -> list[TablePrep]:
    """Each table's region, words and crop."""
    tables = [
        (index, element) for index, element in enumerate(elements)
        if element.get("type") == "table" and isinstance(element.get("bbox"), dict)
        and isinstance(element.get("page"), int)
    ]
    if not tables:
        return []
    by_page: dict[int, list[dict]] = defaultdict(list)
    for element in elements:
        if isinstance(element.get("page"), int):
            by_page[element["page"]].append(element)
    prepared: list[TablePrep] = []
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        with PdfGlyphReader(pdf_path) as glyph_reader, pikepdf.open(str(pdf_path)) as pdf:
            words_cache: dict[int, list[Word]] = {}
            for index, table in tables:
                page_index = table["page"]
                if not 0 <= page_index < len(document):
                    continue
                page = document[page_index]
                frame = PageFrame.of(page)
                if page_index not in words_cache:
                    words_cache[page_index] = page_words(glyph_reader, page_index, frame)
                others = [e for e in by_page[page_index] if e is not table]
                region, words = table_region(table, others, words_cache[page_index])
                prepared.append(TablePrep(
                    index, page_index, region, words, render_crop(page, region),
                    _page_has_hidden_text(pdf.pages[page_index]),
                    {"element": index, "page": page_index,
                     "docling_shape": [table.get("num_rows"), table.get("num_cols")],
                     "region_widened": region != table["bbox"]},
                ))
    finally:
        document.close()
    return prepared


def rebuild_tables(
    elements: list[dict],
    prepared: list[TablePrep],
    answers: list[ModelAnswer | None],
    *,
    min_coverage: float,
    min_coverage_scan: float | None = None,
    correct_scan_text: bool,
) -> tuple[list[dict], list[dict]]:
    """The elements with each table whose model grid passes rebuilt."""
    updated = list(elements)
    reports = []
    for prep, answer in zip(prepared, answers, strict=True):
        report = dict(prep.report, scanned=prep.scanned)
        reports.append(report)
        if answer is None:
            report["result"] = "no answer"
            continue
        report["model_seconds"] = answer.seconds
        report["cached"] = answer.cached
        if answer.finish == "length":
            report["result"] = "answer cut off"
            continue
        grid = parse_otsl(answer.text)
        if grid is None:
            report["result"] = "no grid"
            continue
        report["model_shape"] = [grid.rows, grid.cols]
        report["grid_problems"] = grid.problems
        if grid.rows < 2 or grid.cols < 2:
            report["result"] = "grid under 2x2"
            continue
        assignment = assign_words(grid, prep.words)
        report.update(assignment.stats)
        coverage = min(assignment.stats["page_coverage"], assignment.stats["model_coverage"])
        report["coverage"] = coverage
        threshold = min_coverage
        if prep.scanned and min_coverage_scan is not None:
            threshold = min_coverage_scan
        if coverage < threshold:
            report["result"] = "low coverage"
            continue
        comparison = compare_grids(elements[prep.element_index], grid, assignment)
        report.update(comparison)
        # The model ran complete rows together, and split nothing Docling
        # joined: Docling's grid is the finer reading of the same words.
        if comparison["model_cells_merging_rows"] and not comparison["docling_cells_split"]:
            report["result"] = "model merged rows"
            continue
        # Numbers the page draws that the model left out (a dropped column)
        # would join a neighboring cell and change its value.
        if assignment.stats["unmatched_numbers"] > max(2, 0.02 * assignment.stats["words"]):
            report["result"] = "model left out numbers"
            continue
        table, text_report = rebuild_table(
            elements[prep.element_index], grid, assignment, prep.words,
            scanned=prep.scanned, correct_text=correct_scan_text,
        )
        report.update(text_report)
        updated[prep.element_index] = table
        report["result"] = "replaced"
    return updated, reports


async def rewrite_tables(
    pdf_path: Path,
    structure_json: dict,
    reader: TableReader,
    *,
    min_coverage: float,
    min_coverage_scan: float | None = None,
    correct_scan_text: bool = False,
) -> tuple[dict, dict]:
    """The structure with its tables rebuilt from the model's grids where they
    pass, and a report of every table. The PDF is the one the tagger gets."""
    started = time.monotonic()
    elements = list(structure_json.get("elements") or [])
    prepared = await asyncio.to_thread(prepare_tables, pdf_path, elements)
    answers: list[ModelAnswer | None] = []
    unavailable: str | None = None
    for prep in prepared:
        if unavailable is not None:
            answers.append(None)
            continue
        try:
            answers.append(await reader.read(prep.png))
        except TableModelUnavailable as exc:
            unavailable = str(exc)
            logger.warning("table model unavailable: %s", unavailable)
            answers.append(None)
        except (ValueError, KeyError) as exc:
            logger.warning("table model failed for table %s: %r", prep.element_index, exc)
            answers.append(None)
    updated, reports = await asyncio.to_thread(
        rebuild_tables, elements, prepared, answers,
        min_coverage=min_coverage, min_coverage_scan=min_coverage_scan,
        correct_scan_text=correct_scan_text,
    )
    structure = dict(structure_json, elements=updated)
    audit = {
        "model": reader.model,
        "tables": len(prepared),
        "replaced": sum(r.get("result") == "replaced" for r in reports),
        "seconds": round(time.monotonic() - started, 3),
        "unavailable": unavailable,
        "table_reports": reports,
    }
    return structure, audit
