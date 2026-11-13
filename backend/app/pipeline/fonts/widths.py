"""Align embedded font advances with PDF positioning, without editing /Widths or /W."""

from io import BytesIO

import pikepdf

from app.pipeline.fonts.encoding import _simple_font_unicode_map

_DEFAULT_CFF_FONT_MATRIX = [0.001, 0, 0, 0.001, 0, 0]


def _derive_truetype_widths(font_dict, font_bytes: bytes) -> None:
    """Supply absent simple-font widths using its encoding and embedded program.

    Existing author widths remain authoritative; alignment handles those instead.
    """
    from fontTools.ttLib import TTFont

    if "/Widths" in font_dict:
        return
    with TTFont(BytesIO(font_bytes), recalcTimestamp=False) as font:
        glyphs = _truetype_code_glyphs(font_dict, font)
        if not glyphs:
            return
        scale = 1000 / int(getattr(font["head"], "unitsPerEm"))
        metrics = font["hmtx"].metrics
        missing = metrics[".notdef"][0] * scale
        font_dict["/FirstChar"] = 0
        font_dict["/LastChar"] = 255
        font_dict["/Widths"] = pikepdf.Array(
            round(metrics[glyphs[code]][0] * scale, 6) if code in glyphs else round(missing, 6)
            for code in range(256)
        )


def _truetype_code_glyphs(font_dict, font) -> dict[int, str]:
    """Resolve simple PDF character codes to the program's glyphs.

    Use PDF encoding names for nonsymbolic fonts and the symbol/Macintosh
    cmap for implicit symbolic encodings. ToUnicode describes semantics,
    not glyph selection, and must not be used here.
    """
    encoding = font_dict.get("/Encoding")
    descriptor = font_dict.get("/FontDescriptor", {})
    symbolic = int(descriptor.get("/Flags", 0)) & 4
    mapping = {}
    if not symbolic and encoding is not None:
        unicode = font.getBestCmap() or {}
        for code, text in _simple_font_unicode_map(font_dict, None).items():
            if len(text) == 1 and ord(text) in unicode:
                mapping[code] = unicode[ord(text)]
        if isinstance(encoding, pikepdf.Dictionary):
            code = 0
            glyphs = set(font.getGlyphOrder())
            for item in encoding.get("/Differences") or ():
                if isinstance(item, int):
                    code = item
                else:
                    name = str(item).lstrip("/")
                    # Differences override the base encoding, even when the
                    # named glyph cannot be resolved.
                    mapping.pop(code, None)
                    if name in glyphs:
                        mapping[code] = name
                    else:
                        from app.pipeline.fonts.encoding import _glyph_name_to_unicode

                        text = _glyph_name_to_unicode(name)
                        if text and len(text) == 1 and ord(text) in unicode:
                            mapping[code] = unicode[ord(text)]
                    code += 1
        return mapping
    for platform, encoding_id in ((3, 0), (1, 0)):
        cmap = font["cmap"].getcmap(platform, encoding_id)
        if cmap is not None:
            for code, glyph in cmap.cmap.items():
                if 0xF000 <= code <= 0xF0FF:
                    code -= 0xF000
                if 0 <= code <= 255:
                    mapping.setdefault(code, glyph)
    return mapping


def _align_truetype_advances(font_dict, font_bytes: bytes) -> tuple[bytes, int, int]:
    from fontTools.ttLib import TTFont

    widths = font_dict.get("/Widths")
    if not isinstance(widths, pikepdf.Array) or font_dict.get("/FirstChar") is None:
        return font_bytes, 0, 0
    with TTFont(BytesIO(font_bytes), recalcTimestamp=False) as font:
        if "glyf" not in font or "hmtx" not in font or "cmap" not in font:
            return font_bytes, 0, 0
        upem = int(getattr(font["head"], "unitsPerEm"))
        first = int(font_dict.FirstChar)
        code_glyphs = _truetype_code_glyphs(font_dict, font)
        advances: dict[str, set[int]] = {}
        for offset, width in enumerate(widths):
            glyph = code_glyphs.get(first + offset)
            if glyph is None or glyph == ".notdef":
                continue
            advance = round(float(width) * upem / 1000)
            if 0 <= advance <= 65535:
                advances.setdefault(glyph, set()).add(advance)
        changed = conflicts = 0
        for glyph, values in advances.items():
            # Two PDF codes can share one glyph. A conflicting program width
            # cannot satisfy both; leave it for review instead of choosing one.
            if len(values) != 1:
                conflicts += 1
                continue
            advance = next(iter(values))
            old, bearing = font["hmtx"].metrics[glyph]
            if old != advance:
                font["hmtx"].metrics[glyph] = (advance, bearing)
                changed += 1
        if not changed:
            return font_bytes, 0, conflicts
        buffer = BytesIO()
        font.save(buffer)
        return buffer.getvalue(), changed, conflicts


def _cid_cff_width_key(glyph_name: str | None, fallback_index: int) -> int:
    raw = str(glyph_name or "").strip()
    if raw == ".notdef":
        return 0
    if raw.startswith("cid") and raw[3:].isdigit():
        try:
            return int(raw[3:])
        except ValueError:
            pass
    return fallback_index


def _cff_width_scales(cff_font) -> list[float] | None:
    """PDF glyph-space units per charstring unit, for each Font DICT.

    The top DICT matrix is concatenated with each FDArray matrix, as veraPDF
    reads it. A skewed or rotated matrix has no single horizontal scale.
    """
    top = cff_font.rawDict.get("FontMatrix")
    fd_array = getattr(cff_font, "FDArray", None)
    matrices = [fd.rawDict.get("FontMatrix") for fd in fd_array] if fd_array else [None]
    scales = []
    for own in matrices:
        if top is not None and own is not None:
            a = top[0] * own[0] + top[1] * own[2]
            b = top[0] * own[1] + top[1] * own[3]
        else:
            a, b = (top if top is not None else own or _DEFAULT_CFF_FONT_MATRIX)[:2]
        if not a or b:
            return None
        scales.append(a * 1000)
    return scales


def _set_charstring_advance(char_string, advance: int) -> None:
    """Restate a Type 2 charstring's advance; keep its outline and hints.

    The advance is an optional first operand relative to nominalWidthX, or
    defaultWidthX when absent. A subroutine call may supply that operand, so
    the glyph is desubroutinized first, as fontTools' CFFToCFF2 does to drop
    widths; the font's subroutines stay for the other glyphs.
    """
    from fontTools.cffLib.specializer import programToCommands
    from fontTools.cffLib.transforms import desubroutinizeCharString

    desubroutinizeCharString(char_string)
    program = char_string.program
    commands = programToCommands(program)
    if commands and commands[0][0] == "":
        program.pop(0)
    private = char_string.private
    if advance != private.defaultWidthX:
        program.insert(0, advance - private.nominalWidthX)
    char_string.width = advance


def _align_cid_cff_advances(
    cid_widths: dict[int, int], default_width: float, font_bytes: bytes
) -> tuple[bytes, int]:
    """Give each glyph of a CID-keyed CFF program its CIDFont /W (or /DW) advance.

    Viewers position glyphs by /W (ISO 32000-1, 9.2.4), so the page does not
    move. Acrobat's OCR fonts record some negative program advances as 0 in
    /W. veraPDF also reads a program advance of exactly -1 as a missing
    glyph and compares /DW instead, so copying program advances into /W
    cannot satisfy rule 7.21.5 there; restating the program can.
    """
    from fontTools.cffLib import CFFFontSet
    from fontTools.pens.basePen import NullPen
    from fontTools.ttLib import TTFont

    font_set = CFFFontSet()
    font_set.decompile(BytesIO(font_bytes), None)
    if len(font_set.fontNames) != 1:
        return font_bytes, 0
    cff_font = font_set[font_set.fontNames[0]]
    scales = _cff_width_scales(cff_font)
    if not getattr(cff_font, "ROS", None) or scales is None:
        return font_bytes, 0
    char_strings = cff_font.CharStrings
    changed = 0
    for index, name in enumerate(cff_font.charset):
        char_string, fd_index = char_strings.getItemAndSelector(name)
        width = cid_widths.get(_cid_cff_width_key(name, index), default_width)
        advance = round(width / scales[fd_index or 0])
        try:
            char_string.draw(NullPen())
            if char_string.width == advance:
                continue
            if not -32000 <= advance - char_string.private.nominalWidthX <= 32000:
                continue
            _set_charstring_advance(char_string, advance)
        except Exception:
            # A charstring fontTools cannot read stays as it is; validation
            # keeps reporting it.
            continue
        changed += 1
    if not changed:
        return font_bytes, 0
    buffer = BytesIO()
    font_set.compile(buffer, TTFont(recalcBBoxes=False))
    return buffer.getvalue(), changed
