import pikepdf

from app.pipeline.tagger import (
    StructTreeBuilder,
    _allocate_fragment_mcid,
    _formula_alt_text,
)


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
