"""Born-digital pages position text with scaled text matrices and TJ offsets.
The tagger must know where text really is, and must tag a table row drawn by
one TJ operator cell by cell without changing how the page looks."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.tagger import tag_pdf
from app.pipeline.text_geometry import measure_text_ops
from app.services.structure_text import screen_reader_text

CELLS = [["Year", "Crop", "Price"], ["1918", "Wheat", "2.20"], ["1919", "Corn", "1.51"]]
# Column starts in points; one TJ per row moves between them with offsets.
COLUMNS = [72, 200, 330]
ROW_TOPS = [700, 680, 660]
FONT_SIZE = 10


def _font(pdf: pikepdf.Pdf) -> pikepdf.Object:
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )


def _helvetica_width(text: str) -> float:
    from pdfminer.pdffont import PDFType1Font

    font = PDFType1Font(None, {"BaseFont": "Helvetica"})
    return sum(font.char_width(ord(char)) for char in text) * FONT_SIZE


def _table_row_tj(row: list[str]) -> pikepdf.Array:
    """One TJ drawing every cell of a row: each offset moves the text
    position from the end of one cell to the start of the next column."""
    items: list = []
    x = COLUMNS[0]
    for column, text in enumerate(row):
        if column:
            gap = COLUMNS[column] - x
            items.append(-gap * 1000 / FONT_SIZE)
            x = COLUMNS[column]
        items.append(pikepdf.String(text))
        x += _helvetica_width(text)
    return pikepdf.Array(items)


def _table_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=_font(pdf)))
    lines = ["BT", f"/F1 {FONT_SIZE} Tf"]
    # A scaled text matrix: Td moves are in text space, ten points per unit.
    lines.append(f"10 0 0 10 {COLUMNS[0]} {ROW_TOPS[0]} Tm")
    lines.append("/F1 1 Tf")
    for row_index, row in enumerate(CELLS):
        if row_index:
            lines.append("0 -2 Td")
        tj = _table_row_tj(row)
        # The array is in units of the 1-point font under a 10x matrix, so
        # offsets computed for a 10-point font are unchanged.
        lines.append(pikepdf.unparse_content_stream([([tj], pikepdf.Operator("TJ"))]).decode())
    lines.append("ET")
    page.Contents = pdf.make_stream("\n".join(lines).encode())
    pdf.save(path)


def _table_structure() -> dict:
    cells = []
    for row, texts in enumerate(CELLS):
        for col, text in enumerate(texts):
            left = COLUMNS[col] - 1
            top = ROW_TOPS[row] + FONT_SIZE
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
                    "bbox": {"l": left, "b": ROW_TOPS[row] - 3, "r": left + 100, "t": top},
                }
            )
    return {
        "title": "Prices",
        "elements": [
            {
                "type": "table",
                "page": 0,
                "bbox": {"l": 70, "b": 655, "r": 440, "t": 712},
                "num_rows": 3,
                "num_cols": 3,
                "cells": cells,
            }
        ],
    }


def test_measured_positions_follow_the_text_matrix_and_tj_offsets(tmp_path):
    path = tmp_path / "table.pdf"
    _table_pdf(path)
    ops = measure_text_ops(path)[(0, None)]

    assert [op.text for op in ops] == ["Year Crop Price", "1918 Wheat 2.20", "1919 Corn 1.51"]
    # Rows are 20 points apart (Td of 2 under a 10x matrix), not 2.
    bottoms = [op.bbox["b"] for op in ops]
    assert bottoms[0] - bottoms[1] == pytest.approx(20, abs=0.5)
    # Each row spans all three columns.
    assert ops[0].bbox["l"] == pytest.approx(COLUMNS[0], abs=1)
    assert ops[0].bbox["r"] > COLUMNS[2]


@pytest.mark.asyncio
async def test_a_row_drawn_by_one_tj_is_tagged_cell_by_cell(tmp_path):
    source = tmp_path / "table.pdf"
    tagged = tmp_path / "tagged.pdf"
    _table_pdf(source)

    await tag_pdf(
        input_path=source,
        output_path=tagged,
        structure_json=_table_structure(),
        alt_texts=[],
        language="en",
        original_filename=source.name,
    )

    heard = screen_reader_text(tagged, markdown=True)
    for row in CELLS[1:]:
        assert "".join(f"<td>{text}</td>" for text in row) in heard
    assert "".join(f"<th>{text}</th>" for text in CELLS[0]) in heard

    # Splitting the TJ changes nothing on the page: every glyph is where it was.
    def glyph_positions(path: Path) -> list[tuple[str, float, float]]:
        return [
            (glyph.text, round(glyph.bbox["l"], 2), round(glyph.bbox["b"], 2))
            for op in measure_text_ops(path)[(0, None)]
            for glyph in op.glyphs
        ]

    assert glyph_positions(tagged) == glyph_positions(source)
