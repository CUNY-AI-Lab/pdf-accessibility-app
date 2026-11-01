"""Embedding substitutes must preserve the PDF's text and positioning contracts."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from app.config import Settings
from app.pipeline.fonts import embedding, lanes, programs
from app.pipeline.fonts.diagnostics import _inspect_font_diagnostics
from app.pipeline.fonts.rules import FONT_LANE_EMBED
from app.services.structure_text import screen_reader_text


def _install_test_font(tmp_path, monkeypatch, family="LiberationSans", style="Regular"):
    # A valid, tiny TrueType program keeps these tests independent of OS fonts.
    builder = FontBuilder(1000, isTTF=True)
    glyphs = [".notdef", "space", "A", "B"]
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({32: "space", 65: "A", 66: "B"})
    outlines = {}
    for name in glyphs:
        pen = TTGlyphPen(None)
        if name != "space":
            pen.moveTo((50, 0))
            pen.lineTo((550, 0))
            pen.lineTo((300, 700))
            pen.closePath()
        outlines[name] = pen.glyph()
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({
        "familyName": family, "styleName": style,
        "fullName": f"{family} {style}", "psName": f"{family}-{style}",
    })
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    font_dir = tmp_path / "fonts"
    font_dir.mkdir(exist_ok=True)
    builder.save(str(font_dir / f"{family}-{style}.ttf"))
    monkeypatch.setattr(programs, "SYSTEM_FONT_DIRS", (font_dir,))
    programs._system_font_index.cache_clear()
    programs._system_font_program.cache_clear()


@pytest.fixture(autouse=True)
def _clear_font_caches():
    yield
    programs._system_font_index.cache_clear()
    programs._system_font_program.cache_clear()
    programs._ghostscript_resource_font_dir.cache_clear()
    programs._ghostscript_font_metrics_dir.cache_clear()
    programs._ghostscript_type1_font_program.cache_clear()
    programs._ghostscript_type1_descriptor.cache_clear()


def _unembedded_pdf(path: Path, name="ArialMT"):
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        descriptor = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name(f"/{name}"),
            Flags=32, FontBBox=[-100, -200, 1000, 800], Ascent=800, Descent=-200,
            CapHeight=700, ItalicAngle=0, StemV=80,
        ))
        font = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType,
            BaseFont=pikepdf.Name(f"/{name}"), Encoding=pikepdf.Name.WinAnsiEncoding,
            FontDescriptor=descriptor, FirstChar=32, LastChar=66,
            Widths=[250] + [600] * 34,
            ToUnicode=pdf.make_stream(b"""
                /CIDInit /ProcSet findresource begin 12 dict begin begincmap
                /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
                /CMapName /Test def /CMapType 2 def
                1 begincodespacerange <00> <FF> endcodespacerange
                3 beginbfchar <20> <0020> <41> <0041> <42> <03B2> endbfchar
                endcmap CMapName currentdict /CMap defineresource pop end end
            """),
        ))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.Contents = pdf.make_stream(
            b"/P << /MCID 0 >> BDC BT /F1 12 Tf 72 720 Td (AB A) Tj ET EMC"
        )
        page.StructParents = 0
        root = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot))
        element = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name.P, P=root, Pg=page.obj, K=0,
        ))
        root.K = pikepdf.Array([element])
        root.ParentTree = pdf.make_indirect(pikepdf.Dictionary(Nums=[0, [element]]))
        pdf.Root.StructTreeRoot = root
        pdf.Root.MarkInfo = pikepdf.Dictionary(Marked=True)
        pdf.save(path)


def _text_contract(path):
    with pikepdf.open(path) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        return (
            font.ToUnicode.read_bytes(), str(font.Encoding),
            int(font.FirstChar), int(font.LastChar), tuple(font.Widths),
            pdf.pages[0].Contents.read_bytes(),
        )


@pytest.mark.parametrize(("source", "family", "style"), [
    ("ABCDEF+ArialMT", "LiberationSans", "Regular"),
    ("Arial,Bold", "LiberationSans", "Bold"),
    ("Helvetica,Bold", "LiberationSans", "Bold"),
    ("Helvetica,Italic", "LiberationSans", "Italic"),
    ("Times-Roman", "LiberationSerif", "Regular"),
    ("Arial-ItalicMT", "LiberationSans", "Italic"),
    ("Helvetica-BoldOblique", "LiberationSans", "BoldItalic"),
    ("Times New Roman", "LiberationSerif", "Regular"),
    ("TimesNewRomanPS-BoldMT", "LiberationSerif", "Bold"),
    ("TimesNewRomanPS-ItalicMT", "LiberationSerif", "Italic"),
    ("TimesNewRomanPS-BoldItalicMT", "LiberationSerif", "BoldItalic"),
    ("Times-BoldItalic", "LiberationSerif", "BoldItalic"),
    ("CourierNewPSMT", "LiberationMono", "Regular"),
    ("Courier-Bold", "LiberationMono", "Bold"),
    ("Courier-Oblique", "LiberationMono", "Italic"),
    ("CourierNewPS-BoldItalicMT", "LiberationMono", "BoldItalic"),
])
@pytest.mark.parametrize("subtype", ["/TrueType", "/Type1"])
def test_substitute_embedding_preserves_unicode_widths_and_tagged_text(
    tmp_path, monkeypatch, source, family, style, subtype,
):
    _install_test_font(tmp_path, monkeypatch, family, style)
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    _unembedded_pdf(before, source)
    with pikepdf.open(before, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Resources.Font.F1.Subtype = pikepdf.Name(subtype)
        pdf.save(before)
    text = screen_reader_text(before)
    assert "Aβ A" in text
    diagnostics = _inspect_font_diagnostics(before, include_used_code_analysis=False)
    assert diagnostics["summary"]["local_embed_candidate_count"] == 1
    ok, message, stats = embedding._embed_system_fonts_sync(before, after)
    assert ok, message
    assert stats["fonts_embedded"] == 1
    assert _text_contract(before) == _text_contract(after)
    assert screen_reader_text(after) == text
    with pikepdf.open(after) as pdf:
        from fontTools.ttLib import TTFont

        font = pdf.pages[0].Resources.Font.F1
        with TTFont(BytesIO(font.FontDescriptor.FontFile2.read_bytes())) as embedded:
            assert embedded["name"].getDebugName(6) == f"{family}-{style}"


async def test_embedding_lane_uses_substitute_without_ghostscript_or_retagging(tmp_path, monkeypatch):
    _install_test_font(tmp_path, monkeypatch)
    source = tmp_path / "tagged.pdf"
    _unembedded_pdf(source)
    monkeypatch.setattr(lanes, "create_job_dir", lambda _job_id: tmp_path)

    async def unexpected(*args, **kwargs):
        pytest.fail("Local substitution must preserve the tagged PDF without a rewrite")

    async def validated(**kwargs):
        assert screen_reader_text(kwargs["pdf_path"]) == screen_reader_text(source)
        return SimpleNamespace(compliant=True)

    monkeypatch.setattr(lanes, "_rewrite_pdf_with_ghostscript_embed", unexpected)
    monkeypatch.setattr(lanes, "tag_pdf", unexpected)
    monkeypatch.setattr(lanes, "validate_pdf", validated)
    result = await lanes._attempt_font_lane(
        "test", SimpleNamespace(), Settings(), source, source, {}, [], FONT_LANE_EMBED,
        current_tagging_result="original-tags",
    )
    assert result["success"]
    assert not result["requires_retag"]
    assert result["tagging_result"] == "original-tags"
    assert result["details"]["fonts_embedded"] == 1


def test_original_font_program_is_preferred_over_a_substitute(tmp_path, monkeypatch):
    from fontTools.ttLib import TTFont

    _install_test_font(tmp_path, monkeypatch, family="Arial")
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    _unembedded_pdf(before, "Arial")
    ok, message, _ = embedding._embed_system_fonts_sync(before, after)
    assert ok, message
    with pikepdf.open(after) as pdf:
        stream = pdf.pages[0].Resources.Font.F1.FontDescriptor.FontFile2
        with TTFont(BytesIO(stream.read_bytes())) as font:
            assert font["name"].getDebugName(6) == "Arial-Regular"
    assert screen_reader_text(before) == screen_reader_text(after)


@pytest.mark.parametrize("name", ["ArialNarrow", "HelveticaNeue", "UnknownFont"])
def test_substitution_does_not_guess_other_families(tmp_path, monkeypatch, name):
    _install_test_font(tmp_path, monkeypatch)
    font = pikepdf.Dictionary(Subtype=pikepdf.Name.TrueType, Encoding=pikepdf.Name.WinAnsiEncoding)
    assert programs._local_font_program(font, name)[0] is None


def test_substitution_does_not_reuse_cid_glyph_ids_or_symbolic_encoding(tmp_path, monkeypatch):
    _install_test_font(tmp_path, monkeypatch)
    cid = pikepdf.Dictionary(Subtype=pikepdf.Name.Type0)
    assert programs._local_font_program(cid, "ArialMT", descendant_subtype=pikepdf.Name.CIDFontType2)[0] is None
    implicit = pikepdf.Dictionary(Subtype=pikepdf.Name.TrueType)
    assert programs._local_font_program(implicit, "ArialMT")[0] is None
    symbolic = pikepdf.Dictionary(
        Subtype=pikepdf.Name.TrueType, Encoding=pikepdf.Name.WinAnsiEncoding,
        FontDescriptor=pikepdf.Dictionary(Flags=4),
    )
    assert programs._local_font_program(symbolic, "ArialMT")[0] is None


def test_type1_alias_does_not_guess_an_implicit_or_symbolic_encoding(tmp_path, monkeypatch):
    _install_test_font(tmp_path, monkeypatch)
    for encoding, flags in ((None, 32), (pikepdf.Name.WinAnsiEncoding, 4)):
        font = pikepdf.Dictionary(
            Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.ArialMT,
            FontDescriptor=pikepdf.Dictionary(Flags=flags),
        )
        if encoding is not None:
            font.Encoding = encoding
        assert programs._local_font_program(font, "ArialMT")[0] is None


def test_debian_urw_programs_and_afm_are_discovered(tmp_path, monkeypatch):
    prefix = tmp_path / "usr"
    gs = prefix / "bin/gs"
    gs.parent.mkdir(parents=True)
    gs.touch()
    font_dir = prefix / "share/fonts/type1/urw-base35"
    font_dir.mkdir(parents=True)
    program = b"%!PS-AdobeFont-1.0: NimbusRoman-Regular 1.0\n/FontName /NimbusRoman-Regular def\n"
    (font_dir / "NimbusRoman-Regular.t1").write_bytes(program)
    (font_dir / "NimbusRoman-Regular.afm").write_text(
        "StartFontMetrics 4.1\nFontName NimbusRoman-Regular\nFontBBox -100 -200 1000 800\n"
        "Ascender 800\nDescender -200\nStartCharMetrics 1\n"
        "C 65 ; WX 600 ; N A ; B 0 0 600 700 ;\nEndCharMetrics\nEndFontMetrics\n"
    )
    monkeypatch.setattr(programs, "resolve_binary", lambda *args, **kwargs: str(gs))
    assert programs._ghostscript_resource_font_dir() == font_dir
    assert programs._ghostscript_font_metrics_dir() == font_dir
    assert programs._ghostscript_type1_font_program("Times-Roman")[0] == program
    assert programs._ghostscript_type1_descriptor("Times-Roman")["Widths"][65] == 600


def test_urw_fallback_uses_type1_and_preserves_text_contract(tmp_path, monkeypatch):
    from app.pipeline.fonts.programs import _ghostscript_type1_font_program

    if not _ghostscript_type1_font_program("Helvetica")[0]:
        pytest.skip("Ghostscript URW font programs are not installed")
    monkeypatch.setattr(programs, "_system_font_program", lambda _name: (None, None))
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    _unembedded_pdf(before)
    ok, message, _ = embedding._embed_system_fonts_sync(before, after)
    assert ok, message
    assert _text_contract(before) == _text_contract(after)
    assert screen_reader_text(before) == screen_reader_text(after)
    with pikepdf.open(after) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        assert font.Subtype == pikepdf.Name.Type1
        assert font.FontDescriptor.FontFile.read_bytes().startswith(b"%!")


@pytest.mark.parametrize("encoding", ["/WinAnsiEncoding", None, "differences"])
@pytest.mark.parametrize(("name", "family", "style"), [
    ("Times-Roman", "LiberationSerif", "Regular"),
    ("Helvetica-Bold", "LiberationSans", "Bold"),
    ("Courier-Oblique", "LiberationMono", "Italic"),
])
def test_standard14_without_widths_or_descriptor_embeds_without_afm(
    tmp_path, monkeypatch, encoding, name, family, style,
):
    from fontTools.ttLib import TTFont

    _install_test_font(tmp_path, monkeypatch, family, style)
    font_path = tmp_path / "fonts" / f"{family}-{style}.ttf"
    with TTFont(font_path) as program:
        program["hmtx"].metrics["space"] = (250, 0)
        program["hmtx"].metrics["B"] = (700, 0)
        program.save(font_path)
    monkeypatch.setattr(embedding, "_ghostscript_type1_descriptor", lambda _: None)
    monkeypatch.setattr(programs, "_ghostscript_type1_font_program", lambda _: (None, None, None))
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    _unembedded_pdf(before, name)
    with pikepdf.open(before, allow_overwriting_input=True) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        f.Subtype = pikepdf.Name.Type1
        for key in ("/Widths", "/FirstChar", "/LastChar", "/FontDescriptor"):
            del f[key]
        if encoding is None:
            del f["/Encoding"]
        elif encoding == "differences":
            f.Encoding = pikepdf.Dictionary(Differences=[65, pikepdf.Name.B])
        pdf.save(before)
    text = screen_reader_text(before)
    ok, message, stats = embedding._embed_system_fonts_sync(before, after)
    assert ok, message
    assert stats["fonts_embedded"] == 1
    assert screen_reader_text(after) == text
    with pikepdf.open(before) as old, pikepdf.open(after) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        assert f.Subtype == pikepdf.Name.TrueType
        assert f.FirstChar == 0 and f.LastChar == 255 and len(f.Widths) == 256
        assert pdf.pages[0].Contents.read_bytes() == old.pages[0].Contents.read_bytes()
        assert f.ToUnicode.read_bytes() == old.pages[0].Resources.Font.F1.ToUnicode.read_bytes()
        with TTFont(BytesIO(f.FontDescriptor.FontFile2.read_bytes())) as program:
            assert program["name"].getDebugName(6) == f"{family}-{style}"
            glyph = "B" if encoding == "differences" else "A"
            assert float(f.Widths[65]) == program["hmtx"].metrics[glyph][0]
            assert float(f.Widths[66]) == program["hmtx"].metrics["B"][0] == 700
            assert float(f.Widths[32]) == program["hmtx"].metrics["space"][0] == 250
        if encoding == "differences":
            assert f.Encoding.BaseEncoding == pikepdf.Name.StandardEncoding
            assert list(f.Encoding.Differences) == [65, pikepdf.Name.B]
        else:
            assert f.Encoding == pikepdf.Name(encoding or "/StandardEncoding")


async def test_arial_embeds_after_tagging_and_in_author_repairs(tmp_path, monkeypatch):
    from app.pipeline.author_repairs import repair_author_pdf
    from app.pipeline.tagger import tag_pdf

    _install_test_font(tmp_path, monkeypatch)
    source, tagged, repaired = [tmp_path / f"{name}.pdf" for name in ("input", "tagged", "repaired")]
    _unembedded_pdf(source)
    # This fixture's intentionally conflicting ToUnicode belongs to the
    # substitution tests above; author repairs also run the Unicode repair.
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        f.ToUnicode = pdf.make_stream(f.ToUnicode.read_bytes().replace(b"<03B2>", b"<0042>"))
        pdf.save(source)
    result = await tag_pdf(source, tagged, {"elements": [{
        "type": "paragraph", "page": 0, "bbox": [60, 60, 200, 90], "text": "AB A",
    }]})
    assert result.output_path == tagged
    text = screen_reader_text(tagged)
    audit = repair_author_pdf(tagged, repaired, {})
    assert audit[0]["applied"] and audit[0]["stats"]["fonts_embedded"] == 1
    assert screen_reader_text(repaired) == text
    assert _text_contract(tagged) == _text_contract(tmp_path / "author-embed_fonts.pdf")
    with pikepdf.open(repaired) as pdf:
        assert pdf.pages[0].Resources.Font.F1.FontDescriptor.FontFile2.read_bytes()
