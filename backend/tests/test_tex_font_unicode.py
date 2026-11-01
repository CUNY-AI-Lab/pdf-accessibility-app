"""Unicode for TeX glyph names, Type 1 built-in encodings, and authored gaps."""

from types import SimpleNamespace

import pikepdf
import pytest

from app.pipeline.fonts.diagnostics import (
    _inspect_font_diagnostics,
    _unicode_repair_gate_from_diagnostics,
)
from app.pipeline.fonts.encoding import (
    _parse_tounicode_map,
    _render_tounicode_cmap,
    _simple_font_auto_unicode_policy,
    _simple_font_unicode_map,
)
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode_sync

ARRAY_ENCODING = (
    b"/Encoding 256 array\n0 1 255 {1 index exch /.notdef put} for\n"
    b"dup 1 /alpha put\ndup 2 /summation put\nreadonly def"
)


def _type1_program(encoding: bytes) -> tuple[bytes, int]:
    cleartext = (
        b"%!PS-AdobeFont-1.0: Fixture 001.000\n11 dict begin\n/FontType 1 def\n"
        b"/FontName /Fixture def\n" + encoding + b"\ncurrentdict end\ncurrentfile eexec\n"
    )
    # Stands in for the encrypted part.
    encrypted = b"\x8f\x02 dup 66 /B put \xfe" * 4
    return cleartext + encrypted + b"0" * 64 + b"\ncleartomark\n", len(cleartext)


def _font(pdf, *, program=None, encoding=None, tounicode=None):
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name("/ABCDEF+CMEX10")
    )
    if program is not None:
        data, length1 = program
        stream = pdf.make_stream(data)
        stream.Length1, stream.Length2, stream.Length3 = length1, len(data) - length1, 0
        font.FontDescriptor = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor, FontFile=stream)
        )
    if encoding is not None:
        font.Encoding = encoding
    if tounicode is not None:
        font.ToUnicode = pdf.make_stream(_render_tounicode_cmap(tounicode, 1))
    return pdf.make_indirect(font)


def _write_pdf(path, shown: bytes, **font_args) -> None:
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=_font(pdf, **font_args)))
        page.Contents = pdf.make_stream(b"BT /F1 12 Tf <" + shown.hex().encode() + b"> Tj ET")
        pdf.save(path)


def _repaired_map(tmp_path, shown: bytes, **font_args) -> dict[int, str]:
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _write_pdf(source, shown, **font_args)
    assert _repair_pdf_tounicode_sync(source, output)[0]
    with pikepdf.open(output) as pdf:
        return _parse_tounicode_map(pdf.pages[0].Resources.Font.F1.ToUnicode)


def test_tex_glyph_names_outside_the_agl_map_to_unicode():
    font = pikepdf.Dictionary(
        Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name("/ABCDEF+CMSY10"),
        Encoding=pikepdf.Dictionary(
            Differences=pikepdf.Array(
                [
                    54,
                    pikepdf.Name.negationslash,
                    pikepdf.Name.summationdisplay,
                    pikepdf.Name.mapsto,
                    pikepdf.Name("/parenleftbig.alt"),
                    pikepdf.Name.alpha,
                ]
            )
        ),
    )

    assert _simple_font_unicode_map(font, None) == {
        54: "\u0338",
        55: "\u2211",
        56: "\u21a6",
        57: "(",
        58: "\u03b1",
    }


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        # Code 66 is named only in the encrypted part, which is not an encoding.
        (ARRAY_ENCODING, {1: "\u03b1", 2: "\u2211", 66: None}),
        # StandardEncoding, not ASCII: 39 is quoteright and 96 quoteleft.
        (b"/Encoding StandardEncoding def", {39: "\u2019", 66: "B", 96: "\u2018"}),
    ],
)
def test_type1_font_without_pdf_encoding_uses_its_builtin_encoding(encoding, expected):
    data, _ = _type1_program(encoding)
    font = pikepdf.Dictionary(Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name("/ABCDEF+CMEX10"))

    assert _simple_font_auto_unicode_policy(font, font_bytes=data) == "embedded_type1"
    mapping = _simple_font_unicode_map(font, data)
    assert {code: mapping.get(code) for code in expected} == expected


def test_builtin_type1_encoding_reaches_the_gate_and_the_rebuilt_tounicode(tmp_path):
    source = tmp_path / "source.pdf"
    _write_pdf(source, b"\x01\x02", program=_type1_program(ARRAY_ENCODING))
    violations = [SimpleNamespace(rule_id="ISO 14289-1:2014-7.21.7-1", severity="error")]

    gate = _unicode_repair_gate_from_diagnostics(
        _inspect_font_diagnostics(source, violations), violations=violations
    )

    assert gate["safe_simple_candidates"] == 1
    assert _repaired_map(tmp_path, b"\x01\x02", program=_type1_program(ARRAY_ENCODING)) == {
        1: "\u03b1",
        2: "\u2211",
    }


def test_rebuilt_simple_tounicode_keeps_authored_values_for_used_codes_it_cannot_generate(
    tmp_path,
):
    encoding = pikepdf.Dictionary(
        Differences=pikepdf.Array(
            [65, pikepdf.Name.A, pikepdf.Name.opaqueglyph, pikepdf.Name.C, pikepdf.Name.D]
        )
    )
    # 65 is missing from the authored map; 67 conflicts with its glyph name; 90 is unused.
    authored = {66: "\u03b2", 67: "Z", 68: "D", 90: "z"}

    assert _repaired_map(tmp_path, b"ABCD", encoding=encoding, tounicode=authored) == {
        65: "A",
        66: "\u03b2",
        67: "C",
        68: "D",
    }
