from io import BytesIO
from pathlib import Path

import pikepdf
from PIL import Image

from app.services.font_actualtext import apply_actualtext_to_context
from app.services.pdf_operator_context import (
    extract_operator_text_context,
    extract_operator_visual_context,
)
from app.services.pdf_preview import render_page_jpeg_bytes, render_target_preview_png_bytes
from tests.fixtures import TEST_SAMPLE_PDF


def _resolve_object(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _two_text_stream(font_name: pikepdf.Name) -> bytes:
    return pikepdf.unparse_content_stream(
        [
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
            pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
            pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String("Before text")], pikepdf.Operator("Tj")),
            pikepdf.ContentStreamInstruction([0, -14], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String("Target text")], pikepdf.Operator("Tj")),
            pikepdf.ContentStreamInstruction([0, -14], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String("After text")], pikepdf.Operator("Tj")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
        ]
    )


def _single_text_stream(font_name: pikepdf.Name) -> bytes:
    return pikepdf.unparse_content_stream(
        [
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
            pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
            pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String("Only target")], pikepdf.Operator("Tj")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
        ]
    )


def _replace_page_with_three_text_operators(pdf_path: Path, output_path: Path) -> str:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        page["/Contents"] = pdf.make_stream(_two_text_stream(font_name))
        pdf.save(str(output_path))

    return (
        "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0]/"
        "operators[5]/usedGlyphs[0](Font Font 1 0 0 0 true)"
    )


def _replace_page_with_single_text_operator(pdf_path: Path, output_path: Path) -> str:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        page["/Contents"] = pdf.make_stream(_single_text_stream(font_name))
        pdf.save(str(output_path))

    return (
        "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0]/"
        "operators[4]/usedGlyphs[0](Font Font 1 0 0 0 true)"
    )


def test_extract_operator_text_context_returns_surrounding_text(tmp_path):
    pdf_path = tmp_path / "three_text_ops.pdf"
    context_path = _replace_page_with_three_text_operators(
        TEST_SAMPLE_PDF,
        pdf_path,
    )

    context = extract_operator_text_context(
        pdf_path=pdf_path,
        context_path=context_path,
    )

    assert context["decoded_text"] == "Target text"
    assert context["before_text"] == "Before text"
    assert context["after_text"] == "After text"
    assert context["nearby_text"] == "Before text Target text After text"
    assert len(context["nearby_operators"]) == 3


def test_extract_operator_visual_context_returns_bbox(tmp_path):
    pdf_path = tmp_path / "three_text_ops.pdf"
    context_path = _replace_page_with_three_text_operators(
        TEST_SAMPLE_PDF,
        pdf_path,
    )

    visual = extract_operator_visual_context(
        pdf_path=pdf_path,
        context_path=context_path,
    )

    assert visual["supported"] is True
    assert visual["page_number"] == 1
    assert isinstance(visual["bbox"], dict)
    assert visual["bbox"]["r"] > visual["bbox"]["l"]
    assert visual["bbox"]["t"] > visual["bbox"]["b"]


def test_render_target_preview_png_bytes_returns_png(tmp_path):
    pdf_path = tmp_path / "three_text_ops.pdf"
    context_path = _replace_page_with_three_text_operators(
        TEST_SAMPLE_PDF,
        pdf_path,
    )

    image_bytes = render_target_preview_png_bytes(pdf_path, context_path)

    with Image.open(BytesIO(image_bytes)) as image:
        assert image.format == "PNG"
        assert image.width > 0
        assert image.height > 0


def test_render_page_jpeg_bytes_returns_jpeg():
    image_bytes = render_page_jpeg_bytes(TEST_SAMPLE_PDF, 1)

    with Image.open(BytesIO(image_bytes)) as image:
        assert image.format == "JPEG"
        assert image.width > 0
        assert image.height > 0


def test_render_target_preview_png_bytes_highlights_target_region(tmp_path):
    pdf_path = tmp_path / "three_text_ops.pdf"
    context_path = _replace_page_with_three_text_operators(
        TEST_SAMPLE_PDF,
        pdf_path,
    )

    image_bytes = render_target_preview_png_bytes(pdf_path, context_path)

    with Image.open(BytesIO(image_bytes)) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size

    assert any(
        (lambda pixel: pixel[0] > 180 and pixel[1] < 100 and pixel[2] < 100)(rgb.getpixel((x, y)))
        for y in range(height)
        for x in range(width)
    )


def test_extract_operator_context_handles_actualtext_wrapped_target(tmp_path):
    source_pdf = tmp_path / "three_text_ops.pdf"
    context_path = _replace_page_with_three_text_operators(
        TEST_SAMPLE_PDF,
        source_pdf,
    )
    wrapped_pdf = tmp_path / "three_text_ops_wrapped.pdf"

    apply_actualtext_to_context(
        input_pdf=source_pdf,
        output_pdf=wrapped_pdf,
        context_path=context_path,
        actual_text="Wrapped replacement",
    )

    context = extract_operator_text_context(
        pdf_path=wrapped_pdf,
        context_path=context_path,
    )
    visual = extract_operator_visual_context(
        pdf_path=wrapped_pdf,
        context_path=context_path,
    )

    assert context["decoded_text"] == "Target text"
    assert visual["supported"] is True
    assert isinstance(visual["bbox"], dict)


def test_render_target_preview_png_bytes_handles_actualtext_wrapped_target(tmp_path):
    source_pdf = tmp_path / "three_text_ops.pdf"
    context_path = _replace_page_with_three_text_operators(
        TEST_SAMPLE_PDF,
        source_pdf,
    )
    wrapped_pdf = tmp_path / "three_text_ops_wrapped.pdf"

    apply_actualtext_to_context(
        input_pdf=source_pdf,
        output_pdf=wrapped_pdf,
        context_path=context_path,
        actual_text="Wrapped replacement",
    )

    image_bytes = render_target_preview_png_bytes(wrapped_pdf, context_path)

    with Image.open(BytesIO(image_bytes)) as image:
        assert image.format == "PNG"
        assert image.width > 0
        assert image.height > 0


def test_extract_operator_context_handles_single_text_object_et_target(tmp_path):
    pdf_path = tmp_path / "single_text_op.pdf"
    context_path = _replace_page_with_single_text_operator(
        TEST_SAMPLE_PDF,
        pdf_path,
    )

    context = extract_operator_text_context(
        pdf_path=pdf_path,
        context_path=context_path,
    )
    visual = extract_operator_visual_context(
        pdf_path=pdf_path,
        context_path=context_path,
    )

    assert context["decoded_text"] == "Only target"
    assert visual["supported"] is True
    assert isinstance(visual["bbox"], dict)
