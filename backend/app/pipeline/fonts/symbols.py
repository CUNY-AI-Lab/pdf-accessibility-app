"""Standard symbol correspondences, including producers' re-encoded subsets.

Wingdings codes: Unicode WG2 N4384, section 7 (2012-11-06).
Symbol/Zapf codes: Unicode's VENDORS/ADOBE symbol.txt and zdingbat.txt.
Glyph names also identify remapped TrueType subsets; a PDF byte is not
necessarily the original symbol-font code. See README.md for provenance.
"""

import json
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pikepdf

from app.pipeline.fonts.names import _base_font_name, _normalize_font_name


def _symbol_family(font_dict) -> str:
    name = _normalize_font_name(_base_font_name(font_dict))
    for family in ("wingdings2", "wingdings3", "wingdings", "symbol", "zapfdingbats"):
        if name in (family, family + "regular"):
            return family
    return ""


@lru_cache(maxsize=1)
def _symbol_tables() -> dict:
    return json.loads(Path(__file__).with_name("symbol_mappings.json").read_text())


def _symbol_font_unicode_map(font_dict, font_bytes: bytes | None) -> dict[int, str]:
    family = _symbol_family(font_dict)
    if not family:
        return {}
    table = _symbol_tables()[family]
    names = table["names"]
    mapping = {}
    encoding = font_dict.get("/Encoding")
    if isinstance(encoding, pikepdf.Dictionary):
        code = 0
        for item in encoding.get("/Differences") or ():
            if isinstance(item, int):
                code = item
            else:
                name = str(item).lstrip("/")
                if name in names:
                    mapping[code] = names[name]
                code += 1
    if font_bytes:
        from fontTools.ttLib import TTFont

        try:
            with TTFont(BytesIO(font_bytes)) as font:
                # A symbol cmap takes precedence over Macintosh byte codes.
                cmaps = sorted(
                    font["cmap"].tables, key=lambda t: (t.platformID, t.platEncID) != (3, 0)
                )
                for cmap in cmaps:
                    for code, glyph in cmap.cmap.items():
                        if 0xF000 <= code <= 0xF0FF:
                            code -= 0xF000
                        if 0 <= code <= 255 and glyph in names:
                            mapping.setdefault(code, names[glyph])
            return mapping
        except Exception:
            pass
    # No byte fallback for a subset: it may have been re-encoded arbitrarily.
    if "+" not in str(font_dict.get("/BaseFont", "")):
        for code, text in table["codes"].items():
            mapping.setdefault(int(code), text)
    return mapping
