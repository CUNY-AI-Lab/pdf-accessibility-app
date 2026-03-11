from pathlib import Path

import pikepdf
import pytest

from app.services.font_actualtext import (
    apply_actualtext_batch_to_contexts,
    apply_actualtext_to_context,
    apply_actualtext_to_page_operator,
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


def _two_text_stream(font_name: pikepdf.Name) -> bytes:
    return pikepdf.unparse_content_stream(
        [
            pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
            pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
            pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String("First")], pikepdf.Operator("Tj")),
            pikepdf.ContentStreamInstruction([0, -14], pikepdf.Operator("Td")),
            pikepdf.ContentStreamInstruction([pikepdf.String("Second")], pikepdf.Operator("Tj")),
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


def _replace_page_with_two_text_operators(pdf_path: Path, output_path: Path) -> tuple[str, str]:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        page["/Contents"] = pdf.make_stream(_two_text_stream(font_name))
        pdf.save(str(output_path))

    base = "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0]"
    return (
        f"{base}/operators[3]/usedGlyphs[0](Font Font 1 0 0 0 true)",
        f"{base}/operators[5]/usedGlyphs[0](Font Font 1 0 0 0 true)",
    )


def _replace_page_with_single_text_et_context(pdf_path: Path, output_path: Path) -> str:
    with pikepdf.open(str(pdf_path)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        page["/Contents"] = pdf.make_stream(_single_text_stream(font_name))
        pdf.save(str(output_path))

    return "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0]/operators[4]/usedGlyphs[0](Font Font 1 0 0 0 true)"


def _first_text_showing_operator(pdf_path: Path) -> tuple[int, str]:
    with pikepdf.open(str(pdf_path)) as pdf:
        instructions = list(pikepdf.parse_content_stream(pdf.pages[0]))
    for index, instruction in enumerate(instructions):
        if str(instruction.operator) in {"Tj", "TJ", "'", '"'}:
            return index, str(instruction.operator)
    raise AssertionError("No text-showing operator found in sample PDF")


def test_apply_actualtext_to_page_operator_wraps_target_instruction(tmp_path):
    source_pdf = TEST_SAMPLE_PDF
    output_pdf = tmp_path / "patched.pdf"
    operator_index, operator_name = _first_text_showing_operator(source_pdf)

    apply_actualtext_to_page_operator(
        input_pdf=source_pdf,
        output_pdf=output_pdf,
        page_number=1,
        operator_index=operator_index,
        actual_text="Accessible replacement",
    )

    with pikepdf.open(str(output_pdf)) as pdf:
        instructions = list(pikepdf.parse_content_stream(pdf.pages[0]))

    assert str(instructions[operator_index].operator) == "BDC"
    assert str(instructions[operator_index].operands[0]) == "/Span"
    assert str(instructions[operator_index].operands[1]["/ActualText"]) == "Accessible replacement"
    assert str(instructions[operator_index + 1].operator) == operator_name
    assert str(instructions[operator_index + 2].operator) == "EMC"


def test_apply_actualtext_rejects_non_text_showing_operator(tmp_path):
    source_pdf = TEST_SAMPLE_PDF
    output_pdf = tmp_path / "patched.pdf"

    with pytest.raises(ValueError) as exc:
        apply_actualtext_to_page_operator(
            input_pdf=source_pdf,
            output_pdf=output_pdf,
            page_number=1,
            operator_index=0,
            actual_text="Accessible replacement",
        )

    assert "not a text-showing operator" in str(exc.value)


def test_apply_actualtext_to_nested_xobject_context(tmp_path):
    nested_pdf = tmp_path / "nested_xobject.pdf"
    context_path = _append_form_xobject_page(TEST_SAMPLE_PDF, nested_pdf)
    output_pdf = tmp_path / "nested_xobject_patched.pdf"

    apply_actualtext_to_context(
        input_pdf=nested_pdf,
        output_pdf=output_pdf,
        context_path=context_path,
        actual_text="Nested replacement",
    )

    with pikepdf.open(str(output_pdf)) as pdf:
        page_resources = _resolve_object(pdf.pages[0].obj.get("/Resources"))
        xobject = _resolve_object(_resolve_object(page_resources.get("/XObject")).get("/Fx0"))
        instructions = list(pikepdf.parse_content_stream(xobject))

    assert str(instructions[3].operator) == "BDC"
    assert str(instructions[3].operands[1]["/ActualText"]) == "Nested replacement"
    assert str(instructions[4].operator) == "Tj"
    assert str(instructions[5].operator) == "EMC"


def test_apply_actualtext_to_annotation_appearance_context(tmp_path):
    annotated_pdf = tmp_path / "annot_appearance.pdf"
    context_path = _append_annotation_appearance(TEST_SAMPLE_PDF, annotated_pdf)
    output_pdf = tmp_path / "annot_appearance_patched.pdf"

    apply_actualtext_to_context(
        input_pdf=annotated_pdf,
        output_pdf=output_pdf,
        context_path=context_path,
        actual_text="Appearance replacement",
    )

    with pikepdf.open(str(output_pdf)) as pdf:
        annot = _resolve_object(_resolve_object(pdf.pages[0].obj.get("/Annots"))[0])
        appearance = _resolve_object(_resolve_object(annot.get("/AP")).get("/N"))
        instructions = list(pikepdf.parse_content_stream(appearance))

    assert str(instructions[3].operator) == "BDC"
    assert str(instructions[3].operands[1]["/ActualText"]) == "Appearance replacement"
    assert str(instructions[4].operator) == "Tj"
    assert str(instructions[5].operator) == "EMC"


def test_apply_actualtext_to_unnamed_xobject_context_uses_do_operator_target(tmp_path):
    unnamed_pdf = tmp_path / "unnamed_xobject.pdf"
    context_path, used_xobject_name = _append_unnamed_xobject_context(
        TEST_SAMPLE_PDF,
        unnamed_pdf,
    )
    output_pdf = tmp_path / "unnamed_xobject_patched.pdf"

    parsed = parse_verapdf_context_path(context_path)
    assert parsed["xobject_chain"][0]["from_operator_index"] == 1
    assert parsed["xobject_chain"][0]["content_stream_index"] == 0

    apply_actualtext_to_context(
        input_pdf=unnamed_pdf,
        output_pdf=output_pdf,
        context_path=context_path,
        actual_text="Unnamed replacement",
    )

    with pikepdf.open(str(output_pdf)) as pdf:
        page_resources = _resolve_object(pdf.pages[0].obj.get("/Resources"))
        xobjects = _resolve_object(page_resources.get("/XObject"))
        used_xobject = _resolve_object(xobjects.get(used_xobject_name))
        other_xobject = _resolve_object(xobjects.get("/Fx0"))
        used_instructions = list(pikepdf.parse_content_stream(used_xobject))
        other_instructions = list(pikepdf.parse_content_stream(other_xobject))

    assert str(used_instructions[3].operator) == "BDC"
    assert str(used_instructions[3].operands[1]["/ActualText"]) == "Unnamed replacement"
    assert str(other_instructions[3].operator) == "Tj"


def test_apply_actualtext_batch_to_multiple_targets_in_same_stream(tmp_path):
    source_pdf = tmp_path / "two_text_ops.pdf"
    context_first, context_second = _replace_page_with_two_text_operators(
        TEST_SAMPLE_PDF,
        source_pdf,
    )
    output_pdf = tmp_path / "two_text_ops_patched.pdf"

    apply_actualtext_batch_to_contexts(
        input_pdf=source_pdf,
        output_pdf=output_pdf,
        patches=[
            {"context_path": context_first, "actual_text": "One"},
            {"context_path": context_second, "actual_text": "Two"},
        ],
    )

    with pikepdf.open(str(output_pdf)) as pdf:
        instructions = list(pikepdf.parse_content_stream(pdf.pages[0]))

    actualtexts = [
        str(instruction.operands[1]["/ActualText"])
        for instruction in instructions
        if str(instruction.operator) == "BDC"
    ]
    assert actualtexts == ["One", "Two"]


def test_apply_actualtext_batch_rejects_duplicate_contexts(tmp_path):
    output_pdf = tmp_path / "duplicate.pdf"
    context = "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0]/operators[3]/usedGlyphs[0](Font Font 1 0 0 0 true)"

    with pytest.raises(ValueError) as exc:
        apply_actualtext_batch_to_contexts(
            input_pdf=TEST_SAMPLE_PDF,
            output_pdf=output_pdf,
            patches=[
                {"context_path": context, "actual_text": "One"},
                {"context_path": context, "actual_text": "Two"},
            ],
        )

    assert "Duplicate context_path" in str(exc.value)


def test_apply_actualtext_to_context_updates_existing_wrapper_without_nesting(tmp_path):
    source_pdf = tmp_path / "two_text_ops.pdf"
    context_first, _ = _replace_page_with_two_text_operators(
        TEST_SAMPLE_PDF,
        source_pdf,
    )
    first_output = tmp_path / "wrapped_once.pdf"
    second_output = tmp_path / "wrapped_twice.pdf"

    apply_actualtext_to_context(
        input_pdf=source_pdf,
        output_pdf=first_output,
        context_path=context_first,
        actual_text="Original replacement",
    )
    apply_actualtext_to_context(
        input_pdf=first_output,
        output_pdf=second_output,
        context_path=context_first,
        actual_text="Updated replacement",
    )

    with pikepdf.open(str(second_output)) as pdf:
        instructions = list(pikepdf.parse_content_stream(pdf.pages[0]))

    actualtexts = [
        str(instruction.operands[1]["/ActualText"])
        for instruction in instructions
        if str(instruction.operator) == "BDC"
    ]
    assert actualtexts == ["Updated replacement"]


def test_apply_actualtext_to_context_resolves_single_text_object_et_target(tmp_path):
    source_pdf = tmp_path / "single_text_op.pdf"
    context_path = _replace_page_with_single_text_et_context(
        TEST_SAMPLE_PDF,
        source_pdf,
    )
    output_pdf = tmp_path / "single_text_op_patched.pdf"

    apply_actualtext_to_context(
        input_pdf=source_pdf,
        output_pdf=output_pdf,
        context_path=context_path,
        actual_text="Resolved from ET",
    )

    with pikepdf.open(str(output_pdf)) as pdf:
        instructions = list(pikepdf.parse_content_stream(pdf.pages[0]))

    assert str(instructions[3].operator) == "BDC"
    assert str(instructions[3].operands[1]["/ActualText"]) == "Resolved from ET"
    assert str(instructions[4].operator) == "Tj"
