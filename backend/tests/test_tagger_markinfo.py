from pathlib import Path

import pikepdf
import pytest

from app.pipeline.tagger import (
    ContentRegion,
    _complete_table_grid_cells,
    _emit_tagged_region,
    _table_summary_text,
    tag_pdf,
)
from tests.fixtures import TEST_SAMPLE_PDF

_CONTENT_PAINT_OPERATORS = frozenset({
    "Tj",
    "TJ",
    "'",
    '"',
    "Do",
    "f",
    "F",
    "f*",
    "S",
    "s",
    "B",
    "B*",
    "b",
    "b*",
    "sh",
    "INLINE IMAGE",
})


def _unmarked_paint_operators(page) -> list[str]:
    depth = 0
    unmarked: list[str] = []
    for instr in pikepdf.parse_content_stream(page):
        op = str(instr.operator)
        if op in {"BDC", "BMC"}:
            depth += 1
        elif op == "EMC":
            depth = max(0, depth - 1)
        elif op in _CONTENT_PAINT_OPERATORS and depth == 0:
            unmarked.append(op)
    return unmarked


@pytest.mark.asyncio
async def test_tag_pdf_sets_markinfo_suspects_false(tmp_path):
    input_pdf = TEST_SAMPLE_PDF
    output_pdf = tmp_path / "tagged.pdf"

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "Test Sample"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        mark_info = pdf.Root["/MarkInfo"]
        assert mark_info["/Marked"] is True
        assert mark_info["/Suspects"] is False
        assert all(page.get("/Tabs") == pikepdf.Name("/S") for page in pdf.pages)


def _build_ocr_form_text_pdf(
    path: Path,
    *,
    invocation_cm: tuple[int, int, int, int, int, int] | None = None,
) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Helvetica"),
            }
        )
    )
    font_name = pikepdf.Name("/F1")
    form = pdf.make_stream(
        pikepdf.unparse_content_stream(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([20, 160], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Accessible Heading")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([20, 120], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Body text")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
            ]
        )
    )
    form["/Type"] = pikepdf.Name("/XObject")
    form["/Subtype"] = pikepdf.Name("/Form")
    form["/BBox"] = pikepdf.Array([0, 0, 200, 200])
    form["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})

    page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/OCR0": form})})
    page_instructions = [pikepdf.ContentStreamInstruction([], pikepdf.Operator("q"))]
    if invocation_cm is not None:
        page_instructions.append(
            pikepdf.ContentStreamInstruction(list(invocation_cm), pikepdf.Operator("cm"))
        )
    page_instructions.extend(
        [
            pikepdf.ContentStreamInstruction([pikepdf.Name("/OCR0")], pikepdf.Operator("Do")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
        ]
    )
    page["/Contents"] = pdf.make_stream(pikepdf.unparse_content_stream(page_instructions))
    pdf.save(path)


def _build_page_level_ocr_text_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Helvetica"),
            }
        )
    )
    image = pdf.make_stream(bytes([255]))
    image["/Type"] = pikepdf.Name("/XObject")
    image["/Subtype"] = pikepdf.Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    image["/BitsPerComponent"] = 8

    page["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({"/F1": font}),
        "/XObject": pikepdf.Dictionary({"/Im0": image}),
    })
    page["/Contents"] = pdf.make_stream(
        pikepdf.unparse_content_stream(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                pikepdf.ContentStreamInstruction([200, 0, 0, 200, 0, 0], pikepdf.Operator("cm")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/Im0")], pikepdf.Operator("Do")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/F1"), 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([3], pikepdf.Operator("Tr")),
                pikepdf.ContentStreamInstruction([1, 0, 0, 1, 20, 160], pikepdf.Operator("Tm")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Accessible Heading")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([1, 0, 0, 1, 20, 130], pikepdf.Operator("Tm")),
                pikepdf.ContentStreamInstruction([pikepdf.String("First paragraph text")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([1, 0, 0, 1, 20, 110], pikepdf.Operator("Tm")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Second paragraph text")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
            ]
        )
    )
    pdf.save(path)


def _add_unmatched_page_drawing(path: Path) -> None:
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        instructions = list(pikepdf.parse_content_stream(page))
        decorative_prefix = [
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
            pikepdf.ContentStreamInstruction([0.9, 0.9, 0.9], pikepdf.Operator("rg")),
            pikepdf.ContentStreamInstruction([0, 0, 12, 12], pikepdf.Operator("re")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("f")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
        ]
        decorative_suffix = [
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
            pikepdf.ContentStreamInstruction([0, 0, 0], pikepdf.Operator("RG")),
            pikepdf.ContentStreamInstruction([1], pikepdf.Operator("w")),
            pikepdf.ContentStreamInstruction([0, 0], pikepdf.Operator("m")),
            pikepdf.ContentStreamInstruction([20, 0], pikepdf.Operator("l")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("S")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
        ]
        page["/Contents"] = pdf.make_stream(
            pikepdf.unparse_content_stream(
                decorative_prefix + instructions + decorative_suffix
            )
        )
        pdf.save(path)


def _add_unmatched_inline_image(path: Path) -> None:
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        original = bytes(page.Contents.read_bytes())
        inline_image = (
            b"q 12 0 0 12 0 0 cm "
            b"BI /W 1 /H 1 /BPC 1 /CS /DeviceGray ID \xff EI "
            b"Q\n"
        )
        page["/Contents"] = pdf.make_stream(inline_image + original)
        pdf.save(path)


def _build_single_text_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Helvetica"),
            }
        )
    )
    page["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
    page["/Contents"] = pdf.make_stream(
        pikepdf.unparse_content_stream(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/F1"), 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([1, 0, 0, 1, 20, 160], pikepdf.Operator("Tm")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Running header")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
            ]
        )
    )
    pdf.save(path)


def _artifact_property_dicts(page) -> list[pikepdf.Dictionary]:
    property_dicts: list[pikepdf.Dictionary] = []
    for instr in pikepdf.parse_content_stream(page):
        if str(instr.operator) != "BDC":
            continue
        operands = list(instr.operands)
        if len(operands) >= 2 and operands[0] == pikepdf.Name("/Artifact"):
            property_dicts.append(operands[1])
    return property_dicts


def test_complete_table_grid_cells_requires_explicit_dimensions():
    cells = [{"row": 0, "col": 0, "text": "Header", "is_header": True}]

    assert _complete_table_grid_cells(cells, num_rows=0, num_cols=1) == []
    assert _complete_table_grid_cells(cells, num_rows=1, num_cols=0) == []


def test_complete_table_grid_cells_adds_empty_placeholders_only():
    cells = [
        {"row": 0, "col": 0, "text": "Header", "is_header": True},
        {"row": 1, "col": 1, "text": "Value"},
    ]

    completed = _complete_table_grid_cells(cells, num_rows=2, num_cols=2)

    assert len(completed) == 4
    placeholders = [cell for cell in completed if cell.get("_generated_empty")]
    assert placeholders == [
        {"row": 0, "col": 1, "row_span": 1, "col_span": 1, "text": "", "is_header": False, "_generated_empty": True},
        {"row": 1, "col": 0, "row_span": 1, "col_span": 1, "text": "", "is_header": False, "_generated_empty": True},
    ]
    assert any(cell["text"] == "Header" and cell["is_header"] is True for cell in completed)
    assert any(cell["text"] == "Value" for cell in completed)


def test_table_summary_uses_confirmed_summary_then_dimension_fallback():
    assert (
        _table_summary_text(
            {
                "caption": "Ignored caption",
                "table_llm_confirmed": True,
                "table_llm_summary": "Confirmed table purpose.",
            },
            num_rows=2,
            num_cols=3,
        )
        == "Table with 2 rows and 3 columns. Confirmed table purpose."
    )
    assert _table_summary_text({}, num_rows=1, num_cols=1) == "Table with 1 row and 1 column."


def _build_source_alt_figure_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    image = pdf.make_stream(bytes([128]))
    image["/Type"] = pikepdf.Name("/XObject")
    image["/Subtype"] = pikepdf.Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    image["/BitsPerComponent"] = 8

    page["/Resources"] = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": image})})
    page["/Contents"] = pdf.make_stream(
        pikepdf.unparse_content_stream(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                pikepdf.ContentStreamInstruction([50, 0, 0, 50, 20, 20], pikepdf.Operator("cm")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/Im0")], pikepdf.Operator("Do")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
            ]
        )
    )

    struct_root = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([]),
                "/ParentTree": pdf.make_indirect(pikepdf.Dictionary({"/Nums": pikepdf.Array([])})),
            }
        )
    )
    doc_elem = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Document"),
                "/P": struct_root,
                "/K": pikepdf.Array([]),
            }
        )
    )
    fig_elem = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Figure"),
                "/P": doc_elem,
                "/Alt": pikepdf.String("Detailed source alt text"),
                "/K": pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/MCR"),
                        "/Pg": page.obj,
                        "/MCID": 0,
                    }
                ),
            }
        )
    )
    doc_elem["/K"].append(fig_elem)
    struct_root["/K"] = pikepdf.Array([doc_elem])
    pdf.Root["/StructTreeRoot"] = struct_root
    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
    pdf.save(path)


def _collect_struct_types(root) -> list[str]:
    types: list[str] = []

    def _walk(node) -> None:
        if isinstance(node, pikepdf.Dictionary):
            struct_type = node.get("/S")
            if struct_type:
                types.append(str(struct_type))
            kids = node.get("/K")
            if isinstance(kids, pikepdf.Array):
                for child in kids:
                    _walk(child)
            elif isinstance(kids, pikepdf.Dictionary):
                _walk(kids)
        elif isinstance(node, pikepdf.Array):
            for child in node:
                _walk(child)

    _walk(root)
    return types


def _first_struct_elem(root, struct_type: str):
    if isinstance(root, pikepdf.Dictionary):
        if root.get("/S") == pikepdf.Name(f"/{struct_type}"):
            return root
        kids = root.get("/K")
        if isinstance(kids, pikepdf.Array):
            for child in kids:
                found = _first_struct_elem(child, struct_type)
                if found is not None:
                    return found
        elif isinstance(kids, pikepdf.Dictionary):
            return _first_struct_elem(kids, struct_type)
    elif isinstance(root, pikepdf.Array):
        for child in root:
            found = _first_struct_elem(child, struct_type)
            if found is not None:
                return found
    return None


@pytest.mark.asyncio
async def test_tag_pdf_tags_ocr_form_xobject_text(tmp_path):
    input_pdf = tmp_path / "ocr_form_text.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_ocr_form_text_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 18, "b": 154, "r": 150, "t": 176},
                },
                {
                    "type": "paragraph",
                    "text": "Body text",
                    "page": 0,
                    "bbox": {"l": 18, "b": 114, "r": 100, "t": 136},
                },
            ],
            "title": "OCR Form Text",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        page = pdf.pages[0]
        form = page["/Resources"]["/XObject"]["/OCR0"]
        struct_types = _collect_struct_types(pdf.Root["/StructTreeRoot"])
        page_stream = bytes(page.Contents.read_bytes())
        form_stream = bytes(form.read_bytes())

        assert "/H1" in struct_types
        assert "/P" in struct_types
        assert form.get("/StructParents") is not None
        assert page.get("/Tabs") == pikepdf.Name("/S")
        assert b"/Artifact" not in page_stream
        assert b"/H1" in form_stream
        assert b"/P" in form_stream


@pytest.mark.asyncio
async def test_tag_pdf_tags_transformed_ocr_form_xobject_text(tmp_path):
    input_pdf = tmp_path / "transformed_ocr_form_text.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_ocr_form_text_pdf(input_pdf, invocation_cm=(1, 0, 0, 1, 30, -20))

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 48, "b": 134, "r": 180, "t": 156},
                },
                {
                    "type": "paragraph",
                    "text": "Body text",
                    "page": 0,
                    "bbox": {"l": 48, "b": 94, "r": 130, "t": 116},
                },
            ],
            "title": "Transformed OCR Form Text",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        form = pdf.pages[0]["/Resources"]["/XObject"]["/OCR0"]
        struct_types = _collect_struct_types(pdf.Root["/StructTreeRoot"])
        form_stream = bytes(form.read_bytes())

        assert "/H1" in struct_types
        assert "/P" in struct_types
        assert form.get("/StructParents") is not None
        assert b"/H1" in form_stream
        assert b"/P" in form_stream


@pytest.mark.asyncio
async def test_tag_pdf_tags_page_level_ocr_text_object(tmp_path):
    input_pdf = tmp_path / "page_level_ocr_text.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_page_level_ocr_text_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 18, "b": 154, "r": 150, "t": 176},
                },
                {
                    "type": "paragraph",
                    "text": "First paragraph text",
                    "page": 0,
                    "bbox": {"l": 18, "b": 124, "r": 140, "t": 146},
                },
                {
                    "type": "paragraph",
                    "text": "Second paragraph text",
                    "page": 0,
                    "bbox": {"l": 18, "b": 104, "r": 150, "t": 126},
                },
            ],
            "title": "Page OCR Text",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        page = pdf.pages[0]
        struct_types = _collect_struct_types(pdf.Root["/StructTreeRoot"])
        stream = bytes(page.Contents.read_bytes())

        assert "/H1" in struct_types
        assert struct_types.count("/P") == 2
        assert "/Figure" not in struct_types
        assert page.get("/Tabs") == pikepdf.Name("/S")
        assert page.get("/StructParents") is not None
        assert b"/H1" in stream
        assert stream.count(b"/P") >= 2
        assert b"/Artifact" in stream
        assert b"/Im0 Do" in stream


@pytest.mark.asyncio
async def test_tag_pdf_tags_page_level_ocr_table_cells(tmp_path):
    input_pdf = tmp_path / "page_level_ocr_table.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_page_level_ocr_text_pdf(input_pdf)

    result = await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 18, "b": 154, "r": 150, "t": 176},
                },
                {
                    "type": "table",
                    "page": 0,
                    "bbox": {"l": 18, "b": 100, "r": 190, "t": 150},
                    "num_rows": 2,
                    "num_cols": 2,
                    "cells": [
                        {
                            "row": 0,
                            "col": 0,
                            "text": "First paragraph text",
                            "is_header": True,
                            "column_header": True,
                            "row_span": 1,
                            "col_span": 1,
                        },
                        {
                            "row": 0,
                            "col": 1,
                            "text": "Sparse header",
                            "is_header": True,
                            "column_header": True,
                            "row_span": 1,
                            "col_span": 1,
                        },
                        {
                            "row": 1,
                            "col": 0,
                            "text": "Second paragraph text",
                            "is_header": False,
                            "row_span": 1,
                            "col_span": 1,
                        },
                    ],
                },
            ],
            "title": "Page OCR Table",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        page = pdf.pages[0]
        struct_root = pdf.Root["/StructTreeRoot"]
        struct_types = _collect_struct_types(struct_root)
        table = _first_struct_elem(struct_root, "Table")
        stream = bytes(page.Contents.read_bytes())

        assert result.tables_tagged == 1
        assert table is not None
        assert str(table["/A"]["/Summary"]) == "Table with 2 rows and 2 columns."
        assert "/Table" in struct_types
        assert "/TR" in struct_types
        assert "/TH" in struct_types
        assert "/TD" in struct_types
        assert b"/TH" in stream
        assert b"/TD" in stream
        assert page.get("/Tabs") == pikepdf.Name("/S")


@pytest.mark.asyncio
async def test_tag_pdf_artifacts_unmatched_drawing_in_fragmented_rewrite(tmp_path):
    input_pdf = tmp_path / "page_level_ocr_table_with_decorative_drawing.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_page_level_ocr_text_pdf(input_pdf)
    _add_unmatched_page_drawing(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 18, "b": 154, "r": 150, "t": 176},
                },
                {
                    "type": "table",
                    "page": 0,
                    "bbox": {"l": 18, "b": 100, "r": 190, "t": 150},
                    "num_rows": 2,
                    "num_cols": 2,
                    "cells": [
                        {
                            "row": 0,
                            "col": 0,
                            "text": "First paragraph text",
                            "is_header": True,
                            "column_header": True,
                            "row_span": 1,
                            "col_span": 1,
                        },
                        {
                            "row": 1,
                            "col": 0,
                            "text": "Second paragraph text",
                            "is_header": False,
                            "row_span": 1,
                            "col_span": 1,
                        },
                    ],
                },
            ],
            "title": "Page OCR Table",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        assert _unmarked_paint_operators(pdf.pages[0]) == []


@pytest.mark.asyncio
async def test_tag_pdf_artifacts_unmatched_inline_image_in_fragmented_rewrite(tmp_path):
    input_pdf = tmp_path / "page_level_ocr_table_with_inline_image.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_page_level_ocr_text_pdf(input_pdf)
    _add_unmatched_inline_image(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 18, "b": 154, "r": 150, "t": 176},
                },
                {
                    "type": "table",
                    "page": 0,
                    "bbox": {"l": 18, "b": 100, "r": 190, "t": 150},
                    "num_rows": 2,
                    "num_cols": 2,
                    "cells": [
                        {
                            "row": 0,
                            "col": 0,
                            "text": "First paragraph text",
                            "is_header": True,
                            "column_header": True,
                            "row_span": 1,
                            "col_span": 1,
                        },
                        {
                            "row": 1,
                            "col": 0,
                            "text": "Second paragraph text",
                            "is_header": False,
                            "row_span": 1,
                            "col_span": 1,
                        },
                    ],
                },
            ],
            "title": "Page OCR Table",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        assert _unmarked_paint_operators(pdf.pages[0]) == []


@pytest.mark.asyncio
async def test_tag_pdf_preserves_pagination_artifact_metadata(tmp_path):
    input_pdf = tmp_path / "header.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_single_text_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "artifact",
                    "artifact_type": "page_header",
                    "text": "Running header",
                    "page": 0,
                    "bbox": {"l": 18, "b": 156, "r": 130, "t": 176},
                },
            ],
            "title": "Header Artifact",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        artifact_props = _artifact_property_dicts(pdf.pages[0])
        assert artifact_props
        assert artifact_props[0]["/Type"] == pikepdf.Name("/Pagination")
        assert artifact_props[0]["/Subtype"] == pikepdf.Name("/Header")
        assert artifact_props[0]["/Attached"] == pikepdf.Array([pikepdf.Name("/Top")])
        assert list(artifact_props[0]["/BBox"]) == [18, 156, 130, 176]


@pytest.mark.asyncio
async def test_tag_pdf_tags_clipped_scan_figure_when_alt_is_available(tmp_path):
    input_pdf = tmp_path / "page_level_ocr_text.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_page_level_ocr_text_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 18, "b": 154, "r": 150, "t": 176},
                },
                {
                    "type": "paragraph",
                    "text": "First paragraph text",
                    "page": 0,
                    "bbox": {"l": 18, "b": 124, "r": 140, "t": 146},
                },
                {
                    "type": "paragraph",
                    "text": "Second paragraph text",
                    "page": 0,
                    "bbox": {"l": 18, "b": 104, "r": 150, "t": 126},
                },
                {
                    "type": "figure",
                    "figure_index": 0,
                    "page": 0,
                    "bbox": {"l": 80, "b": 40, "r": 150, "t": 90},
                },
            ],
            "title": "Page OCR Text",
        },
        alt_texts=[
            {
                "figure_index": 0,
                "text": "Gray scan figure region.",
                "status": "approved",
            }
        ],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        page = pdf.pages[0]
        struct_types = _collect_struct_types(pdf.Root["/StructTreeRoot"])
        figure = _first_struct_elem(pdf.Root["/StructTreeRoot"], "Figure")
        stream = bytes(page.Contents.read_bytes())

        assert "/Figure" in struct_types
        assert figure is not None
        assert str(figure.get("/Alt")) == "Gray scan figure region."
        assert b"/Figure" in stream
        assert b"/Im0 Do" in stream


@pytest.mark.asyncio
async def test_tag_pdf_does_not_create_clipped_scan_figure_without_alt_text(tmp_path):
    input_pdf = tmp_path / "page_level_ocr_text.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_page_level_ocr_text_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Accessible Heading",
                    "page": 0,
                    "bbox": {"l": 18, "b": 154, "r": 150, "t": 176},
                },
                {
                    "type": "paragraph",
                    "text": "First paragraph text",
                    "page": 0,
                    "bbox": {"l": 18, "b": 124, "r": 140, "t": 146},
                },
                {
                    "type": "paragraph",
                    "text": "Second paragraph text",
                    "page": 0,
                    "bbox": {"l": 18, "b": 104, "r": 150, "t": 126},
                },
                {
                    "type": "figure",
                    "figure_index": 0,
                    "page": 0,
                    "bbox": {"l": 80, "b": 40, "r": 150, "t": 90},
                },
            ],
            "title": "Page OCR Text",
        },
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        struct_types = _collect_struct_types(pdf.Root["/StructTreeRoot"])
        assert "/Figure" not in struct_types


@pytest.mark.asyncio
async def test_tag_pdf_preserves_source_alt_when_generated_alt_is_generic(tmp_path):
    input_pdf = tmp_path / "source_alt_figure.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_source_alt_figure_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={
            "elements": [
                {
                    "type": "figure",
                    "figure_index": 0,
                    "page": 0,
                    "bbox": {"l": 20, "b": 20, "r": 70, "t": 70},
                }
            ],
            "title": "Source Alt Figure",
        },
        alt_texts=[{"figure_index": 0, "text": "Element 2", "status": "approved"}],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        figure_elem = _first_struct_elem(pdf.Root["/StructTreeRoot"], "Figure")
        assert figure_elem is not None
        assert str(figure_elem.get("/Alt")) == "Detailed source alt text"


def _build_blank_image_only_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    empty_form = pdf.make_stream(b"")
    empty_form["/Type"] = pikepdf.Name("/XObject")
    empty_form["/Subtype"] = pikepdf.Name("/Form")
    empty_form["/FormType"] = 1
    empty_form["/BBox"] = pikepdf.Array([0, 0, 200, 200])
    empty_form["/Resources"] = pikepdf.Dictionary()

    white_image = pdf.make_stream(bytes([255]))
    white_image["/Type"] = pikepdf.Name("/XObject")
    white_image["/Subtype"] = pikepdf.Name("/Image")
    white_image["/Width"] = 1
    white_image["/Height"] = 1
    white_image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    white_image["/BitsPerComponent"] = 8

    page["/Resources"] = pikepdf.Dictionary({
        "/XObject": pikepdf.Dictionary({
            "/OCR0": empty_form,
            "/Im0": white_image,
        }),
    })
    page["/Contents"] = pdf.make_stream(
        b"q 1 0 0 1 0 0 cm\n/OCR0 Do\nQ\nq\n200 0 0 200 0 0 cm\n/Im0 Do\nQ\n"
    )
    pdf.save(path)


def _build_ocr_noise_only_pdf(path: Path, *, ocr_name: str = "/OCR-noise") -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    empty_form = pdf.make_stream(b"")
    empty_form["/Type"] = pikepdf.Name("/XObject")
    empty_form["/Subtype"] = pikepdf.Name("/Form")
    empty_form["/FormType"] = 1
    empty_form["/BBox"] = pikepdf.Array([0, 0, 200, 200])
    empty_form["/Resources"] = pikepdf.Dictionary()

    white_image = pdf.make_stream(bytes([255]))
    white_image["/Type"] = pikepdf.Name("/XObject")
    white_image["/Subtype"] = pikepdf.Name("/Image")
    white_image["/Width"] = 1
    white_image["/Height"] = 1
    white_image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    white_image["/BitsPerComponent"] = 8

    page["/Resources"] = pikepdf.Dictionary({
        "/XObject": pikepdf.Dictionary({
            ocr_name: empty_form,
            "/Im0": white_image,
        }),
    })
    page["/Contents"] = pdf.make_stream(
        f"q 1 0 0 1 0 0 cm\n{ocr_name} Do\nQ\n".encode()
        +
        b"q 200 0 0 200 0 0 cm\n/Im0 Do\nQ\n"
        b"q\n0 0 200 200 re\n0 0 200 200 re\nW*\nn\n"
        b"0.239 w\nq\n1 0 0 1 10 10 cm\n0 0 m\n0 25 l\nS\nQ\nQ\n"
    )
    pdf.save(path)


@pytest.mark.asyncio
async def test_tag_pdf_artifacts_visually_blank_image_only_pages(monkeypatch, tmp_path):
    input_pdf = tmp_path / "blank_image_only.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_blank_image_only_pdf(input_pdf)

    monkeypatch.setattr(
        "app.pipeline.tagger._render_page_ink_ratio",
        lambda *_args, **_kwargs: 0.0,
    )

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "Blank OCR Page"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        stream = bytes(pdf.pages[0].Contents.read_bytes())
        assert b"/Artifact BMC" in stream
        assert b"/OCR0 Do" in stream
        assert b"/Im0 Do" in stream


@pytest.mark.asyncio
async def test_tag_pdf_artifacts_ocr_noise_only_pages(monkeypatch, tmp_path):
    input_pdf = tmp_path / "ocr_noise_only.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_ocr_noise_only_pdf(input_pdf, ocr_name="/OCR0")

    monkeypatch.setattr(
        "app.pipeline.tagger._render_page_ink_ratio",
        lambda *_args, **_kwargs: 0.0,
    )

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "OCR Noise"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        stream = bytes(pdf.pages[0].Contents.read_bytes())
        assert b"/Artifact BMC" in stream
        assert b"/Im0 Do" in stream


@pytest.mark.asyncio
async def test_tag_pdf_does_not_artifact_nonblank_ocr_noise_only_pages(monkeypatch, tmp_path):
    input_pdf = tmp_path / "ocr_noise_only_nonblank.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_ocr_noise_only_pdf(input_pdf)

    monkeypatch.setattr(
        "app.pipeline.tagger._render_page_ink_ratio",
        lambda *_args, **_kwargs: 0.02,
    )

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "OCR Noise"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        stream = bytes(pdf.pages[0].Contents.read_bytes())
        assert b"/Artifact BMC" not in stream


def test_emit_tagged_region_includes_actualtext_for_resolved_heading():
    class _Builder:
        def add_heading(self, level, page_index, page_ref, text, lang=None, stream_owner=None):
            assert level == 2
            assert text == "ABSTRACT"
            return 7

    region = ContentRegion(
        kind="text",
        start_idx=0,
        end_idx=1,
        instructions=[pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT"))],
    )
    instructions: list = []

    _emit_tagged_region(
        instructions,
        region,
        {
            "type": "heading",
            "level": 2,
            "text": "A B S T R A C T",
            "actual_text": "ABSTRACT",
        },
        _Builder(),
        page_index=0,
        page_ref=None,
        alt_lookup={},
        decorative_figures=set(),
    )

    bdc = instructions[0]
    attributes = bdc.operands[1]
    assert attributes["/MCID"] == 7
    assert str(attributes["/ActualText"]) == "ABSTRACT"
