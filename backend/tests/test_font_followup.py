"""Regressions from Docker validation of held-out release repairs."""

from copy import deepcopy
from io import BytesIO

import pikepdf
import pytest
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from app.pipeline.fonts import programs
from app.pipeline.fonts.encoding import (
    _raw_text_bytes,
    _render_tounicode_cmap,
)
from app.pipeline.fonts.gothic import gothic_style
from app.pipeline.fonts.notdef_content import repair_notdef_content
from app.pipeline.fonts.ocr import unmapped_type3_pages
from tests.pdf_fixtures import rendered
from tests.test_heldout_release_repairs import _cid_pdf, _program
from tests.test_quality_ocr import type3_document


def _blank_font_document(
    path, *, operator="Tj", nested=False, spacing=0, painted=False, shared=False
):
    data = _program(square=True)
    with TTFont(BytesIO(data), recalcTimestamp=False) as tt:
        tt["glyf"][".notdef"] = deepcopy(tt["glyf"]["box"]) if painted else TTGlyphPen(None).glyph()
        for table in tt["cmap"].tables:
            table.cmap.update({65: "box", 66: "box"})
        buffer = BytesIO()
        tt.save(buffer)
        data = buffer.getvalue()
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 300))
        desc = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.FontDescriptor,
                FontName=pikepdf.Name.Fixture,
                Flags=32,
                FontBBox=[0, 0, 600, 700],
                Ascent=800,
                Descent=-200,
                CapHeight=700,
                StemV=80,
                ItalicAngle=0,
                FontFile2=pdf.make_stream(data),
            )
        )
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.TrueType,
                BaseFont=pikepdf.Name.Fixture,
                Encoding=pikepdf.Name.WinAnsiEncoding,
                FontDescriptor=desc,
                FirstChar=0,
                LastChar=66,
                Widths=[600] * 67,
                ToUnicode=pdf.make_stream(_render_tounicode_cmap({0: "A", 65: "A", 66: "B"}, 1)),
            )
        )
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        text = {
            "Tj": b"(A\000B) Tj",
            "TJ": b"[(A\000) 50 (B)] TJ",
            "'": b"(A\000B) '",
            '"': b'3 2 (A\000B) "',
        }[operator]
        content = (
            f"BT /F1 12 Tf {spacing} Tc 14 TL 80 Tz 20 220 Td ".encode() + text + b" (B) Tj ET"
        )
        if nested:
            form = pdf.make_stream(content)
            form.Type = pikepdf.Name.XObject
            form.Subtype = pikepdf.Name.Form
            form.Resources = page.Resources
            form.BBox = [0, 0, 300, 300]
            page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm=form))
            content = b"q /Fm Do Q"
        if shared:
            # A resource-less shared Form inherits a different spacing in
            # each invocation. It cannot be rewritten once for both states.
            form = pdf.make_stream(b"(A\000B) Tj")
            form.Subtype = pikepdf.Name.Form
            form.BBox = [0, 0, 300, 300]
            page.Resources.XObject = pikepdf.Dictionary(Fm=form)
            content = b"BT /F1 12 Tf 20 220 Td q 0 Tc /Fm Do Q 2 Tc /Fm Do ET"
        page.Contents = pdf.make_stream(content)
        pdf.save(path)


@pytest.mark.parametrize("operator", ["Tj", "TJ", "'", '"'])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("spacing", [0, 2])
def test_blank_notdef_is_removed_with_advance_and_quote_state_preserved(
    tmp_path, operator, nested, spacing
):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _blank_font_document(source, operator=operator, nested=nested, spacing=spacing)
    with pikepdf.open(source) as pdf:
        stats = repair_notdef_content(pdf)
        assert stats["notdef_glyphs_removed"] == 1
        owner = pdf.pages[0].Resources.XObject.Fm if nested else pdf.pages[0]
        for ins in pikepdf.parse_content_stream(owner):
            assert 0 not in _raw_text_bytes(str(ins.operator), list(ins.operands))
        assert repair_notdef_content(pdf)["notdef_glyphs_removed"] == 0
        pdf.save(output)
    assert rendered(source) == rendered(output)


def test_painted_missing_glyph_uses_an_existing_real_code_without_deleting_ink(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _blank_font_document(source, painted=True)
    with pikepdf.open(source) as pdf:
        stats = repair_notdef_content(pdf)
        assert stats == {"notdef_glyphs_removed": 0, "notdef_codes_remapped": 1}
        pdf.save(output)
    assert rendered(source) == rendered(output)


def test_shared_form_with_incompatible_inherited_spacing_is_left_unchanged(tmp_path):
    source = tmp_path / "source.pdf"
    _blank_font_document(source, shared=True)
    with pikepdf.open(source) as pdf:
        before = pdf.pages[0].Resources.XObject.Fm.read_bytes()
        assert repair_notdef_content(pdf)["notdef_glyphs_removed"] == 0
        assert pdf.pages[0].Resources.XObject.Fm.read_bytes() == before


@pytest.mark.parametrize("value", ["0000", "FEFF", "FFFE"])
@pytest.mark.parametrize("syntax", ["char", "range", "array"])
@pytest.mark.parametrize("known", [False, True])
def test_invalid_author_unicode_forces_ocr_even_when_glyph_names_can_be_decoded(
    tmp_path, value, syntax, known
):
    path = tmp_path / "source.pdf"
    type3_document(path, known=known)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        data = {
            "char": f"1 beginbfchar\n<41> <{value}>\nendbfchar",
            "range": f"1 beginbfrange\n<41> <41> <{value}>\nendbfrange",
            "array": f"1 beginbfrange\n<41> <41> [<{value}>]\nendbfrange",
        }[syntax]
        pdf.pages[0].Resources.Font.F1.ToUnicode = pdf.make_stream(data.encode())
        pdf.save(path)
    assert unmapped_type3_pages(path) == [0]


@pytest.mark.parametrize(
    "name,style",
    [
        ("CenturyGothic", "Book"),
        ("CenturyGothic-Bold", "Demi"),
        ("ABCDEF+CenturyGothic,Italic", "BookOblique"),
        ("CenturyGothic,BoldItalic", "DemiOblique"),
    ],
)
def test_owner_approved_gothic_aliases_select_the_correct_urw_style(name, style):
    from app.pipeline.fonts.names import _normalize_font_name

    assert gothic_style(_normalize_font_name(name)) == style
    assert programs._metric_compatible_names(name)[0] == f"URWGothic-{style}"


def test_gothic_embedding_uses_truetype_width_alignment_and_preserves_dictionary_widths(
    tmp_path, monkeypatch
):
    from app.pipeline.fonts.embedding import _embed_system_fonts_sync
    from tests.test_font_substitution import _install_test_font, _unembedded_pdf

    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _unembedded_pdf(source, name="ABCDEF+CenturyGothic,Bold")
    _install_test_font(tmp_path, monkeypatch)
    data = (tmp_path / "fonts/LiberationSans-Regular.ttf").read_bytes()
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Resources.Font.F1.Widths[65 - 32] = 721
        pdf.pages[0].Resources.Font.F1.Widths[66 - 32] = 433
        pdf.save(source)
    monkeypatch.setattr(programs, "gothic_truetype", lambda _: (data, "URWGothic-Demi"))
    assert _embed_system_fonts_sync(source, output)[0]
    with pikepdf.open(source) as before, pikepdf.open(output) as after:
        old = before.pages[0].Resources.Font.F1
        new = after.pages[0].Resources.Font.F1
        assert list(old.Widths) == list(new.Widths)
        with TTFont(BytesIO(new.FontDescriptor.FontFile2.read_bytes())) as font:
            assert "glyf" in font
            assert font["hmtx"].metrics["A"][0] == 721
            assert font["hmtx"].metrics["B"][0] == 433
    programs._system_font_index.cache_clear()
    programs._system_font_program.cache_clear()


@pytest.mark.parametrize("width_kind", ["array", "range"])
def test_painted_cid_zero_gets_nonzero_operand_and_exact_fractional_width(tmp_path, width_kind):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _cid_pdf(source, "\u2610", square=True)
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        cid = pdf.pages[0].Resources.Font.F1.DescendantFonts[0]
        cid.W = [0, [600.25]] if width_kind == "array" else [0, 0, 600.25]
        # This is the intermediate state after the dictionary repair: the
        # glyph is real, but literal CID zero must also disappear.
        cid.CIDToGIDMap = pdf.make_stream(b"\0\2\0\1\0\2")
        pdf.save(source)
    with pikepdf.open(source) as pdf:
        assert repair_notdef_content(pdf)["notdef_codes_remapped"] == 1
        font = pdf.pages[0].Resources.Font.F1
        shown = next(
            i for i in pikepdf.parse_content_stream(pdf.pages[0]) if str(i.operator) == "TJ"
        )
        alias = int.from_bytes(bytes(shown.operands[0][0]), "big")
        assert alias > 0
        assert float(font.DescendantFonts[0].W[-1][0]) == 600.25
        assert repair_notdef_content(pdf)["notdef_codes_remapped"] == 0
        pdf.save(output)
    assert rendered(source) == rendered(output)


def test_unknown_painted_notdef_is_never_deleted(tmp_path):
    source = tmp_path / "source.pdf"
    _blank_font_document(source, painted=True)
    with pikepdf.open(source) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        font.ToUnicode = pdf.make_stream(_render_tounicode_cmap({0: "Z", 65: "A", 66: "B"}, 1))
        before = pdf.pages[0].Contents.read_bytes()
        assert repair_notdef_content(pdf) == {
            "notdef_glyphs_removed": 0,
            "notdef_codes_remapped": 0,
        }
        assert pdf.pages[0].Contents.read_bytes() == before


@pytest.mark.parametrize("value", ["0000", "FEFF", "FFFE"])
def test_invalid_identity_font_unicode_forces_ocr_only_on_used_codes(tmp_path, value):
    source = tmp_path / "source.pdf"
    _cid_pdf(source, "A", square=True)
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        font.ToUnicode = pdf.make_stream(f"1 beginbfchar\n<0000> <{value}>\nendbfchar".encode())
        pdf.save(source)
    assert unmapped_type3_pages(source) == [0]
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Contents = pdf.make_stream(b"BT /F1 12 Tf <0002> Tj ET")
        pdf.save(source)
    assert unmapped_type3_pages(source) == []
