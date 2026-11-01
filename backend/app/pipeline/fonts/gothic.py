"""Owner-approved Century Gothic substitutes from Ghostscript's URW base35."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory


def gothic_style(normalized: str) -> str | None:
    if not normalized.startswith("centurygothic"):
        return None
    return {
        "": "Book",
        "regular": "Book",
        "bold": "Demi",
        "italic": "BookOblique",
        "oblique": "BookOblique",
        "bolditalic": "DemiOblique",
        "boldoblique": "DemiOblique",
    }.get(normalized[len("centurygothic") :])


@lru_cache(maxsize=4)
def gothic_truetype(style: str) -> tuple[bytes | None, str | None]:
    """Convert the installed cubic outlines to TrueType for the width-aligning path.

    This uses fontTools' public Type1 reader, Cu2QuPen and FontBuilder. The
    source programs are the distribution's URW fonts, not a downloaded font.
    """
    from fontTools import agl, t1Lib
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    from app.pipeline.fonts.programs import _ghostscript_type1_font_program

    aliases = {
        "Book": "CenturyGothic",
        "Demi": "CenturyGothic-Bold",
        "BookOblique": "CenturyGothic-Italic",
        "DemiOblique": "CenturyGothic-BoldItalic",
    }
    data, _, _ = _ghostscript_type1_font_program(aliases[style])
    if not data:
        return None, None
    with TemporaryDirectory(prefix="urw-gothic-") as directory:
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
        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(order)
        builder.setupCharacterMap(cmap)
        builder.setupGlyf(outlines)
        builder.setupHorizontalMetrics(metrics)
        builder.setupHorizontalHeader(ascent=1000, descent=-300)
        name = f"URWGothic-{style}"
        font_dictionary = source.font
        if not isinstance(font_dictionary, dict):
            raise ValueError("URW Gothic Type1 program has no font dictionary")
        raw_info = font_dictionary.get("FontInfo", {})
        info = raw_info if isinstance(raw_info, dict) else {}
        builder.setupNameTable(
            {
                "familyName": "URW Gothic",
                "styleName": style,
                "fullName": name,
                "psName": name,
                "copyright": str(info.get("Notice", "")),
            }
        )
        builder.setupOS2(
            sTypoAscender=1000,
            sTypoDescender=-300,
            usWinAscent=1200,
            usWinDescent=400,
            sCapHeight=740,
        )
        builder.setupPost(italicAngle=-10 if "Oblique" in style else 0)
        buffer = BytesIO()
        builder.save(buffer)
        return buffer.getvalue(), name
