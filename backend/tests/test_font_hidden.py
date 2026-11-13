"""Fonts whose text no reader can see get an empty-glyph program; seen text never does."""

from io import BytesIO
from types import SimpleNamespace

import pikepdf
import pytest
from fontTools.ttLib import TTFont

from app.pipeline.fonts import lanes, programs
from app.pipeline.fonts.embedding import _embed_system_fonts_sync
from app.pipeline.fonts.hidden import simple_font_uses
from app.pipeline.fonts.rules import FONT_LANE_EMBED
from tests.pdf_fixtures import rendered
from tests.test_font_substitution import _install_test_font

TOUNICODE = b"""/CIDInit /ProcSet findresource begin 12 dict begin begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Test def /CMapType 2 def
1 begincodespacerange <00> <FF> endcodespacerange
2 beginbfchar <01> <0020> <02> <0045> endbfchar
endcmap CMapName currentdict /CMap defineresource pop end end"""
# Recognized text, then the page scan over the whole 200 x 200 page.
TEXT = b"BT /F1 12 Tf 20 100 Td <010201> Tj ET"
SCAN = b"q 200 0 0 200 0 0 cm /Im0 Do Q"


@pytest.fixture(autouse=True)
def _no_system_fonts(tmp_path, monkeypatch):
    """No installed program: like AppleMyungjo in the runner image."""
    empty = tmp_path / "no-fonts"
    empty.mkdir()
    monkeypatch.setattr(programs, "SYSTEM_FONT_DIRS", (empty,))
    programs._system_font_index.cache_clear()
    programs._system_font_program.cache_clear()
    yield
    programs._system_font_index.cache_clear()
    programs._system_font_program.cache_clear()


def _scan_pdf(
    path,
    content: bytes = TEXT + b" " + SCAN,
    *,
    name: str = "AppleMyungjo",
    differences=(1, "/space", "/E"),
    extra_resources: dict | None = None,
):
    """A FineReader-style page: an unembedded TrueType font under a page image."""
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 200))
        descriptor = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name(f"/{name}"), Flags=32,
            FontBBox=[0, -250, 1000, 1000], Ascent=1000, Descent=-250, CapHeight=700,
            ItalicAngle=0, StemV=80,
        ))
        font = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType,
            BaseFont=pikepdf.Name(f"/{name}"), FirstChar=1, LastChar=2, Widths=[250, 611],
            Encoding=pikepdf.Dictionary(
                Type=pikepdf.Name.Encoding, BaseEncoding=pikepdf.Name.WinAnsiEncoding,
                Differences=[
                    pikepdf.Name(item) if isinstance(item, str) else item
                    for item in differences
                ],
            ),
            FontDescriptor=descriptor, ToUnicode=pdf.make_stream(TOUNICODE),
        ))
        scan = pdf.make_stream(
            b"\x80", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=1, Height=1,
            ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8,
        )
        page.Resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(F1=font),
            XObject=pikepdf.Dictionary(Im0=scan),
            **(extra_resources or {}),
        )
        page.Contents = pdf.make_stream(content)
        pdf.save(path)
    return path


def _font(path):
    with pikepdf.open(path) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        descriptor = font.FontDescriptor
        program = descriptor.get("/FontFile2")
        return SimpleNamespace(
            program=program.read_bytes() if program is not None else None,
            encoding=font.Encoding.unparse(),
            tounicode=font.ToUnicode.read_bytes(),
            widths=[float(width) for width in font.Widths],
            contents=pdf.pages[0].Contents.read_bytes(),
        )


def test_text_under_the_page_scan_gets_an_empty_glyph_program(tmp_path):
    source = _scan_pdf(tmp_path / "scan.pdf")
    output = tmp_path / "out.pdf"

    ok, _, stats = _embed_system_fonts_sync(source, output)

    assert ok and stats["fonts_glyphless"] == 1 and stats["fonts_missing"] == 0
    before, after = _font(source), _font(output)
    # Text, encoding, widths and content are the document's own.
    assert (after.encoding, after.tounicode, after.widths, after.contents) == (
        before.encoding, before.tounicode, before.widths, before.contents,
    )
    with TTFont(BytesIO(after.program)) as program:
        cmap = program["cmap"].getcmap(3, 1).cmap
        assert {code: program["hmtx"][cmap[code]][0] for code in (0x20, 0x45)} == {
            0x20: 250, 0x45: 611,
        }
        assert all(program["glyf"][name].numberOfContours == 0 for name in program.getGlyphOrder())
    assert rendered(output) == rendered(source)


@pytest.mark.parametrize(
    ("content", "case"),
    [
        pytest.param(SCAN + b" " + TEXT, None, id="text-over-the-scan"),
        pytest.param(TEXT + b" " + SCAN, "soft-mask", id="soft-masked-scan"),
        pytest.param(TEXT + b" " + SCAN, "color-key", id="color-keyed-scan"),
        pytest.param(TEXT + b" q 200 0 0 100 0 0 cm /Im0 Do Q", None, id="half-page-scan"),
        pytest.param(TEXT + b" q 0 0 100 200 re W n " + SCAN + b" Q", None, id="clipped"),
        pytest.param(TEXT + b" q /Alpha gs " + SCAN + b" Q", None, id="translucent"),
        pytest.param(TEXT + b" q /Multiply gs " + SCAN + b" Q", None, id="blended"),
        pytest.param(TEXT + b" q /Overprint gs " + SCAN + b" Q", None, id="overprinted"),
        pytest.param(TEXT + b" /OC /L1 BDC " + SCAN + b" EMC", None, id="optional-content"),
        pytest.param(TEXT + b" q 200 0 0 200 0 0 cm /Fm0 Do Q", "form", id="scan-in-a-form"),
    ],
)
def test_text_a_reader_may_see_keeps_no_program(tmp_path, content, case):
    states = pikepdf.Dictionary(
        Alpha=pikepdf.Dictionary(ca=0.5),
        Multiply=pikepdf.Dictionary(BM=pikepdf.Name.Multiply),
        Overprint=pikepdf.Dictionary(op=True, OPM=1),
    )
    source = _scan_pdf(tmp_path / "scan.pdf", content, extra_resources={"ExtGState": states})
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        scan = page.Resources.XObject.Im0
        if case == "soft-mask":
            scan.SMask = pdf.make_stream(
                b"\x80", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=1,
                Height=1, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8,
            )
        elif case == "color-key":
            scan.Mask = pikepdf.Array([0, 200])
        elif case == "form":
            page.Resources.XObject.Fm0 = pdf.make_stream(
                b"/Im0 Do", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form,
                BBox=[0, 0, 1, 1], Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=scan)),
            )
        pdf.save(source)

    ok, message, stats = _embed_system_fonts_sync(source, tmp_path / "out.pdf")

    assert not ok and stats["fonts_glyphless"] == 0, message


def test_a_scan_a_point_short_of_the_page_edge_still_covers_it(tmp_path):
    # Acrobat Paper Capture placed this scan 0.006 pt short of the page top.
    source = _scan_pdf(tmp_path / "scan.pdf", TEXT + b" q 200 0 0 199.994 0 0 cm /Im0 Do Q")

    ok, _, stats = _embed_system_fonts_sync(source, tmp_path / "out.pdf")

    assert ok and stats["fonts_glyphless"] == 1


@pytest.mark.parametrize(
    ("content", "glyphless"),
    [
        (b"BT 3 Tr /F1 12 Tf 20 100 Td <0102> Tj ET", 1),
        (b"BT /F1 12 Tf 20 100 Td <0101> Tj ET", 1),
        (b"BT /F1 12 Tf 20 100 Td <0102> Tj ET", 0),
    ],
    ids=["invisible-mode", "visible-spaces-only", "visible-letter"],
)
def test_invisible_mode_or_space_only_text_needs_no_cover(tmp_path, content, glyphless):
    source = _scan_pdf(tmp_path / "page.pdf", content)

    _, _, stats = _embed_system_fonts_sync(source, tmp_path / "out.pdf")

    assert stats["fonts_glyphless"] == glyphless


def test_a_substitute_missing_a_hidden_glyph_gives_way(tmp_path, monkeypatch):
    # FineReader recognized a check mark and named it in Times New Roman,
    # which has no such glyph; neither does its metric-compatible substitute.
    _install_test_font(tmp_path, monkeypatch, family="LiberationSerif")
    hidden = _scan_pdf(
        tmp_path / "hidden.pdf", name="TimesNewRomanPSMT", differences=(1, "/A", "/checkmark"),
    )
    seen = _scan_pdf(
        tmp_path / "seen.pdf", SCAN + b" " + TEXT,
        name="TimesNewRomanPSMT", differences=(1, "/A", "/checkmark"),
    )
    covered = _scan_pdf(
        tmp_path / "covered.pdf", name="TimesNewRomanPSMT", differences=(1, "/A", "/B"),
    )

    results = {
        path.stem: _embed_system_fonts_sync(path, tmp_path / f"{path.stem}-out.pdf")[2]
        for path in (hidden, seen, covered)
    }

    assert results["hidden"]["fonts_glyphless"] == 1
    # Visible text keeps the substitute, and a substitute that draws every
    # hidden code is kept too.
    assert results["seen"]["fonts_glyphless"] == 0 and results["seen"]["fonts_embedded"] == 1
    assert results["covered"]["fonts_glyphless"] == 0
    assert results["covered"]["fonts_embedded"] == 1


def test_field_default_fonts_are_never_hidden(tmp_path):
    source = _scan_pdf(tmp_path / "scan.pdf")
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        pdf.Root.AcroForm = pikepdf.Dictionary(
            Fields=[], DR=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        )
        pdf.save(source)

    with pikepdf.open(source) as pdf:
        uses = simple_font_uses(pdf)
        font = pdf.pages[0].Resources.Font.F1
        assert uses.unseen_codes(font) is None and uses.shown_codes(font) is None


def _courier_pdf(path, shown: bytes):
    """Standard Courier whose Differences name the control codes `NUL` ... `US`."""
    controls = "NUL SOH STX ETX EOT ENQ ACK BEL BS HT LF VT FF CR SO SI".split()
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 200))
        font = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Courier,
            Encoding=pikepdf.Dictionary(
                Type=pikepdf.Name.Encoding,
                Differences=[0, *(pikepdf.Name(f"/{name}") for name in controls)],
            ),
        ))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.Contents = pdf.make_stream(b"BT /F1 12 Tf 20 100 Td <" + shown.hex().encode() + b"> Tj ET")
        pdf.save(path)
    return path


@pytest.mark.parametrize(
    ("shown", "base"),
    [(b"AB", "/WinAnsiEncoding"), (b"A\x00", "/StandardEncoding")],
    ids=["control-codes-unshown", "control-code-shown"],
)
def test_unshown_non_agl_names_do_not_block_the_truetype_restatement(
    tmp_path, monkeypatch, shown, base
):
    # QuarkXPress via LaserWriter 8 names codes 0-31 NUL ... US. No content
    # shows them, so they cannot keep Courier's substitute off WinAnsiEncoding.
    _install_test_font(tmp_path, monkeypatch, family="LiberationMono")
    source = _courier_pdf(tmp_path / "courier.pdf", shown)
    output = tmp_path / "out.pdf"

    ok, _, _ = _embed_system_fonts_sync(source, output)

    assert ok
    with pikepdf.open(output) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        assert font.Subtype == pikepdf.Name.TrueType
        assert str(font.Encoding.BaseEncoding) == base
        names = {str(item) for item in font.Encoding.get("/Differences", []) if isinstance(item, pikepdf.Name)}
        if base == "/WinAnsiEncoding":
            assert not names & {"/NUL", "/SOH", "/STX"}


@pytest.mark.asyncio
async def test_embed_lane_keeps_unseen_programs_without_ghostscript(tmp_path, monkeypatch):
    """No local candidate, but the only unembedded font is unseen: no retag, no Ghostscript."""
    tagged = _scan_pdf(tmp_path / "tagged.pdf")

    async def no_ghostscript(*args, **kwargs):
        raise AssertionError("Ghostscript should not rewrite a document the local pass completed")

    async def fake_validate(pdf_path, verapdf_path, flavour, **kwargs):
        return SimpleNamespace(compliant=True, violations=[], raw_report={})

    monkeypatch.setattr(lanes, "create_job_dir", lambda job_id: tmp_path)
    monkeypatch.setattr(lanes, "_rewrite_pdf_with_ghostscript_embed", no_ghostscript)
    monkeypatch.setattr(lanes, "validate_pdf", fake_validate)

    result = await lanes._attempt_font_lane(
        job_id="job", job=SimpleNamespace(original_filename="scan.pdf", ocr_language=None),
        settings=SimpleNamespace(
            verapdf_path="verapdf", verapdf_flavour="ua1", subprocess_timeout_validation=60,
            subprocess_timeout_ghostscript=60,
        ),
        working_pdf=tagged, tagged_pdf=tagged, structure_json={}, reviewed_alts=[],
        lane=FONT_LANE_EMBED, current_tagging_result=SimpleNamespace(),
    )

    assert result["success"] and result["requires_retag"] is False
    assert result["details"]["fonts_glyphless"] == 1
