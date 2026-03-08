from __future__ import annotations

from pathlib import Path

import pikepdf

from app.services.font_actualtext import (
    _resolve_object,
    _resolve_target_stream_from_context,
    _resolve_text_showing_instruction,
)


def _artifact_bmc() -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(
        [pikepdf.Name("/Artifact")],
        pikepdf.Operator("BMC"),
    )


def _emc() -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC"))


def _apply_artifact_to_resolved_target(
    pdf: pikepdf.Pdf,
    *,
    resolved_target: dict,
) -> None:
    page = resolved_target["page"]
    target_owner = resolved_target["target_owner"]
    target_stream = resolved_target["target_stream"]
    operator_index = int(resolved_target["operator_index"])

    parse_target = target_stream or target_owner
    instructions = list(pikepdf.parse_content_stream(parse_target))
    resolved_instruction = _resolve_text_showing_instruction(instructions, operator_index)
    text_operator_index = int(resolved_instruction["text_operator_index"])
    wrapper_bdc_index = resolved_instruction["wrapper_bdc_index"]
    wrapper_emc_index = resolved_instruction["wrapper_emc_index"]

    if wrapper_bdc_index is not None and wrapper_emc_index is not None:
        instructions[wrapper_bdc_index] = _artifact_bmc()
        new_instructions = instructions
    else:
        target_instruction = instructions[text_operator_index]
        new_instructions = [
            *instructions[:text_operator_index],
            _artifact_bmc(),
            target_instruction,
            _emc(),
            *instructions[text_operator_index + 1:],
        ]

    stream_bytes = pikepdf.unparse_content_stream(new_instructions)
    if target_stream is None and isinstance(target_owner, pikepdf.Page):
        page["/Contents"] = pdf.make_stream(stream_bytes)
    else:
        resolved_stream = _resolve_object(target_stream or target_owner)
        resolved_stream.write(stream_bytes)


def apply_artifact_batch_to_contexts(
    *,
    input_pdf: Path,
    output_pdf: Path,
    context_paths: list[str],
) -> None:
    if not context_paths:
        raise ValueError("context_paths must not be empty")

    normalized_paths: list[str] = []
    seen: set[str] = set()
    for context_path in context_paths:
        normalized = str(context_path or "").strip()
        if not normalized:
            raise ValueError("Each context_path must be non-empty")
        if normalized in seen:
            raise ValueError("Duplicate context_path provided in batch artifact request")
        seen.add(normalized)
        normalized_paths.append(normalized)

    with pikepdf.open(str(input_pdf)) as pdf:
        resolved_targets: list[dict[str, object]] = []
        for context_path in normalized_paths:
            resolved_targets.append(_resolve_target_stream_from_context(pdf, context_path))

        resolved_targets.sort(
            key=lambda target: (
                target["stream_key"],
                -int(target["operator_index"]),
            )
        )

        for resolved_target in resolved_targets:
            _apply_artifact_to_resolved_target(
                pdf,
                resolved_target=resolved_target,
            )
        pdf.save(str(output_pdf))
