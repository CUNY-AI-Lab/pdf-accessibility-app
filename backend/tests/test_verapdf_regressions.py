"""PDF/UA repairs preserve placement and operate on emitted, used content."""

from io import BytesIO

import pikepdf
import pytest
from fontTools.ttLib import TTFont
from fontTools.ttLib.scaleUpem import scale_upem

from app.pipeline.fonts import programs
from app.pipeline.fonts.diagnostics import (
    _inspect_font_diagnostics,
    _unicode_repair_gate_from_diagnostics,
)
from app.pipeline.fonts.encoding import (
    _parse_tounicode_map,
    _render_tounicode_cmap,
    _simple_font_unicode_map,
)
from app.pipeline.fonts.repair import _repair_pdf_font_dicts_sync
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode_sync
from app.pipeline.tagger import tag_pdf
from app.pipeline.validator import ValidationResult, Violation
from app.services.structure_text import screen_reader_text
from app.services.validation_compare import is_better_validation
from tests.pdf_fixtures import helvetica, rendered
from tests.test_font_substitution import _install_test_font, _text_contract, _unembedded_pdf


def _embedded_mismatch(tmp_path, monkeypatch, *, conflict=False):
    _install_test_font(tmp_path, monkeypatch)
    try:
        raw, _ = programs._system_font_program("LiberationSans-Regular")
        with TTFont(BytesIO(raw)) as font:
            scale_upem(font, 2048)
            font["hmtx"].metrics["A"] = (562, 17)  # 274.414 PDF units, vs /Widths 272.
            if conflict:
                for cmap in font["cmap"].tables:
                    if 66 in cmap.cmap:
                        cmap.cmap[66] = "A"
            buffer = BytesIO()
            font.save(buffer)
        before = tmp_path / "before.pdf"
        _unembedded_pdf(before)
        with pikepdf.open(before, allow_overwriting_input=True) as pdf:
            f = pdf.pages[0].Resources.Font.F1
            f.Widths[65 - 32] = 272
            f.FontDescriptor.FontFile2 = pdf.make_stream(buffer.getvalue())
            pdf.save(before)
        return before
    finally:
        programs._system_font_index.cache_clear()
        programs._system_font_program.cache_clear()


def test_truetype_width_repair_changes_program_not_pdf_positioning(tmp_path, monkeypatch):
    before = _embedded_mismatch(tmp_path, monkeypatch)
    after = tmp_path / "after.pdf"
    ok, message, stats = _repair_pdf_font_dicts_sync(before, after)
    assert ok, message
    assert stats["truetype_widths_aligned"] > 0
    assert _text_contract(before) == _text_contract(after)
    assert screen_reader_text(before) == screen_reader_text(after)
    assert rendered(before) == rendered(after)
    with pikepdf.open(before) as a, pikepdf.open(after) as b:
        with (
            TTFont(
                BytesIO(a.pages[0].Resources.Font.F1.FontDescriptor.FontFile2.read_bytes())
            ) as old,
            TTFont(
                BytesIO(b.pages[0].Resources.Font.F1.FontDescriptor.FontFile2.read_bytes())
            ) as new,
        ):
            assert new["hmtx"].metrics["A"] == (557, 17)
            assert abs(new["hmtx"].metrics["A"][0] * 1000 / new["head"].unitsPerEm - 272) < 0.5
            assert old.getTableData("glyf") == new.getTableData("glyf")
    assert not _repair_pdf_font_dicts_sync(after, tmp_path / "again.pdf")[0]


def test_width_repair_does_not_choose_between_conflicting_glyph_aliases(tmp_path, monkeypatch):
    before = _embedded_mismatch(tmp_path, monkeypatch, conflict=True)
    after = tmp_path / "after.pdf"
    ok, message, stats = _repair_pdf_font_dicts_sync(before, after)
    assert ok, message
    assert stats["truetype_width_conflicts"] == 1
    with pikepdf.open(after) as pdf:
        with TTFont(
            BytesIO(pdf.pages[0].Resources.Font.F1.FontDescriptor.FontFile2.read_bytes())
        ) as font:
            assert font["hmtx"].metrics["A"] == (562, 17)


@pytest.mark.parametrize(
    ("name", "code", "text"),
    [
        ("Wingdings-Regular", 33, "\U0001f589"),
        ("Wingdings 2", 33, "\U0001f58a"),
        ("Wingdings3", 33, "\u2b60"),
        ("Symbol", 65, "\u0391"),
        ("ZapfDingbats", 33, "\u2701"),
    ],
)
def test_standard_symbol_correspondences(name, code, text):
    font = pikepdf.Dictionary(BaseFont=pikepdf.Name("/" + name), Subtype=pikepdf.Name.Type1)
    assert _simple_font_unicode_map(font, None)[code] == text


@pytest.mark.parametrize("nested", [False, True])
def test_symbol_repair_uses_remapped_glyph_names_and_only_fills_holes(
    tmp_path, monkeypatch, nested
):
    _install_test_font(tmp_path, monkeypatch)
    try:
        raw, _ = programs._system_font_program("LiberationSans-Regular")
        with TTFont(BytesIO(raw)) as font:
            # Recreate the real subset: PDF code 33 is square4, not pencil.
            font["post"].mapping = {"A": "square4"}
            for cmap in font["cmap"].tables:
                cmap.cmap = {33: "A", 34: "B"}
            buffer = BytesIO()
            font.save(buffer)
        source, target = tmp_path / "source.pdf", tmp_path / "target.pdf"
        _unembedded_pdf(source, "ABCDEF+Wingdings-Regular")
        with pikepdf.open(source, allow_overwriting_input=True) as pdf:
            page = pdf.pages[0]
            f = page.Resources.Font.F1
            del f["/Encoding"]
            f.FontDescriptor.Flags = 4
            f.FontDescriptor.FontFile2 = pdf.make_stream(buffer.getvalue())
            f.ToUnicode = pdf.make_stream(_render_tounicode_cmap({34: "author", 99: "unused"}, 1))
            content = b'/P << /MCID 0 >> BDC BT /F1 12 Tf 72 720 Td (!") Tj ET EMC'
            if nested:
                form = pdf.make_stream(content)
                form.Subtype = pikepdf.Name.Form
                form.Type = pikepdf.Name.XObject
                form.BBox = [0, 0, 612, 792]
                form.Resources = page.Resources
                page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Form=form))
                page.Contents = pdf.make_stream(b"/Form Do")
            else:
                page.Contents = pdf.make_stream(content)
            pdf.save(source)
        violations = [Violation("ISO 14289-1:2014-7.21.7-1", "Missing Unicode", "error")]
        diagnostic = _inspect_font_diagnostics(source, violations)
        gate = _unicode_repair_gate_from_diagnostics(diagnostic, violations=violations)
        assert gate["safe_candidate_count"] == 1
        ok, message, stats = _repair_pdf_tounicode_sync(source, target)
        assert ok, message
        assert stats["mappings_overridden"] == 0
        assert stats["stale_entries_removed"] == 0
        with pikepdf.open(target) as pdf:
            page = pdf.pages[0]
            owner = page.Resources.XObject.Form if nested else page
            font = owner.Resources.Font.F1
            assert _parse_tounicode_map(font.ToUnicode) == {
                33: "\u25aa",
                34: "author",
                99: "unused",
            }
            assert (
                owner.Contents.read_bytes() == content
                if not nested
                else owner.read_bytes() == content
            )
        assert not _repair_pdf_tounicode_sync(target, tmp_path / "again.pdf")[0]
    finally:
        programs._system_font_index.cache_clear()
        programs._system_font_program.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_no_layout_or_extracted_text_still_marks_painted_content(tmp_path, nested):
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        content = b"0 g 20 20 400 400 re f 30 30 m 500 500 l S"
        if nested:
            form = pdf.make_stream(content)
            form.Subtype = pikepdf.Name.Form
            form.Type = pikepdf.Name.XObject
            form.BBox = [0, 0, 600, 800]
            page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Form=form))
            page.Contents = pdf.make_stream(b"/Form Do")
        else:
            page.Contents = pdf.make_stream(content)
        pdf.save(before)
    await tag_pdf(before, after, {"elements": []})
    assert rendered(before) == rendered(after)
    with pikepdf.open(after) as pdf:
        page = pdf.pages[0]
        owner = page.Resources.XObject.Form if nested else page
        figure = pdf.Root.StructTreeRoot.K[0].K[0]
        assert figure.S == "/Figure"
        assert "/Alt" not in figure
        assert b"/Artifact" not in page.Contents.read_bytes()
        # A page's Figure BDC also owns paint executed by its Form Do.
        depth, paints = (1 if nested else 0), 0
        for op in pikepdf.parse_content_stream(owner):
            if str(op.operator) in ("BMC", "BDC"):
                depth += 1
            elif str(op.operator) == "EMC":
                depth -= 1
            elif str(op.operator) in ("f", "S"):
                assert depth > 0
                paints += 1
        assert paints == 2
        assert depth == (1 if nested else 0)


@pytest.mark.asyncio
async def test_unowned_heading_does_not_leave_gap_in_emitted_outline(tmp_path):
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
        page.Contents = pdf.make_stream(
            b"BT /F1 12 Tf 72 720 Td (Title) Tj 0 -50 Td (Section) Tj 0 -100 Td (Deep) Tj 0 -50 Td (Sibling) Tj ET"
        )
        pdf.save(before)
    elements = [
        dict(
            type="heading",
            level=level,
            page=0,
            text=text,
            bbox=dict(l=70, r=220, b=y - 3, t=y + 14),
            reading_order=i,
        )
        for i, (level, text, y) in enumerate(
            [
                (1, "Title", 720),
                (2, "Section", 670),
                (3, "Unowned", 620),
                (4, "Deep", 570),
                (4, "Sibling", 520),
            ]
        )
    ]
    await tag_pdf(before, after, {"elements": elements})
    assert "Unowned" not in screen_reader_text(after)
    assert "Deep" in screen_reader_text(after)
    assert rendered(before) == rendered(after)
    with pikepdf.open(after) as pdf:
        assert [str(k.S) for k in pdf.Root.StructTreeRoot.K[0].K] == ["/H1", "/H2", "/H3", "/H3"]
        assert [item.title for item in pdf.open_outline().root] == ["Title"]


def test_partial_embedding_improvement_is_kept_with_same_failed_rule():
    def report(count, other_count=2):
        return ValidationResult(
            False,
            [
                Violation("7.21.4.1-1", "Embed fonts", "error", count=count),
                Violation("7.3-1", "Figure description", "error", count=other_count),
            ],
        )

    assert is_better_validation(report(8), report(22))
    assert not is_better_validation(report(22), report(8))
    assert not is_better_validation(report(8, 3), report(22))
    assert not is_better_validation(report(22), report(22))


@pytest.mark.asyncio
async def test_pipeline_keeps_partial_local_embedding_while_century_gothic_remains(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from app.config import Settings
    from app.pipeline import orchestrator
    from app.pipeline.fonts import embedding, lanes
    from app.pipeline.fonts.result import FontRemediation

    _install_test_font(tmp_path, monkeypatch)
    try:
        source = tmp_path / "source.pdf"
        _unembedded_pdf(source)
        with pikepdf.open(source, allow_overwriting_input=True) as pdf:
            pdf.pages[0].Resources.Font.F1.Encoding = pikepdf.Name.MacRomanEncoding
            font = pikepdf.Dictionary(pdf.pages[0].Resources.Font.F1)
            font.BaseFont = pikepdf.Name.CenturyGothic
            font.FontDescriptor = pikepdf.Dictionary(font.FontDescriptor)
            font.FontDescriptor.FontName = pikepdf.Name.CenturyGothic
            pdf.pages[0].Resources.Font.F2 = pdf.make_indirect(font)
            pdf.pages[0].Contents = pdf.make_stream(
                b"/P << /MCID 0 >> BDC BT /F1 12 Tf 72 720 Td (AB) Tj /F2 12 Tf (A) Tj ET EMC"
            )
            pdf.save(source)
        baseline = ValidationResult(
            False,
            [Violation("ISO 14289-1:2014-7.21.4.1-1", "Fonts not embedded", "error", count=2)],
        )
        partial = ValidationResult(
            False,
            [
                Violation(
                    "ISO 14289-1:2014-7.21.4.1-1", "Century Gothic not embedded", "error", count=1
                )
            ],
        )
        monkeypatch.setattr(lanes, "create_job_dir", lambda _: tmp_path)

        async def validate(**kwargs):
            with pikepdf.open(kwargs["pdf_path"]) as pdf:
                assert "/FontFile2" in pdf.pages[0].Resources.Font.F1.FontDescriptor
                assert "/FontFile2" not in pdf.pages[0].Resources.Font.F2.FontDescriptor
            return partial

        async def unexpected(*args, **kwargs):
            pytest.fail("Local partial embedding should not rewrite or re-tag the PDF")

        monkeypatch.setattr(lanes, "validate_pdf", validate)
        monkeypatch.setattr(lanes, "_rewrite_pdf_with_ghostscript_embed", unexpected)
        monkeypatch.setattr(lanes, "tag_pdf", unexpected)
        tag_result = SimpleNamespace(output_path=source)
        remediation = FontRemediation()
        result = await orchestrator._select_font_lanes(
            job=SimpleNamespace(id="test", input_path=str(source), classification="digital"),
            settings=Settings(),
            working_pdf=source,
            structure_json={},
            reviewed_alts=[],
            tagging_result=tag_result,
            validation=baseline,
            font_remediation=remediation,
        )
        assert result[1] is partial
        assert result[2] != source
        assert remediation.applied
        assert remediation.selected_lanes == ["embed_fonts"]
        assert remediation.lane_results[0]["improved"] is True
        assert _text_contract(source) == _text_contract(result[2])
        assert embedding._embed_system_fonts_sync(result[2], tmp_path / "again.pdf")[0] is False
    finally:
        programs._system_font_index.cache_clear()
        programs._system_font_program.cache_clear()


def test_embedding_aligns_each_use_of_a_shared_descriptor(tmp_path, monkeypatch):
    from app.pipeline.fonts.embedding import _embed_system_fonts_sync

    _install_test_font(tmp_path, monkeypatch)
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    _unembedded_pdf(before)
    with pikepdf.open(before, allow_overwriting_input=True) as pdf:
        first = pdf.pages[0].Resources.Font.F1
        second = pikepdf.Dictionary(first)
        second.Widths = pikepdf.Array(first.Widths)
        first.Widths[65 - 32] = 400
        second.Widths[65 - 32] = 700
        pdf.pages[0].Resources.Font.F2 = pdf.make_indirect(second)
        pdf.pages[0].Contents = pdf.make_stream(
            pdf.pages[0].Contents.read_bytes() + b" BT /F2 12 Tf 72 680 Td (AB) Tj ET"
        )
        pdf.save(before)
    text = screen_reader_text(before)
    ok, message, stats = _embed_system_fonts_sync(before, after)
    assert ok, message
    assert stats["fonts_embedded"] == 2
    assert _text_contract(before) == _text_contract(after)
    assert screen_reader_text(after) == text
    with pikepdf.open(before) as old, pikepdf.open(after) as pdf:
        fonts = pdf.pages[0].Resources.Font
        assert fonts.F1.FontDescriptor.objgen != fonts.F2.FontDescriptor.objgen
        for name, advance in (("/F1", 400), ("/F2", 700)):
            f = fonts[name]
            assert tuple(f.Widths) == tuple(old.pages[0].Resources.Font[name].Widths)
            with TTFont(BytesIO(f.FontDescriptor.FontFile2.read_bytes())) as program:
                assert program["hmtx"].metrics["A"][0] == advance
    # The width pass has no residual work, including on the second use.
    assert not _repair_pdf_font_dicts_sync(after, tmp_path / "again.pdf")[0]
