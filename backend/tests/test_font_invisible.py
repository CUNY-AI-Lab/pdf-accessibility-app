"""Invisible OCR text keeps its codes and stops selecting .notdef."""

from io import BytesIO

import pikepdf
import pypdfium2
import pytest
from fontTools.cffLib import FDArrayIndex, FDSelect, FontDict
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from app.pipeline.fonts import invisible
from app.pipeline.fonts.encoding import _render_tounicode_cmap
from app.pipeline.fonts.invisible import repair_invisible_fonts
from app.pipeline.fonts.notdef_content import repair_notdef_content
from app.pipeline.fonts.repair import _repair_pdf_font_dicts_sync
from tests.pdf_fixtures import rendered
from tests.test_font_followup import _blank_font_document

CODES = {0x61: "a", 0x6E: "n", 0x25A1: "□"}
SHOWN = b"BT 3 Tr /F1 12 Tf 50 700 Td <0061006E> Tj [<25A1> -200 <0061>] TJ ET"


def _notdef_only_cff(*, cid_keyed: bool) -> bytes:
    """Like Acrobat's HiddenHorzOCR program: .notdef is its only glyph."""
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder([".notdef"])
    builder.setupCFF(
        "HiddenFixture", {}, {".notdef": T2CharStringPen(1000, None).getCharString()}, {}
    )
    cff = builder.font["CFF "].cff
    if cid_keyed:
        top = cff.topDictIndex[0]
        top.ROS = ("Adobe", "Identity", 0)
        top.CIDCount = 65536
        fd = FontDict()
        setattr(fd, "Private", top.Private)  # FontDict declares its keys at runtime.
        top.FDArray = FDArrayIndex()
        top.FDArray.append(fd)
        top.FDSelect = FDSelect()
        top.FDSelect.format = 3
        top.FDSelect.gidArray = [0]
        del top.Private
    buffer = BytesIO()
    cff.compile(buffer, builder.font)
    return buffer.getvalue()


def _notdef_only_truetype() -> bytes:
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef"])
    builder.setupCharacterMap({})
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph()})
    builder.setupHorizontalMetrics({".notdef": (1000, 0)})
    builder.setupHorizontalHeader(ascent=1000, descent=-250)
    builder.setupNameTable({"familyName": "HiddenFixture", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    buffer = BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def _ocr_font(pdf, program="cid"):
    if program == "truetype":
        data = _notdef_only_truetype()
        file_key, subtype = "/FontFile2", pikepdf.Name.CIDFontType2
        stream = pdf.make_stream(data, Length1=len(data))
    else:
        data = _notdef_only_cff(cid_keyed=program == "cid")
        file_key, subtype = "/FontFile3", pikepdf.Name.CIDFontType0
        stream = pdf.make_stream(
            data, Subtype=pikepdf.Name("/CIDFontType0C" if program == "cid" else "/Type1C")
        )
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor,
        FontName=pikepdf.Name.HiddenHorzOCR,
        Flags=4,
        FontBBox=[0, -250, 1000, 1000],
        ItalicAngle=0,
        Ascent=1000,
        Descent=-250,
        CapHeight=1000,
        StemV=100,
        CIDSet=pdf.make_stream(b"\x80"),
    )
    descriptor[file_key] = stream
    cid_font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=subtype,
        BaseFont=pikepdf.Name.HiddenHorzOCR,
        CIDSystemInfo=pikepdf.Dictionary(
            Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0
        ),
        FontDescriptor=pdf.make_indirect(descriptor),
        DW=1000,
        W=[0x61, [500], 0x6E, [600]],
    )
    if program == "truetype":
        cid_font.CIDToGIDMap = pikepdf.Name.Identity
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type0,
            BaseFont=pikepdf.Name.HiddenHorzOCR,
            Encoding=pikepdf.Name("/Identity-H"),
            DescendantFonts=[pdf.make_indirect(cid_font)],
            ToUnicode=pdf.make_stream(_render_tounicode_cmap(CODES, 2)),
        )
    )


def _ocr_pdf(
    path,
    *,
    content=SHOWN,
    forms=(),
    appearance=None,
    default_resources=False,
    program="cid",
    second_font=False,
):
    """A page showing OCR codes; Fm0 invokes Fm1 and so on."""
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 800))
        fonts = pikepdf.Dictionary(F1=_ocr_font(pdf, program))
        if second_font:
            fonts.F2 = _ocr_font(pdf, program)
        child = None
        for index in reversed(range(len(forms))):
            form = pdf.make_stream(forms[index], Type=pikepdf.Name.XObject)
            form.Subtype = pikepdf.Name.Form
            form.BBox = [0, 0, 300, 800]
            form.Resources = pikepdf.Dictionary(Font=fonts)
            if child is not None:
                form.Resources.XObject = pikepdf.Dictionary(Fm1=child)
            child = form
        page.Resources = pikepdf.Dictionary(Font=fonts)
        if child is not None:
            page.Resources.XObject = pikepdf.Dictionary(Fm0=child)
        page.Contents = pdf.make_stream(content)
        if appearance is not None:
            stream = pdf.make_stream(appearance, Type=pikepdf.Name.XObject)
            stream.Subtype = pikepdf.Name.Form
            stream.BBox = [0, 0, 100, 20]
            stream.Resources = pikepdf.Dictionary(Font=fonts)
            page.Annots = [
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.Square,
                        Rect=[50, 50, 150, 70],
                        AP=pikepdf.Dictionary(N=stream),
                    )
                )
            ]
        if default_resources:
            pdf.Root.AcroForm = pikepdf.Dictionary(
                Fields=[], DR=pikepdf.Dictionary(Font=pikepdf.Dictionary(OCR=fonts.F1))
            )
        pdf.save(path)


def _text(path):
    document = pypdfium2.PdfDocument(path)
    try:
        return document[0].get_textpage().get_text_range()
    finally:
        document.close()


def _glyphs(font):
    """Glyph ID and contour count for every code the fixture shows."""
    cid_font = font.DescendantFonts[0]
    mapping = cid_font.CIDToGIDMap.read_bytes()
    with TTFont(BytesIO(cid_font.FontDescriptor.FontFile2.read_bytes())) as program:
        order = program.getGlyphOrder()
        result = {}
        for code in CODES:
            gid = int.from_bytes(mapping[2 * code : 2 * code + 2], "big")
            contours = program["glyf"][order[gid]].numberOfContours if gid < len(order) else None
            result[code] = (gid, contours)
        return result


@pytest.mark.parametrize("program", ["cid", "type1c", "truetype"])
def test_invisible_only_font_shows_an_empty_glyph_for_every_used_code(tmp_path, program):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _ocr_pdf(source, program=program)
    with pikepdf.open(source) as pdf:
        old = pdf.pages[0].Resources.Font.F1
        old_cid = old.DescendantFonts[0]
        kept = {key: repr(old_cid[key]) for key in ("/BaseFont", "/CIDSystemInfo", "/W", "/DW")}
        unicode_map = old.ToUnicode.read_bytes()
        assert repair_invisible_fonts(pdf) == {"invisible_fonts_replaced": 1}
        assert repair_invisible_fonts(pdf) == {"invisible_fonts_replaced": 0}
        pdf.save(output)
    with pikepdf.open(output) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        cid_font = font.DescendantFonts[0]
        assert font.ToUnicode.read_bytes() == unicode_map
        assert font.Encoding == "/Identity-H"
        assert cid_font.Subtype == "/CIDFontType2"
        assert {key: repr(cid_font[key]) for key in kept} == kept
        descriptor = cid_font.FontDescriptor
        assert descriptor.FontName == "/HiddenHorzOCR"
        assert "/FontFile3" not in descriptor and "/CIDSet" not in descriptor
        assert all(gid > 0 and contours == 0 for gid, contours in _glyphs(font).values())
    assert _text(source) == _text(output) != ""
    assert rendered(source) == rendered(output)


@pytest.mark.parametrize(
    ("content", "forms", "appearance", "default_resources", "replaced"),
    [
        pytest.param(
            b"3 Tr BT /F1 12 Tf <0061> Tj ET BT /F1 12 Tf <006E> Tj ET",
            (),
            None,
            False,
            True,
            id="mode-outlives-text-object",
        ),
        pytest.param(
            b"q 3 Tr BT /F1 12 Tf <0061> Tj ET Q BT /F1 12 Tf <006E> Tj ET",
            (),
            None,
            False,
            False,
            id="restore-paints",
        ),
        pytest.param(
            b"3 Tr /Fm0 Do", (b"BT /F1 12 Tf <0061> Tj ET",), None, False, True, id="form-inherits"
        ),
        pytest.param(
            b"q BT 3 Tr /F1 12 Tf <0061> Tj ET Q /Fm0 Do",
            (b"BT /F1 12 Tf <006E> Tj ET",),
            None,
            False,
            False,
            id="form-after-restore-paints",
        ),
        pytest.param(
            b"3 Tr /Fm0 Do",
            (b"q /Fm1 Do Q", b"BT /F1 12 Tf <0061> Tj ET"),
            None,
            False,
            True,
            id="nested-form-inherits",
        ),
        pytest.param(
            b"3 Tr /Fm0 Do",
            (b"/Fm1 Do", b"BT 0 Tr /F1 12 Tf <0061> Tj ET"),
            None,
            False,
            False,
            id="nested-form-paints",
        ),
        pytest.param(SHOWN, (), b"BT /F1 12 Tf <0061> Tj ET", False, False, id="appearance-paints"),
        pytest.param(
            SHOWN, (), b"BT 3 Tr /F1 12 Tf <0061> Tj ET", False, True, id="appearance-invisible"
        ),
        pytest.param(SHOWN, (), None, True, False, id="field-default-resources"),
    ],
)
def test_font_painted_anywhere_is_left_alone(
    tmp_path, content, forms, appearance, default_resources, replaced
):
    source = tmp_path / "source.pdf"
    _ocr_pdf(
        source,
        content=content,
        forms=forms,
        appearance=appearance,
        default_resources=default_resources,
    )
    with pikepdf.open(source) as pdf:
        assert repair_invisible_fonts(pdf)["invisible_fonts_replaced"] == int(replaced)
        cid_font = pdf.pages[0].Resources.Font.F1.DescendantFonts[0]
        assert cid_font.Subtype == ("/CIDFontType2" if replaced else "/CIDFontType0")
        assert ("/FontFile3" in cid_font.FontDescriptor) is not replaced


def test_dictionary_repair_replaces_invisible_truetype_font_without_removing_codes(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _ocr_pdf(source, program="truetype")
    ok, message, stats = _repair_pdf_font_dicts_sync(source, output)
    assert ok, message
    assert stats["invisible_fonts_replaced"] == 1
    assert stats["notdef_glyphs_removed"] == 0
    with pikepdf.open(source) as before, pikepdf.open(output) as after:
        assert before.pages[0].Contents.read_bytes() == after.pages[0].Contents.read_bytes()
        assert all(gid > 0 for gid, _ in _glyphs(after.pages[0].Resources.Font.F1).values())
    assert _text(source) == _text(output) != ""


@pytest.mark.parametrize(
    ("content", "removed"),
    [
        (b"BT 3 Tr /F1 12 Tf 20 220 Td (A\000B) Tj ET", 0),
        (b"3 Tr /Fm Do", 0),
        (b"q 3 Tr Q BT /F1 12 Tf 20 220 Td (A\000B) Tj ET", 1),
    ],
)
def test_blank_notdef_shown_invisibly_is_kept(tmp_path, content, removed):
    source = tmp_path / "source.pdf"
    _blank_font_document(source)
    with pikepdf.open(source) as pdf:
        page = pdf.pages[0]
        form = pdf.make_stream(b"BT /F1 12 Tf 20 220 Td (A\000B) Tj ET")
        form.Subtype = pikepdf.Name.Form
        form.BBox = [0, 0, 300, 300]
        form.Resources = page.Resources
        page.Resources.XObject = pikepdf.Dictionary(Fm=form)
        page.Contents = pdf.make_stream(content)
        assert repair_notdef_content(pdf)["notdef_glyphs_removed"] == removed
        if not removed:
            assert page.Contents.read_bytes() == content
            assert form.read_bytes() == b"BT /F1 12 Tf 20 220 Td (A\000B) Tj ET"


def test_fonts_share_one_program_whatever_the_clock(tmp_path, monkeypatch):
    ticks = iter(range(3_100_000_000, 3_200_000_000, 1000))
    monkeypatch.setattr("fontTools.fontBuilder.timestampNow", lambda: next(ticks))
    assert invisible._glyphless_program(1000) == invisible._glyphless_program(1000)
    source = tmp_path / "source.pdf"
    _ocr_pdf(
        source,
        content=b"BT 3 Tr /F1 12 Tf <0061> Tj /F2 12 Tf <006E> Tj ET",
        second_font=True,
    )
    with pikepdf.open(source) as pdf:
        assert repair_invisible_fonts(pdf)["invisible_fonts_replaced"] == 2
        fonts = pdf.pages[0].Resources.Font
        first, second = (fonts[name].DescendantFonts[0].FontDescriptor for name in ("/F1", "/F2"))
        assert first.FontFile2.objgen == second.FontFile2.objgen
