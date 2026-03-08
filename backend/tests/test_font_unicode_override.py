from pathlib import Path

import pikepdf
import pytest

from tests.fixtures import TEST_SAMPLE_PDF

from app.pipeline.orchestrator import _parse_tounicode_map
from app.services.font_unicode_override import apply_unicode_override_to_context


def _resolve_object(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _text_stream(font_name: pikepdf.Name, text: str) -> bytes:
    return pikepdf.unparse_content_stream(
        [
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
            pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
            pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String(text)], pikepdf.Operator("Tj")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
        ]
    )


def _replace_page_with_single_text(pdf_path: Path, output_path: Path, text: str) -> str:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        page["/Contents"] = pdf.make_stream(_text_stream(font_name, text))
        pdf.save(str(output_path))

    return "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0]/operators[3]/usedGlyphs[0](Font Font 1 0 0 0 true)"


def _append_form_xobject_page(pdf_path: Path, output_path: Path, text: str) -> str:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        font_dict = fonts.get(font_name)

        form = pdf.make_stream(_text_stream(font_name, text))
        form["/Type"] = pikepdf.Name("/XObject")
        form["/Subtype"] = pikepdf.Name("/Form")
        form["/BBox"] = pikepdf.Array([0, 0, 200, 50])
        form["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({font_name: font_dict})})

        xobjects = _resolve_object(page_resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            xobjects = pikepdf.Dictionary()
            page_resources["/XObject"] = xobjects
        xobject_name = pikepdf.Name("/Fx0")
        xobjects[xobject_name] = form

        page_instructions = list(pikepdf.parse_content_stream(page))
        page_instructions.extend(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                pikepdf.ContentStreamInstruction([1, 0, 0, 1, 20, 20], pikepdf.Operator("cm")),
                pikepdf.ContentStreamInstruction([xobject_name], pikepdf.Operator("Do")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
            ]
        )
        page["/Contents"] = pdf.make_stream(pikepdf.unparse_content_stream(page_instructions))
        pdf.save(str(output_path))

    return "root/document[0]/pages[0](1 0 obj PDPage)/resources/xObject[0](Fx0 7 0 obj PDFormXObject)/contentStream[0]/operators[3]/usedGlyphs[0](Fx0 Fx0 1 0 0 0 true)"


def _font_tounicode_map(pdf_path: Path, base_font_name: str) -> dict[int, str]:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(resources.get("/Font"))
        for value in fonts.values():
            font_dict = _resolve_object(value)
            if str(font_dict.get("/BaseFont") or "").lstrip("/") != base_font_name:
                continue
            stream = _resolve_object(font_dict.get("/ToUnicode"))
            return _parse_tounicode_map(stream)
    raise AssertionError(f"Font {base_font_name} did not expose a ToUnicode map")


def test_apply_unicode_override_to_context_creates_tounicode_mapping(tmp_path):
    source_pdf = tmp_path / "single_char.pdf"
    context_path = _replace_page_with_single_text(
        TEST_SAMPLE_PDF,
        source_pdf,
        "A",
    )
    output_pdf = tmp_path / "single_char_patched.pdf"

    applied = apply_unicode_override_to_context(
        input_pdf=source_pdf,
        output_pdf=output_pdf,
        context_path=context_path,
        unicode_text="►",
    )

    mapping = _font_tounicode_map(output_pdf, str(applied["font_base_name"]))
    assert mapping[0x41] == "►"
    assert applied["font_code"] == 0x41


def test_apply_unicode_override_to_context_rejects_multi_byte_target(tmp_path):
    source_pdf = tmp_path / "multi_char.pdf"
    context_path = _replace_page_with_single_text(
        TEST_SAMPLE_PDF,
        source_pdf,
        "AB",
    )
    output_pdf = tmp_path / "multi_char_patched.pdf"

    with pytest.raises(ValueError) as exc:
        apply_unicode_override_to_context(
            input_pdf=source_pdf,
            output_pdf=output_pdf,
            context_path=context_path,
            unicode_text="►",
        )

    assert "single-byte target" in str(exc.value)


def test_apply_unicode_override_to_nested_xobject_context(tmp_path):
    nested_pdf = tmp_path / "nested_xobject.pdf"
    context_path = _append_form_xobject_page(
        TEST_SAMPLE_PDF,
        nested_pdf,
        "A",
    )
    output_pdf = tmp_path / "nested_xobject_patched.pdf"

    applied = apply_unicode_override_to_context(
        input_pdf=nested_pdf,
        output_pdf=output_pdf,
        context_path=context_path,
        unicode_text="Ω",
    )

    mapping = _font_tounicode_map(output_pdf, str(applied["font_base_name"]))
    assert mapping[0x41] == "Ω"
