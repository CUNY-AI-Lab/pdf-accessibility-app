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
import io
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from app.pipeline.page_glyphs import StreamKey
from app.services.pdf_objects import pdf_named_resource

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
        lies between them, either is a space the PDF draws, or pdfium
        inferred a space or line break at or between them, unless that line
        break follows a line-end hyphen."""
        if after <= before or any(not self.generated[i] for i in range(before + 1, after)):
            return True
        if self.chars[before].isspace() or self.chars[after].isspace():
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

    def words(
        self, page_index: int, *, excluded_indices: set[int] | None = None
    ) -> list[tuple[str, Box]]:
        """Raw words and positions, optionally omitting specific PDFium characters.

        Indices share the character frame of ``read_page_text``. Filtering
        happens before word assembly, including line-end hyphen joining.
        """
        if page_index not in self.pages:
            self.pages[page_index] = _page_words(self.document[page_index])
        page, words = self.pages[page_index]
        result: list[tuple[str, Box]] = []
        previous_end: int | None = None
        for index, word in enumerate(words):
            indices = [i for i in range(word.start, word.end) if i not in (excluded_indices or ())]
            if not indices:
                previous_end = None
                continue
            text = page.text(indices)
            if index and previous_end is not None and page.chars[previous_end - 1] == LINE_END_HYPHEN:
                previous, box = result[-1]
                result[-1] = (previous + text, box)
            else:
                result.append((text, word.box))
            previous_end = word.end
        return result

    def duplicate_indices(self, page_index: int, *, preferred_indices: set[int]) -> set[int]:
        """Coincident copies of identical glyphs (e.g. fake bold or duplicate layers).

        Require every glyph-box edge to agree within half a point and a
        marked counterpart. Only the unmarked copy is omitted. Equal words
        elsewhere and two unmarked copies remain eligible.
        """
        textpage = self.document[page_index].get_textpage()
        buckets: dict[tuple[str, int, int], list[tuple[int, Box]]] = defaultdict(list)
        duplicates: set[int] = set()
        try:
            for index in range(textpage.count_chars()):
                char = chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, index))
                if char.isspace() or pdfium_c.FPDFText_IsGenerated(textpage.raw, index):
                    continue
                box = textpage.get_charbox(index)
                x, y = round(box[0] * 2), round(box[1] * 2)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for previous, previous_box in buckets.get((char, x + dx, y + dy), ()):
                            if (index in preferred_indices) == (previous in preferred_indices):
                                continue
                            if previous not in duplicates and all(
                                abs(a - b) <= 0.5 for a, b in zip(box, previous_box, strict=True)
                            ):
                                if index in preferred_indices:
                                    duplicates.add(previous)
                                else:
                                    duplicates.add(index)
                buckets[(char, x, y)].append((index, box))
        finally:
            textpage.close()
        return duplicates

    def spatial_exclusions(
        self, page_index: int, figure_boxes: list[Box],
    ) -> tuple[set[int], set[int]]:
        """Whole-word indices outside the visible page, and Artifact words
        at least 60% inside a tagged Figure's bounds. Partial visible words
        and non-Artifact text remain eligible under these rules."""
        if page_index not in self.pages:
            self.pages[page_index] = _page_words(self.document[page_index])
        page, words = self.pages[page_index]
        visible = self.document[page_index].get_bbox()
        artifacts = _artifact_indices(self.document[page_index]) if figure_boxes else set()
        outside: set[int] = set()
        in_figures: set[int] = set()
        for word in words:
            indices = range(word.start, word.end)
            if _share_inside(word.box, visible) == 0:
                outside.update(indices)
            elif (
                figure_boxes and all(index in artifacts for index in indices)
                and any(_share_inside(word.box, box) >= 0.60 for box in figure_boxes)
            ):
                in_figures.update(indices)
        return outside, in_figures

    def marked_bounds(
        self, page_index: int, owner: pikepdf.Object,
    ) -> dict[tuple[StreamKey, int], list[Box]]:
        """Painted MCID bounds in page coordinates, with Form transforms
        and clip paths. A clipped full-page image supplies its clipped
        bounds, not a rectangle that could exempt the rest of the page."""
        page = self.document[page_index]
        references = _object_marked_refs(page, _form_paths_in_order(owner))
        matrices: dict[int, pdfium.PdfMatrix] = {}
        clips: dict[int, Box | None] = {}
        result: dict[tuple[StreamKey, int], list[Box]] = defaultdict(list)
        for obj in page.get_objects(max_depth=15):
            parent = matrices[obj.level - 1] if obj.level else pdfium.PdfMatrix()
            clip = clips.get(obj.level - 1) if obj.level else None
            local_clip = _object_clip_bounds(obj.raw)
            if local_clip is not None:
                local_clip = parent.on_rect(*local_clip)
                clip = _intersect_boxes(clip, local_clip) if clip is not None else local_clip
            if pdfium_c.FPDFPageObj_GetType(obj.raw) == pdfium_c.FPDF_PAGEOBJ_FORM:
                matrices[obj.level] = obj.get_matrix().multiply(parent)
                clips[obj.level] = clip
                continue
            refs = references.get(_address(obj.raw), ())
            if not refs:
                continue
            bounds = parent.on_rect(*obj.get_bounds())
            if not all(math.isfinite(value) for value in bounds):
                raise ValueError("Could not locate finite Figure content bounds")
            if clip is not None:
                bounds = _intersect_boxes(bounds, clip)
            if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
                for ref in refs:
                    result[ref].append(bounds)
        return result

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
    streams = _object_marked_refs(page, form_paths)
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
        for reference in streams.get(_address(text_object), ()):
            result.marked[reference].append(index)
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


def _object_mcids(page_object) -> set[int]:
    """All enclosing MCIDs, not only PDFium's first (outermost) MCID."""
    mcids: set[int] = set()
    for index in range(pdfium_c.FPDFPageObj_CountMarks(page_object)):
        mark = pdfium_c.FPDFPageObj_GetMark(page_object, index)
        value = ctypes.c_int()
        if pdfium_c.FPDFPageObjMark_GetParamIntValue(mark, b"MCID", value) and value.value >= 0:
            mcids.add(value.value)
    return mcids


def _object_marked_refs(
    page: pdfium.PdfPage, form_paths: list[StreamKey]
) -> dict[int, set[tuple[StreamKey, int]]]:
    """The marked content each page object belongs to, including enclosing
    Form invocations and nested BDC marks. pdfium lists a page's objects in
    drawing order, so its forms pair one to one with the page's form Do
    operators. When alignment fails, only known caller marks remain usable.
    A form's own MCIDs are read on its first drawing, as page_glyphs does;
    separate invocations can still belong to different caller MCIDs."""
    paths = iter(form_paths)
    forms = sum(
        1
        for page_object in page.get_objects(max_depth=15)
        if pdfium_c.FPDFPageObj_GetType(page_object.raw) == pdfium_c.FPDF_PAGEOBJ_FORM
    )
    aligned = forms == len(form_paths)
    open_forms: dict[int, StreamKey | None] = {}
    form_marks: dict[int, set[tuple[StreamKey, int]]] = {}
    drawn: set[StreamKey] = set()
    streams: dict[int, set[tuple[StreamKey, int]]] = {}
    for page_object in page.get_objects(max_depth=15):
        level = page_object.level
        stream = open_forms.get(level - 1) if level else ()
        references = set(form_marks.get(level - 1, ())) if level else set()
        if stream is not None:
            references.update((stream, mcid) for mcid in _object_mcids(page_object.raw))
        streams[_address(page_object.raw)] = references
        if pdfium_c.FPDFPageObj_GetType(page_object.raw) == pdfium_c.FPDF_PAGEOBJ_FORM:
            path = next(paths, None) if aligned else None
            readable = path is not None and stream is not None and path not in drawn
            open_forms[level] = path if readable else None
            form_marks[level] = references
            if path is not None:
                drawn.add(path)
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
        xobject = pdf_named_resource(xobjects, name)
        if isinstance(xobject, pikepdf.Stream) and xobject.get("/Subtype") == "/Form":
            path = (*prefix, str(name).lstrip("/"))
            paths.append(path)
            paths.extend(_form_paths_in_order(xobject, path))
    return paths


def pagination_artifact_indices(pdf_path: Path) -> list[set[int]]:
    """Characters in typed Pagination/Header/Footer artifacts in the outer
    top/bottom 10% of each displayed page. Untyped artifacts stay eligible.

    PDFium exposes mark names and string parameters, but cannot read PDF
    Name parameter values. In a memory-only copy, convert the recognized
    artifact parameters to strings. Content, geometry, and character order
    stay the same; indices refer to the original PDFium text page.
    """
    with pikepdf.open(pdf_path) as pdf:
        owners = [(page.obj, page.obj.get("/Resources")) for page in pdf.pages]
        owners.extend(
            (obj, obj.get("/Resources")) for obj in pdf.objects
            if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Form"
        )
        changed = False
        for owner, resources in owners:
            instructions = pikepdf.parse_content_stream(owner)
            rewritten = []
            owner_changed = False
            for instruction in instructions:
                operands, operator = instruction.operands, instruction.operator
                if str(operator) == "BDC" and len(operands) == 2 and operands[0] == "/Artifact":
                    props = operands[1]
                    if isinstance(props, pikepdf.Name) and isinstance(resources, pikepdf.Dictionary):
                        properties = resources.get("/Properties")
                        props = properties.get(props) if isinstance(properties, pikepdf.Dictionary) else None
                    if isinstance(props, pikepdf.Dictionary) and (
                        props.get("/Type") == "/Pagination"
                        or props.get("/Subtype") in ("/Header", "/Footer")
                    ):
                        props = pikepdf.Dictionary(props)
                        for key in ("/Type", "/Subtype"):
                            value = props.get(key)
                            if isinstance(value, pikepdf.Name):
                                props[key] = pikepdf.String(str(value).lstrip("/"))
                        instruction = pikepdf.ContentStreamInstruction([operands[0], props], operator)
                        owner_changed = True
                rewritten.append(instruction)
            if owner_changed:
                contents = pikepdf.unparse_content_stream(rewritten)
                if isinstance(owner, pikepdf.Stream):
                    owner.write(contents)
                else:
                    owner.Contents = pdf.make_stream(contents)
                changed = True
        if not changed:
            return [set() for _ in pdf.pages]
        buffer = io.BytesIO()
        pdf.save(buffer)
    with pdfium.PdfDocument(buffer.getvalue()) as document:
        return [_pagination_page_indices(page) for page in document]


def _mark_string(mark, key: bytes | None = None) -> str:
    length = ctypes.c_ulong()
    args = (mark,) if key is None else (mark, key)
    read = pdfium_c.FPDFPageObjMark_GetName if key is None else pdfium_c.FPDFPageObjMark_GetParamStringValue
    if not read(*args, None, 0, length):
        return ""
    buffer = (pdfium_c.FPDF_WCHAR * ((length.value + 1) // 2))()
    if not read(*args, buffer, ctypes.sizeof(buffer), length):
        return ""
    return bytes(buffer)[:length.value].decode("utf-16-le").rstrip("\0")


def _pagination_page_indices(page: pdfium.PdfPage) -> set[int]:
    objects: set[int] = set()
    enclosing: dict[int, bool] = {}
    for obj in page.get_objects(max_depth=15):
        pagination = enclosing.get(obj.level - 1, False) if obj.level else False
        for index in range(pdfium_c.FPDFPageObj_CountMarks(obj.raw)):
            mark = pdfium_c.FPDFPageObj_GetMark(obj.raw, index)
            if _mark_string(mark) == "Artifact" and (
                _mark_string(mark, b"Type") == "Pagination"
                or _mark_string(mark, b"Subtype") in {"Header", "Footer"}
            ):
                pagination = True
        if pdfium_c.FPDFPageObj_GetType(obj.raw) == pdfium_c.FPDF_PAGEOBJ_FORM:
            enclosing[obj.level] = pagination
        elif pagination and pdfium_c.FPDFPageObj_GetType(obj.raw) == pdfium_c.FPDF_PAGEOBJ_TEXT:
            objects.add(_address(obj.raw))
    marked: set[int] = set()
    textpage = page.get_textpage()
    try:
        for index in range(textpage.count_chars()):
            obj = pdfium_c.FPDFText_GetTextObject(textpage.raw, index)
            if obj and _address(obj) in objects:
                marked.add(index)
    finally:
        textpage.close()
    # Apply the band to whole words in displayed page coordinates. Testing
    # individual glyphs would cut words at the band edge; raw PDF coordinates
    # would treat side furniture on rotated pages as a top/bottom header.
    _, words = _page_words(page)
    indices: set[int] = set()
    device_x, device_y = ctypes.c_int(), ctypes.c_int()
    for word in words:
        if not all(index in marked for index in range(word.start, word.end)):
            continue
        left, bottom, right, top = word.box
        if not pdfium_c.FPDF_PageToDevice(
            page.raw, 0, 0, 1_000_000, 1_000_000, 0,
            (left + right) / 2, (bottom + top) / 2, device_x, device_y,
        ):
            raise ValueError("Could not locate pagination text on the page")
        if device_y.value <= 100_000 or device_y.value >= 900_000:
            indices.update(range(word.start, word.end))
    return indices


def _intersect_boxes(first: Box, second: Box) -> Box:
    left, bottom = max(first[0], second[0]), max(first[1], second[1])
    right, top = min(first[2], second[2]), min(first[3], second[3])
    return left, bottom, max(left, right), max(bottom, top)


def _object_clip_bounds(obj) -> Box | None:
    clip = pdfium_c.FPDFPageObj_GetClipPath(obj)
    if not clip:
        return None
    bounds = None
    count = pdfium_c.FPDFClipPath_CountPaths(clip)
    # PDFium returns -1 for an object with an empty internal clip path,
    # even when GetClipPath supplies a non-null wrapper. No clip is active.
    if count <= 0:
        return None
    for path_index in range(count):
        points = []
        segments = pdfium_c.FPDFClipPath_CountPathSegments(clip, path_index)
        if segments < 0:
            raise ValueError("Could not read Figure clip segments")
        for index in range(segments):
            segment = pdfium_c.FPDFClipPath_GetPathSegment(clip, path_index, index)
            x, y = ctypes.c_float(), ctypes.c_float()
            if not segment or not pdfium_c.FPDFPathSegment_GetPoint(segment, x, y):
                raise ValueError("Could not locate a Figure clip path")
            if not math.isfinite(x.value) or not math.isfinite(y.value):
                raise ValueError("Could not locate finite Figure clip bounds")
            points.append((x.value, y.value))
        current = (
            min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points),
        ) if points else (0.0, 0.0, 0.0, 0.0)
        bounds = _intersect_boxes(bounds, current) if bounds is not None else current
    return bounds


def _artifact_indices(page: pdfium.PdfPage) -> set[int]:
    objects: set[int] = set()
    enclosing: dict[int, bool] = {}
    for obj in page.get_objects(max_depth=15):
        artifact = enclosing.get(obj.level - 1, False) if obj.level else False
        for index in range(pdfium_c.FPDFPageObj_CountMarks(obj.raw)):
            if _mark_string(pdfium_c.FPDFPageObj_GetMark(obj.raw, index)) == "Artifact":
                artifact = True
        if pdfium_c.FPDFPageObj_GetType(obj.raw) == pdfium_c.FPDF_PAGEOBJ_FORM:
            enclosing[obj.level] = artifact
        elif artifact and pdfium_c.FPDFPageObj_GetType(obj.raw) == pdfium_c.FPDF_PAGEOBJ_TEXT:
            objects.add(_address(obj.raw))
    textpage = page.get_textpage()
    try:
        return {
            index for index in range(textpage.count_chars())
            if _address(pdfium_c.FPDFText_GetTextObject(textpage.raw, index)) in objects
        }
    finally:
        textpage.close()
