"""The glyphs a PDF page draws, read by pdfminer.

One pass over a page records every glyph with where it lands, the content
stream and text-showing operator (``Tj``, ``TJ``, ``'``, ``"``) that drew it,
the ``TJ`` array element it came from, and the marked content around it.
From that:

- the tagger gets each text-showing operator's text and position, per
  content stream, in the order its parsed instructions show them
  (``PageGlyphs.ops``);
- the screen-reader reader gets what each marked-content ID reads as: its
  glyphs, with marked content that carries ``/ActualText`` read as that text
  instead (``PageGlyphs.marked``, ISO 32000-1, 14.9.4).

pdfminer interprets the whole text state (text and line matrices, leading,
font widths, ``TJ`` offsets, character and word spacing, horizontal scaling,
rise) and decodes text through each font's encoding and ToUnicode map. A
content stream is keyed by the names of the Form XObjects drawn to reach it
from the page (``()`` for the page's own content), which does not depend on
object numbers. Coordinates are raw user space (no MediaBox offset or page
rotation), the frame the tagger's structure elements use.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from pdfminer.cmapdb import IdentityCMap, IdentityCMapByte
from pdfminer.converter import PDFLayoutAnalyzer
from pdfminer.layout import LTChar
from pdfminer.pdfdocument import PDFDocument, PDFNoPageLabels
from pdfminer.pdffont import PDFSimpleFont
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.psparser import literal_name
from pdfminer.utils import decode_text

from app.pipeline.pdf_repair import pdfminer_readable

logger = logging.getLogger(__name__)

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# Names of the Form XObjects drawn to reach a content stream, without the
# leading slash; () is the page's own content.
StreamKey = tuple[str, ...]


@dataclass(frozen=True)
class MeasuredGlyph:
    text: str
    bbox: dict[str, float]
    # Index of the TJ array element that drew the glyph (0 for Tj, ', ").
    element: int = 0
    # The glyph's code: its byte span in that string, so a string can be cut
    # between glyphs.
    code: tuple[int, int] = (0, 0)


def code_spans(font, string: bytes) -> list[tuple[int, int]]:
    """The byte span of each glyph code in a string, as the font reads it:
    one byte for a simple font, two for an Identity CMap, and otherwise
    wherever decoding a longer prefix yields another code."""
    if isinstance(font, PDFSimpleFont) or isinstance(getattr(font, "cmap", None), IdentityCMapByte):
        width = 1
    elif isinstance(getattr(font, "cmap", None), IdentityCMap):
        width = 2
    else:
        spans: list[tuple[int, int]] = []
        start = seen = 0
        for end in range(1, len(string) + 1):
            count = len(list(font.decode(string[:end])))
            if count > seen:
                spans.append((start, end))
                start, seen = end, count
        return spans
    return [(i, i + width) for i in range(0, len(string) - width + 1, width)]


def union_bbox(boxes: Iterable[dict[str, float]]) -> dict[str, float] | None:
    boxes = list(boxes)
    if not boxes:
        return None
    return {
        "l": min(box["l"] for box in boxes),
        "b": min(box["b"] for box in boxes),
        "r": max(box["r"] for box in boxes),
        "t": max(box["t"] for box in boxes),
    }


def separated(previous: MeasuredGlyph, glyph: MeasuredGlyph) -> bool:
    """Whether a word break falls between two glyphs drawn one after the
    other: a new line, or a gap wider than 0.15 em."""
    size = max(glyph.bbox["t"] - glyph.bbox["b"], previous.bbox["t"] - previous.bbox["b"], 1.0)
    new_line = abs(glyph.bbox["b"] - previous.bbox["b"]) > 0.5 * size
    return new_line or glyph.bbox["l"] - previous.bbox["r"] > 0.15 * size


def join_glyphs(items: Iterable[MeasuredGlyph | str]) -> str:
    """Glyph text in drawing order, with a space at word breaks (many PDFs
    space words with TJ offsets, not space glyphs). A string item, an
    /ActualText, stands as its own word."""
    parts: list[str] = []
    previous: MeasuredGlyph | None = None
    for item in items:
        glyph = None if isinstance(item, str) else item
        if (
            parts
            and not parts[-1].endswith(" ")
            and (glyph is None or previous is None or separated(previous, glyph))
        ):
            parts.append(" ")
        parts.append(item if glyph is None else glyph.text)
        previous = glyph
    return "".join(parts)


@dataclass
class MeasuredTextOp:
    glyphs: list[MeasuredGlyph] = field(default_factory=list)

    @property
    def text(self) -> str:
        return join_glyphs(self.glyphs)

    @property
    def bbox(self) -> dict[str, float] | None:
        return union_bbox(glyph.bbox for glyph in self.glyphs)


@dataclass
class PageGlyphs:
    # Text-showing operators per content stream, in content order.
    ops: dict[StreamKey, list[MeasuredTextOp]] = field(default_factory=dict)
    # What each marked-content ID reads as, keyed by (stream, MCID).
    marked: dict[tuple[StreamKey, int], list[MeasuredGlyph | str]] = field(
        default_factory=lambda: defaultdict(list)
    )


@dataclass
class _MarkedContent:
    stream: StreamKey
    mcid: int | None
    # Read as /ActualText, here or in enclosing marked content.
    replaced: bool
    # Glyphs are kept only on a stream's first drawing.
    recording: bool


class _Recorder(PDFLayoutAnalyzer):
    def __init__(self, manager: PDFResourceManager) -> None:
        super().__init__(manager, laparams=None)
        self.page = PageGlyphs()
        self.streams: list[tuple[StreamKey, bool]] = []
        self.form_name: str | None = None
        self.marked: list[_MarkedContent] = []
        self.op: MeasuredTextOp | None = None
        self.op_elements: list[int] = []

    def enter_stream(self) -> None:
        key: StreamKey = (
            (*self.streams[-1][0], self.form_name)
            if self.streams and self.form_name is not None
            else ()
        )
        self.form_name = None
        # A form drawn more than once is recorded on its first drawing.
        recording = key not in self.page.ops
        if recording:
            self.page.ops[key] = []
        self.streams.append((key, recording))

    def leave_stream(self) -> None:
        self.streams.pop()

    def _target(self) -> _MarkedContent | None:
        return next((entry for entry in reversed(self.marked) if entry.mcid is not None), None)

    def begin_tag(self, tag, props=None) -> None:
        props = props if isinstance(props, dict) else {}
        mcid = props.get("MCID")
        actual_text = props.get("ActualText")
        enclosing_replaced = bool(self.marked) and self.marked[-1].replaced
        stream, recording = self.streams[-1]
        self.marked.append(
            _MarkedContent(
                stream=stream,
                mcid=int(mcid) if isinstance(mcid, int) else None,
                replaced=enclosing_replaced or actual_text is not None,
                recording=recording,
            )
        )
        target = self._target()
        if actual_text is not None and not enclosing_replaced and target and target.recording:
            text = decode_text(actual_text) if isinstance(actual_text, bytes) else str(actual_text)
            self.page.marked[(target.stream, target.mcid)].append(text)

    def end_tag(self) -> None:
        if self.marked:
            self.marked.pop()

    def begin_text_op(self, seq, font) -> None:
        self.op = MeasuredTextOp()
        stream, recording = self.streams[-1]
        if recording:
            self.page.ops[stream].append(self.op)
        # The TJ element and code span of each glyph pdfminer will draw, in
        # order.
        self.op_elements = [
            (index, span)
            for index, element in enumerate(seq)
            if isinstance(element, bytes) and font is not None
            for span in code_spans(font, element)
        ]

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        advance = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        char = self.cur_item._objs[-1]
        if not isinstance(char, LTChar) or self.op is None:
            return advance
        element, code = self.op_elements[len(self.op.glyphs)]
        glyph = MeasuredGlyph(
            text=char.get_text(),
            bbox={"l": char.x0, "b": char.y0, "r": char.x1, "t": char.y1},
            element=element,
            code=code,
        )
        self.op.glyphs.append(glyph)
        target = self._target()
        replaced = bool(self.marked) and self.marked[-1].replaced
        if target and target.recording and not replaced:
            self.page.marked[(target.stream, target.mcid)].append(glyph)
        return advance

    def receive_layout(self, ltpage) -> None:
        return None


class _Interpreter(PDFPageInterpreter):
    device: _Recorder

    def render_contents(self, resources, streams, ctm=IDENTITY) -> None:
        self.device.enter_stream()
        try:
            super().render_contents(resources, streams, ctm=ctm)
        finally:
            self.device.leave_stream()

    def do_Do(self, xobjid_arg) -> None:
        self.device.form_name = literal_name(xobjid_arg)
        try:
            super().do_Do(xobjid_arg)
        finally:
            self.device.form_name = None

    def do_TJ(self, seq) -> None:
        # Every text-showing operator comes through here (Tj, ' and " call
        # it), and is counted even when it draws nothing.
        self.device.begin_text_op(seq, self.textstate.font)
        super().do_TJ(seq)

    def process_page(self, page: PDFPage) -> None:
        self.device.begin_page(page, IDENTITY)
        self.render_contents(page.resources, page.contents, ctm=IDENTITY)
        self.device.end_page(page)


class _UnlabeledDocument(PDFDocument):
    """Page labels are never needed here, and pdfminer fails on some."""

    def get_page_labels(self):
        raise PDFNoPageLabels


class PdfGlyphReader:
    """Opens a PDF once and reads its pages' glyphs one page at a time. A
    page pdfminer cannot read comes back empty, with a warning."""

    def __init__(self, pdf_path: Path) -> None:
        self._resources = ExitStack()
        self._manager = PDFResourceManager()
        self._pages: list[PDFPage] = []
        try:
            readable = self._resources.enter_context(pdfminer_readable(pdf_path))
            handle = self._resources.enter_context(readable.open("rb"))
            document = _UnlabeledDocument(PDFParser(handle))
            self._pages = list(PDFPage.create_pages(document))
        except Exception as exc:  # noqa: BLE001 - an unreadable file reads as no glyphs
            logger.warning("Could not read glyphs from %s: %s", pdf_path.name, exc)

    def __enter__(self) -> PdfGlyphReader:
        return self

    def __exit__(self, *exc_info) -> None:
        self._resources.close()

    def page(self, index: int) -> PageGlyphs:
        if not 0 <= index < len(self._pages):
            return PageGlyphs()
        device = _Recorder(self._manager)
        try:
            _Interpreter(self._manager, device).process_page(self._pages[index])
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
            logger.warning("Could not read glyphs on page %s: %s", index + 1, exc)
            return PageGlyphs()
        return device.page
