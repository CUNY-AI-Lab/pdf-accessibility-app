"""Simple-font encodings, ToUnicode CMaps, and text operand cleanup."""

import json
import re
from collections.abc import Iterable
from enum import StrEnum
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pikepdf

from app.pipeline.fonts.names import FONT_NAME_RE, _base_font_name
from app.pipeline.fonts.symbols import _symbol_family, _symbol_font_unicode_map

SAFE_IMPLICIT_STANDARD_BASEFONTS = frozenset(
    {
        "TimesRoman",
        "TimesBold",
        "TimesItalic",
        "TimesBoldItalic",
        "Helvetica",
        "HelveticaBold",
        "HelveticaOblique",
        "HelveticaBoldOblique",
        "Courier",
        "CourierBold",
        "CourierOblique",
        "CourierBoldOblique",
    }
)


HEX_STR_RE = re.compile(r"<([0-9A-Fa-f]+)>")


MAX_TOUNICODE_BFRANGE_SPAN = 65536


class ToUnicodeMode(StrEnum):
    OUTSIDE = "outside"
    BFCHAR = "bfchar"
    BFRANGE = "bfrange"


# CMaps are read tolerantly: a new begin replaces an unfinished block, any
# end closes it, and EOF retains the entries already read. Writers of the
# parser state still use only these named events.
TOUNICODE_TRANSITIONS = {
    mode: {
        "beginbfchar": ToUnicodeMode.BFCHAR,
        "beginbfrange": ToUnicodeMode.BFRANGE,
        "end": ToUnicodeMode.OUTSIDE,
    }
    for mode in ToUnicodeMode
}


def _tounicode_transition(mode: ToUnicodeMode, event: str) -> ToUnicodeMode:
    target = TOUNICODE_TRANSITIONS.get(mode, {}).get(event)
    if target is None:
        raise ValueError(f"Illegal ToUnicode parser transition: {mode!r} via {event!r}")
    return target


def _simple_font_auto_unicode_policy(font_dict, font_bytes: bytes | None = None) -> str:
    import pikepdf

    if _symbol_family(font_dict):
        return "symbol"
    encoding = font_dict.get("/Encoding")
    if isinstance(encoding, pikepdf.Dictionary):
        if encoding.get("/BaseEncoding") is not None or encoding.get("/Differences") is not None:
            return "explicit"
    elif encoding is not None:
        return "explicit"

    base_font = FONT_NAME_RE.sub("", _base_font_name(font_dict))
    if base_font in SAFE_IMPLICIT_STANDARD_BASEFONTS:
        return "standard14"
    if font_bytes and font_dict.get("/Subtype") in (
        pikepdf.Name("/Type1"),
        pikepdf.Name("/MMType1"),
    ):
        if _cff_builtin_encoding_map(font_bytes):
            return "embedded_cff"
        if _type1_builtin_encoding_map(font_bytes):
            return "embedded_type1"
    return "blocked"


def _is_valid_unicode_text(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if cp in (0x0000, 0xFFFE, 0xFEFF):
            return False
        if cp > 0x10FFFF or (0xD800 <= cp <= 0xDFFF):
            return False
    return True


def _decode_tounicode_hex(value: str) -> str | None:
    try:
        data = bytes.fromhex(value)
    except ValueError:
        return None
    if not data:
        return None
    if len(data) % 2 != 0:
        return None
    try:
        text = data.decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    return text if _is_valid_unicode_text(text) else None


@lru_cache(maxsize=1)
def _tex_glyph_names() -> dict[str, str]:
    # TeX/AMS glyph names outside the AGL; see README.md for provenance.
    return json.loads(Path(__file__).with_name("tex_glyph_names.json").read_text())


def _glyph_name_to_unicode(glyph_name: str | None) -> str | None:
    from fontTools.agl import toUnicode

    raw = str(glyph_name or "").strip().lstrip("/")
    if not raw or raw == ".notdef":
        return None

    base = raw.split(".", 1)[0]
    text = toUnicode(raw)
    if not text and "." in raw:
        text = toUnicode(base)
    if not text:
        tex_names = _tex_glyph_names()
        text = tex_names.get(raw) or tex_names.get(base) or ""
    return text if _is_valid_unicode_text(text) else None


def _named_simple_encoding_map(encoding_name) -> dict[int, str]:
    from fontTools.encodings import MacRoman, StandardEncoding

    name = str(encoding_name or "").strip()
    if not name:
        return {}

    mapping: dict[int, str] = {}
    if name == "/StandardEncoding":
        glyph_names = StandardEncoding.StandardEncoding
        for code, glyph_name in enumerate(glyph_names):
            text = _glyph_name_to_unicode(glyph_name)
            if text:
                mapping[code] = text
        return mapping

    if name == "/MacRomanEncoding":
        glyph_names = MacRoman.MacRoman
        for code, glyph_name in enumerate(glyph_names):
            text = _glyph_name_to_unicode(glyph_name)
            if text:
                mapping[code] = text
        return mapping

    if name == "/WinAnsiEncoding":
        for code in range(256):
            try:
                text = bytes([code]).decode("cp1252")
            except Exception:
                continue
            if _is_valid_unicode_text(text):
                mapping[code] = text
        return mapping

    return {}


def _encoding_differences_names(differences) -> dict[int, str]:
    """Glyph name per code in a Differences array; a later entry for a code wins."""
    import pikepdf

    if not isinstance(differences, pikepdf.Array):
        return {}

    names: dict[int, str] = {}
    current_code: int | None = None
    for item in differences:
        if isinstance(item, int):
            current_code = item
            continue
        if current_code is None:
            continue
        names[current_code] = str(item).lstrip("/")
        current_code += 1
    return names


def _encoding_differences_map(differences) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for code, name in _encoding_differences_names(differences).items():
        text = _glyph_name_to_unicode(name)
        if text:
            mapping[code] = text
    return mapping


def _standard_encoding_over_winansi(
    encoding, *, implicit_standard: bool, shown_codes: set[int] | None = None
):
    """Restate a StandardEncoding-based simple encoding over WinAnsiEncoding.

    PDF/UA-1 (ISO 14289-1, 7.21.6) allows a non-symbolic TrueType font only
    MacRomanEncoding or WinAnsiEncoding as its base, and only Adobe Glyph List
    names in Differences. Each code keeps its glyph name: a Differences entry
    names it wherever WinAnsiEncoding would give another character. Codes that
    StandardEncoding leaves undefined keep WinAnsiEncoding's character. Returns
    None when the encoding is not StandardEncoding-based, or a name is not in
    the AGL.

    With `shown_codes`, every code the font shows in the document, a name
    outside the AGL at a code never shown is dropped: no glyph is ever drawn
    or read for it. Some PostScript drivers name the control codes 0-31 that
    way (`NUL` ... `US`).
    """
    from fontTools.agl import LEGACY_AGL2UV
    from fontTools.encodings.StandardEncoding import StandardEncoding

    standard = pikepdf.Name("/StandardEncoding")
    if isinstance(encoding, pikepdf.Dictionary):
        base = encoding.get("/BaseEncoding")
        if base != standard and not (base is None and implicit_standard):
            return None
        differences = _encoding_differences_names(encoding.get("/Differences"))
    elif encoding == standard or (encoding is None and implicit_standard):
        differences = {}
    else:
        return None

    names = {code: name for code, name in enumerate(StandardEncoding) if name != ".notdef"}
    names.update(differences)
    winansi = _named_simple_encoding_map("/WinAnsiEncoding")
    entries: list[tuple[int, str]] = []
    for code, name in sorted(names.items()):
        if name == ".notdef":
            continue
        unicode = LEGACY_AGL2UV.get(name)
        if unicode is None:
            if shown_codes is not None and code not in shown_codes:
                continue
            return None
        if "".join(map(chr, unicode)) != winansi.get(code):
            entries.append((code, name))

    array = pikepdf.Array()
    previous = None
    for code, name in entries:
        if previous is None or code != previous + 1:
            array.append(code)
        array.append(pikepdf.Name(f"/{name}"))
        previous = code
    restated = pikepdf.Dictionary(
        Type=pikepdf.Name("/Encoding"), BaseEncoding=pikepdf.Name("/WinAnsiEncoding")
    )
    if entries:
        restated["/Differences"] = array
    return restated


def _truetype_glyph_name_unicode(font_bytes: bytes, names: dict[int, str]) -> dict[int, str]:
    """Unicode for codes whose glyph names only the embedded TrueType program knows.

    A name selects a glyph through the program's own `post` names, so its
    Unicode cmap entry for that glyph is the font's statement of the character.
    Symbol cmaps and private-use values are not evidence.
    """
    from fontTools.ttLib import TTFont

    try:
        with TTFont(BytesIO(font_bytes), lazy=True) as font:
            if "cmap" not in font:
                return {}
            glyphs = set(font.getGlyphOrder())
            reverse = font["cmap"].buildReversed()
    except Exception:
        return {}

    mapping: dict[int, str] = {}
    for code, name in names.items():
        if name not in glyphs:
            continue
        values = sorted(value for value in reverse.get(name, ()) if not 0xE000 <= value <= 0xF8FF)
        if values and _is_valid_unicode_text(chr(values[0])):
            mapping[code] = chr(values[0])
    return mapping


def _cff_builtin_encoding_names(font_bytes: bytes) -> dict[int, str]:
    from fontTools.cffLib import CFFFontSet

    try:
        cff = CFFFontSet()
        cff.decompile(BytesIO(font_bytes), None)
        top = cff[list(cff.keys())[0]]
    except Exception:
        return {}

    encoding = getattr(top, "Encoding", None)
    if not isinstance(encoding, list):
        return {}
    return {code: str(name) for code, name in enumerate(encoding) if name and name != ".notdef"}


def _cff_builtin_encoding_map(font_bytes: bytes) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for code, glyph_name in _cff_builtin_encoding_names(font_bytes).items():
        text = _glyph_name_to_unicode(glyph_name)
        if text:
            mapping[code] = text
    return mapping


TYPE1_ENCODING_RE = re.compile(
    rb"/Encoding\s+(?:(?P<standard>StandardEncoding)\s+(?:readonly\s+)?def\b|\d+\s+array\b)"
)
TYPE1_ENCODING_ENTRY_RE = re.compile(rb"(?<!\S)dup\s+(\d+)\s*/([^\s/\[\]{}()<>%]+)\s+put(?!\S)")


def _type1_builtin_encoding_names(font_bytes: bytes) -> dict[int, str]:
    """Read the built-in encoding from the cleartext part of a /FontFile program.

    The cleartext (the stream's first /Length1 bytes) ends at `currentfile
    eexec`, so the token bounds it without /Length1. It sets /Encoding to
    `StandardEncoding` or fills an array with `dup <code> /<name> put`.
    """
    cleartext_end = font_bytes.find(b"eexec")
    if cleartext_end < 0:
        return {}
    cleartext = font_bytes[:cleartext_end]
    match = TYPE1_ENCODING_RE.search(cleartext)
    if match is None:
        return {}
    if match.group("standard"):
        return _named_encoding_glyph_names("/StandardEncoding")
    return {
        int(code): glyph_name.decode("latin-1")
        for code, glyph_name in TYPE1_ENCODING_ENTRY_RE.findall(cleartext, match.end())
        if glyph_name != b".notdef"
    }


def _type1_builtin_encoding_map(font_bytes: bytes) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for code, glyph_name in _type1_builtin_encoding_names(font_bytes).items():
        text = _glyph_name_to_unicode(glyph_name)
        if text:
            mapping[code] = text
    return mapping


def _named_encoding_glyph_names(encoding_name) -> dict[int, str]:
    """Glyph names of the PDF's named simple encodings, by code."""
    from fontTools.agl import UV2AGL
    from fontTools.encodings import MacRoman, StandardEncoding

    name = str(encoding_name or "")
    if name == "/StandardEncoding":
        names = list(StandardEncoding.StandardEncoding)
    elif name == "/MacRomanEncoding":
        names = list(MacRoman.MacRoman)
    elif name == "/WinAnsiEncoding":
        names = [
            UV2AGL.get(ord(text)) or f"uni{ord(text):04X}"
            if 32 <= code != 127 and text != "\ufffd"
            else ".notdef"
            for code, text in enumerate(bytes(range(256)).decode("cp1252", errors="replace"))
        ]
    else:
        return {}
    return {code: glyph for code, glyph in enumerate(names) if glyph != ".notdef"}


def _glyph_name_for_code(font_dict, code: int, font_bytes: bytes | None) -> tuple[str, str] | None:
    """The name the font gives the glyph at a simple-font code, and its source.

    Differences first, then the named base encoding, then the embedded
    program: a Type 1 or CFF built-in encoding, or the stored `post` name of
    the glyph a symbolic TrueType cmap selects.
    """
    encoding = font_dict.get("/Encoding")
    base = encoding
    if isinstance(encoding, pikepdf.Dictionary):
        differences = _encoding_differences_names(encoding.get("/Differences"))
        if code in differences:
            return differences[code], "Differences"
        base = encoding.get("/BaseEncoding")
    if (name := _named_encoding_glyph_names(base).get(code)) is not None:
        return name, str(base).lstrip("/")
    if not font_bytes:
        return None
    subtype = font_dict.get("/Subtype")
    if subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1")):
        names = _cff_builtin_encoding_names(font_bytes) or _type1_builtin_encoding_names(font_bytes)
        if (name := names.get(code)) is not None:
            return name, "font program built-in encoding"
    elif subtype == pikepdf.Name("/TrueType") and encoding is None:
        from fontTools.ttLib import TTFont

        try:
            with TTFont(BytesIO(font_bytes), lazy=True) as font:
                if "post" not in font or getattr(font["post"], "formatType", None) != 2.0:
                    return None
                for platform, encoding_id, offset in ((3, 0, 0xF000), (1, 0, 0)):
                    table = font["cmap"].getcmap(platform, encoding_id)
                    glyph = table.cmap.get(offset + code) if table is not None else None
                    if glyph and glyph != ".notdef":
                        return glyph, "TrueType program glyph name"
        except Exception:
            return None
    return None


def _simple_font_unicode_map(font_dict, font_bytes: bytes | None) -> dict[int, str]:
    import pikepdf

    if _symbol_family(font_dict):
        return {
            code: text
            for code, text in _symbol_font_unicode_map(font_dict, font_bytes).items()
            if _is_valid_unicode_text(text)
        }
    mapping: dict[int, str] = {}

    subtype = font_dict.get("/Subtype")
    encoding = font_dict.get("/Encoding")
    if isinstance(encoding, pikepdf.Dictionary):
        mapping.update(_named_simple_encoding_map(encoding.get("/BaseEncoding")))
        unknown: dict[int, str] = {}
        for code, name in _encoding_differences_names(encoding.get("/Differences")).items():
            text = _glyph_name_to_unicode(name)
            if text:
                mapping[code] = text
            else:
                # A Differences name replaces the glyph at its code, so the
                # base encoding's character there is not evidence for it.
                mapping.pop(code, None)
                unknown[code] = name
        if unknown and font_bytes and subtype == pikepdf.Name("/TrueType"):
            mapping.update(_truetype_glyph_name_unicode(font_bytes, unknown))
    else:
        mapping.update(_named_simple_encoding_map(encoding))

    # Stay conservative in the automatic lane: only use explicit PDF encoding data,
    # or the built-in Latin standard 14 fonts where StandardEncoding is deterministic.
    policy = _simple_font_auto_unicode_policy(font_dict, font_bytes=font_bytes)
    if not mapping and subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1")):
        if policy == "standard14":
            mapping.update(_named_simple_encoding_map("/StandardEncoding"))
        elif policy == "embedded_cff" and font_bytes:
            mapping.update(_cff_builtin_encoding_map(font_bytes))
        elif policy == "embedded_type1" and font_bytes:
            mapping.update(_type1_builtin_encoding_map(font_bytes))

    return {
        code: text
        for code, text in mapping.items()
        if 0 <= code <= 0xFF and _is_valid_unicode_text(text)
    }


def _raw_text_bytes(op: str, operands) -> bytes:
    if op == "Tj" and operands:
        try:
            return bytes(operands[0])
        except Exception:
            return b""
    if op == "TJ" and operands:
        parts: list[bytes] = []
        arr = operands[0]
        if arr is not None:
            try:
                for item in arr:
                    try:
                        if isinstance(item, pikepdf.String):
                            parts.append(bytes(item))
                    except Exception:
                        continue
            except Exception:
                pass
        return b"".join(parts)
    if op == "'" and operands:
        try:
            return bytes(operands[0])
        except Exception:
            return b""
    if op == '"' and len(operands) >= 3:
        try:
            return bytes(operands[2])
        except Exception:
            return b""
    return b""


def _simple_font_zero_byte_repair_candidate(
    font_dict,
    *,
    used_codes: set[int],
    existing_map: dict[int, str],
    generated_map: dict[int, str],
) -> bool:
    if 0 not in used_codes:
        return False

    unresolved_used_codes = {
        code
        for code in used_codes
        if code not in existing_map or not _is_valid_unicode_text(existing_map[code])
    } - set(generated_map)
    return unresolved_used_codes == {0}


def _sanitize_text_showing_zero_bytes(
    op: str,
    operands: Iterable[pikepdf.Object | int | float],
) -> tuple[list[pikepdf.Object | int | float], int]:
    removed = 0
    new_operands = list(operands)

    def _strip_zero_bytes(
        value: pikepdf.Object | int | float,
    ) -> pikepdf.Object | int | float:
        nonlocal removed
        if not isinstance(value, pikepdf.String):
            return value
        raw = bytes(value)
        removed_here = raw.count(0)
        if removed_here <= 0:
            return value
        removed += removed_here
        return pikepdf.String(raw.replace(b"\x00", b""))

    if op == "Tj" and new_operands:
        new_operands[0] = _strip_zero_bytes(new_operands[0])
        return new_operands, removed

    if op == "TJ" and new_operands:
        arr = new_operands[0]
        if isinstance(arr, pikepdf.Array):
            new_arr = pikepdf.Array()
            for item in arr:
                new_arr.append(_strip_zero_bytes(item))
            new_operands[0] = new_arr
        return new_operands, removed

    if op == "'" and new_operands:
        new_operands[0] = _strip_zero_bytes(new_operands[0])
        return new_operands, removed

    if op == '"' and len(new_operands) >= 3:
        new_operands[2] = _strip_zero_bytes(new_operands[2])
        return new_operands, removed

    return new_operands, 0


def _parse_tounicode_map(stream_obj) -> dict[int, str]:
    mapping, _ = _parse_tounicode_map_details(stream_obj)
    return mapping


def _parse_tounicode_map_details(
    stream_obj,
    *,
    invalid_codes: set[int] | None = None,
) -> tuple[dict[int, str], int]:
    if stream_obj is None:
        return {}, 0
    try:
        raw = bytes(stream_obj.read_bytes())
    except Exception:
        return {}, 0

    try:
        text = raw.decode("latin-1")
    except Exception:
        return {}, 0

    mapping: dict[int, str] = {}
    invalid_entries = 0
    mode = ToUnicodeMode.OUTSIDE

    def _bounded_span(start: int, end: int) -> int | None:
        if end < start:
            return None
        span = end - start + 1
        if span > MAX_TOUNICODE_BFRANGE_SPAN:
            return None
        return span

    for raw_line in text.splitlines():
        line = raw_line.split("%", 1)[0].strip()
        if not line:
            continue
        lower = line.lower()
        if lower.endswith("beginbfchar"):
            mode = _tounicode_transition(mode, "beginbfchar")
            continue
        if lower.endswith("beginbfrange"):
            mode = _tounicode_transition(mode, "beginbfrange")
            continue
        if lower.endswith("endbfchar") or lower.endswith("endbfrange"):
            mode = _tounicode_transition(mode, "end")
            continue
        if mode == ToUnicodeMode.OUTSIDE:
            continue

        hex_groups = HEX_STR_RE.findall(line)
        if mode == ToUnicodeMode.BFCHAR:
            if len(hex_groups) < 2:
                continue
            try:
                code = int(hex_groups[0], 16)
            except ValueError:
                continue
            decoded = _decode_tounicode_hex(hex_groups[1])
            if decoded:
                mapping[code] = decoded
            else:
                invalid_entries += 1
                if invalid_codes is not None:
                    invalid_codes.add(code)
            continue

        # bfrange
        if "[" in line and "]" in line:
            if len(hex_groups) < 3:
                continue
            try:
                start = int(hex_groups[0], 16)
                end = int(hex_groups[1], 16)
            except ValueError:
                continue
            targets = hex_groups[2:]
            span = _bounded_span(start, end)
            if span is None:
                invalid_entries += 1
                continue
            for offset, target in enumerate(targets[:span]):
                decoded = _decode_tounicode_hex(target)
                if decoded:
                    mapping[start + offset] = decoded
                else:
                    invalid_entries += 1
                    if invalid_codes is not None:
                        invalid_codes.add(start + offset)
            continue

        if len(hex_groups) < 3:
            continue
        try:
            start = int(hex_groups[0], 16)
            end = int(hex_groups[1], 16)
            seed_raw = bytes.fromhex(hex_groups[2])
            if len(seed_raw) % 2 != 0:
                continue
            seed_text = seed_raw.decode("utf-16-be")
        except Exception:
            continue

        span = _bounded_span(start, end)
        if span is None:
            invalid_entries += 1
            continue
        if len(seed_text) != 1:
            invalid_entries += 1
            continue
        seed_cp = ord(seed_text)
        for offset in range(span):
            cp = seed_cp + offset
            if cp > 0x10FFFF or cp in (0x0000, 0xFFFE, 0xFEFF) or (0xD800 <= cp <= 0xDFFF):
                invalid_entries += 1
                if invalid_codes is not None:
                    invalid_codes.add(start + offset)
                continue
            mapping[start + offset] = chr(cp)

    return mapping, invalid_entries


def _render_tounicode_cmap(mapping: dict[int, str], code_bytes: int) -> bytes:
    if code_bytes not in (1, 2, 4):
        code_bytes = 2
    width = code_bytes * 2
    max_code = (1 << (code_bytes * 8)) - 1
    entries: list[tuple[int, str]] = []
    for code, value in sorted(mapping.items()):
        if code < 0 or code > max_code or not _is_valid_unicode_text(value):
            continue
        entries.append((code, value))

    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        f"<{'0' * width}> <{max_code:0{width}X}>",
        "endcodespacerange",
    ]

    chars: list[tuple[int, str]] = []
    ranges: list[tuple[int, int, str]] = []
    index = 0
    while index < len(entries):
        code, value = entries[index]
        last = index
        if len(value) == 1 and ord(value) <= 0xFFFF:
            while last + 1 < len(entries):
                (code_a, value_a), (code_b, value_b) = entries[last], entries[last + 1]
                if (
                    code_b != code_a + 1
                    or code_b >> 8 != code >> 8
                    or len(value_b) != 1
                    or ord(value_b) != ord(value_a) + 1
                    or ord(value_b) >> 8 != ord(value) >> 8
                ):
                    break
                last += 1
        if last > index:
            ranges.append((code, entries[last][0], value))
        else:
            chars.append((code, value))
        index = last + 1

    def _hex(value: str) -> str:
        return value.encode("utf-16-be").hex().upper()

    # A bfrange may vary only the last byte of its source codes (Adobe
    # Technical Note #5014) and must not carry the last destination byte
    # past 255 (ISO 32000-1, 9.10.3); the checks above keep both within one
    # 256-code block. Supplementary-plane destinations, whose increment some
    # readers apply to the UTF-16 pair differently, stay in bfchar.
    for i in range(0, len(ranges), 100):
        chunk = ranges[i : i + 100]
        lines.append(f"{len(chunk)} beginbfrange")
        for low, high, value in chunk:
            lines.append(f"<{low:0{width}X}> <{high:0{width}X}> <{_hex(value)}>")
        lines.append("endbfrange")
    for i in range(0, len(chars), 100):
        chunk = chars[i : i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for code, value in chunk:
            lines.append(f"<{code:0{width}X}> <{_hex(value)}>")
        lines.append("endbfchar")

    lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
            "",
        ]
    )
    return "\n".join(lines).encode("ascii")


def _merge_tounicode_maps(
    existing: dict[int, str], generated: dict[int, str]
) -> tuple[dict[int, str], int]:
    merged = {code: text for code, text in existing.items() if _is_valid_unicode_text(text)}
    overwritten = 0
    for code, text in generated.items():
        if not _is_valid_unicode_text(text):
            continue
        previous = merged.get(code)
        if previous and previous != text:
            overwritten += 1
        merged[code] = text
    return merged, overwritten


def _collect_gid_to_unicode(font_bytes: bytes) -> dict[int, str]:
    from fontTools.ttLib import TTFont

    gid_to_unicode: dict[int, str] = {}
    tt = TTFont(BytesIO(font_bytes), lazy=True)
    try:
        name_to_unicode: dict[str, str] = {}
        cmap_table = tt.get("cmap")
        if cmap_table is not None:
            for cmap in cmap_table.tables:
                table_map = getattr(cmap, "cmap", None)
                if not isinstance(table_map, dict):
                    continue
                for codepoint, glyph_name in table_map.items():
                    if not isinstance(codepoint, int):
                        continue
                    if codepoint < 0 or codepoint > 0x10FFFF:
                        continue
                    try:
                        ch = chr(codepoint)
                    except ValueError:
                        continue
                    if not _is_valid_unicode_text(ch):
                        continue
                    name_to_unicode.setdefault(str(glyph_name), ch)

        num_glyphs = 0
        try:
            maxp = tt.get("maxp")
            num_glyphs = int(getattr(maxp, "numGlyphs", 0))
        except Exception:
            num_glyphs = 0

        for gid in range(max(0, num_glyphs)):
            try:
                glyph_name = tt.getGlyphName(gid)
            except Exception:
                continue

            value = name_to_unicode.get(glyph_name, "")
            if _is_valid_unicode_text(value):
                gid_to_unicode[gid] = value
    finally:
        try:
            tt.close()
        except Exception:
            pass

    return gid_to_unicode
