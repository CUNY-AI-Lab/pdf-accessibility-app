from __future__ import annotations

from pathlib import Path

import pikepdf

from app.services.font_actualtext import (
    _resolve_object,
    _resolve_target_stream_from_context,
    _resolve_text_showing_instruction,
)


TEXT_SHOWING_OPERATORS = {"Tj", "TJ", "'", '"'}
SIMPLE_FONT_SUBTYPES = {
    pikepdf.Name("/Type1"),
    pikepdf.Name("/MMType1"),
    pikepdf.Name("/TrueType"),
}


def _base_font_name(font_dict) -> str:
    from app.pipeline.orchestrator import _base_font_name as _orchestrator_base_font_name

    return _orchestrator_base_font_name(font_dict)


def _is_valid_unicode_text(text: str) -> bool:
    from app.pipeline.orchestrator import _is_valid_unicode_text as _orchestrator_is_valid_unicode_text

    return _orchestrator_is_valid_unicode_text(text)


def _parse_tounicode_map_details(stream_obj):
    from app.pipeline.orchestrator import _parse_tounicode_map_details as _orchestrator_parse_tounicode_map_details

    return _orchestrator_parse_tounicode_map_details(stream_obj)


def _render_tounicode_cmap(mapping: dict[int, str], code_bytes: int) -> bytes:
    from app.pipeline.orchestrator import _render_tounicode_cmap as _orchestrator_render_tounicode_cmap

    return _orchestrator_render_tounicode_cmap(mapping, code_bytes)


def _font_resource_key(value) -> pikepdf.Name | None:
    if isinstance(value, pikepdf.Name):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    if not raw.startswith("/"):
        raw = f"/{raw}"
    try:
        return pikepdf.Name(raw)
    except Exception:
        return None


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


def _resolve_target_font_details(pdf: pikepdf.Pdf, context_path: str) -> dict[str, object]:
    resolved_target = _resolve_target_stream_from_context(pdf, context_path)
    parse_target = resolved_target["target_stream"] or resolved_target["target_owner"]
    instructions = list(pikepdf.parse_content_stream(parse_target))
    resolved_instruction = _resolve_text_showing_instruction(
        instructions,
        int(resolved_target["operator_index"]),
    )
    text_operator_index = int(resolved_instruction["text_operator_index"])
    target_instruction = instructions[text_operator_index]
    target_operator = str(target_instruction.operator)
    if target_operator not in TEXT_SHOWING_OPERATORS:
        raise ValueError("Target operator does not resolve to a text-showing instruction")

    resources = _resolve_object(resolved_target.get("resources"))
    if not hasattr(resources, "get"):
        raise ValueError("Target content stream does not expose font resources")
    fonts = _resolve_object(resources.get("/Font"))
    if not hasattr(fonts, "get"):
        raise ValueError("Target content stream does not expose a font dictionary")

    current_font = None
    for instruction in instructions[:text_operator_index + 1]:
        op = str(instruction.operator)
        operands = list(instruction.operands) if hasattr(instruction, "operands") else []
        if op == "Tf" and operands:
            font_key = _font_resource_key(operands[0])
            if font_key is None:
                continue
            current_font = _resolve_object(fonts.get(font_key))

    if not isinstance(current_font, pikepdf.Dictionary):
        raise ValueError("Could not resolve the active font for the target operator")
    if current_font.get("/Subtype") not in SIMPLE_FONT_SUBTYPES:
        raise ValueError("Font-map override currently supports simple Type1/TrueType fonts only")

    operands = list(target_instruction.operands) if hasattr(target_instruction, "operands") else []
    raw_bytes = _raw_text_bytes(target_operator, operands)
    if len(raw_bytes) != 1:
        raise ValueError("Font-map override currently requires a localized single-byte target")

    return {
        "resolved_target": resolved_target,
        "font_dict": current_font,
        "font_code": raw_bytes[0],
        "font_base_name": _base_font_name(current_font) or "(unnamed)",
        "text_operator_index": text_operator_index,
        "target_operator": target_operator,
    }


def inspect_context_font_target(*, pdf_path: Path, context_path: str) -> dict[str, object]:
    with pikepdf.open(str(pdf_path)) as pdf:
        details = _resolve_target_font_details(pdf, context_path)
    return {
        "font_code": int(details["font_code"]),
        "font_code_hex": f"{int(details['font_code']):02X}",
        "font_base_name": str(details["font_base_name"]),
        "target_operator": str(details["target_operator"]),
    }


def _apply_unicode_override_to_font(
    pdf: pikepdf.Pdf,
    *,
    font_dict,
    font_code: int,
    unicode_text: str,
) -> None:
    normalized = unicode_text.strip()
    if not normalized:
        raise ValueError("unicode_text must not be empty")
    if not _is_valid_unicode_text(normalized):
        raise ValueError("unicode_text must be valid Unicode text")

    existing_stream = _resolve_object(font_dict.get("/ToUnicode"))
    existing_map, _ = _parse_tounicode_map_details(existing_stream)
    merged_map = {
        code: text
        for code, text in existing_map.items()
        if _is_valid_unicode_text(text)
    }
    merged_map[int(font_code)] = normalized
    font_dict[pikepdf.Name("/ToUnicode")] = pdf.make_stream(
        _render_tounicode_cmap(merged_map, 1),
    )


def apply_unicode_override_to_context(
    *,
    input_pdf: Path,
    output_pdf: Path,
    context_path: str,
    unicode_text: str,
) -> dict[str, object]:
    normalized = unicode_text.strip()
    if not normalized:
        raise ValueError("unicode_text must not be empty")

    with pikepdf.open(str(input_pdf)) as pdf:
        details = _resolve_target_font_details(pdf, context_path)
        _apply_unicode_override_to_font(
            pdf,
            font_dict=details["font_dict"],
            font_code=int(details["font_code"]),
            unicode_text=normalized,
        )
        pdf.save(str(output_pdf))

    return {
        "font_code": int(details["font_code"]),
        "font_base_name": str(details["font_base_name"]),
        "operator_index": int(details["text_operator_index"]),
        "unicode_text": normalized,
    }
