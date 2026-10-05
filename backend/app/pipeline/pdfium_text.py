"""Page text as pdfium extracts it: by marked content, or inside a box.

pdfium is the PDF engine of Chrome, whose accessibility tree screen readers
read. Its text page infers each word space from the font's own space width,
expands ligatures, drops a string drawn again over itself (fake bold), and
marks a hyphen that breaks a word at a line end. Each character belongs to
the text object that drew it, and so to that object's marked content. pdfium
tells which characters lie under marked content with /ActualText, but its
mark API does not decode the text reliably (it drops characters past U+00FF),
so the text itself comes from the caller's own parse of the content stream.
The screen-reader scorer reads marked content this way, and the tagger takes
an element's text from the words inside its boxes this way.
"""

from __future__ import annotations

import ctypes
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from app.pipeline.page_glyphs import StreamKey

# pdfium's code for a hyphen that breaks a word at a line end.
LINE_END_HYPHEN = "\x02"


@dataclass
class PageText:
    chars: list[str] = field(default_factory=list)
    # Characters pdfium inferred: a word space or line break the file omits.
    generated: list[bool] = field(default_factory=list)
    # The marked content with /ActualText covering each character, or None.
    actual: list[int | None] = field(default_factory=list)
    # The character indices of each marked-content sequence, in pdfium's order.
    marked: dict[tuple[StreamKey, int], list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def text(self, indices: list[int], actual_texts: list[str] | None = None) -> str:
        """The characters at ``indices`` as text: pdfium's inferred spaces
        and line breaks as word breaks, a space where the run skips other
        text, and a word broken at a line end joined again. With
        ``actual_texts``, the /ActualText of the marked content under which
        the characters lie, in order, each run of characters under one reads
        as the next of them."""
        replacements = iter(actual_texts or [])
        parts: list[str] = []
        for position, index in enumerate(indices):
            previous = indices[position - 1] if position else None
            if previous is not None and index != previous + 1:
                if self.breaks_between(previous, index):
                    parts.append(" ")
            actual = self.actual[index] if actual_texts else None
            if actual is not None:
                if previous is None or self.actual[previous] != actual:
                    parts.append(next(replacements, ""))
            elif self.generated[index]:
                if previous is None or self.chars[previous] != LINE_END_HYPHEN:
                    parts.append(" ")
            elif self.chars[index] != LINE_END_HYPHEN:
                parts.append(self.chars[index])
        # pdfium gives UTF-16 code units; join surrogate pairs into characters.
        text = "".join(parts).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        return " ".join(text.split())

    def breaks_between(self, before: int, after: int) -> bool:
        """Whether a word break falls between two characters: other text
        lies between them, or pdfium inferred a space or line break at or
        between them, unless that line break follows a line-end hyphen."""
        if after <= before or any(not self.generated[i] for i in range(before + 1, after)):
            return True
        if self.chars[before] == LINE_END_HYPHEN:
            return False
        return any(self.generated[i] for i in range(before, after + 1))


Box = tuple[float, float, float, float]


@dataclass
class Word:
    """A run of characters pdfium did not separate with a space or line
    break, and the box around their glyphs (left, bottom, right, top). A
    word hyphenated at a line end is two words, one on each line."""

    start: int
    end: int
    box: Box


class PdfiumText:
    """The words pdfium extracts inside boxes on a document's pages."""

    def __init__(self, pdf_path: Path) -> None:
        self.document = pdfium.PdfDocument(pdf_path)
        self.pages: dict[int, tuple[PageText, list[Word]]] = {}

    def __enter__(self) -> PdfiumText:
        return self

    def __exit__(self, *_exc) -> None:
        self.document.close()

    def in_boxes(self, page_index: int, boxes: list[dict[str, float]]) -> str:
        """The text of the words inside these boxes (left, bottom, right, top
        in PDF user space), box after box. A word is inside a box that holds
        at least half of it, as Docling gives a word to the layout region
        holding the largest share of it: layout boxes are approximate, a
        scan's above all, and one that stops short of a word's last letters
        still holds the word, while one that grazes the next line does not
        take its words."""
        if page_index not in self.pages:
            self.pages[page_index] = _page_words(self.document[page_index])
        page, words = self.pages[page_index]
        texts = []
        for box in boxes:
            area = (box["l"], box["b"], box["r"], box["t"])
            indices = [
                index
                for word in words
                if _share_inside(word.box, area) >= 0.5
                for index in range(word.start, word.end)
            ]
            texts.append(page.text(indices))
        return " ".join(" ".join(texts).split())


def _share_inside(word: Box, box: Box) -> float:
    """The share of the word's box inside the box; along an axis on which
    the word has no extent, all of it or none."""
    share = 1.0
    for low, high in ((0, 2), (1, 3)):
        extent = word[high] - word[low]
        inside = min(word[high], box[high]) - max(word[low], box[low])
        if extent > 0:
            share *= max(inside, 0.0) / extent
        elif inside < 0:
            return 0.0
    return share


def _page_words(page: pdfium.PdfPage) -> tuple[PageText, list[Word]]:
    textpage = page.get_textpage()
    result = PageText()
    words: list[Word] = []
    for index in range(textpage.count_chars()):
        char = chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, index))
        generated = bool(pdfium_c.FPDFText_IsGenerated(textpage.raw, index))
        result.chars.append(char)
        result.generated.append(generated)
        result.actual.append(None)
        if generated or char.isspace():
            continue
        box = textpage.get_charbox(index)
        if words and words[-1].end == index and result.chars[index - 1] != LINE_END_HYPHEN:
            word = words[-1]
            word.end = index + 1
            word.box = (
                min(word.box[0], box[0]),
                min(word.box[1], box[1]),
                max(word.box[2], box[2]),
                max(word.box[3], box[3]),
            )
        else:
            words.append(Word(index, index + 1, box))
    return result, words


def read_page_text(pdf_path: Path) -> list[PageText]:
    """Each page's characters, with the marked content that drew them."""
    pages: list[PageText] = []
    document = pdfium.PdfDocument(pdf_path)
    try:
        with pikepdf.open(pdf_path) as pdf:
            for page_index, page in enumerate(document):
                pages.append(_page_text(page, _form_paths_in_order(pdf.pages[page_index].obj)))
    finally:
        document.close()
    return pages


def _page_text(page: pdfium.PdfPage, form_paths: list[StreamKey]) -> PageText:
    streams = _text_object_streams(page, form_paths)
    textpage = page.get_textpage()
    result = PageText()
    for index in range(textpage.count_chars()):
        result.chars.append(chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, index)))
        generated = bool(pdfium_c.FPDFText_IsGenerated(textpage.raw, index))
        result.generated.append(generated)
        text_object = pdfium_c.FPDFText_GetTextObject(textpage.raw, index)
        result.actual.append(_actual_text_mark(text_object) if text_object else None)
        if not text_object:
            continue
        mcid = pdfium_c.FPDFPageObj_GetMarkedContentID(text_object)
        stream = streams.get(_address(text_object))
        if mcid >= 0 and stream is not None:
            result.marked[(stream, mcid)].append(index)
    return result


def _address(pointer) -> int:
    return ctypes.cast(pointer, ctypes.c_void_p).value or 0


def _actual_text_mark(text_object) -> int | None:
    """The identity of marked content with /ActualText around a text object."""
    for index in range(pdfium_c.FPDFPageObj_CountMarks(text_object)):
        mark = pdfium_c.FPDFPageObj_GetMark(text_object, index)
        length = ctypes.c_ulong()
        if pdfium_c.FPDFPageObjMark_GetParamStringValue(mark, b"ActualText", None, 0, length):
            return _address(mark)
    return None


def _text_object_streams(page: pdfium.PdfPage, form_paths: list[StreamKey]) -> dict[int, StreamKey]:
    """The content stream (see page_glyphs.StreamKey) each text object is
    drawn in. pdfium lists a page's objects, forms' contents included, in
    drawing order, so its forms pair one to one with the page's form Do
    operators; when they do not, text inside forms is left unread. A form
    drawn more than once is read on its first drawing, as page_glyphs does."""
    paths = iter(form_paths)
    forms = sum(
        1
        for page_object in page.get_objects(max_depth=15)
        if page_object.type == pdfium_c.FPDF_PAGEOBJ_FORM
    )
    aligned = forms == len(form_paths)
    open_forms: dict[int, StreamKey | None] = {}
    drawn: set[StreamKey] = set()
    streams: dict[int, StreamKey] = {}
    for page_object in page.get_objects(max_depth=15):
        level = page_object.level
        stream = open_forms.get(level - 1) if level else ()
        if page_object.type == pdfium_c.FPDF_PAGEOBJ_FORM:
            path = next(paths, None) if aligned else None
            readable = path is not None and stream is not None and path not in drawn
            open_forms[level] = path if readable else None
            if path is not None:
                drawn.add(path)
        elif page_object.type == pdfium_c.FPDF_PAGEOBJ_TEXT and stream is not None:
            streams[_address(page_object.raw)] = stream
    return streams


def _form_paths_in_order(owner: pikepdf.Object, prefix: StreamKey = ()) -> list[StreamKey]:
    """The name path of each Form XObject the content draws, in drawing
    order, depth first."""
    resources = owner.get("/Resources")
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    if not isinstance(xobjects, pikepdf.Dictionary):
        return []
    paths: list[StreamKey] = []
    try:
        instructions = pikepdf.parse_content_stream(owner)
    except pikepdf.PdfError:
        return []
    for instruction in instructions:
        if str(instruction.operator) != "Do" or not instruction.operands:
            continue
        name = instruction.operands[0]
        xobject = xobjects.get(name)
        if isinstance(xobject, pikepdf.Stream) and xobject.get("/Subtype") == "/Form":
            path = (*prefix, str(name).lstrip("/"))
            paths.append(path)
            paths.extend(_form_paths_in_order(xobject, path))
    return paths
