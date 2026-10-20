"""Docling's page frame to PDF user space: a word's displayed box, as pdfminer
(like Docling) places it after the page's /Rotate and box origin, read back
by pdfium in user space."""

from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar

from app.pipeline.page_frame import PageFrame
from app.pipeline.pdfium_text import PdfiumText
from tests.pdf_fixtures import helvetica


def _glyphs(item):
    if isinstance(item, LTChar):
        yield item
    elif hasattr(item, "__iter__"):
        for child in item:
            yield from _glyphs(child)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_displayed_box_reads_its_word_in_user_space(tmp_path: Path, rotation: int):
    path = tmp_path / "page.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 300))
    page.obj["/MediaBox"] = pikepdf.Array([30, 20, 430, 320])
    page.obj["/CropBox"] = pikepdf.Array([0, 0, 500, 400])
    page.obj["/Rotate"] = rotation
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(
        b"BT /F1 12 Tf 60 260 Td (Corner) Tj ET BT /F1 12 Tf 300 60 Td (Lump) Tj ET"
    )
    pdf.save(path)

    # "Lump" shares no letter with "Corner".
    chars = [glyph for glyph in _glyphs(next(extract_pages(path))) if glyph.get_text() in "Corner"]
    assert len(chars) == len("Corner")
    displayed = {
        "l": min(glyph.x0 for glyph in chars),
        "b": min(glyph.y0 for glyph in chars),
        "r": max(glyph.x1 for glyph in chars),
        "t": max(glyph.y1 for glyph in chars),
    }
    with PdfiumText(path) as pdfium_text:
        frame = PageFrame.of(pdfium_text.document[0])
        assert pdfium_text.in_boxes(0, [frame.to_user_space(displayed)]) == "Corner"


def test_visible_box_is_the_crop_box_clipped_to_the_media_box(tmp_path: Path):
    path = tmp_path / "page.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 300))
    page.obj["/MediaBox"] = pikepdf.Array([30, 20, 430, 320])
    page.obj["/CropBox"] = pikepdf.Array([0, 50, 500, 400])
    pdf.save(path)
    frame = PageFrame.of(pdfium.PdfDocument(path)[0])
    assert (frame.left, frame.bottom, frame.right, frame.top) == (30, 50, 430, 320)


def test_tagger_moves_element_boxes_on_copies():
    from app.pipeline.tagger import _elements_in_user_space

    frame = PageFrame(rotation=0, left=30, bottom=20, right=430, top=320)
    box = {"l": 10, "b": 10, "r": 20, "t": 20}
    table = {"type": "table", "page": 0, "bbox": dict(box), "cells": [{"bbox": dict(box)}]}
    para = {"type": "paragraph", "page": 0, "bbox": dict(box), "extra_bboxes": [dict(box)]}
    moved = {"l": 40, "b": 30, "r": 50, "t": 40}

    converted = _elements_in_user_space([table, para], [frame])

    assert converted[0]["bbox"] == moved and converted[0]["cells"][0]["bbox"] == moved
    assert converted[1]["bbox"] == moved and converted[1]["extra_bboxes"] == [moved]
    assert table["bbox"] == box and table["cells"][0]["bbox"] == box
