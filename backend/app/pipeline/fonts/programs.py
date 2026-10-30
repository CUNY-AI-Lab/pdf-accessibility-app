"""Discover system/Ghostscript font programs and read their metrics."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from app.config import get_settings
from app.pipeline.fonts.names import _base_font_name, _normalize_font_name
from app.services.runtime_paths import resolve_binary

SYSTEM_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}


SYSTEM_FONT_DIRS = (
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
    Path.home() / "Library/Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)


GHOSTSCRIPT_TYPE1_SUBSTITUTES = {
    "timesroman": "NimbusRoman-Regular",
    "timesbold": "NimbusRoman-Bold",
    "timesitalic": "NimbusRoman-Italic",
    "timesbolditalic": "NimbusRoman-BoldItalic",
    "helvetica": "NimbusSans-Regular",
    "helveticabold": "NimbusSans-Bold",
    "helveticaoblique": "NimbusSans-Italic",
    "helveticaboldoblique": "NimbusSans-BoldItalic",
    "courier": "NimbusMonoPS-Regular",
    "courierbold": "NimbusMonoPS-Bold",
    "courieroblique": "NimbusMonoPS-Italic",
    "courierboldoblique": "NimbusMonoPS-BoldItalic",
    "symbol": "StandardSymbolsPS",
    "zapfdingbats": "D050000L",
}


GHOSTSCRIPT_TYPE1_DESCRIPTOR_SPECS = {
    "timesroman": {"afm": "n021003l.afm", "flags": 34},
    "timesbold": {"afm": "n021004l.afm", "flags": 34},
    "timesitalic": {"afm": "n021023l.afm", "flags": 98},
    "timesbolditalic": {"afm": "n021024l.afm", "flags": 98},
    "helvetica": {"afm": "n019003l.afm", "flags": 32},
    "helveticabold": {"afm": "n019004l.afm", "flags": 32},
    "helveticaoblique": {"afm": "n019023l.afm", "flags": 96},
    "helveticaboldoblique": {"afm": "n019024l.afm", "flags": 96},
    "courier": {"afm": "n022003l.afm", "flags": 33},
    "courierbold": {"afm": "n022004l.afm", "flags": 33},
    "courieroblique": {"afm": "n022023l.afm", "flags": 97},
    "courierboldoblique": {"afm": "n022024l.afm", "flags": 97},
    "symbol": {"afm": "s050000l.afm", "flags": 4},
    "zapfdingbats": {"afm": "d050000l.afm", "flags": 4},
}


def _local_embed_support_kind(font_dict, descendant_subtype=None) -> str:
    import pikepdf

    subtype = font_dict.get("/Subtype")
    if subtype == pikepdf.Name("/TrueType"):
        return "truetype"
    if (
        subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1"))
        and _normalize_font_name(_base_font_name(font_dict)) in GHOSTSCRIPT_TYPE1_SUBSTITUTES
    ):
        return "type1_standard14"
    if subtype == pikepdf.Name("/Type0") and descendant_subtype == pikepdf.Name("/CIDFontType2"):
        return "cidfonttype2"
    return ""


def _coerce_metric_int(value, default: int = 0) -> int:
    """Parse AFM-style numeric values that may arrive as ints, floats, or numeric strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return default


def _font_name_metadata(ttfont) -> dict[str, str]:
    meta = {
        "family": "",
        "subfamily": "",
        "full_name": "",
        "postscript": "",
        "combined": "",
    }
    name_table = ttfont.get("name")
    if name_table is None:
        return meta

    for record in name_table.names:
        if record.nameID not in (1, 2, 4, 6):
            continue
        try:
            text = str(record.toUnicode()).strip()
        except Exception:
            continue
        if not text:
            continue
        normalized = _normalize_font_name(text)
        if record.nameID == 1 and not meta["family"]:
            meta["family"] = normalized
        elif record.nameID == 2 and not meta["subfamily"]:
            meta["subfamily"] = normalized
        elif record.nameID == 4 and not meta["full_name"]:
            meta["full_name"] = normalized
        elif record.nameID == 6 and not meta["postscript"]:
            meta["postscript"] = normalized

    if meta["family"]:
        meta["combined"] = _normalize_font_name(f"{meta['family']}{meta['subfamily']}")
    return meta


def _font_name_keys(ttfont) -> set[str]:
    keys: set[str] = set()
    meta = _font_name_metadata(ttfont)
    for key in (meta["postscript"], meta["full_name"], meta["family"], meta["combined"]):
        if key:
            keys.add(key)
    return keys


@lru_cache(maxsize=1)
def _system_font_index() -> dict[str, list[tuple[str, int, str, str, str, str]]]:
    from fontTools.ttLib import TTCollection, TTFont

    index: dict[str, list[tuple[str, int, str, str, str, str]]] = {}
    for root in SYSTEM_FONT_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in SYSTEM_FONT_EXTENSIONS:
                continue
            try:
                if path.suffix.lower() == ".ttc":
                    collection = TTCollection(str(path), lazy=False)
                    fonts = list(enumerate(collection.fonts))
                else:
                    collection = None
                    fonts = [(0, TTFont(str(path), lazy=False))]
            except Exception:
                continue

            try:
                for face_index, ttfont in fonts:
                    try:
                        if "glyf" not in ttfont:
                            continue
                        meta = _font_name_metadata(ttfont)
                        candidate = (
                            str(path),
                            face_index,
                            meta["postscript"],
                            meta["full_name"],
                            meta["family"],
                            meta["subfamily"],
                        )
                        for key in _font_name_keys(ttfont):
                            index.setdefault(key, []).append(candidate)
                    finally:
                        try:
                            ttfont.close()
                        except Exception:
                            pass
            finally:
                if collection is not None:
                    try:
                        collection.close()
                    except Exception:
                        pass

    return index


@lru_cache(maxsize=256)
def _system_font_program(font_name: str) -> tuple[bytes | None, str | None]:
    from fontTools.ttLib import TTCollection, TTFont

    normalized = _normalize_font_name(font_name)
    if not normalized:
        return None, None

    def _score_candidate(
        requested: str,
        postscript: str,
        full_name: str,
        family: str,
        subfamily: str,
    ) -> int:
        if requested == postscript:
            return 500
        if requested == full_name:
            return 400
        combined = _normalize_font_name(f"{family}{subfamily}")
        if requested == combined:
            return 300
        if requested == family:
            if subfamily in {"", "regular", "roman", "book", "normal", "plain"}:
                return 200
            return 100
        return 0

    candidates = sorted(
        _system_font_index().get(normalized, []),
        key=lambda item: _score_candidate(normalized, item[2], item[3], item[4], item[5]),
        reverse=True,
    )
    for path_str, face_index, _, _, _, _ in candidates:
        path = Path(path_str)
        try:
            if path.suffix.lower() == ".ttc":
                collection = TTCollection(str(path), lazy=False)
                try:
                    ttfont = collection.fonts[face_index]
                    if "glyf" not in ttfont:
                        continue
                    buffer = BytesIO()
                    ttfont.save(buffer)
                    return buffer.getvalue(), path.name
                finally:
                    try:
                        collection.close()
                    except Exception:
                        pass

            ttfont = TTFont(str(path), lazy=False)
            try:
                if "glyf" not in ttfont:
                    continue
            finally:
                try:
                    ttfont.close()
                except Exception:
                    pass

            return path.read_bytes(), path.name
        except Exception:
            continue

    return None, None


@lru_cache(maxsize=1)
def _ghostscript_resource_font_dir() -> Path | None:
    gs = resolve_binary("gs", explicit=get_settings().ghostscript_path)
    if not gs:
        return None

    gs_path = Path(gs).resolve()
    candidate_suffixes = (
        Path("share/ghostscript/Resource/Font"),
        Path("Resource/Font"),
    )
    for parent in (gs_path.parent,) + tuple(gs_path.parents):
        for suffix in candidate_suffixes:
            candidate = parent / suffix
            if candidate.exists():
                return candidate
    return None


@lru_cache(maxsize=1)
def _ghostscript_font_metrics_dir() -> Path | None:
    gs = resolve_binary("gs", explicit=get_settings().ghostscript_path)
    if not gs:
        return None

    gs_path = Path(gs).resolve()
    candidate_suffixes = (
        Path("share/ghostscript/fonts"),
        Path("fonts"),
    )
    for parent in (gs_path.parent,) + tuple(gs_path.parents):
        for suffix in candidate_suffixes:
            candidate = parent / suffix
            if candidate.exists():
                return candidate
    return None


@lru_cache(maxsize=64)
def _ghostscript_type1_font_program(
    font_name: str,
) -> tuple[bytes | None, str | None, dict[str, int] | None]:
    from fontTools import t1Lib

    normalized = _normalize_font_name(font_name)
    if not normalized:
        return None, None, None

    resource_name = GHOSTSCRIPT_TYPE1_SUBSTITUTES.get(normalized)
    if not resource_name:
        return None, None, None

    resource_dir = _ghostscript_resource_font_dir()
    if resource_dir is None:
        return None, None, None

    path = resource_dir / resource_name
    if not path.exists():
        return None, None, None

    try:
        raw = path.read_bytes()
        chunks = t1Lib.findEncryptedChunks(raw)
    except Exception:
        return None, None, None

    font_bytes = b"".join(chunk for _, chunk in chunks)
    if not font_bytes:
        return None, None, None

    length1 = len(chunks[0][1]) if chunks else len(font_bytes)
    length2 = sum(len(chunk) for encrypted, chunk in chunks if encrypted)
    length3 = max(0, len(font_bytes) - length1 - length2)
    lengths = {
        "Length1": length1,
        "Length2": length2,
        "Length3": length3,
    }
    return font_bytes, path.name, lengths


@lru_cache(maxsize=64)
def _ghostscript_type1_descriptor(
    font_name: str,
) -> dict[str, object] | None:
    from fontTools import afmLib

    normalized = _normalize_font_name(font_name)
    if not normalized:
        return None

    spec = GHOSTSCRIPT_TYPE1_DESCRIPTOR_SPECS.get(normalized)
    metrics_dir = _ghostscript_font_metrics_dir()
    if spec is None or metrics_dir is None:
        return None

    path = metrics_dir / spec["afm"]
    if not path.exists():
        return None

    try:
        afm = afmLib.AFM(str(path))
    except Exception:
        return None

    attrs = afm._attrs
    font_bbox = attrs.get("FontBBox")
    if not isinstance(font_bbox, tuple) or len(font_bbox) != 4:
        return None

    widths_by_code = {
        _coerce_metric_int(charnum): _coerce_metric_int(width)
        for charnum, width, _ in afm._chars.values()
        if isinstance(_coerce_metric_int(charnum, -1), int)
        and 0 <= _coerce_metric_int(charnum, -1) <= 255
    }
    weight = str(attrs.get("Weight", "") or "").lower()
    stem_v = 120 if any(token in weight for token in ("bold", "demi", "black")) else 80
    descriptor = {
        "Flags": int(spec["flags"]),
        "ItalicAngle": _coerce_metric_int(attrs.get("ItalicAngle", 0), 0),
        "Ascent": _coerce_metric_int(attrs.get("Ascender", 0), 0),
        "Descent": _coerce_metric_int(attrs.get("Descender", 0), 0),
        "CapHeight": _coerce_metric_int(attrs.get("CapHeight", attrs.get("Ascender", 0)), 0),
        "StemV": stem_v,
        "FontBBox": [_coerce_metric_int(value, 0) for value in font_bbox],
        "FirstChar": 0,
        "LastChar": 255,
        "Widths": [_coerce_metric_int(widths_by_code.get(code, 0), 0) for code in range(256)],
        "MissingWidth": _coerce_metric_int(widths_by_code.get(0, widths_by_code.get(32, 0)), 0),
    }
    return descriptor


def _local_font_program(
    font_dict,
    font_name: str,
    *,
    descendant_subtype=None,
) -> tuple[bytes | None, str | None, str | None, dict[str, int] | None]:
    import pikepdf

    support_kind = _local_embed_support_kind(font_dict, descendant_subtype=descendant_subtype)
    if support_kind in {"truetype", "cidfonttype2"}:
        font_bytes, matched_name = _system_font_program(font_name)
        if not font_bytes:
            return None, None, None, None
        return (
            font_bytes,
            matched_name,
            str(pikepdf.Name("/FontFile2")),
            {"Length1": len(font_bytes)},
        )

    if support_kind == "type1_standard14":
        font_bytes, matched_name, lengths = _ghostscript_type1_font_program(font_name)
        if not font_bytes or not lengths:
            return None, None, None, None
        return font_bytes, matched_name, str(pikepdf.Name("/FontFile")), lengths

    return None, None, None, None
