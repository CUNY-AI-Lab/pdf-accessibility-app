from pathlib import Path

import pikepdf

from app.services.font_artifact import apply_artifact_batch_to_contexts
from tests.fixtures import TEST_SAMPLE_PDF


def _resolve_object(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _build_two_glyph_pdf(output_path: Path) -> None:
    with pikepdf.open(str(TEST_SAMPLE_PDF)) as sample_pdf:
        sample_page = sample_pdf.pages[0]
        sample_resources = _resolve_object(sample_page.obj.get("/Resources"))
        sample_fonts = _resolve_object(sample_resources.get("/Font"))
        sample_font_name = next(iter(sample_fonts.keys()))
        sample_font = sample_fonts.get(sample_font_name)

        pdf = pikepdf.new()
        page = pdf.add_blank_page(page_size=(200, 200))
        imported_font = pdf.copy_foreign(sample_font)
        page.obj["/Resources"] = pikepdf.Dictionary({
            "/Font": pikepdf.Dictionary({"/F1": imported_font}),
        })
        page["/Contents"] = pdf.make_stream(
            pikepdf.unparse_content_stream(
                [
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                    pikepdf.ContentStreamInstruction([pikepdf.Name("/F1"), 12], pikepdf.Operator("Tf")),
                    pikepdf.ContentStreamInstruction([20, 20], pikepdf.Operator("Td")),
                    pikepdf.ContentStreamInstruction([pikepdf.String("A")], pikepdf.Operator("Tj")),
                    pikepdf.ContentStreamInstruction([0, 20], pikepdf.Operator("Td")),
                    pikepdf.ContentStreamInstruction([pikepdf.String("B")], pikepdf.Operator("Tj")),
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
                ]
            )
        )
        pdf.save(str(output_path))


def test_apply_artifact_batch_to_contexts_wraps_targeted_glyphs(tmp_path):
    input_pdf = tmp_path / "input.pdf"
    output_pdf = tmp_path / "output.pdf"
    _build_two_glyph_pdf(input_pdf)

    apply_artifact_batch_to_contexts(
        input_pdf=input_pdf,
        output_pdf=output_pdf,
        context_paths=[
            "root/document[0]/pages[0]/contentStream[0]/operators[3]",
            "root/document[0]/pages[0]/contentStream[0]/operators[5]",
        ],
    )

    with pikepdf.open(str(output_pdf)) as pdf:
        instructions = list(pikepdf.parse_content_stream(pdf.pages[0]))

    operator_names = [str(instruction.operator) for instruction in instructions]
    assert operator_names.count("BMC") == 2
    assert operator_names.count("EMC") == 2
    assert operator_names[3:6] == ["BMC", "Tj", "EMC"]
    assert operator_names[7:10] == ["BMC", "Tj", "EMC"]
