"""Born-digital pages position text with scaled text matrices, TJ offsets,
and character spacing. The tagger must know where text really is, and must
tag a table row drawn by one TJ cell by cell without changing how the page
looks."""

from pathlib import Path

import pikepdf
import pytest
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar

from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica, rendered

CELLS = [["Year", "Crop", "Price"], ["1918", "Wheat", "2.20"], ["1919", "Corn", "1.51"]]
COLUMNS = [72, 200, 330]
ROW_TOPS = [700, 680, 660]
CHAR_SPACING = 0.05  # text space units: half a point under the 10x matrix


def _table_pdf(path: Path) -> None:
    """Each row is one TJ; offsets of -9000 (90 points under the 10x text
    matrix) separate its cells. Rows are 2 text-space units (20 points)
    apart."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    rows = []
    for index, row in enumerate(CELLS):
        cells = " -9000 ".join(f"({text})" for text in row)
        rows.append(("0 -2 Td " if index else "") + f"[{cells}] TJ")
    page.Contents = pdf.make_stream(
        (
            f"BT /F1 1 Tf {CHAR_SPACING} Tc 10 0 0 10 {COLUMNS[0]} {ROW_TOPS[0]} Tm "
            + " ".join(rows)
            + " ET"
        ).encode()
    )
    pdf.save(path)


def _table_structure(path: Path) -> dict:
    """The table as Docling reports it, with each cell's box fitted to the
    glyphs drawn for it."""
    glyphs = [glyph for op in PdfGlyphReader(path).page(0).ops[()] for glyph in op.glyphs]
    cells, cursor = [], 0
    for row, texts in enumerate(CELLS):
        for col, text in enumerate(texts):
            drawn = glyphs[cursor : cursor + len(text)]
            cursor += len(text)
            cells.append(
                {
                    "text": text,
                    "row": row,
                    "col": col,
                    "row_span": 1,
                    "col_span": 1,
                    "column_header": row == 0,
                    "row_header": False,
                    "is_header": row == 0,
                    "bbox": {
                        "l": drawn[0].bbox["l"] - 1,
                        "b": min(g.bbox["b"] for g in drawn) - 1,
                        "r": drawn[-1].bbox["r"] + 1,
                        "t": max(g.bbox["t"] for g in drawn) + 1,
                    },
                }
            )
    return {
        "title": "Prices",
        "elements": [
            {
                "type": "table",
                "page": 0,
                "bbox": {"l": 60, "b": 640, "r": 560, "t": 720},
                "num_rows": 3,
                "num_cols": 3,
                "cells": cells,
            }
        ],
    }


def test_glyph_positions_match_pdfminers_own_layout(tmp_path):
    path = tmp_path / "table.pdf"
    _table_pdf(path)

    measured = sorted(
        (glyph.text, round(glyph.bbox["l"], 3), round(glyph.bbox["b"], 3))
        for op in PdfGlyphReader(path).page(0).ops[()]
        for glyph in op.glyphs
    )
    # pdfminer's own layout groups glyphs into boxes, so compare as sets.
    reference = sorted(
        (char.get_text(), round(char.x0, 3), round(char.y0, 3))
        for page in extract_pages(path)
        for char in _chars(page)
    )
    assert measured == reference
    ops = PdfGlyphReader(path).page(0).ops[()]
    assert [op.text for op in ops] == [" ".join(row) for row in CELLS]
    assert ops[0].bbox["b"] - ops[1].bbox["b"] == pytest.approx(20, abs=0.5)


def _chars(item):
    if isinstance(item, LTChar):
        yield item
    for child in getattr(item, "_objs", []):
        yield from _chars(child)


@pytest.mark.asyncio
async def test_a_row_drawn_by_one_tj_is_tagged_cell_by_cell(tmp_path):
    source = tmp_path / "table.pdf"
    tagged = tmp_path / "tagged.pdf"
    _table_pdf(source)

    await tag_pdf(
        input_path=source,
        output_path=tagged,
        structure_json=_table_structure(source),
        alt_texts=[],
        language="en",
        original_filename=source.name,
    )

    heard = screen_reader_text(tagged, markdown=True)
    assert "".join(f"<th>{text}</th>" for text in CELLS[0]) in heard
    for row in CELLS[1:]:
        assert "".join(f"<td>{text}</td>" for text in row) in heard
    assert rendered(tagged) == rendered(source)


def _page_with_ocr_form(path: Path) -> None:
    """Native text drawn by one TJ spanning two paragraphs, then graphics
    state and an OCRmyPDF-style form holding an invisible text layer."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = helvetica(pdf)
    form = pdf.make_stream(b"BT 3 Tr /F1 10 Tf 72 500 Td (Hidden words here) Tj ET")
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = [0, 0, 612, 792]
    form.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=font), XObject=pikepdf.Dictionary({"/OCR-abc": form})
    )
    page.Contents = pdf.make_stream(
        b"BT /F1 10 Tf 72 700 Td [(Left) -5000 (Right)] TJ ET\n"
        b"3 w 0.5 g 100 600 200 20 re f\n"
        b"q 1 0 0 1 0 0 cm /OCR-abc Do Q\n"
    )
    pdf.save(path)


def _operators(path: Path) -> list[str]:
    with pikepdf.open(path) as pdf:
        return [
            str(instr.operator)
            for instr in pikepdf.parse_content_stream(pdf.pages[0])
            if str(instr.operator) not in ("BDC", "BMC", "EMC")
        ]


@pytest.mark.asyncio
async def test_splitting_a_tj_leaves_the_rest_of_the_page_in_place(tmp_path):
    source = tmp_path / "page.pdf"
    tagged = tmp_path / "tagged.pdf"
    _page_with_ocr_form(source)
    structure = {
        "title": "Page",
        "elements": [
            {
                "type": "paragraph",
                "page": 0,
                "text": "Left",
                "bbox": {"l": 71, "b": 697, "r": 92, "t": 710},
            },
            {
                "type": "paragraph",
                "page": 0,
                "text": "Right",
                "bbox": {"l": 138, "b": 697, "r": 170, "t": 710},
            },
            {
                "type": "paragraph",
                "page": 0,
                "text": "Hidden words here",
                "bbox": {"l": 71, "b": 497, "r": 170, "t": 510},
            },
        ],
    }

    await tag_pdf(
        input_path=source,
        output_path=tagged,
        structure_json=structure,
        alt_texts=[],
        language="en",
        original_filename=source.name,
    )

    before = _operators(source)
    split_at = before.index("TJ")
    assert _operators(tagged) == [*before[: split_at + 1], "TJ", *before[split_at + 1 :]]
    assert rendered(tagged) == rendered(source)
    assert " ".join(screen_reader_text(tagged).split()) == "Left Right Hidden words here"
