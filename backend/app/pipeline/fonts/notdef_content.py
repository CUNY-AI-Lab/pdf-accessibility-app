"""Remove blank .notdef references and rewrite painted CID-zero references."""

from io import BytesIO

import pikepdf
from fontTools.ttLib import TTFont

from app.pipeline.fonts.encoding import _parse_tounicode_map, _render_tounicode_cmap
from app.pipeline.fonts.invisible import INVISIBLE, _render_mode
from app.pipeline.fonts.resources import TOUNICODE_RESOURCES, _iter_pdf_resource_roots
from app.pipeline.fonts.widths import _truetype_code_glyphs


def _cid_widths(values):
    """Read /W without rounding fractional PDF advances."""
    widths = {}
    if not isinstance(values, pikepdf.Array):
        return widths
    index = 0
    while index + 1 < len(values):
        start, following = int(values[index]), values[index + 1]
        if isinstance(following, pikepdf.Array):
            widths.update({start + offset: float(width) for offset, width in enumerate(following)})
            index += 2
        elif index + 2 < len(values):
            widths.update(
                {code: float(values[index + 2]) for code in range(start, int(following) + 1)}
            )
            index += 3
        else:
            break
    return widths


def repair_notdef_content(pdf) -> dict[str, int]:
    """Preserve advances, q/Q state and quote-operator side effects.

    Plans are compared before writing shared streams: differing inherited
    text states must never make the first invocation change the later ones.
    Codes shown in rendering mode 3 are never removed: an invisible OCR layer
    is the page's recognized text.
    """
    stats = {"notdef_glyphs_removed": 0, "notdef_codes_remapped": 0}
    actions, font_info, plans = {}, {}, {}

    def info(font):
        key = font.objgen if font.is_indirect else font.unparse()
        if key in font_info:
            return key, font_info[key]
        subtype = font.get("/Subtype")
        cid = font.DescendantFonts[0] if subtype == "/Type0" else font
        program = cid.get("/FontDescriptor", {}).get("/FontFile2")
        result = None
        if isinstance(program, pikepdf.Stream) and (
            subtype == "/TrueType"
            or (subtype == "/Type0" and font.get("/Encoding") == "/Identity-H")
        ):
            with TTFont(BytesIO(program.read_bytes()), recalcTimestamp=False) as tt:
                if "glyf" in tt:
                    order = tt.getGlyphOrder()
                    empty = {
                        i for i, name in enumerate(order) if tt["glyf"][name].numberOfContours == 0
                    }
                    code_glyphs = (
                        _truetype_code_glyphs(font, tt)
                        if subtype == "/TrueType" and "cmap" in tt
                        else {}
                    )
                    result = (cid, order, empty, code_glyphs)
        font_info[key] = result
        return key, result

    def action(font, code):
        key, data = info(font)
        if (key, code) in actions:
            return actions[key, code]
        result = None
        if data is not None:
            cid, order, empty, mapping = data
            composite = font.get("/Subtype") == "/Type0"
            raw = None
            if composite:
                cidmap = cid.get("/CIDToGIDMap")
                raw = cidmap.read_bytes() if isinstance(cidmap, pikepdf.Stream) else None
                gid = code if raw is None else int.from_bytes(raw[2 * code : 2 * code + 2], "big")
                widths = _cid_widths(cid.get("/W"))
                width = widths.get(code, float(cid.get("/DW", 1000)))
            else:
                glyph = mapping.get(code, ".notdef")
                gid = order.index(glyph) if glyph in order else 0
                first = int(font.get("/FirstChar", 0))
                widths = font.get("/Widths", [])
                width = (
                    float(widths[code - first])
                    if 0 <= code - first < len(widths)
                    else float(font.get("/FontDescriptor", {}).get("/MissingWidth", 0))
                )
            if gid == 0 and 0 in empty:
                result = ("remove", None, width)
            elif composite and code == 0 and gid != 0:
                # CID 0 itself remains .notdef to validators. Give the real
                # glyph its own nonzero CID and rewrite every reference.
                semantics = _parse_tounicode_map(font.get("/ToUnicode"))
                alias = max([len(order), len(raw or b"") // 2, *semantics, *widths], default=0) + 1
                if alias <= 65535 and code in semantics:
                    gids = bytearray(
                        raw
                        if raw is not None
                        else b"".join(i.to_bytes(2, "big") for i in range(alias + 1))
                    )
                    gids.extend(b"\0" * max(0, 2 * (alias + 1) - len(gids)))
                    gids[2 * alias : 2 * alias + 2] = gid.to_bytes(2, "big")
                    cid.CIDToGIDMap = pdf.make_stream(bytes(gids))
                    cid.W = pikepdf.Array([*list(cid.get("/W", [])), alias, pikepdf.Array([width])])
                    semantics[alias] = semantics[code]
                    font.ToUnicode = pdf.make_stream(_render_tounicode_cmap(semantics, 2))
                    result = ("remap", alias, width)
            elif not composite and gid == 0:
                # Use an already encoded real glyph when author semantics
                # identify it; unknown painted missing letters remain held.
                semantics = _parse_tounicode_map(font.get("/ToUnicode"))
                target = semantics.get(code)
                for candidate, glyph in mapping.items():
                    if glyph != ".notdef" and target and semantics.get(candidate) == target:
                        offset = candidate - int(font.get("/FirstChar", 0))
                        if 0 <= offset < len(widths) and float(widths[offset]) == width:
                            result = ("remap", candidate, width)
                            break
        actions[key, code] = result
        return result

    def rewrite(owner, resources, state=(None, 0.0, 0.0, 0.0, 0), ancestors=frozenset()):
        key = owner.objgen
        if key in ancestors:
            return
        try:
            instructions = list(pikepdf.parse_content_stream(owner))
        except (pikepdf.PdfError, ValueError, TypeError):
            return
        font, size, spacing, word_space, render = state
        stack, output = [], []
        removed = remapped = 0
        for instruction in instructions:
            op, args = str(instruction.operator), list(instruction.operands)
            if op == "q":
                stack.append((font, size, spacing, word_space, render))
            elif op == "Q" and stack:
                font, size, spacing, word_space, render = stack.pop()
            elif op == "Tf":
                font = resources.get("/Font", {}).get(str(args[0]))
                size = float(args[1])
            elif op == "Tc":
                spacing = float(args[0])
            elif op == "Tw":
                word_space = float(args[0])
            elif op == "Tr" and args:
                render = _render_mode(args[0])
            elif op == "Do":
                child = resources.get("/XObject", {}).get(str(args[0]))
                if child is not None and child.get("/Subtype") == "/Form":
                    rewrite(
                        child,
                        child.get("/Resources", resources),
                        (font, size, spacing, word_space, render),
                        ancestors | {key},
                    )
            if op not in {"Tj", "TJ", "'", '"'} or font is None:
                output.append(instruction)
                continue
            next_spacing, next_word_space = (
                (float(args[1]), float(args[0])) if op == '"' else (spacing, word_space)
            )
            values = list(args[0]) if op == "TJ" else [args[-1]]
            parts, changed = [], False
            code_bytes = 2 if font.get("/Subtype") == "/Type0" else 1
            for value in values:
                if not isinstance(value, pikepdf.String):
                    parts.append(value)
                    continue
                raw, chunk = bytes(value), bytearray()
                if len(raw) % code_bytes:
                    parts.append(value)
                    continue
                for offset in range(0, len(raw), code_bytes):
                    token = raw[offset : offset + code_bytes]
                    code = int.from_bytes(token, "big")
                    decision = action(font, code)
                    extra = next_spacing + (
                        next_word_space if code == 32 and code_bytes == 1 else 0
                    )
                    if decision is None or (
                        decision[0] == "remove" and (render == INVISIBLE or (not size and extra))
                    ):
                        chunk.extend(token)
                        continue
                    changed = True
                    if decision[0] == "remap":
                        chunk.extend(int(decision[1]).to_bytes(code_bytes, "big"))
                        remapped += 1
                    else:
                        if chunk:
                            parts.append(pikepdf.String(bytes(chunk)))
                            chunk.clear()
                        parts.append(-float(decision[2]) - (1000 * extra / size if size else 0))
                        removed += 1
                if chunk:
                    parts.append(pikepdf.String(bytes(chunk)))
            if changed:
                if op == '"':
                    output.extend(
                        [
                            pikepdf.ContentStreamInstruction([args[0]], pikepdf.Operator("Tw")),
                            pikepdf.ContentStreamInstruction([args[1]], pikepdf.Operator("Tc")),
                        ]
                    )
                if op in {"'", '"'}:
                    output.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
                output.append(
                    pikepdf.ContentStreamInstruction([pikepdf.Array(parts)], pikepdf.Operator("TJ"))
                )
            else:
                output.append(instruction)
            spacing, word_space = next_spacing, next_word_space
        data = pikepdf.unparse_content_stream(output)
        previous = plans.get(key)
        if previous is None:
            plans[key] = [owner, data, removed, remapped]
        elif previous[1] != data:
            previous[1] = None

    def appearance(obj, resources, ancestors=frozenset()):
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)) or (
            obj.is_indirect and obj.objgen in ancestors
        ):
            return
        resources = obj.get("/Resources", resources) or {}
        if isinstance(obj, pikepdf.Stream):
            rewrite(obj, resources)
        else:
            for _, child in obj.items():
                appearance(
                    child, resources, ancestors | ({obj.objgen} if obj.is_indirect else set())
                )

    for page in pdf.pages:
        rewrite(page, page.get("/Resources", {}))
    for kind, obj, resources in _iter_pdf_resource_roots(pdf, TOUNICODE_RESOURCES):
        if kind == "appearance":
            appearance(obj, resources)
    for owner, data, removed, remapped in plans.values():
        if data is not None and (removed or remapped):
            if isinstance(owner, pikepdf.Page):
                owner.Contents = pdf.make_stream(data)
            else:
                owner.write(data)
            stats["notdef_glyphs_removed"] += removed
            stats["notdef_codes_remapped"] += remapped
    return stats
