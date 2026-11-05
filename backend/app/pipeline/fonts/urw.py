"""URW base-35 substitutes, converted to TrueType, for families Liberation lacks."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

_GOTHIC_STYLES = {
    "": "Book",
    "regular": "Book",
    "bold": "Demi",
    "italic": "BookOblique",
    "oblique": "BookOblique",
    "bolditalic": "DemiOblique",
    "boldoblique": "DemiOblique",
}
_NARROW_STYLES = {
    "": "Regular",
    "mt": "Regular",
    "regular": "Regular",
    "bold": "Bold",
    "boldmt": "Bold",
    "italic": "Oblique",
    "italicmt": "Oblique",
    "oblique": "Oblique",
    "bolditalic": "BoldOblique",
    "bolditalicmt": "BoldOblique",
    "boldoblique": "BoldOblique",
}
# Century Gothic -> URW Gothic is owner-approved; the two are not metric
# compatible. fontconfig's 30-metric-aliases.conf lists Arial Narrow,
# Helvetica Narrow and Nimbus Sans Narrow as metric-compatible.
_FAMILIES = (
    (("centurygothic",), "URWGothic", _GOTHIC_STYLES),
    (("arialnarrow", "helveticanarrow"), "NimbusSansNarrow", _NARROW_STYLES),
)


def urw_substitute(normalized: str) -> str | None:
    """The URW program (e.g. `NimbusSansNarrow-Bold`) for a normalized font name."""
    for prefixes, family, styles in _FAMILIES:
        for prefix in prefixes:
            if normalized.startswith(prefix):
                style = styles.get(normalized[len(prefix) :])
                if style:
                    return f"{family}-{style}"
    return None


@lru_cache(maxsize=8)
def urw_truetype(resource: str) -> tuple[bytes | None, str | None]:
    """Convert the installed cubic outlines to TrueType for the width-aligning path.

    This uses fontTools' public Type1 reader, Cu2QuPen and FontBuilder. The
    source programs are the distribution's URW fonts, not a downloaded font.
    """
    from fontTools import agl, t1Lib
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    from app.pipeline.fonts.programs import _urw_type1_program

    data, _, _ = _urw_type1_program(resource)
    if not data:
        return None, None
    with TemporaryDirectory(prefix="urw-") as directory:
        path = Path(directory) / "font.t1"
        path.write_bytes(data)
        source = t1Lib.T1Font(str(path))
        glyphs = source.getGlyphSet()
        order = [".notdef", *(name for name in glyphs if name != ".notdef")]
        outlines, metrics, cmap = {}, {}, {}
        for name in order:
            pen = TTGlyphPen(None)
            glyphs[name].draw(Cu2QuPen(pen, max_err=0.5, reverse_direction=False))
            outlines[name] = pen.glyph()
            metrics[name] = (round(glyphs[name].width), 0)
            text = agl.toUnicode(name)
            if len(text) == 1:
                cmap.setdefault(ord(text), name)

        def extent(name: str, index: int, default: int) -> int:
            # index 1 is a glyph's lowest point, 3 its highest.
            if name not in glyphs:
                return default
            pen = BoundsPen(glyphs)
            glyphs[name].draw(pen)
            return default if pen.bounds is None else round(pen.bounds[index])

        ascent, descent, cap_height = (
            extent("d", 3, 1000),
            extent("p", 1, -300),
            extent("H", 3, 740),
        )
        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(order)
        builder.setupCharacterMap(cmap)
        builder.setupGlyf(outlines)
        builder.setupHorizontalMetrics(metrics)
        builder.setupHorizontalHeader(ascent=ascent, descent=descent)
        font_dictionary = source.font
        if not isinstance(font_dictionary, dict):
            raise ValueError(f"URW Type1 program {resource} has no font dictionary")
        raw_info = font_dictionary.get("FontInfo", {})
        info = raw_info if isinstance(raw_info, dict) else {}
        family, _, style = resource.partition("-")
        builder.setupNameTable(
            {
                "familyName": str(info.get("FamilyName", family)),
                "styleName": style,
                "fullName": resource,
                "psName": resource,
                "copyright": str(info.get("Notice", "")),
            }
        )
        builder.setupOS2(
            sTypoAscender=ascent,
            sTypoDescender=descent,
            usWinAscent=max(ascent, 0) + 200,
            usWinDescent=max(-descent, 0) + 100,
            sCapHeight=cap_height,
        )
        builder.setupPost(italicAngle=float(info.get("ItalicAngle", 0) or 0))
        buffer = BytesIO()
        builder.save(buffer)
        return buffer.getvalue(), resource
