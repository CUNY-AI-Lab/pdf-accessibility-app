"""Unicode for TeX glyph names, Type 1 built-in encodings, and authored gaps."""

import unicodedata
from types import SimpleNamespace

import pikepdf
import pytest

from app.pipeline.fonts.diagnostics import (
    _inspect_font_diagnostics,
    _unicode_repair_gate_from_diagnostics,
)
from app.pipeline.fonts.encoding import (
    _glyph_name_to_unicode,
    _parse_tounicode_map,
    _render_tounicode_cmap,
    _simple_font_auto_unicode_policy,
    _simple_font_unicode_map,
    _tex_glyph_names,
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


def test_every_tex_glyph_name_resolves_to_text_that_nfc_keeps():
    # A deprecated code point such as U+2329 becomes a CJK bracket under NFC.
    changed = {}
    for name in _tex_glyph_names():
        text = _glyph_name_to_unicode(name)
        if text is None or unicodedata.normalize("NFC", text) != text:
            changed[name] = text

    assert changed == {}


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


def _truetype_program(cmap: dict[int, str]) -> bytes:
    from io import BytesIO

    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000, isTTF=True)
    order = [".notdef", "space", "glyph37"]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in order})
    builder.setupHorizontalMetrics({name: (500, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupPost()
    buffer = BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def _winansi_font(subtype: str, name: str = "opaqueglyph"):
    # Code 54 is "6" in WinAnsiEncoding; Differences gives it another glyph.
    return pikepdf.Dictionary(
        Subtype=pikepdf.Name(subtype),
        BaseFont=pikepdf.Name("/ABCDEF+Fixture"),
        Encoding=pikepdf.Dictionary(
            BaseEncoding=pikepdf.Name.WinAnsiEncoding,
            Differences=pikepdf.Array([54, pikepdf.Name(f"/{name}")]),
        ),
    )


@pytest.mark.parametrize("subtype", ["/Type1", "/TrueType", "/Type3"])
def test_unknown_differences_name_drops_the_base_encoding_character(subtype):
    mapping = _simple_font_unicode_map(_winansi_font(subtype), None)

    assert 54 not in mapping
    assert mapping[55] == "7"


def test_rebuilt_tounicode_leaves_an_unknown_glyph_to_the_author_or_unmapped(tmp_path):
    encoding = _winansi_font("/Type1").Encoding

    assert _repaired_map(tmp_path, b"67", encoding=encoding) == {55: "7"}
    authored = _repaired_map(tmp_path, b"67", encoding=encoding, tounicode={54: "≠"})
    assert authored == {54: "≠", 55: "7"}


@pytest.mark.parametrize(
    ("cmap", "expected"),
    [
        ({0x20: "space", 0x2260: "glyph37"}, "≠"),
        # Private-use values carry no character.
        ({0x20: "space", 0xE000: "glyph37"}, None),
        ({0x20: "space"}, None),
    ],
)
def test_truetype_program_cmap_names_the_character_of_an_unknown_glyph_name(cmap, expected):
    program = _truetype_program(cmap)
    mapping = _simple_font_unicode_map(_winansi_font("/TrueType", "glyph37"), program)

    assert mapping.get(54) == expected
    # A Type 1 dictionary does not select glyphs through a TrueType cmap.
    assert 54 not in _simple_font_unicode_map(_winansi_font("/Type1", "glyph37"), program)
