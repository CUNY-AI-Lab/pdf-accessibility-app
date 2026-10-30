"""Figures the model reclassifies as a table, part of a form, or an artifact.

Removing a figure turns its pixels into an artifact, so a reclassified figure
is removed only when a tagged table or form field carries its region's content.
Otherwise it stays a figure with no description, which holds the document.
"""

from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from app.pipeline import alt_text
from app.pipeline.orchestrator import _alt_text_entry_state, _apply_figure_reclassification
from app.pipeline.structure import FigureInfo
from app.pipeline.tagger import tag_pdf
from tests.pdf_fixtures import helvetica

REGION = {"l": 50, "b": 150, "r": 250, "t": 250}
TABLE_BOX = {"l": 45, "b": 145, "r": 255, "t": 255}
ROWS = [("Year", "Enrollment"), ("2024", "1200"), ("2025", "1350")]


def _table_cells() -> list[dict]:
    return [
        {"row": row, "col": col, "text": text, "is_header": row == 0}
        for row, values in enumerate(ROWS)
        for col, text in enumerate(values)
    ]


def _figure(**extra) -> dict:
    return {"type": "figure", "figure_index": 0, "page": 0, "bbox": dict(REGION), **extra}


def _table(**extra) -> dict:
    return {
        "type": "table",
        "page": 0,
        "bbox": dict(TABLE_BOX),
        "num_rows": len(ROWS),
        "num_cols": 2,
        "cells": _table_cells(),
        **extra,
    }


def _result(kind: str) -> SimpleNamespace:
    return SimpleNamespace(
        figure_index=0,
        status="reclassified",
        resolved_kind=kind,
        generated_text="",
        suggested_action="reclassify_region",
        reviewable=False,
        importance="high",
    )


def _text_table_pdf(path: Path) -> Path:
    """The table drawn as text inside the region."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    lines = ["BT", "/F1 10 Tf"]
    for index, (left, right) in enumerate(ROWS):
        y = 230 - index * 20
        lines += [f"1 0 0 1 60 {y} Tm ({left}) Tj", f"1 0 0 1 160 {y} Tm ({right}) Tj"]
    lines.append("ET")
    page.Contents = pdf.make_stream("\n".join(lines).encode())
    pdf.save(path)
    return path


def _raster_pdf(path: Path, *, widget: bool = False) -> Path:
    """Only an image inside the region (a scanned or pasted-in table or form)."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    image = pdf.make_stream(bytes([120]))
    image.Type = pikepdf.Name.XObject
    image.Subtype = pikepdf.Name.Image
    image.Width = 1
    image.Height = 1
    image.ColorSpace = pikepdf.Name.DeviceGray
    image.BitsPerComponent = 8
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image))
    page.Contents = pdf.make_stream(b"q 200 0 0 100 50 150 cm /Im0 Do Q\n")
    if widget:
        field = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Widget,
                FT=pikepdf.Name.Tx,
                T=pikepdf.String("enrollment"),
                TU=pikepdf.String("Enrollment"),
                Rect=pikepdf.Array([150, 180, 240, 200]),
                P=page.obj,
            )
        )
        page.Annots = pdf.make_indirect(pikepdf.Array([field]))
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
    pdf.save(path)
    return path


def _figure_types(structure: dict) -> list[str]:
    return [element["type"] for element in structure["elements"]]


def test_figure_covered_by_a_text_table_is_removed(tmp_path):
    result = _result("table")
    structure = {"elements": [_figure(), _table()]}

    updated, audit = _apply_figure_reclassification(
        structure, [result], working_pdf=_text_table_pdf(tmp_path / "in.pdf")
    )

    assert _figure_types(updated) == ["table"]
    assert audit["covered_by"] == {"0": "table"}
    assert result.status == "reclassified"


def test_figure_of_a_raster_table_stays_a_figure_that_needs_a_description(tmp_path):
    # Docling's table cells came from OCR of the image; the page has no text
    # for the tagger to put in them, so removing the figure would lose the table.
    pdf = _raster_pdf(tmp_path / "in.pdf")
    result = _result("table")
    structure = {"elements": [_figure(), _table()]}

    updated, audit = _apply_figure_reclassification(structure, [result], working_pdf=pdf)

    assert _figure_types(updated) == ["figure", "table"]
    assert audit["kept_for_review_indexes"] == [0]
    assert (result.status, result.generated_text, result.reviewable) == ("pending_review", "", True)


@pytest.mark.asyncio
async def test_kept_figure_ships_as_a_figure_not_an_artifact(tmp_path):
    pdf = _raster_pdf(tmp_path / "in.pdf")
    result = _result("table")
    updated, _ = _apply_figure_reclassification(
        {"elements": [_figure(), _table()]}, [result], working_pdf=pdf
    )
    output = tmp_path / "tagged.pdf"

    tagging = await tag_pdf(
        input_path=pdf,
        output_path=output,
        structure_json=updated,
        alt_texts=[],
        original_filename="in.pdf",
    )

    assert tagging.figures_tagged == 1
    with pikepdf.open(output) as tagged:
        assert b"/Figure" in tagged.pages[0].Contents.read_bytes()


@pytest.mark.parametrize(
    "elements",
    [
        [_figure()],
        [_figure(), _table(page=1)],
        [_figure(), _table(bbox={"l": 45, "b": 145, "r": 150, "t": 255})],
        [_figure(), _table(cells=[{"row": 0, "col": 0, "text": "Unrelated words entirely"}])],
    ],
    ids=["no-table", "table-on-another-page", "table-holds-half", "table-text-not-on-page"],
)
def test_figure_without_a_covering_table_is_kept(tmp_path, elements):
    result = _result("table")

    updated, audit = _apply_figure_reclassification(
        {"elements": elements}, [result], working_pdf=_text_table_pdf(tmp_path / "in.pdf")
    )

    assert "figure" in _figure_types(updated)
    assert audit["removed_count"] == 0
    assert result.status == "pending_review"


def test_figure_is_kept_when_coverage_cannot_be_checked():
    result = _result("table")

    updated, _ = _apply_figure_reclassification(
        {"elements": [_figure(), _table()]}, [result], working_pdf=None
    )

    assert _figure_types(updated) == ["figure", "table"]
    assert result.status == "pending_review"


@pytest.mark.parametrize(("widget", "removed"), [(True, True), (False, False)])
def test_form_region_is_removed_only_when_a_form_field_covers_it(tmp_path, widget, removed):
    result = _result("form_region")

    updated, audit = _apply_figure_reclassification(
        {"elements": [_figure()]},
        [result],
        working_pdf=_raster_pdf(tmp_path / "in.pdf", widget=widget),
    )

    assert ("figure" not in _figure_types(updated)) is removed
    assert audit["covered_by"] == ({"0": "form_field"} if removed else {})


def test_artifact_is_kept_and_marked_decorative_for_review():
    result = _result("artifact")
    structure = {"elements": [_figure(synthetic_figure=True, page=1)]}

    updated, audit = _apply_figure_reclassification(structure, [result])

    assert _figure_types(updated) == ["figure"]
    assert updated["visual_meaning_ignored_pages"] == [2]
    assert audit["decorative_indexes"] == [0]
    assert (result.status, result.reviewable) == ("rejected", True)


def test_kept_figure_review_card_says_why(tmp_path):
    figure = FigureInfo(index=0, path=tmp_path / "f.png", page=0)
    result = alt_text.AltTextResult(
        figure_index=0, generated_text="", status="pending_review", resolved_kind="table"
    )

    [spec] = alt_text.figure_applied_change_specs(figures=[figure], alt_texts=[result])

    assert spec["title"] == "Figure 1 needs a description"
    assert "table" in spec["detail"]


@pytest.mark.parametrize("kind", ["table", "form_region"])
def test_kept_figure_waits_for_a_person_even_with_a_caption(tmp_path, kind):
    result = _result(kind)
    _apply_figure_reclassification(
        {"elements": [_figure()]}, [result], working_pdf=_raster_pdf(tmp_path / "in.pdf")
    )

    state = _alt_text_entry_state(result, caption="Table 2. Enrollment by year", auto_approve=True)

    assert state == ("pending_review", "")
