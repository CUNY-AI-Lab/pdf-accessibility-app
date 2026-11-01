"""Behavior at the font, annotation and tagging boundaries behind release holds."""

from io import BytesIO

import pikepdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from app.pipeline.fonts.encoding import (
    _parse_tounicode_map_details,
    _raw_text_bytes,
    _render_tounicode_cmap,
    _sanitize_text_showing_zero_bytes,
    _simple_font_unicode_map,
)
from app.pipeline.fonts.repair import _repair_pdf_font_dicts_sync
from app.pipeline.fonts.resources import _iter_pdf_fonts
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode_sync
from app.pipeline.tagger import tag_pdf
from app.pipeline.tagger_layout_tables import _layout_table_plan
from app.pipeline.tagger_tables import _infer_table_headers
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import rendered
from tests.test_tagger_figure_text import _fixture, _structs


def _program(*, square=False, triangle=False):
    builder = FontBuilder(1000, isTTF=True)
    order = [".notdef", "space"] + (["box"] if square else [])
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({32: "space", **({0x2610: "box", 0x25A1: "box"} if square else {})})
    outlines = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name != "space":
            pen.moveTo((0, 0))
            pen.lineTo((0, 700))
            pen.lineTo((500, 700))
            if not triangle:
                pen.lineTo((500, 0))
            pen.closePath()
            if not triangle:
                pen.moveTo((20, 20))
                pen.lineTo((480, 20))
                pen.lineTo((480, 680))
                pen.lineTo((20, 680))
                pen.closePath()
        outlines[name] = pen.glyph()
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({name: (600, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Fixture", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    buffer = BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def _cid_pdf(path, text, *, square=False, triangle=False):
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        desc = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.FontDescriptor,
                FontName=pikepdf.Name.Fixture,
                FontFile2=pdf.make_stream(_program(square=square, triangle=triangle)),
            )
        )
        cid = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.CIDFontType2,
                FontDescriptor=desc,
                CIDToGIDMap=pikepdf.Name.Identity,
                W=pikepdf.Array([0, pikepdf.Array([600])]),
            )
        )
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type0,
                BaseFont=pikepdf.Name.Fixture,
                Encoding=pikepdf.Name("/Identity-H"),
                DescendantFonts=pikepdf.Array([cid]),
                ToUnicode=pdf.make_stream(_render_tounicode_cmap({0: text}, 2)),
            )
        )
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.Contents = pdf.make_stream(b"BT /F1 12 Tf 50 50 Td <0000> Tj ET")
        pdf.save(path)


@pytest.mark.parametrize(
    ("text", "square", "triangle", "expected"),
    [
        ("\u2610", True, False, True),  # Real source glyph wins; no font replacement.
        ("\u2610", False, False, True),
        ("\uf0ff", False, False, True),
        ("A", False, False, False),  # A visible missing letter is not a checkbox.
        ("\u2610", False, True, False),
    ],
)
def test_notdef_repair_requires_real_glyph_or_box_evidence(
    tmp_path, monkeypatch, text, square, triangle, expected
):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _cid_pdf(source, text, square=square, triangle=triangle)
    changed, _, stats = _repair_pdf_font_dicts_sync(source, output)
    assert changed is expected
    assert stats["notdef_mappings_repaired"] == int(expected)
    if not expected:
        return
    with pikepdf.open(source) as before, pikepdf.open(output) as after:
        old = before.pages[0].Resources.Font.F1
        font = after.pages[0].Resources.Font.F1
        shown = next(
            i for i in pikepdf.parse_content_stream(after.pages[0]) if str(i.operator) == "TJ"
        )
        code = int.from_bytes(bytes(shown.operands[0][0]), "big")
        assert code > 0
        cid = font.DescendantFonts[0]
        gid = int.from_bytes(cid.CIDToGIDMap.read_bytes()[2 * code : 2 * code + 2], "big")
        assert gid > 0
        with TTFont(BytesIO(cid.FontDescriptor.FontFile2.read_bytes())) as program:
            assert program.getGlyphOrder()[gid] != ".notdef"
            assert program["glyf"][program.getGlyphOrder()[gid]].numberOfContours > 0
        if square:
            assert (
                old.DescendantFonts[0].FontDescriptor.FontFile2.read_bytes()
                == cid.FontDescriptor.FontFile2.read_bytes()
            )
    assert (
        _repair_pdf_font_dicts_sync(output, tmp_path / "again.pdf")[2]["notdef_mappings_repaired"]
        == 0
    )


def test_spacing_operands_are_never_character_bytes():
    operands = [pikepdf.Array([pikepdf.String(b"A\x00"), 120, -40, pikepdf.String(b"B")])]
    assert _raw_text_bytes("TJ", operands) == b"A\x00B"
    cleaned, count = _sanitize_text_showing_zero_bytes("TJ", operands)
    assert count == 1
    assert list(cleaned[0]) == [pikepdf.String(b"A"), 120, -40, pikepdf.String(b"B")]


@pytest.mark.parametrize("bad", ["\x00", "\ufeff", "\ufffe"])
def test_type3_unicode_repair_preserves_author_entries_and_removes_forbidden_values(tmp_path, bad):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type3,
                Encoding=pikepdf.Dictionary(
                    Differences=pikepdf.Array([65, pikepdf.Name.A, pikepdf.Name.B])
                ),
                ToUnicode=pdf.make_stream(
                    (
                        "1 beginbfchar\n<41> <"
                        + bad.encode("utf-16-be").hex()
                        + ">\nendbfchar\n1 beginbfchar\n<42> <03B2>\nendbfchar"
                    ).encode()
                ),
            )
        )
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.Contents = pdf.make_stream(b"BT /F1 12 Tf (AB) Tj ET")
        pdf.save(source)
    assert _repair_pdf_tounicode_sync(source, output)[0]
    with pikepdf.open(output) as pdf:
        assert _parse_tounicode_map_details(pdf.pages[0].Resources.Font.F1.ToUnicode) == (
            {65: "A", 66: "\u03b2"},
            0,
        )


def test_symbol_generation_rejects_forbidden_table_values(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.fonts.encoding._symbol_font_unicode_map",
        lambda *_: {
            1: "\x00",
            2: "\ufeff",
            3: "\ufffe",
            4: "\u2022",
        },
    )
    font = pikepdf.Dictionary(BaseFont=pikepdf.Name.Symbol)
    assert _simple_font_unicode_map(font, None) == {4: "\u2022"}


def test_font_resources_reach_named_widget_appearance_states():
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        font = pdf.make_indirect(pikepdf.Dictionary(BaseFont=pikepdf.Name.Helvetica))
        stream = pdf.make_stream(b"BT /F1 12 Tf (A) Tj ET")
        stream.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        states = pdf.make_indirect(pikepdf.Dictionary(Yes=stream, Off=stream))
        states.Cycle = states
        page.Annots = pikepdf.Array(
            [pdf.make_indirect(pikepdf.Dictionary(AP=pikepdf.Dictionary(N=states)))]
        )
        assert [f.objgen for f in _iter_pdf_fonts(pdf)] == [font.objgen]


@pytest.mark.parametrize("form", [False, True])
async def test_described_figure_does_not_swallow_live_labels(tmp_path, form):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    elements = _fixture(source, image=True, form=form)
    await tag_pdf(
        source,
        output,
        {"elements": [dict(e, page=0) for e in elements]},
        [{"figure_index": 0, "status": "approved", "text": "A donation banner."}],
    )
    assert "Support students with a donation" in screen_reader_text(output)
    with pikepdf.open(output) as pdf:
        figures = [s for s in _structs(pdf.Root.StructTreeRoot) if s.get("/S") == "/Figure"]
        assert figures and all(str(f.Alt) == "A donation banner." for f in figures)
    assert rendered(source) == rendered(output)


async def test_blank_white_path_page_is_artifact_without_undescribed_figure(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        page.Contents = pdf.make_stream(b"q 1 1 1 rg 0 0 612 792 re f Q")
        pdf.save(source)
    await tag_pdf(source, output, {"elements": []})
    with pikepdf.open(output) as pdf:
        assert not any(s.get("/S") == "/Figure" for s in _structs(pdf.Root.StructTreeRoot))
        assert b"/Artifact" in pdf.pages[0].Contents.read_bytes()
    assert rendered(source) == rendered(output)


def test_collapsed_contents_requires_context_and_printed_destinations():
    cells = [
        dict(
            row=0,
            col=col,
            text=" ".join(f"{col + 1}.{i} Section title {i + 10}" for i in range(1, 8)),
            bbox={"l": col * 200, "r": col * 200 + 180, "b": 100, "t": 700},
        )
        for col in range(2)
    ]
    table = dict(
        type="table",
        cells=cells,
        num_rows=1,
        num_cols=2,
        bbox={"l": 0, "r": 400, "b": 100, "t": 700},
    )
    assert _layout_table_plan(table, []) is None
    assert _layout_table_plan(table, [], contents_context=True)
    cells[0]["is_header"] = True
    assert _layout_table_plan(table, [], contents_context=True) is None


def test_directory_uses_row_headers_and_retains_data_table():
    cells = [
        dict(
            row=row,
            col=col,
            text=("Department name" if col % 2 == 0 else f"AC-2C{row + 10}"),
            bbox={"l": col * 100, "r": col * 100 + 90, "b": 500 - row * 20, "t": 510 - row * 20},
        )
        for row in range(3)
        for col in range(4)
    ]
    table = dict(type="table", cells=cells, num_rows=3, num_cols=4)
    _infer_table_headers([table], [], None)
    assert table["type"] == "table"
    assert all(bool(c.get("row_header")) == (c["col"] % 2 == 0) for c in cells)
    from app.pipeline.tagger_structure import StructTreeBuilder

    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        builder = StructTreeBuilder(pdf)
        builder.setup()
        structs = builder._ensure_fragmented_table(table)
        for row in range(3):
            for col in (1, 3):
                assert list(structs[row, col].A.Headers) == [structs[row, col - 1].ID]


async def test_tagging_describes_annotations_and_keeps_widget_objrs(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        page.Contents = pdf.make_stream(b"")
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Widget,
                Rect=[20, 20, 80, 40],
                FT=pikepdf.Name.Tx,
                T=pikepdf.String("Name"),
            )
        )
        highlight = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Highlight,
                Rect=[20, 60, 80, 80],
            )
        )
        page.Annots = pikepdf.Array([highlight, widget])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([widget]))
        pdf.save(source)
    await tag_pdf(source, output, {"elements": []})
    with pikepdf.open(output) as pdf:
        page = pdf.pages[0]
        assert str(page.Annots[0].Contents).strip()
        assert str(page.Annots[1].TU).strip()
        entries = list(pdf.Root.StructTreeRoot.ParentTree.Nums)
        parents = {int(entries[i]): entries[i + 1] for i in range(0, len(entries), 2)}
        for annotation, role in zip(page.Annots, ["/Annot", "/Form"], strict=True):
            node = parents[int(annotation.StructParent)]
            assert node.S == role
            assert node.K.Type == "/OBJR"
            assert node.K.Obj.objgen == annotation.objgen
        assert pdf.Root.AcroForm.Fields[0].objgen == page.Annots[1].objgen


def test_embedding_reaches_acroform_dr_and_named_appearances_with_liberation(tmp_path, monkeypatch):
    from app.pipeline.fonts import embedding, programs

    monkeypatch.setattr(
        programs,
        "_system_font_program",
        lambda name: (
            (_program(square=True), "LiberationSans-Regular")
            if name == "LiberationSans-Regular"
            else (None, None)
        ),
    )
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        fonts = [
            pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Font,
                    Subtype=pikepdf.Name.Type1,
                    BaseFont=pikepdf.Name.Helvetica,
                )
            )
            for _ in range(2)
        ]
        appearance = pdf.make_stream(b"BT /Helv 12 Tf (A) Tj ET")
        appearance.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(Helv=fonts[1]))
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Subtype=pikepdf.Name.Widget,
                AP=pikepdf.Dictionary(N=pikepdf.Dictionary(Yes=appearance)),
            )
        )
        page.Annots = pikepdf.Array([widget])
        pdf.Root.AcroForm = pikepdf.Dictionary(
            DR=pikepdf.Dictionary(Font=pikepdf.Dictionary(Helv=fonts[0])),
            Fields=pikepdf.Array([widget]),
        )
        pdf.save(source)
    changed, _, stats = embedding._embed_system_fonts_sync(source, output)
    assert changed and stats["fonts_embedded"] == 2
    with pikepdf.open(output) as pdf:
        assert all(
            isinstance(f.FontDescriptor.FontFile2, pikepdf.Stream) for f in _iter_pdf_fonts(pdf)
        )


@pytest.mark.parametrize("state", ["outside", "bfchar", "bfrange"])
@pytest.mark.parametrize(
    ("event", "expected"),
    [("beginbfchar", "bfchar"), ("beginbfrange", "bfrange"), ("end", "outside")],
)
def test_tounicode_parser_edges_are_explicit(state, event, expected):
    from app.pipeline.fonts.encoding import ToUnicodeMode, _tounicode_transition

    assert _tounicode_transition(ToUnicodeMode(state), event) == expected


def test_tounicode_parser_rejects_unknown_writes_but_reads_malformed_blocks():
    from app.pipeline.fonts.encoding import ToUnicodeMode, _tounicode_transition

    with pytest.raises(ValueError):
        _tounicode_transition(ToUnicodeMode.OUTSIDE, "unknown")
    with pikepdf.new() as pdf:
        stream = pdf.make_stream(
            b"endbfrange\n1 beginbfchar\n<41> <0041>\n1 beginbfrange\n<42> <43> <0042>\nendbfchar\n1 beginbfchar\n<44> <0044>"
        )
        assert _parse_tounicode_map_details(stream) == ({65: "A", 66: "B", 67: "C", 68: "D"}, 0)


def test_type3_opaque_charproc_does_not_inherit_a_guessed_base_character():
    font = pikepdf.Dictionary(
        Subtype=pikepdf.Name.Type3,
        Encoding=pikepdf.Dictionary(
            BaseEncoding=pikepdf.Name.WinAnsiEncoding,
            Differences=pikepdf.Array([65, pikepdf.Name.g42]),
        ),
    )
    mapping = _simple_font_unicode_map(font, None)
    assert 65 not in mapping
    assert mapping[66] == "B"
