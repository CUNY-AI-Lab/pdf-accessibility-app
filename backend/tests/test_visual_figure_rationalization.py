import base64
from pathlib import Path

import pikepdf

from app.pipeline.fidelity import assess_fidelity
from app.services import visual_figure_rationalization
from app.services.visual_figure_rationalization import (
    collect_missing_visual_figure_targets,
    synthesize_missing_visual_figures,
)

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5uJpQAAAAASUVORK5CYII="
)


def _build_dominant_image_pdf(path: Path, *, with_widget: bool = False) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    image = pdf.make_stream(bytes([255]))
    image["/Type"] = pikepdf.Name("/XObject")
    image["/Subtype"] = pikepdf.Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
    image["/BitsPerComponent"] = 8

    page["/Resources"] = pikepdf.Dictionary({
        "/XObject": pikepdf.Dictionary({
            "/Im0": image,
        }),
    })
    page["/Contents"] = pdf.make_stream(b"q 200 0 0 200 0 0 cm\n/Im0 Do\nQ\n")

    if with_widget:
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Annot"),
                    "/Subtype": pikepdf.Name("/Widget"),
                    "/Rect": pikepdf.Array([10, 10, 80, 26]),
                    "/FT": pikepdf.Name("/Tx"),
                    "/T": pikepdf.String("field1"),
                }
            )
        )
        page["/Annots"] = pikepdf.Array([widget])
        pdf.Root["/AcroForm"] = pikepdf.Dictionary(
            {
                "/Fields": pikepdf.Array([widget]),
                "/DA": pikepdf.String("/Helv 10 Tf 0 g"),
                "/DR": pikepdf.Dictionary(),
            }
        )

    pdf.save(path)


def test_collect_missing_visual_figure_targets_flags_sparse_image_page(tmp_path):
    pdf_path = tmp_path / "chart.pdf"
    _build_dominant_image_pdf(pdf_path)

    structure_json = {
        "page_count": 1,
        "elements": [
            {"type": "paragraph", "page": 0, "bbox": {"l": 8, "b": 140, "r": 28, "t": 154}, "text": "600"},
            {"type": "paragraph", "page": 0, "bbox": {"l": 8, "b": 108, "r": 28, "t": 122}, "text": "500"},
            {
                "type": "paragraph",
                "page": 0,
                "bbox": {"l": 30, "b": 16, "r": 128, "t": 32},
                "text": "Nuclear Warfare",
            },
        ],
    }

    targets = collect_missing_visual_figure_targets(
        working_pdf=pdf_path,
        structure_json=structure_json,
    )

    assert len(targets) == 1
    assert targets[0]["page"] == 1
    assert targets[0]["area_ratio"] >= 0.9
    assert targets[0]["reason"] in {
        "dominant_image_without_text",
        "dominant_image_with_sparse_text",
        "dominant_image_with_fragmentary_text",
    }


def test_collect_missing_visual_figure_targets_skips_narrative_pages(tmp_path):
    pdf_path = tmp_path / "narrative.pdf"
    _build_dominant_image_pdf(pdf_path)

    structure_json = {
        "page_count": 1,
        "elements": [
            {
                "type": "paragraph",
                "page": 0,
                "bbox": {"l": 20, "b": 110, "r": 180, "t": 180},
                "text": (
                    "This page already contains a long extracted paragraph describing the visible content "
                    "in complete sentences for assistive technology."
                ),
            },
            {
                "type": "paragraph",
                "page": 0,
                "bbox": {"l": 20, "b": 30, "r": 180, "t": 100},
                "text": (
                    "A second long paragraph continues the narrative so the image should not be promoted "
                    "to a synthetic standalone figure."
                ),
            },
        ],
    }

    targets = collect_missing_visual_figure_targets(
        working_pdf=pdf_path,
        structure_json=structure_json,
    )

    assert targets == []


def test_collect_missing_visual_figure_targets_skips_pages_with_fields(tmp_path):
    pdf_path = tmp_path / "form_like.pdf"
    _build_dominant_image_pdf(pdf_path, with_widget=True)

    structure_json = {
        "page_count": 1,
        "elements": [
            {"type": "paragraph", "page": 0, "bbox": {"l": 8, "b": 140, "r": 28, "t": 154}, "text": "Name"},
        ],
    }

    targets = collect_missing_visual_figure_targets(
        working_pdf=pdf_path,
        structure_json=structure_json,
    )

    assert targets == []


def test_collect_missing_visual_figure_targets_respects_ignored_pages(tmp_path):
    pdf_path = tmp_path / "artifact_page.pdf"
    _build_dominant_image_pdf(pdf_path)

    structure_json = {
        "page_count": 1,
        "visual_meaning_ignored_pages": [1],
        "elements": [
            {"type": "paragraph", "page": 0, "bbox": {"l": 8, "b": 140, "r": 28, "t": 154}, "text": "600"},
        ],
    }

    targets = collect_missing_visual_figure_targets(
        working_pdf=pdf_path,
        structure_json=structure_json,
    )

    assert targets == []


def test_synthesize_missing_visual_figures_appends_structure_and_figure(monkeypatch, tmp_path):
    pdf_path = tmp_path / "chart.pdf"
    _build_dominant_image_pdf(pdf_path)

    monkeypatch.setattr(
        visual_figure_rationalization,
        "render_bbox_preview_png_bytes",
        lambda *_args, **_kwargs: _ONE_PIXEL_PNG,
    )

    structure_json = {
        "page_count": 1,
        "elements": [
            {"type": "paragraph", "page": 0, "bbox": {"l": 8, "b": 140, "r": 28, "t": 154}, "text": "600"},
        ],
    }

    updated_structure, updated_figures, audit = synthesize_missing_visual_figures(
        working_pdf=pdf_path,
        structure_json=structure_json,
        figures=[],
        figures_dir=tmp_path / "figures",
    )

    assert audit["applied"] is True
    assert audit["applied_count"] == 1
    assert len(updated_figures) == 1
    assert updated_figures[0].index == 0
    assert updated_figures[0].path.exists()
    figure_elements = [
        element for element in updated_structure["elements"] if element.get("type") == "figure"
    ]
    assert len(figure_elements) == 1
    assert figure_elements[0]["synthetic_figure"] is True
    assert updated_structure["figures_count"] == 1


def test_assess_fidelity_flags_visual_meaning_gap(tmp_path):
    pdf_path = tmp_path / "chart.pdf"
    _build_dominant_image_pdf(pdf_path)

    structure_json = {
        "page_count": 1,
        "elements": [
            {"type": "paragraph", "page": 0, "bbox": {"l": 8, "b": 140, "r": 28, "t": 154}, "text": "600"},
            {"type": "paragraph", "page": 0, "bbox": {"l": 8, "b": 108, "r": 28, "t": 122}, "text": "500"},
        ],
    }

    fidelity_report, review_tasks = assess_fidelity(
        input_pdf=pdf_path,
        output_pdf=pdf_path,
        comparison_source_pdf=None,
        structure_json=structure_json,
        alt_entries=[],
        validation_report={"violations": [], "summary": {"errors": 0}},
        raw_validation_report=None,
        tagging_metrics={},
        classification="digital",
    )

    assert any(
        isinstance(check, dict)
        and str(check.get("check") or "") == "visual_meaning_gap"
        and str(check.get("status") or "") == "fail"
        for check in fidelity_report.get("checks", [])
    )
    assert any(
        isinstance(task, dict)
        and bool(task.get("blocking"))
        and isinstance(task.get("metadata"), dict)
        and bool(task["metadata"].get("visual_meaning_gap"))
        for task in review_tasks
    )
