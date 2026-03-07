from __future__ import annotations

from pathlib import Path
from typing import Any

import pikepdf

from app.pipeline.tagger import (
    IDENTITY,
    _bbox_from_center,
    _bbox_from_points,
    _extract_text_from_operands,
    _mat_multiply,
    _safe_float,
    _transform_point,
)
from app.services.font_actualtext import (
    _resolve_target_stream_from_context,
    _resolve_text_showing_instruction,
)
from app.services.pdf_context import parse_verapdf_context_path


TEXT_SHOWING_OPERATORS = {"Tj", "TJ", "'", '"'}


def _normalize_excerpt(text: str | None) -> str:
    return " ".join(str(text or "").split())


def extract_operator_text_context(
    *,
    pdf_path: Path,
    context_path: str,
    window: int = 2,
) -> dict[str, Any]:
    if window < 0:
        raise ValueError("window must be 0 or greater")
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    with pikepdf.open(str(pdf_path)) as pdf:
        resolved = _resolve_target_stream_from_context(pdf, context_path)
        parse_target = resolved["target_stream"] or resolved["target_owner"]
        instructions = list(pikepdf.parse_content_stream(parse_target))
        resolved_instruction = _resolve_text_showing_instruction(
            instructions,
            int(resolved["operator_index"]),
        )
        operator_index = int(resolved_instruction["text_operator_index"])

    chunks: list[dict[str, Any]] = []
    for index, instruction in enumerate(instructions):
        op = str(instruction.operator)
        if op not in TEXT_SHOWING_OPERATORS:
            continue
        operands = list(instruction.operands) if hasattr(instruction, "operands") else []
        decoded = _normalize_excerpt(_extract_text_from_operands(op, operands))
        chunks.append({
            "operator_index": index,
            "text": decoded,
            "is_target": index == operator_index,
        })

    target_chunk = next((chunk for chunk in chunks if chunk["operator_index"] == operator_index), None)
    if target_chunk is None:
        raise ValueError("Target operator is not a text-showing operator")

    target_pos = chunks.index(target_chunk)
    nearby_chunks = chunks[max(0, target_pos - window): target_pos + window + 1]
    before_chunks = [chunk["text"] for chunk in nearby_chunks if chunk["operator_index"] < operator_index and chunk["text"]]
    after_chunks = [chunk["text"] for chunk in nearby_chunks if chunk["operator_index"] > operator_index and chunk["text"]]

    return {
        "decoded_text": target_chunk["text"],
        "before_text": " ".join(before_chunks[-window:]),
        "after_text": " ".join(after_chunks[:window]),
        "nearby_text": " ".join(chunk["text"] for chunk in nearby_chunks if chunk["text"]),
        "nearby_operators": [
            {
                "operator_index": chunk["operator_index"],
                "text": chunk["text"],
                "is_target": chunk["is_target"],
            }
            for chunk in nearby_chunks
            if chunk["text"] or chunk["is_target"]
        ],
    }


def extract_operator_visual_context(
    *,
    pdf_path: Path,
    context_path: str,
) -> dict[str, Any]:
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    parsed_context = parse_verapdf_context_path(context_path)
    if parsed_context.get("annotation_index") is not None or parsed_context.get("xobject_chain"):
        return {
            "page_number": parsed_context.get("page_number"),
            "bbox": None,
            "supported": False,
            "reason": "preview_bbox_not_supported_for_nested_targets",
        }

    with pikepdf.open(str(pdf_path)) as pdf:
        resolved = _resolve_target_stream_from_context(pdf, context_path)
        parse_target = resolved["target_stream"] or resolved["target_owner"]
        instructions = list(pikepdf.parse_content_stream(parse_target))
        resolved_instruction = _resolve_text_showing_instruction(
            instructions,
            int(resolved["operator_index"]),
        )
        operator_index = int(resolved_instruction["text_operator_index"])
        page = resolved["page"]
        page_number = int(parsed_context.get("page_number") or 1)

        ctm = IDENTITY
        ctm_stack: list[tuple[float, float, float, float, float, float]] = []
        text_matrix = IDENTITY
        font_size = 12.0
        inside_text = False

        for index, instruction in enumerate(instructions):
            op = str(instruction.operator)
            operands = list(instruction.operands) if hasattr(instruction, "operands") else []

            if op == "q":
                ctm_stack.append(ctm)
            elif op == "Q":
                if ctm_stack:
                    ctm = ctm_stack.pop()
            elif op == "cm" and len(operands) >= 6:
                ctm = _mat_multiply(tuple(_safe_float(x) for x in operands[:6]), ctm)
            elif op == "BT":
                inside_text = True
                text_matrix = IDENTITY
            elif op == "ET":
                inside_text = False
            elif inside_text and op == "Tm" and len(operands) >= 6:
                text_matrix = tuple(_safe_float(x) for x in operands[:6])
            elif inside_text and op in ("Td", "TD") and len(operands) >= 2:
                tx, ty = _safe_float(operands[0]), _safe_float(operands[1])
                text_matrix = (
                    text_matrix[0], text_matrix[1],
                    text_matrix[2], text_matrix[3],
                    text_matrix[4] + tx, text_matrix[5] + ty,
                )
            elif inside_text and op == "T*":
                text_matrix = (
                    text_matrix[0], text_matrix[1],
                    text_matrix[2], text_matrix[3],
                    text_matrix[4], text_matrix[5] - font_size * 1.2,
                )
            elif inside_text and op == "Tf" and len(operands) >= 2:
                font_size = abs(_safe_float(operands[1])) or 12.0

            if index != operator_index:
                continue

            text = " ".join(str(_extract_text_from_operands(op, operands) or "").split())
            pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
            bbox = _bbox_from_points([pos[0]], [pos[1]])
            if bbox:
                text_width_guess = max(font_size * 0.5 * max(len(text), 1), font_size * 2)
                if bbox["r"] - bbox["l"] < font_size * 0.5:
                    bbox["r"] = bbox["l"] + text_width_guess
                bbox["t"] += font_size
                bbox["b"] -= font_size * 0.2
            else:
                bbox = _bbox_from_center(
                    pos[0],
                    pos[1],
                    font_size * max(len(text), 1),
                    font_size,
                )

            crop_box = page.obj.get("/CropBox") or page.obj.get("/MediaBox")
            if isinstance(crop_box, pikepdf.Array) and len(crop_box) >= 4:
                page_width = float(crop_box[2]) - float(crop_box[0])
                page_height = float(crop_box[3]) - float(crop_box[1])
            else:
                page_width = float(page.mediabox[2]) - float(page.mediabox[0])
                page_height = float(page.mediabox[3]) - float(page.mediabox[1])

            return {
                "page_number": page_number,
                "bbox": bbox,
                "page_width": page_width,
                "page_height": page_height,
                "decoded_text": text,
                "supported": True,
                "reason": "",
            }

    raise ValueError("Target operator was not found in the content stream")
