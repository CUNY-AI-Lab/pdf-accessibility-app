"""Align embedded TrueType advances with PDF positioning, without editing /Widths."""

from io import BytesIO

import pikepdf

from app.pipeline.fonts.encoding import _simple_font_unicode_map


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
