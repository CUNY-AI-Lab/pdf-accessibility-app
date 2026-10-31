"""Regression cases scored through the tag tree, not the searchable text layer."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica, rendered


def _page(path: Path, lines: list[tuple[int, int, str]], *, scan=False, form=False):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 300))
    resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    text = b"\n".join(
        f"BT /F1 10 Tf {'3 Tr ' if scan else ''}1 0 0 1 {x} {y} Tm ".encode()
        + pikepdf.unparse_content_stream(
            [pikepdf.ContentStreamInstruction([pikepdf.String(line)], pikepdf.Operator("Tj"))]
        )
        + b" ET"
        for x, y, line in lines
    )
    content = b""
    if scan:
        image = pdf.make_stream(bytes([230]))
        image.Type = pikepdf.Name.XObject
        image.Subtype = pikepdf.Name.Image
        image.Width = image.Height = 1
        image.ColorSpace = pikepdf.Name.DeviceGray
        image.BitsPerComponent = 8
        resources.XObject = pikepdf.Dictionary(Im0=image)
        content = b"q 400 0 0 300 0 0 cm /Im0 Do Q\n"
    if form:
        ocr = pdf.make_stream(text)
        ocr.Type = pikepdf.Name.XObject
        ocr.Subtype = pikepdf.Name.Form
        ocr.BBox = [0, 0, 400, 300]
        ocr.Resources = pikepdf.Dictionary(Font=resources.Font)
        resources.XObject["/OCR-test"] = ocr
        text = b"q /OCR-test Do Q"
    page.Resources = resources
    # Decorative vector paint must not turn into spoken content.
    page.Contents = pdf.make_stream(content + text + b"\n10 10 5 5 re f")
    pdf.save(path)
    with PdfGlyphReader(path) as reader:
        return [op.bbox for op in reader.page(0).ops[("OCR-test",) if form else ()]]


def _element(box, kind="paragraph", **kwargs):
    return {"type": kind, "page": 0, "bbox": box, **kwargs}


async def _tag(source, output, elements):
    result = await tag_pdf(source, output, {"elements": elements, "title": "Fallbacks"}, [])
    assert rendered(output) == rendered(source)
    return result


def _heard(path, **kwargs):
    return " ".join(screen_reader_text(path, **kwargs).split())


@pytest.mark.asyncio
async def test_table_line_outside_cell_box_reaches_nearest_row_and_column(tmp_path):
    source, output = tmp_path / "table.pdf", tmp_path / "tagged.pdf"
    boxes = _page(
        source,
        [
            (30, 240, "Crop"),
            (200, 240, "Price"),
            (30, 226, "Wheat"),
            (30, 195, "Corn"),
            (200, 195, "Two"),
        ],
    )
    table = _element(
        {"l": 20, "b": 180, "r": 360, "t": 260},
        "table",
        num_rows=2,
        num_cols=2,
        cells=[
            {"row": r, "col": c, "bbox": boxes[i], "is_header": r == 0}
            for i, r, c in [(0, 0, 0), (1, 0, 1), (3, 1, 0), (4, 1, 1)]
        ],
    )
    result = await _tag(source, output, [table])
    heard = _heard(output, markdown=True)
    assert "<th>Crop Wheat</th><th>Price</th>" in heard
    assert "<td>Corn</td><td>Two</td>" in heard
    assert result.unowned_words_by_page == [{"page": 0, "before": 1, "after": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [False, True])
async def test_hanging_list_number_is_read_before_item_even_when_drawn_last(tmp_path, corrected):
    source, output = tmp_path / "list.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(50, 240, "First item"), (34, 240, "1.")])
    result = await _tag(
        source,
        output,
        [
            _element(
                boxes[0],
                "list_item",
                marker="1.",
                enumerated=True,
                resolved_text="First item" if corrected else None,
            )
        ],
    )
    assert _heard(output, markdown=True) == "- 1. First item"
    assert result.unowned_words_by_page == [{"page": 0, "before": 1, "after": 0}]


@pytest.mark.asyncio
async def test_paragraph_line_outside_box_is_read_in_same_column(tmp_path):
    source, output = tmp_path / "paragraph.pdf", tmp_path / "tagged.pdf"
    boxes = _page(
        source, [(30, 240, "Start here"), (30, 226, "Continue here"), (230, 228, "Other column")]
    )
    result = await _tag(source, output, [_element(boxes[0]), _element(boxes[2])])
    assert _heard(output) == "Start here Continue here Other column"
    assert result.unowned_words_by_page == [{"page": 0, "before": 2, "after": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_running_head_stays_hidden_while_missing_body_text_is_recovered(tmp_path, explicit):
    source, output = tmp_path / "header.pdf", tmp_path / "tagged.pdf"
    # Explicit header away from the margin also exercises Docling's region guard.
    head_y = 265 if explicit else 290
    boxes = _page(
        source,
        [
            (30, 240, "Body starts"),
            (30, 226, "Body continues"),
            (30, head_y, "Running head"),
            (190, 5, "17"),
        ],
    )
    elements = [_element(boxes[0])]
    if explicit:
        # This box overlaps both header words, but holds less than half of
        # either; the exact ownership rule therefore leaves them unclaimed.
        elements.append(
            _element({**boxes[2], "l": 63, "r": 75}, "artifact", artifact_type="page_header")
        )
    result = await _tag(source, output, elements)
    assert _heard(output) == "Body starts Body continues"
    assert result.unowned_words_by_page[0]["after"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("form", [False, True])
async def test_ocr_text_under_full_page_picture_becomes_ordered_paragraphs(tmp_path, form):
    source, output = tmp_path / "scan.pdf", tmp_path / "tagged.pdf"
    _page(
        source,
        [(30, 130, "Second passage"), (30, 240, "First passage"), (30, 290, "Running head")],
        scan=True,
        form=form,
    )
    result = await _tag(
        source, output, [_element({"l": 0, "b": 0, "r": 400, "t": 300}, "figure", figure_index=0)]
    )
    assert _heard(output) == "First passage Second passage"
    assert result.unowned_words_by_page == [{"page": 0, "before": 6, "after": 2}]
    with pikepdf.open(output) as pdf:
        assert all(kid.S == "/P" for kid in pdf.Root.StructTreeRoot.K[0].K)


@pytest.mark.asyncio
async def test_isolated_body_line_gets_new_paragraph_between_existing_elements(tmp_path):
    source, output = tmp_path / "gap.pdf", tmp_path / "tagged.pdf"
    boxes = _page(
        source, [(30, 70, "Last passage"), (30, 245, "First passage"), (30, 160, "Missing passage")]
    )
    await _tag(source, output, [_element(boxes[1]), _element(boxes[0])])
    assert _heard(output) == "First passage Missing passage Last passage"


@pytest.mark.asyncio
async def test_missing_layout_still_recovers_body_text(tmp_path):
    source, output = tmp_path / "no_layout.pdf", tmp_path / "tagged.pdf"
    _page(source, [(30, 70, "Last passage"), (30, 245, "First passage")])
    await _tag(source, output, [])
    assert _heard(output) == "First passage Last passage"


@pytest.mark.asyncio
async def test_ordinary_figure_text_is_not_promoted_to_body_paragraph(tmp_path):
    source, output = tmp_path / "figure.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(30, 240, "Body text"), (30, 150, "Diagram label")], scan=True)
    await _tag(
        source,
        output,
        [
            _element(boxes[0]),
            _element({"l": 20, "b": 100, "r": 220, "t": 180}, "figure", figure_index=0),
        ],
    )
    assert _heard(output) == "Body text"


@pytest.mark.asyncio
@pytest.mark.parametrize(("share", "expected"), [(0.79, ""), (0.80, "Recognized text")])
async def test_scan_picture_fallback_requires_eighty_percent_page_coverage(
    tmp_path, share, expected
):
    source, output = tmp_path / "threshold.pdf", tmp_path / "tagged.pdf"
    _page(source, [(30, 150, "Recognized text")], scan=True)
    await _tag(
        source,
        output,
        [_element({"l": 0, "b": 0, "r": 400 * share, "t": 300}, "figure", figure_index=0)],
    )
    assert _heard(output) == expected


@pytest.mark.asyncio
async def test_already_owned_text_in_margin_band_keeps_its_owner(tmp_path):
    source, output = tmp_path / "owned.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(30, 290, "Owned heading"), (230, 290, "Running head")])
    result = await _tag(source, output, [_element(boxes[0], "heading", level=1)])
    assert _heard(output) == "Owned heading"
    assert result.unowned_words_by_page == [{"page": 0, "before": 2, "after": 2}]


@pytest.mark.asyncio
async def test_new_paragraph_after_table_uses_table_as_reading_order_anchor(tmp_path):
    source, output = tmp_path / "table_gap.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(30, 70, "After table"), (30, 240, "Cell")])
    table = _element(
        boxes[1], "table", num_rows=1, num_cols=1, cells=[{"row": 0, "col": 0, "bbox": boxes[1]}]
    )
    await _tag(source, output, [table])
    assert _heard(output) == "Cell After table"


@pytest.mark.asyncio
@pytest.mark.parametrize("stroke", ["|", "l", "I", "!"])
@pytest.mark.parametrize("x", [18, 100, 202])
async def test_unclaimed_table_rule_strokes_stay_hidden(tmp_path, stroke, x):
    source, output = tmp_path / "rule.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(30, 240, "R2"), (x, 220, stroke)])
    table = _element(
        {"l": 20, "b": 200, "r": 200, "t": 260}, "table", num_rows=1, num_cols=1,
        cells=[{"row": 0, "col": 0, "bbox": boxes[0]}],
    )
    await _tag(source, output, [table])
    assert _heard(output) == "R2"
    assert "<td>R2</td>" in _heard(output, markdown=True)


@pytest.mark.asyncio
async def test_unowned_stroke_next_to_painted_vertical_rule_stays_hidden(tmp_path):
    source, output = tmp_path / "rule.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(30, 240, "Body text"), (200, 180, "|")])
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Contents = pdf.make_stream(
            pdf.pages[0].Contents.read_bytes() + b"\n199 150 m 199 210 l S"
        )
        pdf.save(source)
    await _tag(source, output, [_element(boxes[0])])
    assert _heard(output) == "Body text"


@pytest.mark.asyncio
async def test_owned_cell_letter_and_unclaimed_body_letter_are_preserved(tmp_path):
    source, output = tmp_path / "letters.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(30, 240, "I"), (30, 120, "I")])
    table = _element(
        {"l": 20, "b": 220, "r": 200, "t": 260}, "table", num_rows=1, num_cols=1,
        cells=[{"row": 0, "col": 0, "bbox": boxes[0]}],
    )
    await _tag(source, output, [table])
    assert _heard(output) == "I I"


@pytest.mark.asyncio
@pytest.mark.parametrize("y", [18, 277])
async def test_unclaimed_outer_band_url_stays_hidden(tmp_path, y):
    source, output = tmp_path / "footer.pdf", tmp_path / "tagged.pdf"
    boxes = _page(source, [(30, 240, "Body text"), (30, y, "health.example.test/library")])
    await _tag(source, output, [_element(boxes[0])])
    assert _heard(output) == "Body text"
