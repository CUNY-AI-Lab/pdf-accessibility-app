"""Pages whose text runs sideways, and Docling's reading of them upright.

Docling's layout and table models read each page image as it is displayed.
A table printed sideways on a portrait page (LaTeX's ``sidewaystable``, a
landscape table placed on a portrait report page) reaches TableFormer
sideways, and its grid comes back as nonsense: columns read as rows, a word
to a cell (docling-project/docling#1376, #2343). Docling's maintainers
suggest turning such pages upright before conversion; docling.rs does so
with an orientation probe and maps what it finds back to the displayed page
(docling.rs#937), and OCRmyPDF's ``--rotate-pages`` does the same for scans
whose text it recognizes sideways.

Here the evidence is the page's own text: a page where a strict majority of
the characters run in one direction other than the displayed one is
sideways. Docling converts a copy whose /Rotate shows that text upright, and
the boxes it returns move back into the displayed page's frame (see
page_frame). The copy is only Docling's input; the document keeps its
/Rotate.
"""

from __future__ import annotations

import ctypes
import dataclasses
import logging
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from app.pipeline.page_frame import Box, PageFrame

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SidewaysPage:
    shown: PageFrame  # the page as the document displays it
    upright: PageFrame  # the page as Docling reads it, its text upright

    def to_shown(self, box: Box) -> Box:
        """A box in Docling's frame of the upright copy, in its frame of the
        page as shown; other keys are kept."""
        return self.shown.to_docling(self.upright.to_user_space(box))


def text_turns(page: pdfium.PdfPage) -> Counter[int]:
    """How many of the page's characters run each number of quarter turns
    counterclockwise, from each character's matrix in user space (as
    MeasuredGlyph.turn). Spaces and characters pdfium inferred do not count."""
    text_page = page.get_textpage()
    try:
        turns: Counter[int] = Counter()
        matrix = pdfium_c.FS_MATRIX()
        for index in range(text_page.count_chars()):
            if (
                pdfium_c.FPDFText_GetUnicode(text_page, index) <= 0x20
                or pdfium_c.FPDFText_IsGenerated(text_page, index) == 1
                or not pdfium_c.FPDFText_GetMatrix(text_page, index, ctypes.byref(matrix))
            ):
                continue
            turns[round(math.atan2(matrix.b, matrix.a) / (math.pi / 2)) % 4] += 1
        return turns
    finally:
        text_page.close()


def sideways_pages(pdf_path: Path) -> dict[int, SidewaysPage]:
    """Each page, by index, where a strict majority of the characters run
    one way other than upright as shown, with the frame that shows them
    upright. A file pdfium cannot read has none."""
    pages: dict[int, SidewaysPage] = {}
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except (pdfium.PdfiumError, OSError):
        logger.warning("Could not read %s to find sideways pages", pdf_path.name, exc_info=True)
        return pages
    try:
        for index, page in enumerate(document):
            try:
                turns = text_turns(page)
                if not turns:
                    continue
                turn, count = turns.most_common(1)[0]
                shown = PageFrame.of(page)
                if count * 2 > turns.total() and turn * 90 != shown.rotation:
                    pages[index] = SidewaysPage(
                        shown, dataclasses.replace(shown, rotation=turn * 90)
                    )
            finally:
                page.close()
    finally:
        document.close()
    return pages


def upright_copy(pdf_path: Path, directory: Path) -> tuple[Path, dict[int, SidewaysPage]]:
    """The file Docling should read for ``pdf_path``, and its sideways
    pages: the file itself when none is, else a copy in ``directory``, under
    the same name, whose sideways pages are shown upright. A copy that
    cannot be written leaves Docling the file itself."""
    pages = sideways_pages(pdf_path)
    if not pages:
        return pdf_path, {}
    copy = directory / pdf_path.name
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with pikepdf.open(pdf_path) as pdf:
            for index, page in pages.items():
                pdf.pages[index].Rotate = page.upright.rotation
            pdf.save(copy)
    except (pikepdf.PdfError, OSError):
        logger.warning("Could not turn %s's sideways pages upright", pdf_path.name, exc_info=True)
        return pdf_path, {}
    logger.info(
        "Docling reads %d sideways page(s) of %s upright: %s",
        len(pages),
        pdf_path.name,
        sorted(index + 1 for index in pages),
    )
    return copy, pages


def elements_to_shown(elements: list[dict], pages: dict[int, SidewaysPage]) -> None:
    """Move the boxes of elements Docling found on sideways pages (their
    boxes, extra boxes, and table cells' boxes) into the frame of the page
    as shown, in place."""
    for element in elements:
        index = element.get("page")
        if not isinstance(index, int) or (page := pages.get(index)) is None:
            continue
        for owner in [element, *(element.get("cells") or [])]:
            if isinstance(owner, dict) and isinstance(owner.get("bbox"), dict):
                owner["bbox"] = page.to_shown(owner["bbox"])
        if element.get("extra_bboxes"):
            element["extra_bboxes"] = [page.to_shown(box) for box in element["extra_bboxes"]]
