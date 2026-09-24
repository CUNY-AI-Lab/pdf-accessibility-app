"""Where each text-showing operator draws its glyphs, measured by pdfminer.

The tagger rewrites content streams operator by operator, so for every
text-showing operator (``Tj``, ``TJ``, ``'``, ``"``) it needs the text shown
and where it lands. pdfminer interprets the whole text state (text and line
matrices, leading, font widths, ``TJ`` offsets, character and word spacing,
horizontal scaling, rise) and decodes text through each font's encoding and
ToUnicode map. Operators are listed in content order per content stream (the
page's own content, and each Form XObject the page draws), which is the order
the tagger's parsed instructions show them in.

Coordinates are raw user space (no MediaBox offset or page rotation applied),
the frame the tagger's structure elements use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pdfminer.converter import PDFLayoutAnalyzer
from pdfminer.layout import LTChar
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.psparser import literal_name

from app.pipeline.pdf_repair import pdfminer_readable

logger = logging.getLogger(__name__)

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# Content stream key: None for the page's own content, else the object number
# of the Form XObject.
StreamKey = int | None


@dataclass(frozen=True)
class MeasuredGlyph:
    text: str
    bbox: dict[str, float]
    # Index of the TJ array element that drew the glyph (0 for Tj, ', ").
    element: int


@dataclass
class MeasuredTextOp:
    glyphs: list[MeasuredGlyph] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The glyphs' text, with a space where a gap or a new line separates
        two glyphs (many PDFs space words with TJ offsets, not space glyphs)."""
        parts: list[str] = []
        previous: MeasuredGlyph | None = None
        for glyph in self.glyphs:
            if previous is not None and parts and not parts[-1].endswith(" "):
                size = max(glyph.bbox["t"] - glyph.bbox["b"], 1.0)
                new_line = abs(glyph.bbox["b"] - previous.bbox["b"]) > 0.5 * size
                if new_line or glyph.bbox["l"] - previous.bbox["r"] > 0.15 * size:
                    parts.append(" ")
            parts.append(glyph.text)
            previous = glyph
        return "".join(parts)

    @property
    def bbox(self) -> dict[str, float] | None:
        if not self.glyphs:
            return None
        return {
            "l": min(glyph.bbox["l"] for glyph in self.glyphs),
            "b": min(glyph.bbox["b"] for glyph in self.glyphs),
            "r": max(glyph.bbox["r"] for glyph in self.glyphs),
            "t": max(glyph.bbox["t"] for glyph in self.glyphs),
        }


class _GlyphRecorder(PDFLayoutAnalyzer):
    """Records each glyph under the text operator and stream drawing it."""

    def __init__(self, manager: PDFResourceManager) -> None:
        super().__init__(manager, laparams=None)
        self.ops: dict[StreamKey, list[MeasuredTextOp]] = {}
        self.stream_stack: list[tuple[StreamKey, bool]] = []
        self.pending_form: StreamKey = None
        self.current: MeasuredTextOp | None = None
        self.element = 0

    def enter_stream(self, key: StreamKey) -> None:
        # A form drawn more than once is measured on its first drawing only.
        recording = key not in self.ops
        if recording:
            self.ops[key] = []
        self.stream_stack.append((key, recording))

    def leave_stream(self) -> None:
        self.stream_stack.pop()

    def begin_text_op(self) -> None:
        self.current = MeasuredTextOp()
        key, recording = self.stream_stack[-1]
        if recording:
            self.ops[key].append(self.current)

    def render_string(self, textstate, seq, ncs, graphicstate) -> None:
        # One element at a time, so each glyph knows its TJ array element.
        for index, element in enumerate(seq):
            self.element = index
            super().render_string(textstate, [element], ncs, graphicstate)

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        advance = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        char = self.cur_item._objs[-1]
        if self.current is not None and isinstance(char, LTChar):
            self.current.glyphs.append(
                MeasuredGlyph(
                    text=char.get_text(),
                    bbox={"l": char.x0, "b": char.y0, "r": char.x1, "t": char.y1},
                    element=self.element,
                )
            )
        return advance

    def receive_layout(self, ltpage) -> None:
        return None


class _MeasuringInterpreter(PDFPageInterpreter):
    device: _GlyphRecorder

    def render_contents(self, resources, streams, ctm=IDENTITY) -> None:
        self.device.enter_stream(self.device.pending_form)
        self.device.pending_form = None
        try:
            super().render_contents(resources, streams, ctm=ctm)
        finally:
            self.device.leave_stream()

    def do_Do(self, xobjid_arg) -> None:
        xobj = self.xobjmap.get(literal_name(xobjid_arg))
        self.device.pending_form = getattr(xobj, "objid", None)
        try:
            super().do_Do(xobjid_arg)
        finally:
            self.device.pending_form = None

    def do_TJ(self, seq) -> None:
        # Every text-showing operator comes through here (Tj, ' and " call
        # it), and it is counted even when it draws nothing.
        self.device.begin_text_op()
        super().do_TJ(seq)

    def process_page(self, page: PDFPage) -> None:
        self.device.begin_page(page, IDENTITY)
        self.render_contents(page.resources, page.contents, ctm=IDENTITY)
        self.device.end_page(page)


def measure_text_ops(pdf_path: Path) -> dict[tuple[int, StreamKey], list[MeasuredTextOp]]:
    """The text operators of every page and of the forms each page draws,
    keyed by (page index, stream key), each list in content order. A page
    pdfminer cannot interpret is left out."""
    measured: dict[tuple[int, StreamKey], list[MeasuredTextOp]] = {}
    manager = PDFResourceManager()
    with pdfminer_readable(pdf_path) as readable, readable.open("rb") as handle:
        for page_index, page in enumerate(PDFPage.get_pages(handle)):
            device = _GlyphRecorder(manager)
            try:
                _MeasuringInterpreter(manager, device).process_page(page)
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
                logger.warning("Could not measure text on page %s: %s", page_index + 1, exc)
                continue
            for key, ops in device.ops.items():
                measured[(page_index, key)] = ops
    return measured
