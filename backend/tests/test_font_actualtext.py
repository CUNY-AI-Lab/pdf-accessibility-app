from pathlib import Path

import pikepdf
import pytest

from app.services.font_actualtext import (
    _resolve_target_stream_from_context,
    _resolve_text_showing_instruction,
)
from app.services.pdf_context import parse_verapdf_context_path
from tests.fixtures import TEST_SAMPLE_PDF


def _resolve_object(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _text_stream(font_name: pikepdf.Name) -> bytes:
    return pikepdf.unparse_content_stream(
        [
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
            pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
            pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String("Nested text")], pikepdf.Operator("Tj")),
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
        ]
    )


def _append_form_xobject_page(pdf_path: Path, output_path: Path) -> str:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        font_dict = fonts.get(font_name)

        form = pdf.make_stream(_text_stream(font_name))
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


def _append_annotation_appearance(pdf_path: Path, output_path: Path) -> str:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        font_dict = fonts.get(font_name)

        appearance = pdf.make_stream(_text_stream(font_name))
        appearance["/Type"] = pikepdf.Name("/XObject")
        appearance["/Subtype"] = pikepdf.Name("/Form")
        appearance["/BBox"] = pikepdf.Array([0, 0, 200, 50])
        appearance["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({font_name: font_dict})})

        annot = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Annot"),
                    "/Subtype": pikepdf.Name("/FreeText"),
                    "/Rect": pikepdf.Array([20, 20, 180, 80]),
                    "/Contents": pikepdf.String("Annotation"),
                    "/AP": pikepdf.Dictionary({"/N": appearance}),
                }
            )
        )
        annots = _resolve_object(page.obj.get("/Annots"))
        if not isinstance(annots, pikepdf.Array):
            annots = pikepdf.Array()
            page.obj["/Annots"] = annots
        annots.append(annot)
        pdf.save(str(output_path))

    return "root/document[0]/pages[0](1 0 obj PDPage)/annotations[0](12 0 obj PDAnnotation)/appearanceStream[0](13 0 obj PDFormXObject)/operators[3]/usedGlyphs[0](Annot Annot 1 0 0 0 true)"


def _append_unnamed_xobject_context(pdf_path: Path, output_path: Path) -> tuple[str, str]:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        font_dict = fonts.get(font_name)

        first_form = pdf.make_stream(_text_stream(font_name))
        first_form["/Type"] = pikepdf.Name("/XObject")
        first_form["/Subtype"] = pikepdf.Name("/Form")
        first_form["/BBox"] = pikepdf.Array([0, 0, 200, 50])
        first_form["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({font_name: font_dict})})

        second_form = pdf.make_stream(_text_stream(font_name))
        second_form["/Type"] = pikepdf.Name("/XObject")
        second_form["/Subtype"] = pikepdf.Name("/Form")
        second_form["/BBox"] = pikepdf.Array([0, 0, 200, 50])
        second_form["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({font_name: font_dict})})

        xobjects = pikepdf.Dictionary()
        xobjects[pikepdf.Name("/Fx0")] = first_form
        xobjects[pikepdf.Name("/Fx1")] = second_form
        page_resources["/XObject"] = xobjects

        page["/Contents"] = pdf.make_stream(
            pikepdf.unparse_content_stream(
                [
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                    pikepdf.ContentStreamInstruction([pikepdf.Name("/Fx1")], pikepdf.Operator("Do")),
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
                ]
            )
        )
        pdf.save(str(output_path))

    context_path = (
        "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0]/operators[1]/"
        "xObject[0]/contentStream[0]/operators[3]/usedGlyphs[0](Fx1 Fx1 1 0 0 0 true)"
    )
    return context_path, "/Fx1"


def _operator_at_target(target: dict) -> str:
    instructions = list(
        pikepdf.parse_content_stream(target["target_stream"] or target["target_owner"])
    )
    return str(instructions[target["operator_index"]].operator)


def test_resolve_text_showing_instruction_rejects_non_text_showing_operator():
    with pikepdf.open(str(TEST_SAMPLE_PDF)) as pdf:
        instructions = list(pikepdf.parse_content_stream(pdf.pages[0]))

    with pytest.raises(ValueError) as exc:
        _resolve_text_showing_instruction(instructions, 0)

    assert "not a text-showing operator" in str(exc.value)


def test_resolve_target_in_nested_xobject_context(tmp_path):
    nested_pdf = tmp_path / "nested_xobject.pdf"
    context_path = _append_form_xobject_page(TEST_SAMPLE_PDF, nested_pdf)

    with pikepdf.open(str(nested_pdf)) as pdf:
        target = _resolve_target_stream_from_context(pdf, context_path)
        page_resources = _resolve_object(pdf.pages[0].obj.get("/Resources"))
        xobject = _resolve_object(_resolve_object(page_resources.get("/XObject")).get("/Fx0"))

        assert target["target_stream"].objgen == xobject.objgen
        assert _operator_at_target(target) == "Tj"


def test_resolve_target_in_annotation_appearance_context(tmp_path):
    annotated_pdf = tmp_path / "annot_appearance.pdf"
    context_path = _append_annotation_appearance(TEST_SAMPLE_PDF, annotated_pdf)

    with pikepdf.open(str(annotated_pdf)) as pdf:
        target = _resolve_target_stream_from_context(pdf, context_path)
        annot = _resolve_object(_resolve_object(pdf.pages[0].obj.get("/Annots"))[0])
        appearance = _resolve_object(_resolve_object(annot.get("/AP")).get("/N"))

        assert target["target_stream"].objgen == appearance.objgen
        assert _operator_at_target(target) == "Tj"


def test_resolve_target_in_unnamed_xobject_context_uses_do_operator_target(tmp_path):
    unnamed_pdf = tmp_path / "unnamed_xobject.pdf"
    context_path, used_xobject_name = _append_unnamed_xobject_context(
        TEST_SAMPLE_PDF,
        unnamed_pdf,
    )

    parsed = parse_verapdf_context_path(context_path)
    assert parsed["xobject_chain"][0]["from_operator_index"] == 1
    assert parsed["xobject_chain"][0]["content_stream_index"] == 0

    with pikepdf.open(str(unnamed_pdf)) as pdf:
        target = _resolve_target_stream_from_context(pdf, context_path)
        page_resources = _resolve_object(pdf.pages[0].obj.get("/Resources"))
        xobjects = _resolve_object(page_resources.get("/XObject"))
        used_xobject = _resolve_object(xobjects.get(used_xobject_name))
        other_xobject = _resolve_object(xobjects.get("/Fx0"))

        assert target["target_stream"].objgen == used_xobject.objgen
        assert target["target_stream"].objgen != other_xobject.objgen
        assert _operator_at_target(target) == "Tj"
