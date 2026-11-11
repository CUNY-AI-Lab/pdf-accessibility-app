"""Reading-order votes scored through the tagged PDF a screen reader reads."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica, rendered


def _columns(rows=3):
    return [
        {"type": "paragraph", "page": 0, "text": f"{column} paragraph {row}",
         "bbox": {"l": x, "b": 680 - 70 * row, "r": x + 210, "t": 700 - 70 * row}}
        for column, x in (("Left", 50), ("Right", 340)) for row in range(rows)
    ]


def _write_page(path: Path, drawn: list[dict], rotation: int = 0, form: bool = False):
    """Boxes are in Docling's displayed frame. Draw upright after /Rotate,
    including sideways text matrices, without changing the supplied order."""
    pdf = pikepdf.new()
    width, height = (792, 612) if rotation in (90, 270) else (612, 792)
    page = pdf.add_blank_page(page_size=(width, height))
    page.obj["/Rotate"] = rotation
    resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Resources = resources
    commands = []
    for element in drawn:
        box = element["bbox"]
        x, y = box["l"], box["b"] + 4
        matrix = {
            0: f"1 0 0 1 {x} {y}",
            90: f"0 1 -1 0 {width - y} {x}",
            180: f"-1 0 0 -1 {width - x} {height - y}",
            270: f"0 -1 1 0 {y} {height - x}",
        }[rotation]
        commands.append(f"BT /F1 10 Tf {matrix} Tm ({element['text']}) Tj ET")
    stream = pdf.make_stream("\n".join(commands).encode())
    if form:
        stream["/Type"] = pikepdf.Name.XObject
        stream["/Subtype"] = pikepdf.Name.Form
        stream["/BBox"] = pikepdf.Array([0, 0, width, height])
        stream["/Resources"] = resources
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(OCR=stream))
        page.Contents = pdf.make_stream(b"q /OCR Do Q")
    else:
        page.Contents = stream
    pdf.save(path)


async def _tag_and_hear(tmp_path, drawn, docling, rotation=0, form=False):
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    _write_page(source, drawn, rotation, form)
    result = await tag_pdf(
        input_path=source, output_path=tagged,
        structure_json={"title": "Reading order", "elements": docling},
        alt_texts=[], language="en", original_filename=source.name,
    )
    assert rendered(tagged) == rendered(source)
    heard = "\n".join(line.strip() for line in screen_reader_text(tagged, markdown=True).splitlines())
    return heard, result.reading_order_diagnostics[0]


def _heard_text(elements):
    return "\n\n".join(
        ("#" * element.get("expected_level", 1) + " " if element["type"] == "heading" else "")
        + element["text"] for element in elements if element["type"] != "artifact"
    )


@pytest.mark.parametrize("rows", [2, 3])
async def test_drawing_order_wins_when_xy_cut_confirms_columns(tmp_path, rows):
    drawn = _columns(rows)
    docling = [drawn[i] for row in range(rows) for i in (row, rows + row)]
    heard, diagnostic = await _tag_and_hear(tmp_path, drawn, docling)
    assert heard.strip() == _heard_text(drawn)
    assert diagnostic["order_used"] == "drawing"
    assert diagnostic["agreement"]["drawing_xy_cut"] == 1.0
    assert diagnostic["agreement"]["docling_drawing"] < 0.8


async def test_docling_kept_when_footnote_is_drawn_first(tmp_path):
    columns = _columns()
    footnote = {"type": "paragraph", "page": 0, "text": "Footnote drawn first",
                "bbox": {"l": 50, "b": 70, "r": 570, "t": 90}}
    heard, diagnostic = await _tag_and_hear(tmp_path, [footnote, *columns], [*columns, footnote])
    assert heard.strip() == _heard_text([*columns, footnote])
    assert diagnostic["order_used"] == "docling"
    assert diagnostic["agreement"]["docling_xy_cut"] == 1.0
    assert diagnostic["agreement"]["drawing_xy_cut"] < 0.8


async def test_full_width_title_is_reinserted_before_columns(tmp_path):
    columns = _columns()
    title = {"type": "heading", "level": 1, "page": 0, "text": "Full width title",
             "bbox": {"l": 40, "b": 735, "r": 580, "t": 760}}
    drawn = [title, *columns]
    docling = [title, *(columns[i] for row in range(3) for i in (row, 3 + row))]
    heard, diagnostic = await _tag_and_hear(tmp_path, drawn, docling)
    assert heard.strip() == _heard_text(drawn)
    assert diagnostic["order_used"] == "drawing"
    assert diagnostic["agreement"]["drawing_xy_cut"] == 1.0


@pytest.mark.parametrize("rotation", [90, 180, 270])
async def test_rotated_page_votes_in_displayed_coordinates(tmp_path, rotation):
    columns = _columns()
    docling = [columns[i] for row in range(3) for i in (row, 3 + row)]
    heard, diagnostic = await _tag_and_hear(tmp_path, columns, docling, rotation)
    assert heard.strip() == _heard_text(columns)
    assert diagnostic["order_used"] == "drawing"
    assert diagnostic["agreement"]["drawing_xy_cut"] == 1.0


async def test_heading_levels_follow_chosen_column_order(tmp_path):
    columns = _columns(2)
    for element, level in zip(columns, [1, 4, 2, 4]):
        element.update(type="heading", level=level)
    for element, level in zip(columns, [1, 2, 2, 3]):
        element["expected_level"] = level
    heard, diagnostic = await _tag_and_hear(
        tmp_path, columns, [columns[i] for i in (0, 2, 1, 3)],
    )
    assert heard.strip() == _heard_text(columns)
    assert diagnostic["order_used"] == "drawing"


async def test_artifacts_do_not_vote_or_get_read(tmp_path):
    columns = _columns(2)
    artifact = {"type": "artifact", "page": 0, "text": "Running header",
                "bbox": {"l": 40, "b": 740, "r": 580, "t": 760}}
    docling = [columns[0], columns[2], artifact, columns[1], columns[3]]
    heard, diagnostic = await _tag_and_hear(tmp_path, [artifact, *columns], docling)
    assert heard.strip() == _heard_text(columns)
    assert diagnostic["element_count"] == 4
    assert diagnostic["drawn_element_count"] == 4
    assert diagnostic["order_used"] == "drawing"


async def test_ocr_form_drawing_order_uses_the_same_vote(tmp_path):
    columns = _columns(2)
    heard, diagnostic = await _tag_and_hear(
        tmp_path, columns, [columns[i] for i in (0, 2, 1, 3)], form=True,
    )
    assert heard.strip() == _heard_text(columns)
    assert diagnostic["order_used"] == "drawing"


async def test_nested_ordinary_forms_recover_words_before_the_page_vote(tmp_path):
    source, tagged = tmp_path / "nested.pdf", tmp_path / "tagged.pdf"
    columns = _columns(2)
    for elem in columns:
        elem["bbox"] = {**elem["bbox"], "b": elem["bbox"]["b"] - 100,
                        "t": elem["bbox"]["t"] - 100}
    missing = {"type": "paragraph", "page": 0,
               "text": "Recovered passage spanning both columns of this ordinary nested form",
               "bbox": {"l": 50, "b": 650, "r": 580, "t": 670}}
    _write_page(source, [missing, *columns], form=True)
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        inner = page.Resources.XObject.OCR
        inner.Matrix = [1, 0, 0, 1, 7, 13]
        outer = pdf.make_stream(b"/Inner Do")
        outer.Type, outer.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
        outer.BBox, outer.Matrix = [0, 0, 612, 792], [1, 0, 0, 1, 11, 17]
        outer.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Inner=inner))
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Ordinary=outer))
        page.Contents = pdf.make_stream(b"/Ordinary Do")
        pdf.save(source)
    docling = []
    for index in (0, 2, 1, 3):
        elem = columns[index]
        docling.append({**elem, "bbox": {
            side: value + (18 if side in {"l", "r"} else 30)
            for side, value in elem["bbox"].items()
        }})
    result = await tag_pdf(source, tagged, {"elements": docling, "title": "Nested forms"}, [])
    assert rendered(tagged) == rendered(source)
    assert " ".join(screen_reader_text(tagged).split()) == " ".join(
        elem["text"] for elem in [missing, *columns]
    )
    diagnostic = result.reading_order_diagnostics[0]
    assert diagnostic["element_count"] == diagnostic["drawn_element_count"] == 5
    assert diagnostic["order_used"] == "drawing"
    assert result.unowned_words_by_page[0]["before"] > 0
    assert result.unowned_words_by_page[0]["after"] == 0
