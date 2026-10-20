"""OCR text layers draw each line as its own text object. Every recognized line
must be tagged, never an artifact, and reach the screen reader exactly once,
whichever paragraph it belongs to."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica

PARAGRAPHS = [
    (["Alpha line one", "Alpha line two", "Alpha line three"], 250),
    (["Beta line one", "Beta line two"], 180),
]
LINE_GAP = 14


def _instruction(operands, operator: str) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(operands, pikepdf.Operator(operator))


def _build_line_per_text_object_pdf(path: Path) -> None:
    """A scanned page with an invisible OCR layer, one text object per line."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    font = helvetica(pdf)
    image = pdf.make_stream(bytes([255]))
    image["/Type"] = pikepdf.Name("/XObject")
    image["/Subtype"] = pikepdf.Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    page["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": font}),
            "/XObject": pikepdf.Dictionary({"/Im0": image}),
        }
    )
    instructions = [
        _instruction([], "q"),
        _instruction([300, 0, 0, 300, 0, 0], "cm"),
        _instruction([pikepdf.Name("/Im0")], "Do"),
        _instruction([], "Q"),
    ]
    for lines, top in PARAGRAPHS:
        for index, line in enumerate(lines):
            instructions += [
                _instruction([], "BT"),
                _instruction([3], "Tr"),
                _instruction([pikepdf.Name("/F1"), 10], "Tf"),
                _instruction([20, top - index * LINE_GAP], "Td"),
                _instruction([pikepdf.String(line)], "Tj"),
                _instruction([], "ET"),
            ]
    page["/Contents"] = pdf.make_stream(pikepdf.unparse_content_stream(instructions))
    pdf.save(path)


def _paragraph_elements() -> list[dict]:
    elements = []
    for lines, top in PARAGRAPHS:
        bottom = top - (len(lines) - 1) * LINE_GAP - 4
        elements.append(
            {
                "type": "paragraph",
                "text": " ".join(lines),
                "page": 0,
                "bbox": {"l": 18, "b": bottom, "r": 200, "t": top + 12},
            }
        )
    return elements


def _assert_every_line_tagged_once(path: Path) -> None:
    """Every text operator sits in marked content with an MCID (not in an
    artifact), and the structure tree refers to each MCID exactly once."""
    with pikepdf.open(path) as pdf:
        open_tags: list[int | None] = []
        text_mcids = []
        for instr in pikepdf.parse_content_stream(pdf.pages[0]):
            op = str(instr.operator)
            if op in ("BDC", "BMC"):
                props = instr.operands[1] if op == "BDC" else None
                mcid = props.get("/MCID") if isinstance(props, pikepdf.Dictionary) else None
                open_tags.append(None if mcid is None else int(mcid))
            elif op == "EMC":
                open_tags.pop()
            elif op in ("Tj", "TJ", "'", '"'):
                assert open_tags and open_tags[-1] is not None, "a text line was not tagged"
                text_mcids.append(open_tags[-1])

        referenced: list[int] = []

        def collect(node) -> None:
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    collect(kid)
            elif isinstance(node, pikepdf.Dictionary):
                if node.get("/Type") == "/MCR":
                    referenced.append(int(node.MCID))
                elif "/K" in node:
                    collect(node.K)
            elif isinstance(node, int):
                referenced.append(int(node))

        collect(pdf.Root.StructTreeRoot.K)
    assert sorted(referenced) == sorted(set(text_mcids))


@pytest.mark.asyncio
async def test_every_ocr_line_is_read_once_in_order(tmp_path):
    input_pdf = tmp_path / "ocr_lines.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    _build_line_per_text_object_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": _paragraph_elements(), "title": "OCR lines"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    _assert_every_line_tagged_once(output_pdf)
    heard = " ".join(screen_reader_text(output_pdf).split())
    expected = " ".join(line for lines, _top in PARAGRAPHS for line in lines)
    assert heard == expected


def _build_interleaved_columns_pdf(path: Path) -> list[dict]:
    """Two columns drawn row by row, so each paragraph's lines alternate in
    the content stream with the other column's."""
    left = ["Left column one", "Left column two", "Left column three"]
    right = ["Right column one", "Right column two", "Right column three"]
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 300))
    font = helvetica(pdf)
    page["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
    instructions = []
    for row, (left_line, right_line) in enumerate(zip(left, right, strict=True)):
        y = 250 - row * LINE_GAP
        for x, line in ((20, left_line), (220, right_line)):
            instructions += [
                _instruction([], "BT"),
                _instruction([3], "Tr"),
                _instruction([pikepdf.Name("/F1"), 10], "Tf"),
                _instruction([x, y], "Td"),
                _instruction([pikepdf.String(line)], "Tj"),
                _instruction([], "ET"),
            ]
    page["/Contents"] = pdf.make_stream(pikepdf.unparse_content_stream(instructions))
    pdf.save(path)
    bottom = 250 - 2 * LINE_GAP - 4
    return [
        {
            "type": "paragraph",
            "text": " ".join(left),
            "page": 0,
            "bbox": {"l": 18, "b": bottom, "r": 190, "t": 262},
        },
        {
            "type": "paragraph",
            "text": " ".join(right),
            "page": 0,
            "bbox": {"l": 218, "b": bottom, "r": 390, "t": 262},
        },
    ]


@pytest.mark.asyncio
async def test_interleaved_column_lines_are_read_once_per_paragraph(tmp_path):
    input_pdf = tmp_path / "columns.pdf"
    output_pdf = tmp_path / "tagged.pdf"
    elements = _build_interleaved_columns_pdf(input_pdf)

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": elements, "title": "Columns"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    _assert_every_line_tagged_once(output_pdf)
    heard = " ".join(screen_reader_text(output_pdf).split())
    assert heard == " ".join(element["text"] for element in elements)
