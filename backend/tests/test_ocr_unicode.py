"""OCR recovers Unicode for fonts that carry none, without guessing."""

import shutil
from io import BytesIO
from pathlib import Path

import ocrmypdf
import pikepdf
import pypdfium2 as pdfium
import pytest
from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import cmap_format_6

from app.pipeline.fonts import ocr_unicode
from app.pipeline.fonts.encoding import _parse_tounicode_map
from app.pipeline.fonts.lanes import _font_remediation_lanes
from app.pipeline.fonts.rules import (
    FONT_LANE_EMBED,
    FONT_LANE_OCR_REDO,
    FONT_LANE_OCR_TOUNICODE,
    FONT_LANE_REPAIR_TOUNICODE,
)
from app.pipeline.validator import Violation

NOTO = Path(ocrmypdf.__file__).parent / "data" / "NotoSans-Regular.ttf"
LINES = ["Archive minutes 1968", "minutes of the Archive", "1968 Archive minutes"]
EMPTY_TOUNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00><FF>
endcodespacerange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def _print_driver_font() -> tuple[bytes, dict[str, int], list[int]]:
    """A Noto Sans subset as some print drivers embed it: codes from 33 in
    drawing order, a Mac (1,0) cmap only, no glyph names."""
    characters = sorted({ch for line in LINES for ch in line} - {" "})
    font = TTFont(NOTO)
    options = subset.Options()
    options.glyph_names = False
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=[ord(ch) for ch in [*characters, " "]])
    subsetter.subset(font)
    by_char = font.getBestCmap()
    codes = {ch: 33 + i for i, ch in enumerate(characters)}
    codes[" "] = 33 + len(characters)
    table = cmap_format_6(6)
    table.platformID, table.platEncID, table.language = 1, 0, 0
    table.cmap = {code: by_char[ord(ch)] for ch, code in codes.items()}
    font["cmap"].tables = [table]
    font["post"].formatType = 3.0
    scale = 1000 / font["head"].unitsPerEm
    widths = [round(font["hmtx"][by_char[ord(ch)]][0] * scale) for ch, _ in sorted(codes.items(), key=lambda kv: kv[1])]
    buffer = BytesIO()
    font.save(buffer)
    return buffer.getvalue(), codes, widths


def _broken_font_pdf(path: Path, base_font: str = "/AAAAAC+NotoSans") -> dict[str, int]:
    """A page drawn with that font and an empty ToUnicode map, as the archive files have."""
    program, codes, widths = _print_driver_font()
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        descriptor = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name(base_font), Flags=4,
            FontBBox=[-500, -300, 1500, 1000], ItalicAngle=0, Ascent=1000, Descent=-300, CapHeight=700,
            StemV=80, FontFile2=pdf.make_stream(program),
        ))
        font = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name(base_font),
            FirstChar=33, LastChar=33 + len(widths) - 1, Widths=widths, FontDescriptor=descriptor,
            ToUnicode=pdf.make_stream(EMPTY_TOUNICODE),
        ))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.Contents = pdf.make_stream(b"".join(
            b"BT /F1 40 Tf 60 %d Td <%s> Tj ET\n" % (700 - 120 * i, "".join(f"{codes[ch]:02X}" for ch in line).encode())
            for i, line in enumerate(LINES)
        ))
        pdf.save(path)
    return codes


def _words_ocr(source: Path, misreadings: list[tuple[str, int, str]] = ()):
    """OCR that reports each word of LINES with a box around its glyphs, read
    correctly except (character, occurrence, read-as) misreadings."""
    with pikepdf.open(source) as pdf:
        font = pdf.pages[0].Resources.Font.F1.objgen[0]
    glyphs = ocr_unicode._page_glyphs(source, {font})[0]

    def ocr_page(page, languages, tesseract):
        frame, scale = ocr_unicode.PageFrame.of(page), 300 / 72
        seen: dict[str, int] = {}
        words, cursor = [], 0
        for line in LINES:
            for position, word in enumerate(line.split(" ")):
                cursor += 1 if position else 0  # the space glyph between words
                run = glyphs[cursor: cursor + len(word)]
                cursor += len(word)
                read = ""
                for ch in word:
                    seen[ch] = seen.get(ch, 0) + 1
                    read += next((r for c, n, r in misreadings if c == ch and n == seen[ch]), ch)
                boxes = [ocr_unicode._image_box(frame, box, scale) for _, _, box in run]
                words.append(ocr_unicode._OcrChar(
                    read, min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes), 95.0))
        return [], words, scale, frame, page.render(scale=scale).to_pil().convert("L")

    return ocr_page


def _mapping(path: Path) -> dict[int, str]:
    with pikepdf.open(path) as pdf:
        return _parse_tounicode_map(pdf.pages[0].Resources.Font.F1.ToUnicode)


def _text(path: Path) -> str:
    document = pdfium.PdfDocument(str(path))
    try:
        return document[0].get_textpage().get_text_range()
    finally:
        document.close()


def test_codes_vote_and_a_lone_misreading_is_outvoted(tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "mapped.pdf"
    codes = _broken_font_pdf(source)
    monkeypatch.setattr(ocr_unicode, "resolve_binary", lambda *a, **k: "tesseract")
    # "e" occurs seven times; its first occurrence is misread as "c".
    monkeypatch.setattr(ocr_unicode, "_ocr_page", _words_ocr(source, [("e", 1, "c")]))

    ok, message, stats = ocr_unicode._recover_sync(source, output, "eng", 300)

    assert ok, message
    assert stats["codes_unresolved"] == 0
    mapping = _mapping(output)
    assert {ch: mapping[code] for ch, code in codes.items()} == {ch: ch for ch in codes}
    assert _text(output).split() == " ".join(LINES).split()


def test_codes_without_agreement_stay_unmapped(tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "mapped.pdf"
    codes = _broken_font_pdf(source)
    monkeypatch.setattr(ocr_unicode, "resolve_binary", lambda *a, **k: "tesseract")
    # "h" occurs three times and reads differently each time.
    monkeypatch.setattr(ocr_unicode, "_ocr_page", _words_ocr(source, [("h", 1, "b"), ("h", 2, "n")]))

    ok, _, stats = ocr_unicode._recover_sync(source, output, "eng", 300)

    assert ok
    assert stats["codes_unresolved"] == 1
    mapping = _mapping(output)
    assert codes["h"] not in mapping
    assert mapping[codes["A"]] == "A"


def test_symbol_fonts_are_never_read_by_ocr(tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "mapped.pdf"
    _broken_font_pdf(source, base_font="/AAAAAC+Wingdings-Regular")
    monkeypatch.setattr(ocr_unicode, "resolve_binary", lambda *a, **k: "tesseract")

    ok, _, _ = ocr_unicode._recover_sync(source, output, "eng", 300)

    assert not ok
    assert not output.exists()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs Tesseract")
def test_tesseract_recovers_a_print_driver_font(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "mapped.pdf"
    codes = _broken_font_pdf(source)

    ok, message, stats = ocr_unicode._recover_sync(source, output, "eng", 300)

    assert ok, message
    assert stats["codes_unresolved"] == 0
    mapping = _mapping(output)
    assert {ch: mapping[code] for ch, code in codes.items()} == {ch: ch for ch in codes}


def test_ocr_mapping_lane_runs_before_reocr_and_on_digital_documents():
    def violation(rule: str) -> Violation:
        return Violation(rule_id=rule, description="", severity="error")

    settings = ocr_unicode.get_settings()
    features = {"pages": 10, "has_forms": False, "unembedded_fonts": 0}
    digital, _ = _font_remediation_lanes(
        [violation("ISO 14289-1:2014-7.21.7-1")], classification="digital", pdf_features=features, settings=settings,
    )
    scanned, _ = _font_remediation_lanes(
        [violation("ISO 14289-1:2014-7.21.7-1")], classification="scanned", pdf_features=features, settings=settings,
    )
    notdef_only, _ = _font_remediation_lanes(
        [violation("ISO 14289-1:2014-7.21.8-1")], classification="digital", pdf_features=features, settings=settings,
    )
    assert digital == [FONT_LANE_REPAIR_TOUNICODE, FONT_LANE_EMBED, FONT_LANE_OCR_TOUNICODE]
    assert scanned.index(FONT_LANE_OCR_TOUNICODE) < scanned.index(FONT_LANE_OCR_REDO)
    assert FONT_LANE_OCR_TOUNICODE not in notdef_only
