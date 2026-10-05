"""Text by glyph position as pdfium extracts it: the behaviors the tagger
relies on for element text."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.pdfium_text import read_page_text
from tests.pdf_fixtures import helvetica


def _page_text(tmp_path: Path, content: bytes) -> str:
    path = tmp_path / "page.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 300))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(content)
    pdf.save(path)
    with PdfGlyphReader(path) as reader:
        glyphs = [glyph for op in reader.page(0).ops[()] for glyph in op.glyphs]
    return read_page_text(path)[0].text_in_glyphs(glyphs)


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
def test_text_in_glyphs(tmp_path, content, heard):
    assert _page_text(tmp_path, content) == heard
