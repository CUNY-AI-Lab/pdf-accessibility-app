"""Link and form reading-order contracts, inspected in the produced PDF tree."""

import pikepdf
import pytest

from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica, rendered


def _kids(node):
    kids = node.get("/K", [])
    return list(kids) if isinstance(kids, pikepdf.Array) else [kids]


def _structs(node):
    yield node
    for kid in _kids(node):
        if isinstance(kid, pikepdf.Dictionary) and "/S" in kid:
            yield from _structs(kid)


def _text_pdf(path, lines):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    instructions = []
    elements = []
    for index, (text, x, y) in enumerate(lines):
        instructions.append(
            f"BT /F1 8 Tf {x} {y} Td ".encode()
            + pikepdf.String(text.encode("cp1252")).unparse()
            + b" Tj ET"
        )
        elements.append({
            "type": "paragraph", "page": 0, "text": text,
            "bbox": {"l": x - 1, "b": y - 3, "r": 600, "t": y + 9},
        })
    page.Contents = pdf.make_stream(b"\n".join(instructions))
    return pdf, page, elements


async def _tag(source, output, elements, language="en"):
    await tag_pdf(
        input_path=source, output_path=output,
        structure_json={"title": "Reading contracts", "elements": elements},
        alt_texts=[], language=language, original_filename=source.name,
    )


def _marked_text(path):
    with PdfGlyphReader(path) as reader:
        return {
            mcid: "".join(glyph if isinstance(glyph, str) else glyph.text for glyph in glyphs)
            for (stream, mcid), glyphs in reader.page(0).marked.items()
            if stream == ()
        }


@pytest.mark.parametrize("geometry", ["rect", "quads", "whole_paragraph", "padded_rect"])
@pytest.mark.parametrize("role", ["paragraph", "list_item", "cell"])
async def test_link_words_and_objr_share_link_in_paragraph_order(tmp_path, geometry, role):
    source, output = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    lines = [("Read the library guide for details.", 72, 700)]
    if geometry == "quads":
        lines += [("More linked words then a conclusion.", 72, 680)]
    pdf, page, elements = _text_pdf(source, lines)
    # Use measured glyphs so annotation geometry is independent of font estimates.
    pdf.save(source)
    with PdfGlyphReader(source) as reader:
        ops = reader.page(0).ops[()]
        spans = [(ops[0], "library guide")]
        if geometry == "quads":
            spans = [(ops[0], "library guide for details.")]
            spans.append((ops[1], "More linked words"))
        elif geometry in {"whole_paragraph", "padded_rect"}:
            spans = [(ops[0], lines[0][0])]
        boxes = []
        for op, words in spans:
            start = op.text.index(words)
            glyphs = op.glyphs[start:start + len(words)]
            boxes.append([
                min(g.bbox["l"] for g in glyphs) - 0.1,
                min(g.bbox["b"] for g in glyphs) - 0.1,
                max(g.bbox["r"] for g in glyphs) + 0.1,
                max(g.bbox["t"] for g in glyphs) + 0.1,
            ])
    if geometry == "padded_rect":
        left, bottom, right, top = boxes[0]
        elements[0]["bbox"] = {"l": left, "b": bottom, "r": right, "t": top}
        boxes = [[left - 10, bottom - 10, right + 10, top + 10]]
    annotation = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
        Rect=[min(b[0] for b in boxes), min(b[1] for b in boxes),
              max(b[2] for b in boxes), max(b[3] for b in boxes)],
        A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String("https://example.test")),
    ))
    if geometry == "quads":
        annotation.QuadPoints = pikepdf.Array([
            coord for left, bottom, right, top in boxes
            for coord in (left, top, right, top, left, bottom, right, bottom)
        ])
        elements[0]["bbox"]["b"] = 677
        elements = [elements[0]]
        elements[0]["text"] = " ".join(line[0] for line in lines)
    if role == "cell":
        elements[0].update(type="table", num_rows=1, num_cols=1, cells=[{
            "row": 0, "col": 0, "text": elements[0]["text"], "bbox": elements[0]["bbox"],
        }])
    else:
        elements[0]["type"] = role
    page.Annots = [annotation]
    pdf.save(source)
    pdf.close()
    await _tag(source, output, elements)

    marked = _marked_text(output)
    with pikepdf.open(output) as result:
        document = result.Root.StructTreeRoot.K[0]
        parent_role = {"paragraph": "/P", "list_item": "/LBody", "cell": "/TD"}[role]
        paragraph = next(node for node in _structs(document) if node.S == parent_role)
        links = [node for node in _structs(paragraph) if node.S == "/Link"]
        assert len(links) == 1
        link = links[0]
        assert link.P == paragraph
        mcrs = [kid for kid in _kids(link) if kid.get("/Type") == "/MCR"]
        objrs = [kid for kid in _kids(link) if kid.get("/Type") == "/OBJR"]
        assert mcrs, "The link must own its visible words"
        assert len(objrs) == 1
        assert objrs[0].Obj == result.pages[0].Annots[0]
        assert " ".join(" ".join(marked[int(k.MCID)] for k in mcrs).split()) == " ".join(
            words for _, words in spans
        )
        nums = result.Root.StructTreeRoot.ParentTree.Nums
        parents = {int(nums[i]): nums[i + 1] for i in range(0, len(nums), 2)}
        for mcr in mcrs:
            assert parents[int(result.pages[0].StructParents)][int(mcr.MCID)] == link
        assert parents[int(objrs[0].Obj.StructParent)] == link
        assert not any("/ActualText" in node for node in (document, paragraph, link))
        assert result.pages[0].Tabs == "/S"
    assert " ".join(screen_reader_text(output).split()) == " ".join(line[0] for line in lines)
    assert rendered(output) == rendered(source)


@pytest.mark.parametrize("placement", ["left", "above", "two_columns"])
async def test_form_fields_follow_their_labels_in_structure_order(tmp_path, placement):
    source, output = tmp_path / "form.pdf", tmp_path / "tagged.pdf"
    last_x, last_y = (350, 700) if placement == "two_columns" else (72, 640)
    pdf, page, elements = _text_pdf(source, [
        ("End of form", 72, 560), ("Last name", last_x, last_y), ("First name", 72, 700),
    ])
    elements.reverse()  # Logical order deliberately differs from painting order.
    for element in elements:
        element["bbox"]["r"] = element["bbox"]["l"] + 65
    widgets = []
    for name, x, y in [("last", last_x, last_y), ("first", 72, 700)]:
        rect = [x + 98, y - 3, x + 208, y + 9]
        if placement == "above":
            rect = [x, y - 28, x + 108, y - 16]
        widgets.append(pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
            Rect=rect, FT=pikepdf.Name.Tx, T=pikepdf.String(name), TU=pikepdf.String(name),
        )))
    page.Annots = widgets  # Deliberately reversed: /Tabs /S must use the structure.
    page.Tabs = pikepdf.Name.R
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=widgets)
    pdf.save(source)
    pdf.close()
    await _tag(source, output, elements)

    marked = _marked_text(output)
    with pikepdf.open(output) as result:
        heard = []
        for kid in result.Root.StructTreeRoot.K[0].K:
            if kid.S == "/Form":
                heard.append(str(kid.K.Obj.T))
            else:
                heard.append("".join(marked[int(m.MCID)] for m in _kids(kid)))
        assert heard == ["First name", "first", "Last name", "last", "End of form"]
        assert result.pages[0].Tabs == "/S"
