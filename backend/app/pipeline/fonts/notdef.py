"""Recover missing CID glyphs from real outlines without replacing the font."""

from io import BytesIO

import pikepdf

from app.pipeline.fonts.encoding import _parse_tounicode_map, _render_tounicode_cmap


def _hollow_rectangle(glyph) -> bool:
    """Positive outline evidence for an empty box, never arbitrary missing text."""
    if glyph.numberOfContours != 2 or list(glyph.endPtsOfContours) != [3, 7]:
        return False
    if not all(flag & 1 for flag in glyph.flags):
        return False
    boxes = []
    winding = []
    for start in (0, 4):
        points = list(glyph.coordinates[start : start + 4])
        xs, ys = sorted({p[0] for p in points}), sorted({p[1] for p in points})
        if len(xs) != 2 or len(ys) != 2 or set(points) != {(x, y) for x in xs for y in ys}:
            return False
        if any(a[0] != b[0] and a[1] != b[1] for a, b in zip(points, points[1:] + points[:1])):
            return False
        winding.append(
            sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))
        )
        boxes.append((xs[0], ys[0], xs[1], ys[1]))
    outer, inner = sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    return (
        winding[0] * winding[1] < 0
        and outer[0] < inner[0] < inner[2] < outer[2]
        and outer[1] < inner[1] < inner[3] < outer[3]
    )


def _box_glyph(font, missing, text):
    """A proven hollow rectangle is already the outline of the known square.

    Copy that ink to a real Unicode square glyph; retain .notdef separately
    and preserve every point, advance and subsequent glyph ID.
    """
    from copy import deepcopy

    return deepcopy(missing)


def repair_cid_notdef(pdf, parent, cid_font) -> int:
    """Map known Unicode to a real glyph; leave unresolved content for validation.

    Only Identity CMaps have a deterministic code-to-CID relation here. Keep
    the embedded program and all existing glyph IDs. A visible .notdef box
    must never be artifacted. The only geometric fallback is an empty square,
    supported by both a rectangular outline and a square/PUA author mapping.
    """
    from fontTools.ttLib import TTFont

    if str(parent.get("/Encoding")) not in {"/Identity-H", "/Identity-V"}:
        return 0
    descriptor = cid_font.get("/FontDescriptor", {})
    program = descriptor.get("/FontFile2")
    if not isinstance(program, pikepdf.Stream):
        return 0
    semantics = _parse_tounicode_map(parent.get("/ToUnicode"))
    cidmap = cid_font.get("/CIDToGIDMap")
    if cidmap is None or cidmap == pikepdf.Name.Identity:
        mapping = None
    elif isinstance(cidmap, pikepdf.Stream):
        mapping = cidmap.read_bytes()
    else:
        return 0
    changes = {}
    with TTFont(BytesIO(program.read_bytes()), recalcTimestamp=False) as font:
        if "glyf" not in font or "cmap" not in font:
            return 0
        order = list(font.getGlyphOrder())
        cmap = font.getBestCmap() or {}
        missing = font["glyf"][order[0]]
        added = False
        for code, text in semantics.items():
            if not 0 <= code <= 65535 or len(text) != 1:
                continue
            gid = (
                code if mapping is None else int.from_bytes(mapping[2 * code : 2 * code + 2], "big")
            )
            if gid != 0:
                continue
            glyph = cmap.get(ord(text))
            if not glyph or glyph == ".notdef":
                if not _hollow_rectangle(missing) or not (
                    text in {"\u2610", "\u25a1"} or 0xE000 <= ord(text) <= 0xF8FF
                ):
                    continue
                text = text if text in {"\u2610", "\u25a1"} else "\u25a1"
                glyph = cmap.get(ord(text))
                if not glyph:
                    outline = _box_glyph(font, missing, text)
                    if outline is None:
                        continue
                    glyph = f"uni{ord(text):04X}.recovered"
                    font["glyf"][glyph] = outline
                    font["hmtx"].metrics[glyph] = font["hmtx"].metrics[order[0]]
                    order.append(glyph)
                    font.setGlyphOrder(order)
                    for table in font["cmap"].tables:
                        if table.isUnicode():
                            table.cmap[ord(text)] = glyph
                    cmap[ord(text)] = glyph
                    added = True
            changes[code] = font.getGlyphID(glyph)
            semantics[code] = text
        if not changes:
            return 0
        # Materialize Identity before overriding selected CIDs; preserve every
        # other CID, including explicit zero mappings in an existing stream.
        count = max(len(order), max(changes) + 1, len(mapping or b"") // 2)
        gids = bytearray(mapping or b"".join(i.to_bytes(2, "big") for i in range(count)))
        gids.extend(b"\0" * max(0, 2 * count - len(gids)))
        for code, gid in changes.items():
            gids[2 * code : 2 * code + 2] = gid.to_bytes(2, "big")
        cid_font.CIDToGIDMap = pdf.make_stream(bytes(gids))
        if added:
            buffer = BytesIO()
            font.save(buffer)
            descriptor = pikepdf.Dictionary(descriptor)
            descriptor.FontFile2 = pdf.make_stream(buffer.getvalue())
            descriptor.FontFile2.Length1 = len(buffer.getvalue())
            cid_font.FontDescriptor = pdf.make_indirect(descriptor)
        parent.ToUnicode = pdf.make_stream(_render_tounicode_cmap(semantics, 2))
    return len(changes)
