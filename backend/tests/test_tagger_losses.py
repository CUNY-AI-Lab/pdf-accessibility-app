"""Text reaches its logical owner without changing the page's appearance."""

import pikepdf
import pytest

from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica, rendered


def _paragraph(text, box):
    return {"type": "paragraph", "page": 0, "text": text, "bbox": box}


def _forms(path, *, nested=False, repeated=False, ocr_name=False):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = helvetica(pdf)
    form = pdf.make_stream(b"BT /F1 10 Tf 10 10 Td (Form words) Tj ET")
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = [0, 0, 200, 100]
    if not ocr_name:
        form.Matrix = [1, 0, 0, 1, 0, 10]
    form.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    if nested:
        outer = pdf.make_stream(b"q 1 0 0 1 0 0 cm /Inner Do Q")
        outer.Type = pikepdf.Name.XObject
        outer.Subtype = pikepdf.Name.Form
        outer.BBox = [0, 0, 200, 100]
        outer.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Inner=form))
        form = outer
    page.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=font), XObject=pikepdf.Dictionary(Ordinary=form)
    )
    content = b"BT /F1 10 Tf 60 680 Td (Native words) Tj ET\n"
    content += b"q 1 0 0 1 50 600 cm /Ordinary Do Q\n"
    if repeated:
        content += b"q 1 0 0 1 50 500 cm /Ordinary Do Q\n"
    if ocr_name:
        page.Resources.XObject = pikepdf.Dictionary(OCRBody=form)
        content = content.replace(b"/Ordinary", b"/OCRBody")
    page.Contents = pdf.make_stream(content)
    pdf.save(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_text_in_ordinary_and_nested_forms_reaches_screen_reader(tmp_path, nested):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    _forms(source, nested=nested)
    elements = [
        _paragraph("Native words", {"l": 59, "b": 677, "r": 160, "t": 691}),
        _paragraph("Form words", {"l": 59, "b": 617, "r": 160, "t": 631}),
    ]
    await tag_pdf(source, tagged, {"elements": elements})
    assert " ".join(screen_reader_text(tagged).split()) == "Native words Form words"
    assert rendered(tagged) == rendered(source)
    with pikepdf.open(tagged) as pdf:
        def refs(node):
            if isinstance(node, pikepdf.Array):
                return [ref for kid in node for ref in refs(kid)]
            if isinstance(node, pikepdf.Dictionary):
                return [node] if node.get("/Type") == "/MCR" else refs(node.get("/K"))
            return []

        form_refs = [ref for ref in refs(pdf.Root.StructTreeRoot.K) if "/Stm" in ref]
        assert form_refs
        parents = pdf.Root.StructTreeRoot.ParentTree.Nums
        for ref in form_refs:
            assert "/StructParents" in ref.Stm
            parent_array = next(parents[i + 1] for i in range(0, len(parents), 2)
                                if parents[i] == ref.Stm.StructParents)
            assert parent_array[ref.MCID].get("/Type") == "/StructElem"


@pytest.mark.asyncio
async def test_reused_form_has_separate_reachable_stream_for_each_placement(tmp_path):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    _forms(source, nested=True, repeated=True)
    elements = [
        _paragraph("Native words", {"l": 59, "b": 677, "r": 160, "t": 691}),
        _paragraph("Form words", {"l": 59, "b": 617, "r": 160, "t": 631}),
        _paragraph("Form words", {"l": 59, "b": 517, "r": 160, "t": 531}),
    ]
    await tag_pdf(source, tagged, {"elements": elements})
    assert " ".join(screen_reader_text(tagged).split()) == "Native words Form words Form words"
    assert rendered(tagged) == rendered(source)


@pytest.mark.asyncio
async def test_one_element_keeps_text_from_page_and_multiple_forms(tmp_path):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    _forms(source, repeated=True, ocr_name=True)
    await tag_pdf(source, tagged, {"elements": [
        _paragraph("Native words Form words Form words", {"l": 59, "b": 510, "r": 160, "t": 691}),
    ]})
    assert " ".join(screen_reader_text(tagged).split()) == "Native words Form words Form words"
    assert rendered(tagged) == rendered(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", ["'", '"'])
async def test_quote_text_operators_split_across_table_cells(tmp_path, operator):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    show = "(55 54 60) '" if operator == "'" else '2 1 (55 54 60) "'
    page.Contents = pdf.make_stream(f"BT /F1 10 Tf 20 TL 72 720 Td {show} ET".encode())
    pdf.save(source)
    with PdfGlyphReader(source) as reader:
        glyphs = [g for op in reader.page(0).ops[()] for g in op.glyphs if g.text.strip()]
    cells = []
    for col, text in enumerate(("55", "54", "60")):
        cells.append({"row": 0, "col": col, "text": text,
                      "bbox": union_bbox(g.bbox for g in glyphs[2 * col:2 * col + 2])})
    await tag_pdf(source, tagged, {"elements": [{
        "type": "table", "page": 0, "num_rows": 1, "num_cols": 3, "cells": cells,
        "bbox": {"l": 70, "b": 690, "r": 200, "t": 715},
    }]})
    assert "<td>55</td><td>54</td><td>60</td>" in screen_reader_text(tagged, markdown=True)
    assert rendered(tagged) == rendered(source)


@pytest.mark.asyncio
async def test_list_items_follow_structure_order_when_drawn_in_reverse(tmp_path):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(
        b"BT /F1 10 Tf 72 600 Td (Second reference) Tj ET "
        b"BT /F1 10 Tf 72 700 Td (First reference) Tj ET"
    )
    pdf.save(source)
    elements = [
        {**_paragraph(text, {"l": 71, "b": y - 3, "r": 180, "t": y + 12}),
         "type": "list_item", "list_group_ref": "references"}
        for text, y in (("First reference", 700), ("Second reference", 600))
    ]
    await tag_pdf(source, tagged, {"elements": elements})
    assert " ".join(screen_reader_text(tagged).split()) == "First reference Second reference"
    assert rendered(tagged) == rendered(source)


@pytest.mark.asyncio
async def test_type3_font_with_nonstandard_units_reaches_its_cell(tmp_path):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    glyph = pdf.make_stream(b"20 0 0 -30 20 0 d1 0 -30 20 30 re f")
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type3,
        FontBBox=[0, -30, 20, 0], FontMatrix=[1, 0, 0, -1, 0, 0],
        FirstChar=65, LastChar=65, Widths=[20],
        Encoding=pikepdf.Dictionary(Type=pikepdf.Name.Encoding, Differences=[65, pikepdf.Name.A]),
        CharProcs=pikepdf.Dictionary(A=glyph), Resources=pikepdf.Dictionary(),
    ))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Contents = pdf.make_stream(b"BT /F1 0.3 Tf 72 500 Td (A) Tj ET")
    pdf.save(source)
    box = {"l": 71, "b": 500, "r": 79, "t": 509}
    await tag_pdf(source, tagged, {"elements": [{
        "type": "table", "page": 0, "num_rows": 1, "num_cols": 1, "bbox": box,
        "cells": [{"row": 0, "col": 0, "text": "A", "bbox": box}],
    }]})
    assert "<td>A</td>" in screen_reader_text(tagged, markdown=True)
    assert rendered(tagged) == rendered(source)


@pytest.mark.asyncio
async def test_overlapping_table_cells_keep_numbers_on_their_own_line(tmp_path):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(
        b"BT /F1 10 Tf 72 700 Td (418.2) Tj ET "
        b"BT /F1 10 Tf 80 691 Td (0.0) Tj ET"
    )
    pdf.save(source)
    await tag_pdf(source, tagged, {"elements": [{
        "type": "table", "page": 0, "num_rows": 2, "num_cols": 1,
        "bbox": {"l": 70, "b": 688, "r": 105, "t": 710},
        "cells": [
            {"row": 0, "col": 0, "text": "418.2",
             "bbox": {"l": 71, "b": 699, "r": 103, "t": 708}},
            {"row": 1, "col": 0, "text": "0.0",
             "bbox": {"l": 80, "b": 690, "r": 99, "t": 704}},
        ],
    }]})
    heard = screen_reader_text(tagged, markdown=True)
    assert "<td>418.2</td>" in heard
    assert "<td>0.0</td>" in heard
    assert rendered(tagged) == rendered(source)



@pytest.mark.asyncio
@pytest.mark.parametrize("named_properties", [False, True])
async def test_source_actual_text_survives_replacing_old_tagging(tmp_path, named_properties):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    properties = pikepdf.Dictionary(MCID=44, ActualText=pikepdf.String("9 GPa"))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    if named_properties:
        page.Resources.Properties = pikepdf.Dictionary(Replacement=properties)
        marker = b"/Replacement"
    else:
        marker = b"<< /MCID 44 /ActualText (9 GPa) >>"
    page.Contents = pdf.make_stream(
        b"BT /F1 10 Tf 72 700 Td /Span " + marker + b" BDC (X) Tj ( Y) Tj EMC ET"
    )
    pdf.save(source)
    await tag_pdf(source, tagged, {"elements": [
        _paragraph("9 GPa", {"l": 71, "b": 697, "r": 120, "t": 711}),
    ]})
    assert screen_reader_text(tagged) == "9 GPa"
    assert rendered(tagged) == rendered(source)
    with pikepdf.open(tagged) as output:
        markers = [i for i in pikepdf.parse_content_stream(output.pages[0])
                   if str(i.operator) == "BDC"]
        assert all(i.operands[1].get("/MCID") != 44 for i in markers)
