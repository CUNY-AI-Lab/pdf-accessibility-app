"""A decorative image decision must not hide the PDF's live text."""

import pikepdf
import pytest

from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica, rendered


def _structs(node):
    yield node
    kids = node.get("/K", [])
    for kid in list(kids) if isinstance(kids, pikepdf.Array) else [kids]:
        if isinstance(kid, pikepdf.Dictionary) and "/S" in kid:
            yield from _structs(kid)


def _marked(path):
    with PdfGlyphReader(path) as reader:
        return {
            key: "".join(g if isinstance(g, str) else g.text for g in glyphs)
            for key, glyphs in reader.page(0).marked.items()
        }


def _fixture(source, *, image=False, form=False):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 300))
    font = helvetica(pdf)
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    text = b"BT /F1 10 Tf 30 200 Td (Support students with a donation) Tj ET"
    if form:
        overlay = pdf.make_stream(text)
        overlay.Type = pikepdf.Name.XObject
        overlay.Subtype = pikepdf.Name.Form
        overlay.BBox = [0, 0, 400, 300]
        overlay.Resources = pikepdf.Dictionary(Font=page.Resources.Font)
        page.Resources.XObject = pikepdf.Dictionary(Overlay=overlay)
        text = b"/Overlay Do"
    if image:
        background = pdf.make_stream(bytes([210]))
        background.Type = pikepdf.Name.XObject
        background.Subtype = pikepdf.Name.Image
        background.Width = background.Height = 1
        background.ColorSpace = pikepdf.Name.DeviceGray
        background.BitsPerComponent = 8
        if "/XObject" not in page.Resources:
            page.Resources.XObject = pikepdf.Dictionary()
        page.Resources.XObject["/Banner"] = background
        text = b"q 350 0 0 120 20 140 cm /Banner Do Q\n" + text
    page.Contents = pdf.make_stream(
        b"BT /F1 10 Tf 30 270 Td (Before) Tj ET\n" + text + b"\nBT /F1 10 Tf 30 60 Td (After) Tj ET"
    )
    pdf.save(source)
    with PdfGlyphReader(source) as reader:
        ops = reader.page(0).ops
        quote = ops[("Overlay",) if form else ()][0 if form else 1]
        box = quote.bbox
        link_box = union_bbox(g.bbox for g in quote.glyphs if g.text.strip())
    page.Annots = pikepdf.Array(
        [
            pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.Link,
                    Rect=[link_box["l"], link_box["b"], link_box["r"], link_box["t"]],
                    A=pikepdf.Dictionary(
                        S=pikepdf.Name.URI, URI=pikepdf.String("https://example.org/give")
                    ),
                )
            )
        ]
    )
    pdf.save(source)
    pdf.close()
    return [
        {"type": "paragraph", "bbox": {"l": 25, "b": 265, "r": 100, "t": 285}},
        {
            "type": "figure",
            "figure_index": 0,
            "bbox": {"l": 20, "b": 140, "r": 370, "t": 260} if image else box,
        },
        {"type": "paragraph", "bbox": {"l": 25, "b": 55, "r": 100, "t": 75}},
    ]


@pytest.mark.parametrize("status", ["rejected", "pending_review"])
async def test_text_box_mistaken_for_figure_is_paragraph_from_glyph_evidence(tmp_path, status):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    elements = _fixture(source)
    await tag_pdf(
        source,
        output,
        {"elements": [dict(e, page=0) for e in elements]},
        [{"figure_index": 0, "status": status, "text": ""}],
    )
    assert " ".join(screen_reader_text(output).split()) == (
        "Before Support students with a donation After"
    )
    marks = _marked(output)
    # Existing mappings stay fixed; recovered text gets appended MCIDs.
    assert marks[(), 0] == "Before"
    assert marks[(), 1] == "After"
    assert "Support students with a donation" in "".join(marks[k] for k in marks if k[1] >= 2)
    with pikepdf.open(output) as pdf:
        structs = list(_structs(pdf.Root.StructTreeRoot))
        assert not any(s.get("/S") == pikepdf.Name.Figure for s in structs)
        link = next(s for s in structs if s.get("/S") == pikepdf.Name.Link)
        assert link.P.S == pikepdf.Name.P
        assert any(k.get("/Type") == pikepdf.Name.OBJR for k in link.K)
        assert any(k.get("/Type") == pikepdf.Name.MCR for k in link.K)
    assert rendered(source) == rendered(output)


@pytest.mark.parametrize("form", [False, True])
async def test_live_text_over_decorative_banner_keeps_content_and_mcid_owners(tmp_path, form):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    elements = _fixture(source, image=True, form=form)
    await tag_pdf(
        source,
        output,
        {"elements": [dict(e, page=0) for e in elements]},
        [{"figure_index": 0, "status": "rejected", "text": ""}],
    )
    assert " ".join(screen_reader_text(output).split()) == (
        "Before Support students with a donation After"
    )
    marks = _marked(output)
    assert marks[(), 0] == "Before"
    assert marks[(), 1] == "After"
    if form:
        assert marks[("Overlay",), 0] == "Support students with a donation"
    with pikepdf.open(output) as pdf:
        stack = []
        for instruction in pikepdf.parse_content_stream(pdf.pages[0]):
            op = str(instruction.operator)
            if op in {"BMC", "BDC"}:
                stack.append(str(instruction.operands[0]))
            elif op == "EMC":
                stack.pop()
            elif op == "Do" and str(instruction.operands[0]) == "/Banner":
                assert stack == ["/Artifact"]
        assert not any(
            s.get("/S") == pikepdf.Name.Figure for s in _structs(pdf.Root.StructTreeRoot)
        )
    assert rendered(source) == rendered(output)
