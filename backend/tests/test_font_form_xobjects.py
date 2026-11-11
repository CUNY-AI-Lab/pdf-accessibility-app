"""Font repair follows text drawn through a Form XObject.

A Form XObject is a stream; pikepdf's Stream is not a Dictionary, so the font
diagnostics, the ToUnicode repair, and the zero-byte cleanup used to skip the
text inside forms.
"""

from pathlib import Path

import pikepdf
from pdfminer.high_level import extract_text

from app.pipeline.fonts.diagnostics import _inspect_font_diagnostics
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode_sync
from tests.pdf_fixtures import helvetica

# Drawn through the form: "Hi", a stray zero byte, "there".
FORM_TEXT = b"Hi\x00there"


def _form_text_pdf(path: Path) -> Path:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    form = pdf.make_stream(b"BT /F1 12 Tf 10 100 Td (Hi\\000there) Tj ET\n")
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = pikepdf.Array([0, 0, 200, 200])
    form.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=form))
    page.Contents = pdf.make_stream(b"q /Fm0 Do Q\n")
    pdf.save(path)
    return path


def _form(pdf: pikepdf.Pdf) -> pikepdf.Stream:
    return pdf.pages[0].Resources.XObject.Fm0


def test_diagnostics_count_the_codes_drawn_through_a_form(tmp_path):
    diagnostics = _inspect_font_diagnostics(_form_text_pdf(tmp_path / "in.pdf"))

    assert diagnostics.get("error") is None
    [profile] = diagnostics["profiles"]
    assert profile["used_code_count"] == len(set(FORM_TEXT))


def test_tounicode_repair_reaches_text_drawn_through_a_form(tmp_path):
    source = _form_text_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    success, message, stats = _repair_pdf_tounicode_sync(source, output)

    assert success, message
    assert stats["zero_bytes_removed"] == 1
    with pikepdf.open(output) as pdf:
        form = _form(pdf)
        assert b"\x00" not in b"".join(
            bytes(operand)
            for instruction in pikepdf.parse_content_stream(form)
            for operand in instruction.operands
            if isinstance(operand, pikepdf.String)
        )
        assert "/ToUnicode" in form.Resources.Font.F1
    assert "".join(extract_text(str(output)).split()) == "Hithere"
