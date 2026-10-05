"""Text inside a box as pdfium extracts it: the behaviors the tagger relies
on for element text."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.pdfium_text import PdfiumText
from tests.pdf_fixtures import helvetica

WHOLE_PAGE = {"l": 0, "b": 0, "r": 400, "t": 300}


def _page_text(tmp_path: Path, content: bytes, box: dict[str, float] = WHOLE_PAGE) -> str:
    path = tmp_path / "page.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 300))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(content)
    pdf.save(path)
    with PdfiumText(path) as pdfium_text:
        return pdfium_text.in_boxes(0, [box])


@pytest.mark.parametrize(
    ("content", "heard"),
    [
        # Words placed by TJ offsets, with no space characters.
        (b"BT /F1 12 Tf 20 250 Td [(In) -600 (addition,) -600 (the)] TJ ET", "In addition, the"),
        # Letter spacing (Tc) within one word.
        (b"BT /F1 12 Tf 4 Tc 20 250 Td (SPACED) Tj ET", "SPACED"),
        # Fake bold: the same string drawn twice, a fraction of a point apart.
        (
            b"BT /F1 12 Tf 20 250 Td (Errors) Tj ET BT /F1 12 Tf 20.3 250 Td (Errors) Tj ET",
            "Errors",
        ),
        # A word hyphenated at a line end.
        (b"BT /F1 12 Tf 20 250 Td (evalu-) Tj 0 -14 Td (ated) Tj ET", "evaluated"),
        # Curly quotes (WinAnsi 0x93, 0x94).
        (b"BT /F1 12 Tf 20 250 Td (\x93aircraft\x94) Tj ET", "“aircraft”"),
    ],
)
def test_text_in_boxes(tmp_path, content, heard):
    assert _page_text(tmp_path, content) == heard


def test_box_holds_the_words_it_overlaps(tmp_path):
    # "Improvement" in 12 pt Helvetica runs from x=20 to about x=85; a cell
    # box (as Docling gives on scans) that stops at x=70 still holds it,
    # and the word in the next column is left out.
    content = b"BT /F1 12 Tf 20 250 Td (Improvement) Tj 100 0 Td (Decrease) Tj ET"
    box = {"l": 15, "b": 245, "r": 70, "t": 262}
    assert _page_text(tmp_path, content, box) == "Improvement"


def test_box_leaves_out_the_line_below(tmp_path):
    content = b"BT /F1 12 Tf 20 250 Td (First line) Tj 0 -14 Td (second line) Tj ET"
    box = {"l": 15, "b": 248, "r": 200, "t": 262}
    assert _page_text(tmp_path, content, box) == "First line"
