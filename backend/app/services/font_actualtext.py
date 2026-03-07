import re
from pathlib import Path

import pikepdf

from app.services.pdf_context import parse_verapdf_context_path


TEXT_SHOWING_OPERATORS = {"Tj", "TJ", "'", '"'}


def _actualtext_bdc(actual_text: str) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(
        [
            pikepdf.Name("/Span"),
            pikepdf.Dictionary({"/ActualText": pikepdf.String(actual_text)}),
        ],
        pikepdf.Operator("BDC"),
    )


def _emc() -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC"))


def _resolve_object(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _instruction_operator_name(instruction) -> str:
    return str(getattr(instruction, "operator", ""))


def _is_actualtext_bdc(instruction) -> bool:
    if _instruction_operator_name(instruction) != "BDC":
        return False
    operands = list(instruction.operands) if hasattr(instruction, "operands") else []
    if len(operands) < 2:
        return False
    if str(operands[0]) != "/Span":
        return False
    properties = _resolve_object(operands[1])
    return isinstance(properties, pikepdf.Dictionary) and properties.get("/ActualText") is not None


def _text_operator_indices_in_range(
    instructions: list[pikepdf.ContentStreamInstruction],
    start_index: int,
    end_index: int,
) -> list[int]:
    indices: list[int] = []
    for index in range(max(start_index, 0), min(end_index + 1, len(instructions))):
        if _instruction_operator_name(instructions[index]) in TEXT_SHOWING_OPERATORS:
            indices.append(index)
    return indices


def _resolve_single_text_object_target(
    instructions: list[pikepdf.ContentStreamInstruction],
    operator_index: int,
) -> dict[str, int | None] | None:
    text_object_start = None
    for index in range(operator_index, -1, -1):
        op = _instruction_operator_name(instructions[index])
        if op == "ET" and index < operator_index:
            break
        if op == "BT":
            text_object_start = index
            break
    if text_object_start is None:
        return None

    text_object_end = None
    for index in range(text_object_start + 1, len(instructions)):
        if _instruction_operator_name(instructions[index]) == "ET":
            text_object_end = index
            break
    if text_object_end is None or operator_index > text_object_end:
        return None

    text_indices = _text_operator_indices_in_range(
        instructions,
        text_object_start,
        text_object_end,
    )
    if len(text_indices) != 1:
        return None

    return {
        "text_operator_index": text_indices[0],
        "wrapper_bdc_index": None,
        "wrapper_emc_index": None,
    }


def _resolve_text_showing_instruction(
    instructions: list[pikepdf.ContentStreamInstruction],
    operator_index: int,
) -> dict[str, int | None]:
    if operator_index < 0 or operator_index >= len(instructions):
        raise ValueError("operator_index exceeds content stream length")

    current = instructions[operator_index]
    current_op = _instruction_operator_name(current)
    if current_op in TEXT_SHOWING_OPERATORS:
        wrapper_bdc_index = operator_index - 1 if operator_index >= 1 else None
        wrapper_emc_index = operator_index + 1 if operator_index + 1 < len(instructions) else None
        if (
            wrapper_bdc_index is not None
            and wrapper_emc_index is not None
            and _is_actualtext_bdc(instructions[wrapper_bdc_index])
            and _instruction_operator_name(instructions[wrapper_emc_index]) == "EMC"
        ):
            return {
                "text_operator_index": operator_index,
                "wrapper_bdc_index": wrapper_bdc_index,
                "wrapper_emc_index": wrapper_emc_index,
            }
        return {
            "text_operator_index": operator_index,
            "wrapper_bdc_index": None,
            "wrapper_emc_index": None,
        }

    if (
        _is_actualtext_bdc(current)
        and operator_index + 2 < len(instructions)
        and _instruction_operator_name(instructions[operator_index + 1]) in TEXT_SHOWING_OPERATORS
        and _instruction_operator_name(instructions[operator_index + 2]) == "EMC"
    ):
        return {
            "text_operator_index": operator_index + 1,
            "wrapper_bdc_index": operator_index,
            "wrapper_emc_index": operator_index + 2,
        }

    if (
        current_op == "EMC"
        and operator_index >= 2
        and _instruction_operator_name(instructions[operator_index - 1]) in TEXT_SHOWING_OPERATORS
        and _is_actualtext_bdc(instructions[operator_index - 2])
    ):
        return {
            "text_operator_index": operator_index - 1,
            "wrapper_bdc_index": operator_index - 2,
            "wrapper_emc_index": operator_index,
        }

    single_text_object_target = _resolve_single_text_object_target(instructions, operator_index)
    if single_text_object_target is not None:
        return single_text_object_target

    for offset in (1, -1, 2, -2, 3, -3):
        neighbor_index = operator_index + offset
        if neighbor_index < 0 or neighbor_index >= len(instructions):
            continue
        single_text_object_target = _resolve_single_text_object_target(
            instructions,
            neighbor_index,
        )
        if single_text_object_target is None:
            continue
        if abs(int(single_text_object_target["text_operator_index"]) - operator_index) <= 6:
            return single_text_object_target

    raise ValueError(
        f"operator_index {operator_index} is not a text-showing operator or an ActualText-wrapped text target"
    )


def _page_content_stream_target(page: pikepdf.Page, stream_index: int):
    contents = _resolve_object(page.obj.get("/Contents"))
    if isinstance(contents, pikepdf.Array):
        if stream_index >= len(contents):
            raise ValueError(
                f"contentStream[{stream_index}] exceeds page content stream count"
            )
        target_stream = _resolve_object(contents[stream_index])
        return target_stream, target_stream, page.obj.get("/Resources")
    if stream_index != 0:
        raise ValueError("Single-stream page content only supports contentStream[0]")
    return page, None, page.obj.get("/Resources")


def _owner_content_stream_target(owner, stream_index: int, default_resources):
    resolved_owner = _resolve_object(owner)
    if isinstance(resolved_owner, pikepdf.Stream):
        if stream_index != 0:
            raise ValueError("Single-stream object only supports contentStream[0]")
        target_resources = resolved_owner.get("/Resources") if hasattr(resolved_owner, "get") else None
        return resolved_owner, resolved_owner, target_resources or default_resources

    if not hasattr(resolved_owner, "get"):
        raise ValueError("Current target does not expose nested content streams")

    contents = _resolve_object(resolved_owner.get("/Contents"))
    if isinstance(contents, pikepdf.Array):
        if stream_index >= len(contents):
            raise ValueError(
                f"contentStream[{stream_index}] exceeds nested content stream count"
            )
        target_stream = _resolve_object(contents[stream_index])
        target_resources = target_stream.get("/Resources") if hasattr(target_stream, "get") else None
        return target_stream, target_stream, target_resources or default_resources
    if contents is not None:
        if stream_index != 0:
            raise ValueError("Single nested content stream only supports contentStream[0]")
        target_stream = _resolve_object(contents)
        target_resources = target_stream.get("/Resources") if hasattr(target_stream, "get") else None
        return target_stream, target_stream, target_resources or default_resources
    if stream_index != 0:
        raise ValueError("Current target does not expose contentStream index > 0")
    target_resources = resolved_owner.get("/Resources") if hasattr(resolved_owner, "get") else None
    return resolved_owner, None, target_resources or default_resources


def _xobject_name_from_do_operator(parse_target, operator_index: int) -> str | None:
    if operator_index < 0:
        return None
    try:
        instructions = list(pikepdf.parse_content_stream(parse_target))
    except Exception:
        return None
    if operator_index >= len(instructions):
        return None
    operands, operator = instructions[operator_index]
    if str(operator) != "Do" or not operands:
        return None
    return str(operands[0]).lstrip("/")


def _resource_xobject_target(resources, xobject_index: int, xobject_name: str | None):
    resolved_resources = _resolve_object(resources)
    if not hasattr(resolved_resources, "get"):
        raise ValueError("Current content stream does not expose XObject resources")

    xobjects = _resolve_object(resolved_resources.get("/XObject"))
    if not hasattr(xobjects, "keys") or not xobjects:
        raise ValueError("No XObject resources available for the current target")

    if xobject_name:
        key = pikepdf.Name(f"/{xobject_name.lstrip('/')}")
        candidate = xobjects.get(key)
        if candidate is None:
            raise ValueError(f"XObject '{xobject_name}' not found in resources")
        target = _resolve_object(candidate)
    else:
        keys = list(xobjects.keys())
        if xobject_index >= len(keys):
            raise ValueError(f"xObject[{xobject_index}] exceeds XObject resource count")
        target = _resolve_object(xobjects.get(keys[xobject_index]))

    target_resources = target.get("/Resources") if hasattr(target, "get") else None
    return target, target_resources


def _appearance_streams(ap_object) -> list[pikepdf.Object]:
    resolved = _resolve_object(ap_object)
    if resolved is None:
        return []
    if isinstance(resolved, pikepdf.Stream):
        return [resolved]
    if isinstance(resolved, pikepdf.Dictionary):
        streams: list[pikepdf.Object] = []
        for key in ("/N", "/R", "/D"):
            child = resolved.get(key)
            if child is not None:
                streams.extend(_appearance_streams(child))
        for key, value in resolved.items():
            if key in (pikepdf.Name("/N"), pikepdf.Name("/R"), pikepdf.Name("/D")):
                continue
            streams.extend(_appearance_streams(value))
        return streams
    return []


def _resolve_target_stream_from_context(pdf: pikepdf.Pdf, context_path: str):
    parsed = parse_verapdf_context_path(context_path)
    page_number = parsed.get("page_number")
    operator_index = parsed.get("operator_index")
    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError("Context path did not include a valid page reference")
    if not isinstance(operator_index, int) or operator_index < 0:
        raise ValueError("Context path did not include a valid operator reference")
    if page_number > len(pdf.pages):
        raise ValueError(f"page_number {page_number} exceeds document length")

    page = pdf.pages[page_number - 1]
    target_owner = page
    target_stream = None
    current_resources = page.obj.get("/Resources")

    page_stream_index = parsed.get("page_content_stream_index")
    if isinstance(page_stream_index, int):
        target_owner, target_stream, current_resources = _page_content_stream_target(
            page,
            page_stream_index,
        )

    annotation_index = parsed.get("annotation_index")
    if isinstance(annotation_index, int):
        annots = _resolve_object(page.obj.get("/Annots"))
        if not isinstance(annots, pikepdf.Array) or annotation_index >= len(annots):
            raise ValueError(f"annotations[{annotation_index}] exceeds page annotation count")
        target_owner = _resolve_object(annots[annotation_index])
        target_stream = None
        appearance_index = parsed.get("appearance_index")
        if not isinstance(appearance_index, int):
            raise ValueError("Context path referenced an annotation without an appearanceStream")
        appearance_candidates = _appearance_streams(
            target_owner.get("/AP") if isinstance(target_owner, pikepdf.Dictionary) else None
        )
        if appearance_index >= len(appearance_candidates):
            raise ValueError(
                f"appearanceStream[{appearance_index}] exceeds available annotation appearance streams"
            )
        target_owner = appearance_candidates[appearance_index]
        target_stream = target_owner
        current_resources = (
            target_owner.get("/Resources")
            if hasattr(target_owner, "get")
            else current_resources
        )

    xobject_chain = parsed.get("xobject_chain")
    if isinstance(xobject_chain, list):
        for entry in xobject_chain:
            if not isinstance(entry, dict):
                continue
            xobject_name = str(entry.get("name") or "") or None
            do_operator_index = entry.get("from_operator_index")
            if not xobject_name and isinstance(do_operator_index, int):
                parse_target = target_stream or target_owner
                xobject_name = _xobject_name_from_do_operator(parse_target, do_operator_index)
            target_owner, current_resources = _resource_xobject_target(
                current_resources,
                int(entry.get("index", 0)),
                xobject_name,
            )
            target_stream = target_owner
            nested_stream_index = entry.get("content_stream_index")
            if isinstance(nested_stream_index, int):
                target_owner, target_stream, current_resources = _owner_content_stream_target(
                    target_owner,
                    nested_stream_index,
                    current_resources,
                )

    return {
        "page": page,
        "target_owner": target_owner,
        "target_stream": target_stream,
        "resources": current_resources,
        "operator_index": operator_index,
        "stream_key": (
            parsed.get("page_number"),
            parsed.get("annotation_index"),
            parsed.get("appearance_index"),
            parsed.get("page_content_stream_index"),
            tuple(
                (
                    entry.get("index"),
                    entry.get("name"),
                    entry.get("from_operator_index"),
                    entry.get("content_stream_index"),
                )
                for entry in parsed.get("xobject_chain", [])
                if isinstance(entry, dict)
            ),
        ),
    }


def _apply_actualtext_to_resolved_target(
    pdf: pikepdf.Pdf,
    *,
    resolved_target: dict,
    actual_text: str,
) -> None:
    normalized_text = actual_text.strip()
    if not normalized_text:
        raise ValueError("actual_text must not be empty")

    page = resolved_target["page"]
    target_owner = resolved_target["target_owner"]
    target_stream = resolved_target["target_stream"]
    operator_index = resolved_target["operator_index"]

    parse_target = target_stream or target_owner
    instructions = list(pikepdf.parse_content_stream(parse_target))
    resolved_instruction = _resolve_text_showing_instruction(instructions, operator_index)
    text_operator_index = int(resolved_instruction["text_operator_index"])
    wrapper_bdc_index = resolved_instruction["wrapper_bdc_index"]
    wrapper_emc_index = resolved_instruction["wrapper_emc_index"]

    if wrapper_bdc_index is not None and wrapper_emc_index is not None:
        instructions[wrapper_bdc_index] = _actualtext_bdc(normalized_text)
        new_instructions = instructions
    else:
        target_instruction = instructions[text_operator_index]
        new_instructions = [
            *instructions[:text_operator_index],
            _actualtext_bdc(normalized_text),
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


def apply_actualtext_to_page_operator(
    *,
    input_pdf: Path,
    output_pdf: Path,
    page_number: int,
    operator_index: int,
    actual_text: str,
) -> None:
    if page_number < 1:
        raise ValueError("page_number must be 1 or greater")
    if operator_index < 0:
        raise ValueError("operator_index must be 0 or greater")
    normalized_text = actual_text.strip()
    if not normalized_text:
        raise ValueError("actual_text must not be empty")

    with pikepdf.open(str(input_pdf)) as pdf:
        if page_number > len(pdf.pages):
            raise ValueError(f"page_number {page_number} exceeds document length")

        page = pdf.pages[page_number - 1]
        instructions = list(pikepdf.parse_content_stream(page))
        resolved_instruction = _resolve_text_showing_instruction(instructions, operator_index)
        text_operator_index = int(resolved_instruction["text_operator_index"])
        wrapper_bdc_index = resolved_instruction["wrapper_bdc_index"]
        wrapper_emc_index = resolved_instruction["wrapper_emc_index"]

        if wrapper_bdc_index is not None and wrapper_emc_index is not None:
            instructions[wrapper_bdc_index] = _actualtext_bdc(normalized_text)
            new_instructions = instructions
        else:
            target_instruction = instructions[text_operator_index]
            new_instructions = [
                *instructions[:text_operator_index],
                _actualtext_bdc(normalized_text),
                target_instruction,
                _emc(),
                *instructions[text_operator_index + 1:],
            ]
        page["/Contents"] = pdf.make_stream(pikepdf.unparse_content_stream(new_instructions))
        pdf.save(str(output_pdf))


def apply_actualtext_to_context(
    *,
    input_pdf: Path,
    output_pdf: Path,
    context_path: str,
    actual_text: str,
) -> None:
    normalized_text = actual_text.strip()
    if not normalized_text:
        raise ValueError("actual_text must not be empty")

    with pikepdf.open(str(input_pdf)) as pdf:
        target = _resolve_target_stream_from_context(pdf, context_path)
        _apply_actualtext_to_resolved_target(
            pdf,
            resolved_target=target,
            actual_text=normalized_text,
        )
        pdf.save(str(output_pdf))


def apply_actualtext_batch_to_contexts(
    *,
    input_pdf: Path,
    output_pdf: Path,
    patches: list[dict[str, str | int]],
) -> None:
    if not patches:
        raise ValueError("patches must not be empty")

    normalized_patches: list[dict[str, str]] = []
    seen_targets: set[str] = set()
    for patch in patches:
        context_path = str(patch.get("context_path") or "").strip()
        actual_text = str(patch.get("actual_text") or "").strip()
        if not context_path:
            raise ValueError("Each batch patch must include context_path")
        if not actual_text:
            raise ValueError("Each batch patch must include non-empty actual_text")
        if context_path in seen_targets:
            raise ValueError("Duplicate context_path provided in batch patches")
        seen_targets.add(context_path)
        normalized_patches.append({
            "context_path": context_path,
            "actual_text": actual_text,
        })

    with pikepdf.open(str(input_pdf)) as pdf:
        resolved_patches: list[dict[str, object]] = []
        for patch in normalized_patches:
            target = _resolve_target_stream_from_context(pdf, patch["context_path"])
            resolved_patches.append({
                "target": target,
                "actual_text": patch["actual_text"],
            })

        resolved_patches.sort(
            key=lambda item: (
                item["target"]["stream_key"],
                -int(item["target"]["operator_index"]),
            )
        )

        for patch in resolved_patches:
            _apply_actualtext_to_resolved_target(
                pdf,
                resolved_target=patch["target"],
                actual_text=str(patch["actual_text"]),
            )
        pdf.save(str(output_pdf))
