import pikepdf

from app.pipeline.tagger import (
    ContentRegion,
    StructTreeBuilder,
    _emit_tagged_region,
    _formula_alt_text,
)


def test_formula_regions_emit_formula_tag_with_alt_text():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    builder = StructTreeBuilder(pdf)
    builder.setup()

    region = ContentRegion(
        kind="text",
        start_idx=0,
        end_idx=0,
        instructions=[],
    )
    element = {
        "type": "formula",
        "text": "E = mc^2",
    }
    new_instructions = []

    _emit_tagged_region(
        new_instructions,
        region,
        element,
        builder,
        0,
        page.obj,
        {},
        set(),
    )
    builder.finalize()

    assert len(new_instructions) == 2
    assert str(new_instructions[0].operator) == "BDC"
    assert new_instructions[0].operands[0] == pikepdf.Name("/Formula")
    assert str(new_instructions[0].operands[1]["/ActualText"]) == "E = mc^2"
    assert str(new_instructions[1].operator) == "EMC"

    formula_elem = builder.doc_elem["/K"][0]
    assert formula_elem.get("/S") == pikepdf.Name("/Formula")
    assert str(formula_elem.get("/Alt")) == "E equals m c squared"


def test_formula_alt_text_speaks_subscripts_superscripts_and_symbols():
    assert _formula_alt_text("x_2 + y²") == "x sub 2 plus y squared"


def test_note_regions_emit_note_tag_with_unique_id():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    builder = StructTreeBuilder(pdf)
    builder.setup()

    region = ContentRegion(
        kind="text",
        start_idx=0,
        end_idx=0,
        instructions=[],
    )
    element = {
        "type": "note",
        "text": "Footnote text",
    }
    new_instructions = []

    _emit_tagged_region(
        new_instructions,
        region,
        element,
        builder,
        0,
        page.obj,
        {},
        set(),
    )
    builder.finalize()

    note_elem = builder.doc_elem["/K"][0]
    assert len(new_instructions) == 2
    assert new_instructions[0].operands[0] == pikepdf.Name("/Note")
    assert note_elem.get("/S") == pikepdf.Name("/Note")
    assert str(note_elem.get("/ID")).startswith("note-")


def test_toc_regions_emit_toc_caption_and_toci_children():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    builder = StructTreeBuilder(pdf)
    builder.setup()

    caption_region = ContentRegion(kind="text", start_idx=0, end_idx=0, instructions=[])
    entry_region = ContentRegion(kind="text", start_idx=1, end_idx=1, instructions=[])
    caption_element = {
        "type": "toc_caption",
        "text": "Contents",
        "toc_group_ref": "toc-0",
    }
    entry_element = {
        "type": "toc_item",
        "text": "Introduction ........ 1",
        "toc_group_ref": "toc-0",
    }
    new_instructions = []

    _emit_tagged_region(new_instructions, caption_region, caption_element, builder, 0, page.obj, {}, set())
    _emit_tagged_region(new_instructions, entry_region, entry_element, builder, 0, page.obj, {}, set())
    builder.finalize()

    toc_elem = builder.doc_elem["/K"][0]
    assert toc_elem.get("/S") == pikepdf.Name("/TOC")
    assert toc_elem["/K"][0].get("/S") == pikepdf.Name("/Caption")
    assert toc_elem["/K"][1].get("/S") == pikepdf.Name("/TOCI")
    assert new_instructions[0].operands[0] == pikepdf.Name("/Caption")
    assert new_instructions[2].operands[0] == pikepdf.Name("/TOCI")


def test_toc_table_regions_emit_toci_tag():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    builder = StructTreeBuilder(pdf)
    builder.setup()

    region = ContentRegion(kind="text", start_idx=0, end_idx=0, instructions=[])
    element = {
        "type": "toc_item_table",
        "toc_group_ref": "toc-0",
    }
    new_instructions = []

    _emit_tagged_region(new_instructions, region, element, builder, 0, page.obj, {}, set())
    builder.finalize()

    toc_elem = builder.doc_elem["/K"][0]
    assert toc_elem.get("/S") == pikepdf.Name("/TOC")
    assert toc_elem["/K"][0].get("/S") == pikepdf.Name("/TOCI")
    assert new_instructions[0].operands[0] == pikepdf.Name("/TOCI")
