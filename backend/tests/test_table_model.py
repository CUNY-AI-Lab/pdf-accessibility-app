"""The table-model step: the model's grid, the page's words in its cells."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.page_glyphs import MeasuredGlyph
from app.pipeline.table_model import (
    AnswerCache,
    CachedReader,
    ModelAnswer,
    TableModelUnavailable,
    Word,
    assign_words,
    cache_key,
    ocr_correction,
    parse_otsl,
    rewrite_tables,
)
from tests.pdf_fixtures import helvetica


def word(text: str, left: float, top: float, *, size: float = 10.0) -> Word:
    """A word drawn at (left, top) in Docling's frame, 0.6 em per glyph."""
    glyphs, x = [], left
    for ch in text:
        box = {"l": x, "b": top - size, "r": x + 0.6 * size, "t": top}
        glyphs.append(MeasuredGlyph(ch, box))
        x += 0.6 * size
    return Word(glyphs, {"l": left, "b": top - size, "r": x, "t": top}, True)


def cell_texts(grid, assignment) -> dict[tuple[int, int], str]:
    texts = {}
    for index, cell in enumerate(grid.cells):
        pieces = sorted(assignment.pieces.get(index, []), key=lambda p: (-p.box["t"], p.box["l"]))
        texts[(cell.row, cell.col)] = " ".join("".join(g.text for g in p.glyphs) for p in pieces)
    return texts


def test_otsl_spans_become_cells():
    grid = parse_otsl(
        "<fcel>Year<fcel>Population<lcel><nl><ucel><fcel>Men<fcel>Women<nl>"
        "<fcel>1900<fcel>10<fcel>12<nl>"
    )
    assert grid is not None and (grid.rows, grid.cols) == (3, 3)
    spans = {(c.row, c.col): (c.row_span, c.col_span, c.text) for c in grid.cells}
    assert spans[(0, 0)] == (2, 1, "Year")
    assert spans[(0, 1)] == (1, 2, "Population")
    assert spans[(1, 1)] == (1, 1, "Men") and spans[(2, 2)] == (1, 1, "12")
    assert grid.problems == []


def test_otsl_ragged_rows_and_stray_spans_are_reported():
    grid = parse_otsl("<lcel>a<fcel>b<nl><fcel>c<nl>")
    assert grid is not None and (grid.rows, grid.cols) == (2, 2)
    assert set(grid.problems) == {"ragged rows", "span without a cell"}
    assert parse_otsl("no table here") is None


def test_multi_line_cells_stay_together_and_rows_follow_lines():
    # Row 0: "Name of / variable" and "Value / (units)" on two lines each.
    words = [
        word("Name", 0, 100), word("of", 30, 100), word("Value", 100, 100),
        word("variable", 0, 88), word("(units)", 100, 88),
        word("Height", 0, 70), word("1.75", 100, 70),
        word("Weight", 0, 58), word("70", 100, 58),
    ]
    grid = parse_otsl(
        "<fcel>Name of variable<fcel>Value (units)<nl>"
        "<fcel>Height<fcel>1.75<nl><fcel>Weight<fcel>70<nl>"
    )
    assert grid is not None
    assignment = assign_words(grid, words)
    assert cell_texts(grid, assignment) == {
        (0, 0): "Name of variable", (0, 1): "Value (units)",
        (1, 0): "Height", (1, 1): "1.75", (2, 0): "Weight", (2, 1): "70",
    }
    assert assignment.stats["page_coverage"] == 1.0
    assert assignment.stats["model_coverage"] == 1.0


def test_a_word_drawn_across_two_cells_is_split_between_them():
    # The PDF draws "12.5" and "3.40" as one run with no gap.
    words = [word("A", 0, 100), word("B", 60, 100), word("12.53.40", 0, 88)]
    grid = parse_otsl("<fcel>A<fcel>B<nl><fcel>12.5<fcel>3.40<nl>")
    assert grid is not None
    assignment = assign_words(grid, words)
    assert cell_texts(grid, assignment)[(1, 0)] == "12.5"
    assert cell_texts(grid, assignment)[(1, 1)] == "3.40"
    assert assignment.stats["split_words"] == 1


def test_the_page_text_is_kept_where_the_model_misreads():
    words = [word("Total", 0, 100), word("1,204", 60, 100)]
    grid = parse_otsl("<fcel>Total<fcel>1,284<nl><fcel>x<fcel>y<nl>")
    assert grid is not None
    assignment = assign_words(grid, words)
    assert cell_texts(grid, assignment)[(0, 1)] == "1,204"


@pytest.mark.parametrize(
    ("ocr", "model", "expected"),
    [
        ("1O5", "105", "105"),
        ("l2.5", "12.5", "12.5"),
        ("125", "1.25", "1.25"),
        ("| 42", "42", "42"),
        ("5outh", "South", "South"),
        ("1,204", "1,284", None),  # a digit for another digit
        ("Total", "TOTAL", None),  # case is not an OCR confusion
        ("12.5", "125", None),  # dropping a decimal point is not a correction
        ("42", "42", None),
    ],
)
def test_scan_corrections_take_only_known_ocr_confusions(ocr, model, expected):
    corrected, _ = ocr_correction(ocr, model)
    assert corrected == expected


def test_answer_cache_is_keyed_by_crop_model_and_prompt(tmp_path: Path):
    cache = AnswerCache(tmp_path, "paddle")
    cache.put(b"png-a", ModelAnswer("<fcel>a<nl>", "stop", 1.5))
    assert cache.get(b"png-a") is not None and cache.get(b"png-a").cached
    assert cache.get(b"png-b") is None
    assert cache_key("paddle", b"x") != cache_key("other", b"x")
    assert cache_key("paddle", b"x") != cache_key("paddle", b"x", prompt="OCR:")


def _drawn_table(path: Path) -> dict:
    """A 3x3 table drawn without rules, and a Docling structure that read it
    as one row whose cells hold whole columns."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    rows = [("Item", "Count", "Share"), ("Apples", "12", "40%"), ("Pears", "18", "60%")]
    commands = [
        f"BT /F1 10 Tf {72 + 120 * c} {700 - 20 * r} Td ({text}) Tj ET"
        for r, row in enumerate(rows) for c, text in enumerate(row)
    ]
    page.Contents = pdf.make_stream("\n".join(commands).encode("ascii"))
    pdf.save(path)
    columns = [
        {"text": " ".join(row[c] for row in rows), "row": 0, "col": c, "row_span": 1,
         "col_span": 1, "bbox": {"l": 70 + 120 * c, "b": 655, "r": 150 + 120 * c, "t": 712},
         "column_header": False, "row_header": False, "is_header": False}
        for c in range(3)
    ]
    table = {"type": "table", "page": 0, "bbox": {"l": 70, "b": 655, "r": 400, "t": 712},
             "num_rows": 1, "num_cols": 3, "cells": columns, "caption": None}
    return {"elements": [table]}


class FixedReader:
    model = "test"

    def __init__(self, text: str) -> None:
        self.text = text

    async def read(self, png: bytes) -> ModelAnswer | None:
        assert png.startswith(b"\x89PNG")
        return ModelAnswer(self.text)


async def test_rewrite_puts_the_page_words_into_the_model_grid(tmp_path: Path):
    path = tmp_path / "table.pdf"
    structure = _drawn_table(path)
    reader = FixedReader(
        "<fcel>Item<fcel>Count<fcel>Share<nl><fcel>Apples<fcel>12<fcel>40%<nl>"
        "<fcel>Pears<fcel>18<fcel>60%<nl>"
    )
    rewritten, audit = await rewrite_tables(path, structure, reader, min_coverage=0.8)
    table = rewritten["elements"][0]
    assert audit["replaced"] == 1 and (table["num_rows"], table["num_cols"]) == (3, 3)
    texts = {(c["row"], c["col"]): c["text"] for c in table["cells"]}
    assert texts[(1, 0)] == "Apples" and texts[(2, 2)] == "60%"
    # The input structure is not changed.
    assert structure["elements"][0]["num_rows"] == 1


async def test_docling_table_is_kept_when_the_texts_do_not_align(tmp_path: Path):
    path = tmp_path / "table.pdf"
    structure = _drawn_table(path)
    reader = FixedReader("<fcel>Something<fcel>else<nl><fcel>entirely<fcel>here<nl>")
    rewritten, audit = await rewrite_tables(path, structure, reader, min_coverage=0.8)
    assert audit["replaced"] == 0
    assert audit["table_reports"][0]["result"] == "low coverage"
    assert rewritten["elements"][0] == structure["elements"][0]


async def test_cached_reader_answers_only_from_the_cache(tmp_path: Path):
    path = tmp_path / "table.pdf"
    structure = _drawn_table(path)
    reader = CachedReader(AnswerCache(tmp_path / "cache", "test"))
    rewritten, audit = await rewrite_tables(path, structure, reader, min_coverage=0.8)
    assert reader.misses == 1 and audit["table_reports"][0]["result"] == "no answer"
    assert rewritten["elements"][0] == structure["elements"][0]


class DownReader:
    model = "test"

    def __init__(self) -> None:
        self.calls = 0

    async def read(self, png: bytes) -> ModelAnswer | None:
        self.calls += 1
        raise TableModelUnavailable("connection refused")


async def test_a_model_that_is_down_is_asked_once_per_document(tmp_path: Path):
    path = tmp_path / "table.pdf"
    structure = _drawn_table(path)
    structure["elements"].append(dict(structure["elements"][0]))
    reader = DownReader()
    rewritten, audit = await rewrite_tables(path, structure, reader, min_coverage=0.8)
    assert reader.calls == 1 and audit["unavailable"] == "connection refused"
    assert rewritten["elements"] == structure["elements"]


async def test_the_pipeline_step_is_off_without_a_url(tmp_path: Path):
    from app.config import Settings
    from app.pipeline.orchestrator import _apply_table_model

    structure = {"elements": [{"type": "table"}]}
    settings = Settings(llm_api_key="test-key", table_model_url="")
    assert await _apply_table_model(tmp_path / "x.pdf", structure, settings) == (
        structure, {"enabled": False},
    )
