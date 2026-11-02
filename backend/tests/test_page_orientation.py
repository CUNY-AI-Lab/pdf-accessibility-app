"""Docling reads sideways pages upright, and its boxes return to the page as shown."""

from types import SimpleNamespace

import pikepdf
import pypdfium2 as pdfium
import pytest

from app.pipeline import structure
from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.page_orientation import elements_to_shown, sideways_pages, upright_copy
from app.pipeline.structure import FigureInfo
from tests.pdf_fixtures import helvetica

# Text matrices running each number of quarter turns counterclockwise, placed
# inside a 612 x 792 page.
RUNS = {0: "1 0 0 1 72 600", 1: "0 1 -1 0 300 72", 2: "-1 0 0 -1 540 300", 3: "0 -1 1 0 300 720"}
LINE = "Stock Lead Review Level Rationale"


def page(pdf, lines, rotate=0):
    """A page drawing each (turn, text) line from its own origin."""
    new = pdf.add_blank_page(page_size=(612, 792))
    new.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    ops = []
    for index, (turn, text) in enumerate(lines):
        a, b, c, d, e, f = (float(v) for v in RUNS[turn].split())
        # Later lines one leading further along the text's own downward axis.
        e, f = e + index * 14 * c * -1, f + index * 14 * d * -1
        ops.append(f"BT /F1 10 Tf {a} {b} {c} {d} {e} {f} Tm ({text}) Tj ET")
    new.Contents = pdf.make_stream(" ".join(ops).encode())
    if rotate:
        new.Rotate = rotate
    return new


def document(path, pages):
    pdf = pikepdf.new()
    for lines, rotate in pages:
        page(pdf, lines, rotate)
    pdf.save(path)


PAGES = [
    ([(0, LINE)] * 4, 0),  # upright
    ([(1, LINE)] * 4, 0),  # a table printed sideways, reading up the page
    ([(3, LINE)] * 4, 0),  # reading down the page
    ([(1, LINE)] * 4, 90),  # drawn sideways, shown upright by /Rotate
    ([(0, LINE)] * 4, 90),  # drawn upright, shown sideways by /Rotate
    ([(0, LINE)] * 3 + [(1, "Rotated column heading")], 0),  # a minority turned
    ([(1, LINE), (1, LINE), (0, LINE), (2, LINE)], 0),  # mostly up the page, not a majority
    ([], 0),  # no text
]


def test_a_page_is_sideways_when_most_of_its_text_runs_another_way_than_shown(tmp_path):
    source = tmp_path / "in.pdf"
    document(source, PAGES)
    pages = sideways_pages(source)
    assert {index: page.upright.rotation for index, page in pages.items()} == {1: 90, 2: 270, 4: 0}
    assert {index: page.shown.rotation for index, page in pages.items()} == {1: 0, 2: 0, 4: 90}


def test_a_file_pdfium_cannot_read_has_no_sideways_pages(tmp_path):
    source = tmp_path / "in.pdf"
    source.write_bytes(b"not a pdf")
    assert sideways_pages(source) == {}
    assert upright_copy(source, tmp_path / "upright") == (source, {})


def test_only_the_copy_docling_reads_is_turned(tmp_path):
    source = tmp_path / "in.pdf"
    document(source, PAGES)
    before = source.read_bytes()
    copy, pages = upright_copy(source, tmp_path / "upright")
    assert copy == tmp_path / "upright" / "in.pdf"
    assert source.read_bytes() == before
    with pikepdf.open(copy) as pdf:
        assert [int(p.obj.Rotate) if "/Rotate" in p.obj else 0 for p in pdf.pages] == [0, 90, 270, 90, 0, 0, 0, 0]
    assert set(pages) == {1, 2, 4}

    upright = tmp_path / "upright.pdf"
    document(upright, PAGES[:1])
    assert upright_copy(upright, tmp_path / "none") == (upright, {})
    assert not (tmp_path / "none").exists()


@pytest.mark.parametrize("index", [1, 2, 4])
def test_the_copy_shows_the_text_upright_and_its_boxes_land_on_the_text_as_shown(tmp_path, index):
    source = tmp_path / "in.pdf"
    document(source, PAGES)
    copy, pages = upright_copy(source, tmp_path / "upright")
    with PdfGlyphReader(source) as reader:
        glyphs = [g for op in next(iter(reader.page(index).ops.values())) for g in op.glyphs]
    word = union_bbox(g.bbox for g in glyphs[:5])
    with pdfium.PdfDocument(str(copy)) as turned, pdfium.PdfDocument(str(source)) as original:
        upright, shown = PageFrame.of(turned[index]), PageFrame.of(original[index])
    # Docling reads the copy upright: each glyph right of the one before.
    lefts = [upright.to_docling(g.bbox)["l"] for g in glyphs[:5]]
    assert lefts == sorted(lefts) and lefts[0] < lefts[-1]
    seen = upright.to_docling(word)
    element = {"page": index, "bbox": seen, "extra_bboxes": [seen], "cells": [{"bbox": seen}]}
    elements_to_shown([element], pages)
    expected = pytest.approx(shown.to_docling(word))
    assert element["bbox"] == expected
    assert element["extra_bboxes"] == [expected]
    assert element["cells"][0]["bbox"] == expected


async def test_extract_structure_converts_the_upright_copy_and_moves_its_boxes_back(
    tmp_path, monkeypatch
):
    source = tmp_path / "in.pdf"
    document(source, PAGES[:2])
    before = source.read_bytes()
    with PdfGlyphReader(source) as reader:
        glyphs = [g for op in next(iter(reader.page(1).ops.values())) for g in op.glyphs]
    word = union_bbox(g.bbox for g in glyphs[:5])
    seen = {}

    async def convert(pdf_path, job_dir, *args, **kwargs):
        seen["path"] = pdf_path
        with pdfium.PdfDocument(str(pdf_path)) as pdf:
            frame = PageFrame.of(pdf[1])
            height = pdf[1].get_size()[1]
        box = frame.to_docling(word)
        cell = {
            "text": "Stock",
            "start_row_offset_idx": 0,
            "start_col_offset_idx": 0,
            "bbox": {
                **box,
                "t": height - box["t"],
                "b": height - box["b"],
                "coord_origin": "TOPLEFT",
            },
        }
        doc = {
            "body": {"children": [{"$ref": "#/tables/0"}]},
            "tables": [
                {
                    "label": "table",
                    "prov": [{"page_no": 2, "bbox": {**box, "coord_origin": "BOTTOMLEFT"}}],
                    "data": {"num_rows": 1, "num_cols": 1, "table_cells": [cell]},
                }
            ],
            "pages": {
                "1": {"size": {"width": 612, "height": 792}},
                "2": {"size": {"width": 792, "height": 612}},
            },
        }
        return doc, [FigureInfo(index=0, path=job_dir / "f.png", page=1, bbox=box)]

    monkeypatch.setattr(structure, "_convert_via_docling_serve", convert)
    monkeypatch.setattr(
        structure,
        "get_settings",
        lambda: SimpleNamespace(
            docling_serve_url="http://docling",
            docling_serve_timeout=60,
            docling_serve_ocr_engine="rapidocr",
            docling_serve_token="",
        ),
    )
    result = await structure.extract_structure(source, tmp_path / "job")
    assert seen["path"] == tmp_path / "job" / "docling-upright" / "in.pdf"
    assert source.read_bytes() == before
    assert result.processed_pdf_path == source
    (table,) = [e for e in result.document_json["elements"] if e["type"] == "table"]
    expected = pytest.approx(word)  # page 2 is shown unturned: Docling's frame is user space
    assert table["bbox"] == expected
    assert table["cells"][0]["bbox"] == expected
    assert result.figures[0].bbox == expected
