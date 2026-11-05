"""Repair CID dictionaries and embedded font width mismatches."""

from io import BytesIO
from pathlib import Path

from app.pipeline.fonts.names import FONT_SUBSET_RE
from app.pipeline.fonts.notdef import repair_cid_notdef
from app.pipeline.fonts.resources import (
    _has_embedded_font,
    _iter_pdf_fonts,
    _obj_key,
    _resolve_dictionary,
)
from app.pipeline.fonts.widths import _align_truetype_advances
from app.services.pdf_objects import pdf_text
from app.services.thread_work import to_thread


def _parse_cid_widths_array(widths_obj) -> dict[int, int]:
    import pikepdf

    widths: dict[int, int] = {}
    if not isinstance(widths_obj, pikepdf.Array):
        return widths

    idx = 0
    while idx < len(widths_obj):
        try:
            start_cid = int(widths_obj[idx])
        except Exception:
            break
        idx += 1
        if idx >= len(widths_obj):
            break

        entry = widths_obj[idx]
        idx += 1
        if isinstance(entry, pikepdf.Array):
            cid = start_cid
            for width_value in entry:
                try:
                    widths[cid] = int(round(float(width_value)))
                except Exception:
                    pass
                cid += 1
            continue

        try:
            end_cid = int(entry)
        except Exception:
            break
        if idx >= len(widths_obj):
            break
        try:
            width = int(round(float(widths_obj[idx])))
        except Exception:
            idx += 1
            continue
        idx += 1
        for cid in range(start_cid, end_cid + 1):
            widths[cid] = width

    return widths


def _render_cid_widths_array(widths: dict[int, int]):
    import pikepdf

    array = pikepdf.Array()
    if not widths:
        return array

    sorted_items = sorted(widths.items())
    run_start, first_width = sorted_items[0]
    run_widths = [first_width]

    for cid, width in sorted_items[1:]:
        if cid == run_start + len(run_widths):
            run_widths.append(width)
            continue
        array.append(run_start)
        array.append(pikepdf.Array(run_widths))
        run_start = cid
        run_widths = [width]

    array.append(run_start)
    array.append(pikepdf.Array(run_widths))
    return array


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


def _collect_cid_cff_widths(font_bytes: bytes) -> dict[int, int]:
    from fontTools.cffLib import CFFFontSet
    from fontTools.pens.basePen import NullPen

    widths: dict[int, int] = {}
    if not font_bytes:
        return widths

    font_set = CFFFontSet()
    font_set.decompile(BytesIO(font_bytes), None)
    for font_name in font_set.keys():
        cff_font = font_set[font_name]
        if not getattr(cff_font, "ROS", None):
            continue
        charset = getattr(cff_font, "charset", None)
        char_strings = getattr(cff_font, "CharStrings", None)
        if not isinstance(charset, list) or char_strings is None:
            continue
        for glyph_index, glyph_name in enumerate(charset):
            try:
                char_string = char_strings[glyph_name]
                char_string.draw(NullPen())
            except Exception:
                continue
            width = getattr(char_string, "width", None)
            if isinstance(width, (int, float)):
                cid = _cid_cff_width_key(glyph_name, glyph_index)
                widths[cid] = int(round(width))
    return widths


def _repair_pdf_font_dicts_sync(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    """Apply safe dictionary-level fixes for CID font compliance issues."""
    import pikepdf

    stats = {
        "fonts_touched": 0,
        "cidtogid_added": 0,
        "cidset_removed": 0,
        "truetype_widths_aligned": 0,
        "truetype_width_conflicts": 0,
        "notdef_mappings_repaired": 0,
        "invisible_fonts_replaced": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()

    def _repair_cid_font_dict(cid_font) -> None:
        if not isinstance(cid_font, pikepdf.Dictionary):
            return
        obj_key = _obj_key(cid_font)
        if obj_key and obj_key in seen_fonts:
            return

        changed = False
        subtype = cid_font.get("/Subtype")
        if subtype not in (pikepdf.Name("/CIDFontType2"), pikepdf.Name("/CIDFontType0")):
            if obj_key:
                seen_fonts.add(obj_key)
            return

        descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
        if (
            subtype == pikepdf.Name("/CIDFontType2")
            and _has_embedded_font(descriptor)
            and pikepdf.Name("/CIDToGIDMap") not in cid_font
        ):
            cid_font[pikepdf.Name("/CIDToGIDMap")] = pikepdf.Name("/Identity")
            stats["cidtogid_added"] += 1
            changed = True

        if (
            isinstance(descriptor, pikepdf.Dictionary)
            and pikepdf.Name("/CIDSet") in descriptor
            and FONT_SUBSET_RE.match(pdf_text(cid_font.get("/BaseFont")).lstrip("/"))
        ):
            del descriptor[pikepdf.Name("/CIDSet")]
            stats["cidset_removed"] += 1
            changed = True

        if changed:
            stats["fonts_touched"] += 1
        if obj_key:
            seen_fonts.add(obj_key)

    def _process_font(font_dict) -> None:
        subtype = font_dict.get("/Subtype")
        if subtype == pikepdf.Name("/TrueType"):
            font_key = _obj_key(font_dict)
            if font_key and font_key in seen_fonts:
                return
            if font_key:
                seen_fonts.add(font_key)
            descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
            if not isinstance(descriptor, pikepdf.Dictionary):
                return
            program = descriptor.get("/FontFile2")
            if not isinstance(program, pikepdf.Stream):
                return
            try:
                aligned, count, conflicts = _align_truetype_advances(
                    font_dict, program.read_bytes()
                )
            except Exception:
                # One malformed/unsupported font must not prevent repairs of
                # other fonts. Validation retains its unresolved failure.
                return
            stats["truetype_width_conflicts"] += conflicts
            if count:
                replacement = pdf.make_stream(aligned)
                replacement["/Length1"] = len(aligned)
                # A descriptor/program may be shared by dictionaries with
                # different encodings or widths. Repair this use independently.
                descriptor = pikepdf.Dictionary(descriptor)
                descriptor["/FontFile2"] = replacement
                font_dict["/FontDescriptor"] = pdf.make_indirect(descriptor)
                stats["truetype_widths_aligned"] += count
                stats["fonts_touched"] += 1
        elif subtype == pikepdf.Name("/Type0"):
            descendants = font_dict.get("/DescendantFonts")
            if not isinstance(descendants, pikepdf.Array):
                return
            for descendant in descendants:
                cid_font = _resolve_dictionary(descendant)
                _repair_cid_font_dict(cid_font)
                if isinstance(cid_font, pikepdf.Dictionary):
                    count = repair_cid_notdef(pdf, font_dict, cid_font)
                    stats["notdef_mappings_repaired"] += count
                    if count:
                        stats["fonts_touched"] += 1
        elif subtype == pikepdf.Name("/CIDFontType2"):
            _repair_cid_font_dict(font_dict)

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for font_dict in _iter_pdf_fonts(pdf):
                _process_font(font_dict)

            from app.pipeline.fonts.invisible import repair_invisible_fonts
            from app.pipeline.fonts.notdef_content import repair_notdef_content

            invisible_stats = repair_invisible_fonts(pdf)
            stats.update(invisible_stats)
            stats["fonts_touched"] += invisible_stats["invisible_fonts_replaced"]
            content_stats = repair_notdef_content(pdf)
            stats.update(content_stats)
            if stats["fonts_touched"] <= 0 and not any(content_stats.values()):
                return False, "No eligible font dictionary or TrueType width repairs found", stats

            pdf.save(str(output_path))
        return (
            True,
            (
                f"Font dictionary repair completed "
                f"(fonts={stats['fonts_touched']}, "
                f"CIDToGIDMap added={stats['cidtogid_added']}, "
                f"CIDSet removed={stats['cidset_removed']}, "
                f"TrueType advances aligned={stats['truetype_widths_aligned']}, "
                f"invisible fonts replaced={stats['invisible_fonts_replaced']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"Font dictionary repair failed: {exc}", stats


async def _repair_pdf_font_dicts(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await to_thread(_repair_pdf_font_dicts_sync, input_path, output_path)


def _sync_pdf_cid_cff_widths_sync(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    """Sync CIDFontType0 width arrays to embedded CFF program widths."""
    import pikepdf

    stats = {
        "fonts_touched": 0,
        "widths_synced": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()

    def _repair_cid_font_widths(cid_font) -> None:
        if not isinstance(cid_font, pikepdf.Dictionary):
            return
        obj_key = _obj_key(cid_font)
        if obj_key and obj_key in seen_fonts:
            return

        try:
            if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType0"):
                return
            widths_obj = cid_font.get("/W")
            current_widths = _parse_cid_widths_array(widths_obj)
            if not current_widths:
                return

            descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
            if not isinstance(descriptor, pikepdf.Dictionary):
                return
            font_stream = _resolve_dictionary(descriptor.get("/FontFile3"))
            if font_stream is None:
                return
            if font_stream.get("/Subtype") != pikepdf.Name("/CIDFontType0C"):
                return
            try:
                font_bytes = bytes(font_stream.read_bytes())
            except Exception:
                return
            program_widths = _collect_cid_cff_widths(font_bytes)
            if not program_widths:
                return

            updated_widths = dict(current_widths)
            font_changed = False
            for cid, current_width in current_widths.items():
                program_width = program_widths.get(cid)
                if program_width is None or program_width == current_width:
                    continue
                updated_widths[cid] = program_width
                stats["widths_synced"] += 1
                font_changed = True

            if font_changed:
                cid_font[pikepdf.Name("/W")] = _render_cid_widths_array(updated_widths)
                stats["fonts_touched"] += 1
        finally:
            if obj_key:
                seen_fonts.add(obj_key)

    def _process_font(font_dict) -> None:
        subtype = font_dict.get("/Subtype")
        if subtype == pikepdf.Name("/Type0"):
            descendants = font_dict.get("/DescendantFonts")
            if not isinstance(descendants, pikepdf.Array):
                return
            for descendant in descendants:
                _repair_cid_font_widths(_resolve_dictionary(descendant))
        elif subtype == pikepdf.Name("/CIDFontType0"):
            _repair_cid_font_widths(font_dict)

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for font_dict in _iter_pdf_fonts(pdf):
                _process_font(font_dict)
            if stats["widths_synced"] <= 0:
                return False, "No eligible CID CFF width mismatches found", stats
            pdf.save(str(output_path))
        return (
            True,
            (
                f"CID CFF width sync completed "
                f"(fonts={stats['fonts_touched']}, widths synced={stats['widths_synced']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"CID CFF width sync failed: {exc}", stats


async def _sync_pdf_cid_cff_widths(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await to_thread(_sync_pdf_cid_cff_widths_sync, input_path, output_path)
