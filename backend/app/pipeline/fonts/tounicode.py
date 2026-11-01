"""Rebuild ToUnicode maps and remove eligible zero-byte text residue."""

import asyncio
from pathlib import Path

from app.pipeline.fonts.encoding import (
    _collect_gid_to_unicode,
    _is_valid_unicode_text,
    _merge_tounicode_maps,
    _parse_tounicode_map_details,
    _raw_text_bytes,
    _render_tounicode_cmap,
    _sanitize_text_showing_zero_bytes,
    _simple_font_auto_unicode_policy,
    _simple_font_unicode_map,
    _simple_font_zero_byte_repair_candidate,
)
from app.pipeline.fonts.resources import (
    TOUNICODE_RESOURCES,
    ZERO_BYTE_RESOURCES,
    _font_stream_bytes,
    _is_form_xobject,
    _is_pdf_mapping,
    _iter_pdf_fonts,
    _obj_key,
    _resolve_dictionary,
)
from app.pipeline.fonts.symbols import _symbol_family
from app.services.pdf_objects import pdf_named_resource


def _repair_pdf_tounicode_sync(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    """Rebuild/augment ToUnicode maps for embedded Type0/CIDFontType2 fonts."""
    import pikepdf

    stats = {
        "fonts_touched": 0,
        "maps_rebuilt": 0,
        "mappings_generated": 0,
        "mappings_total": 0,
        "invalid_entries_removed": 0,
        "mappings_overridden": 0,
        "stale_entries_removed": 0,
        "text_operands_sanitized": 0,
        "zero_bytes_removed": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()
    used_simple_codes: dict[tuple[int, int], set[int]] = {}
    seen_content_streams: set[tuple[int, int]] = set()
    seen_collect_appearances: set[tuple[int, int]] = set()

    def _cid_to_gid_mapping(cid_font) -> tuple[dict[int, int] | None, bool]:
        cid_to_gid = cid_font.get("/CIDToGIDMap")
        if cid_to_gid is None:
            return None, True
        if cid_to_gid == pikepdf.Name("/Identity"):
            return None, True

        stream_obj = _resolve_dictionary(cid_to_gid)
        if stream_obj is None:
            return None, True
        try:
            raw = bytes(stream_obj.read_bytes())
        except Exception:
            return None, True

        if len(raw) < 2:
            return {}, False

        mapping: dict[int, int] = {}
        pair_count = len(raw) // 2
        for cid in range(pair_count):
            gid = int.from_bytes(raw[cid * 2 : (cid + 1) * 2], "big")
            mapping[cid] = gid
        return mapping, False

    def _collect_used_simple_font_codes(content_obj, resources) -> None:
        if resources is None:
            return
        resolved_obj = _resolve_dictionary(content_obj)
        if resolved_obj is None:
            return
        content_key = _obj_key(resolved_obj)
        if content_key and content_key in seen_content_streams:
            return
        if content_key:
            seen_content_streams.add(content_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        xobjects = _resolve_dictionary(resources.get("/XObject"))
        current_font = None
        try:
            instructions = pikepdf.parse_content_stream(resolved_obj)
        except Exception:
            return

        for instr in instructions:
            op = str(instr.operator)
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            if op == "Tf" and operands and isinstance(fonts, pikepdf.Dictionary):
                font_ref = pdf_named_resource(fonts, operands[0])
                current_font = _resolve_dictionary(font_ref)
                continue

            if op in ("Tj", "TJ", "'", '"') and isinstance(current_font, pikepdf.Dictionary):
                if current_font.get("/Subtype") not in (
                    pikepdf.Name("/Type1"),
                    pikepdf.Name("/MMType1"),
                    pikepdf.Name("/TrueType"),
                ):
                    continue
                font_key = _obj_key(current_font)
                if not font_key:
                    continue
                raw = _raw_text_bytes(op, operands)
                if not raw:
                    continue
                used_simple_codes.setdefault(font_key, set()).update(raw)
                continue

            if op == "Do" and operands and isinstance(xobjects, pikepdf.Dictionary):
                xobject = _resolve_dictionary(pdf_named_resource(xobjects, operands[0]))
                if not _is_form_xobject(xobject):
                    continue
                child_resources = _resolve_dictionary(xobject.get("/Resources")) or resources
                _collect_used_simple_font_codes(xobject, child_resources)

    def _collect_used_simple_font_codes_from_appearance(obj, fallback_resources) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_collect_appearances:
            return
        if appearance_key:
            seen_collect_appearances.add(appearance_key)

        appearance_resources = (
            _resolve_dictionary(appearance_obj.get("/Resources")) or fallback_resources
        )
        if hasattr(appearance_obj, "read_bytes"):
            _collect_used_simple_font_codes(appearance_obj, appearance_resources)

        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _collect_used_simple_font_codes_from_appearance(child, appearance_resources)

    zero_byte_candidate_fonts: set[tuple[int, int]] = set()
    candidate_seen_fonts: set[tuple[int, int]] = set()

    def _collect_zero_byte_candidate(font_dict) -> None:
        if font_dict.get("/Subtype") not in (
            pikepdf.Name("/Type1"),
            pikepdf.Name("/MMType1"),
            pikepdf.Name("/TrueType"),
        ):
            return

        font_key = _obj_key(font_dict)
        if font_key and font_key in candidate_seen_fonts:
            return
        if font_key:
            candidate_seen_fonts.add(font_key)

        used_codes = used_simple_codes.get(font_key or (-1, -1), set())
        if 0 not in used_codes:
            return

        existing_stream = _resolve_dictionary(font_dict.get("/ToUnicode"))
        existing_map, _ = _parse_tounicode_map_details(existing_stream)
        descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
        font_bytes = _font_stream_bytes(descriptor)
        generated_map = {}
        if _simple_font_auto_unicode_policy(font_dict, font_bytes=font_bytes) != "blocked":
            generated_map = _simple_font_unicode_map(font_dict, font_bytes)

        if _simple_font_zero_byte_repair_candidate(
            font_dict,
            used_codes=used_codes,
            existing_map=existing_map,
            generated_map=generated_map,
        ):
            zero_byte_candidate_fonts.add(font_key)

    rewritten_content_streams: set[tuple[int, int]] = set()
    sanitized_font_keys: set[tuple[int, int]] = set()
    sanitized_appearance_streams: set[tuple[int, int]] = set()

    def _write_content_stream(pdf, content_obj, resolved_obj, instructions) -> None:
        stream_bytes = pikepdf.unparse_content_stream(instructions)
        if isinstance(content_obj, pikepdf.Page):
            content_obj["/Contents"] = pdf.make_stream(stream_bytes)
        else:
            resolved_obj.write(stream_bytes)

    def _sanitize_simple_font_zero_bytes(pdf, content_obj, resources) -> None:
        if resources is None or not zero_byte_candidate_fonts:
            return

        resolved_obj = _resolve_dictionary(content_obj)
        if resolved_obj is None:
            return
        content_key = _obj_key(resolved_obj)
        if content_key and content_key in rewritten_content_streams:
            return
        if content_key:
            rewritten_content_streams.add(content_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        xobjects = _resolve_dictionary(resources.get("/XObject"))
        parse_target = content_obj if isinstance(content_obj, pikepdf.Page) else resolved_obj
        current_font = None
        changed = False
        new_instructions = []

        try:
            instructions = list(pikepdf.parse_content_stream(parse_target))
        except Exception:
            return

        for instr in instructions:
            op = str(instr.operator)
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            current_instruction = instr

            if op == "Tf" and operands and isinstance(fonts, pikepdf.Dictionary):
                current_font = _resolve_dictionary(pdf_named_resource(fonts, operands[0]))
            elif op in ("Tj", "TJ", "'", '"') and isinstance(current_font, pikepdf.Dictionary):
                font_key = _obj_key(current_font)
                if font_key in zero_byte_candidate_fonts:
                    new_operands, removed = _sanitize_text_showing_zero_bytes(op, operands)
                    if removed > 0:
                        current_instruction = pikepdf.ContentStreamInstruction(
                            new_operands,
                            instr.operator,
                        )
                        stats["text_operands_sanitized"] += 1
                        stats["zero_bytes_removed"] += removed
                        sanitized_font_keys.add(font_key)
                        changed = True
            elif op == "Do" and operands and isinstance(xobjects, pikepdf.Dictionary):
                xobject = _resolve_dictionary(pdf_named_resource(xobjects, operands[0]))
                if _is_form_xobject(xobject):
                    child_resources = _resolve_dictionary(xobject.get("/Resources")) or resources
                    _sanitize_simple_font_zero_bytes(pdf, xobject, child_resources)

            new_instructions.append(current_instruction)

        if changed:
            _write_content_stream(pdf, content_obj, resolved_obj, new_instructions)

    def _sanitize_simple_font_zero_bytes_from_appearance(pdf, obj, fallback_resources) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in sanitized_appearance_streams:
            return
        if appearance_key:
            sanitized_appearance_streams.add(appearance_key)

        appearance_resources = (
            _resolve_dictionary(appearance_obj.get("/Resources")) or fallback_resources
        )
        if hasattr(appearance_obj, "read_bytes"):
            _sanitize_simple_font_zero_bytes(pdf, appearance_obj, appearance_resources)

        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _sanitize_simple_font_zero_bytes_from_appearance(pdf, child, appearance_resources)

    def _rebuild_type0_tounicode(pdf, type0_font, cid_font) -> tuple[bool, int, int, int, int]:
        descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
        font_bytes = _font_stream_bytes(descriptor)
        gid_to_unicode: dict[int, str] = {}
        if font_bytes:
            try:
                gid_to_unicode = _collect_gid_to_unicode(font_bytes)
            except Exception:
                gid_to_unicode = {}

        cid_map, is_identity = _cid_to_gid_mapping(cid_font)
        generated: dict[int, str] = {}
        if gid_to_unicode:
            if is_identity:
                for gid, text in gid_to_unicode.items():
                    if gid < 0:
                        continue
                    generated[gid] = text
            else:
                for cid, gid in (cid_map or {}).items():
                    text = gid_to_unicode.get(gid)
                    if text:
                        generated[cid] = text

        existing_stream = _resolve_dictionary(type0_font.get("/ToUnicode"))
        existing_map, invalid_entries = _parse_tounicode_map_details(existing_stream)
        merged_map, overwritten = _merge_tounicode_maps(existing_map, generated)
        if not merged_map:
            return False, 0, len(generated), invalid_entries, overwritten

        if existing_map == merged_map and invalid_entries <= 0 and overwritten <= 0:
            return False, len(merged_map), len(generated), 0, 0

        max_code = max(merged_map.keys(), default=0)
        code_bytes = 1 if max_code <= 0xFF else (2 if max_code <= 0xFFFF else 4)
        cmap_bytes = _render_tounicode_cmap(merged_map, code_bytes)
        type0_font[pikepdf.Name("/ToUnicode")] = pdf.make_stream(cmap_bytes)
        return True, len(merged_map), len(generated), invalid_entries, overwritten

    def _rebuild_simple_font_tounicode(pdf, font_dict) -> tuple[bool, int, int, int, int, int]:
        font_key = _obj_key(font_dict)
        used_codes = used_simple_codes.get(font_key or (-1, -1), set())
        existing_stream = _resolve_dictionary(font_dict.get("/ToUnicode"))
        existing_map, invalid_entries = _parse_tounicode_map_details(existing_stream)
        if not used_codes:
            return False, len(existing_map), 0, invalid_entries, 0, 0

        missing_used_codes = {
            code
            for code in used_codes
            if code not in existing_map or not _is_valid_unicode_text(existing_map[code])
        }

        if not missing_used_codes and invalid_entries <= 0:
            return False, len(existing_map), 0, 0, 0, 0

        descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
        font_bytes = _font_stream_bytes(descriptor)
        if _simple_font_auto_unicode_policy(font_dict, font_bytes=font_bytes) == "blocked":
            return False, 0, 0, invalid_entries, 0, 0
        generated = _simple_font_unicode_map(font_dict, font_bytes)
        if not generated:
            return False, 0, 0, invalid_entries, 0, 0

        target_map = {
            code: text for code, text in generated.items() if not used_codes or code in used_codes
        }
        if _symbol_family(font_dict):
            # Standard symbol correspondences only fill holes. In particular,
            # preserve author mappings and entries for codes not used here.
            target_map = {**target_map, **existing_map}
        if not target_map:
            return False, 0, len(generated), invalid_entries, 0, 0

        overwritten = sum(
            1
            for code, text in target_map.items()
            if existing_map.get(code) and existing_map.get(code) != text
        )
        stale_entries_removed = sum(1 for code in existing_map if code not in target_map)

        if (
            existing_map == target_map
            and invalid_entries <= 0
            and overwritten <= 0
            and stale_entries_removed <= 0
        ):
            return False, len(target_map), len(target_map), 0, 0, 0

        cmap_bytes = _render_tounicode_cmap(target_map, 1)
        font_dict[pikepdf.Name("/ToUnicode")] = pdf.make_stream(cmap_bytes)
        return (
            True,
            len(target_map),
            len(target_map),
            invalid_entries,
            overwritten,
            stale_entries_removed,
        )

    def _repair_font(type0_font) -> None:
        font_key = _obj_key(type0_font)
        if font_key and font_key in seen_fonts:
            return
        if font_key:
            seen_fonts.add(font_key)

        if type0_font.get("/Subtype") != pikepdf.Name("/Type0"):
            if type0_font.get("/Subtype") in (
                pikepdf.Name("/Type1"),
                pikepdf.Name("/MMType1"),
                pikepdf.Name("/TrueType"),
            ):
                (
                    changed,
                    merged_count,
                    generated_count,
                    invalid_entries,
                    overwritten,
                    stale_entries_removed,
                ) = _rebuild_simple_font_tounicode(
                    pdf,
                    type0_font,
                )
            else:
                return
        else:
            descendants = type0_font.get("/DescendantFonts")
            if not isinstance(descendants, pikepdf.Array) or len(descendants) <= 0:
                return
            cid_font = _resolve_dictionary(descendants[0])
            if not isinstance(cid_font, pikepdf.Dictionary):
                return
            if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType2"):
                return

            changed, merged_count, generated_count, invalid_entries, overwritten = (
                _rebuild_type0_tounicode(
                    pdf,
                    type0_font,
                    cid_font,
                )
            )
            stale_entries_removed = 0
        if changed:
            stats["fonts_touched"] += 1
            stats["maps_rebuilt"] += 1
        stats["mappings_generated"] += generated_count
        stats["mappings_total"] += merged_count
        stats["invalid_entries_removed"] += invalid_entries
        stats["mappings_overridden"] += overwritten
        stats["stale_entries_removed"] += stale_entries_removed

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for page in pdf.pages:
                page_resources = _resolve_dictionary(page.get("/Resources"))
                _collect_used_simple_font_codes(page, page_resources)
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _collect_used_simple_font_codes_from_appearance(
                            annot.get("/AP"),
                            _resolve_dictionary(annot.get("/Resources")),
                        )

            acroform = _resolve_dictionary(pdf.Root.get("/AcroForm"))
            if _is_pdf_mapping(acroform):
                seen_fields: set[tuple[int, int]] = set()

                def _walk_field(field_obj) -> None:
                    field = _resolve_dictionary(field_obj)
                    if not isinstance(field, pikepdf.Dictionary):
                        return
                    field_key = _obj_key(field)
                    if field_key and field_key in seen_fields:
                        return
                    if field_key:
                        seen_fields.add(field_key)

                    field_resources = _resolve_dictionary(field.get("/DR"))
                    _collect_used_simple_font_codes_from_appearance(
                        field.get("/AP"), field_resources
                    )

                    kids = field.get("/Kids")
                    if isinstance(kids, pikepdf.Array):
                        for kid in kids:
                            _walk_field(kid)

                fields = acroform.get("/Fields")
                if isinstance(fields, pikepdf.Array):
                    for field in fields:
                        _walk_field(field)

            for font_dict in _iter_pdf_fonts(pdf, policy=ZERO_BYTE_RESOURCES):
                _collect_zero_byte_candidate(font_dict)

            if zero_byte_candidate_fonts:
                for page in pdf.pages:
                    page_resources = _resolve_dictionary(page.get("/Resources"))
                    _sanitize_simple_font_zero_bytes(pdf, page, page_resources)
                    annots = page.get("/Annots")
                    if isinstance(annots, pikepdf.Array):
                        for annot in annots:
                            _sanitize_simple_font_zero_bytes_from_appearance(
                                pdf,
                                annot.get("/AP"),
                                _resolve_dictionary(annot.get("/Resources")),
                            )

                if isinstance(acroform, pikepdf.Dictionary):
                    fields = acroform.get("/Fields")
                    if isinstance(fields, pikepdf.Array):
                        for field in fields:
                            field_dict = _resolve_dictionary(field)
                            if not isinstance(field_dict, pikepdf.Dictionary):
                                continue
                            _sanitize_simple_font_zero_bytes_from_appearance(
                                pdf,
                                field_dict.get("/AP"),
                                _resolve_dictionary(field_dict.get("/DR")),
                            )

                for font_key in sanitized_font_keys:
                    used_simple_codes.setdefault(font_key, set()).discard(0)

            for font_dict in _iter_pdf_fonts(pdf, policy=TOUNICODE_RESOURCES):
                _repair_font(font_dict)

            if stats["maps_rebuilt"] <= 0 and stats["zero_bytes_removed"] <= 0:
                return False, "No eligible ToUnicode repairs were applied", stats

            pdf.save(str(output_path))

        return (
            True,
            (
                f"ToUnicode repair completed "
                f"(fonts={stats['fonts_touched']}, maps={stats['maps_rebuilt']}, "
                f"generated={stats['mappings_generated']}, total={stats['mappings_total']}, "
                f"invalid_removed={stats['invalid_entries_removed']}, "
                f"overridden={stats['mappings_overridden']}, "
                f"stale_removed={stats['stale_entries_removed']}, "
                f"text_ops_sanitized={stats['text_operands_sanitized']}, "
                f"zero_bytes_removed={stats['zero_bytes_removed']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"ToUnicode repair failed: {exc}", stats


async def _repair_pdf_tounicode(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await asyncio.to_thread(_repair_pdf_tounicode_sync, input_path, output_path)
