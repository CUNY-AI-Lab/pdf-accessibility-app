import pikepdf
import pytest

from app.pipeline.tagger import (
    StructTreeBuilder,
    _allocate_fragment_mcid,
    _formula_alt_text,
    tag_pdf,
)
from tests.pdf_fixtures import helvetica


def _builder() -> tuple[StructTreeBuilder, pikepdf.Object]:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    builder = StructTreeBuilder(pdf)
    builder.setup()
    return builder, page.obj


def test_formula_elements_get_formula_tag_with_alt_text():
    builder, page = _builder()

    tag, _mcid, actual_text = _allocate_fragment_mcid(
        builder, {"type": "formula", "text": "E = mc^2"}, 0, page, {}, set()
    )
    builder.finalize()

    assert tag == "Formula"
    assert actual_text == "E = mc^2"
    formula_elem = builder.doc_elem["/K"][0]
    assert formula_elem.get("/S") == pikepdf.Name("/Formula")
    assert str(formula_elem.get("/Alt")) == "E equals m c squared"


def test_formula_alt_text_speaks_subscripts_superscripts_and_symbols():
    assert _formula_alt_text("x_2 + y²") == "x sub 2 plus y squared"


def test_note_elements_get_note_tag_with_unique_id():
    builder, page = _builder()

    tag, _mcid, _text = _allocate_fragment_mcid(
        builder, {"type": "note", "text": "Footnote text"}, 0, page, {}, set()
    )
    builder.finalize()

    note_elem = builder.doc_elem["/K"][0]
    assert tag == "Note"
    assert note_elem.get("/S") == pikepdf.Name("/Note")
    assert str(note_elem.get("/ID")).startswith("note-")


def test_toc_elements_get_toc_caption_and_toci_children():
    builder, page = _builder()

    caption = {"type": "toc_caption", "text": "Contents", "toc_group_ref": "toc-0"}
    entry = {"type": "toc_item", "text": "Introduction ........ 1", "toc_group_ref": "toc-0"}
    caption_tag = _allocate_fragment_mcid(builder, caption, 0, page, {}, set())[0]
    entry_tag = _allocate_fragment_mcid(builder, entry, 0, page, {}, set())[0]
    builder.finalize()

    toc_elem = builder.doc_elem["/K"][0]
    assert toc_elem.get("/S") == pikepdf.Name("/TOC")
    assert toc_elem["/K"][0].get("/S") == pikepdf.Name("/Caption")
    assert toc_elem["/K"][1].get("/S") == pikepdf.Name("/TOCI")
    assert (caption_tag, entry_tag) == ("Caption", "TOCI")


def test_toc_table_elements_get_toci_tag():
    builder, page = _builder()

    tag = _allocate_fragment_mcid(
        builder, {"type": "toc_item_table", "toc_group_ref": "toc-0"}, 0, page, {}, set()
    )[0]
    builder.finalize()

    toc_elem = builder.doc_elem["/K"][0]
    assert toc_elem.get("/S") == pikepdf.Name("/TOC")
    assert toc_elem["/K"][0].get("/S") == pikepdf.Name("/TOCI")
    assert tag == "TOCI"


@pytest.mark.asyncio
async def test_formula_without_structure_text_reads_its_own_glyphs(tmp_path):
    source = tmp_path / "formula.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 200))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Contents = pdf.make_stream(b"BT /F1 14 Tf 100 100 Td (E = mc2 + ab) Tj ET\n")
    pdf.save(source)
    output = tmp_path / "tagged.pdf"

    await tag_pdf(
        input_path=source,
        output_path=output,
        structure_json={
            "title": "Formula",
            "elements": [
                {"type": "formula", "text": "", "page": 0, "bbox": {"l": 95, "b": 95, "r": 210, "t": 118}}
            ],
        },
        alt_texts=[],
        original_filename="formula.pdf",
    )

    def formulas(node):
        if isinstance(node, pikepdf.Array):
            return [found for child in node for found in formulas(child)]
        if not isinstance(node, pikepdf.Dictionary):
            return []
        found = [node] if node.get("/S") == pikepdf.Name.Formula else []
        return found + formulas(node.get("/K"))

    with pikepdf.open(output) as tagged:
        [formula] = formulas(tagged.Root.StructTreeRoot.K)
        assert str(formula.get("/Alt")) == _formula_alt_text("E = mc2 + ab")
        assert b"/ActualText" in tagged.pages[0].Contents.read_bytes()
