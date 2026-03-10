"""Grounded-text auto-apply policy and structure mutations."""

from __future__ import annotations

import copy
import re

from app.services.page_intelligence import repair_text_candidate, text_similarity_score

PRETAG_GROUNDED_TEXT_TARGET_LIMIT = 12
PRETAG_GROUNDED_TEXT_MAX_CHARS = 64
PRETAG_GROUNDED_TEXT_ENCODING_MAX_CHARS = 128
PRETAG_GROUNDED_TEXT_ENCODING_MIN_SIMILARITY = 0.94
PRETAG_GROUNDED_TEXT_ALLOWED_ROLES = frozenset({
    "heading",
    "paragraph",
    "note",
    "toc_caption",
    "toc_item",
})
PRETAG_GROUNDED_TEXT_ARTIFACT_MAX_CHARS = 96
PRETAG_GROUNDED_TEXT_ALLOWED_ARTIFACT_ROLES = frozenset({
    "heading",
    "paragraph",
    "note",
    "toc_caption",
    "toc_item",
})
PRETAG_GROUNDED_TEXT_ALLOWED_DUPLICATE_ROLES = frozenset({
    "heading",
    "paragraph",
    "note",
    "toc_caption",
    "toc_item",
})
PRETAG_GROUNDED_CODE_MAX_CHARS = 2000
PRETAG_GROUNDED_CODE_MAX_LINES = 40
PRETAG_GROUNDED_CODE_MIN_SUPPORT = 0.55


def collect_safe_grounded_text_resolutions(
    adjudication: dict[str, object] | None,
) -> dict[tuple[int, str], tuple[str, dict[str, object]]]:
    approved_by_key: dict[tuple[int, str], tuple[str, dict[str, object]]] = {}
    if not isinstance(adjudication, dict):
        return approved_by_key
    blocks = adjudication.get("blocks")
    if not isinstance(blocks, list):
        return approved_by_key
    for block in blocks:
        if not isinstance(block, dict):
            continue
        page = block.get("page")
        review_id = str(block.get("review_id") or "").strip()
        if not isinstance(page, int) or page < 1 or not review_id:
            continue
        if should_auto_apply_grounded_text_block(block):
            approved_by_key[(page, review_id)] = ("actual_text", block)
            continue
        if should_auto_apply_grounded_encoding_block(block):
            approved_by_key[(page, review_id)] = ("actual_text", block)
            continue
        if should_auto_apply_grounded_code_block(block):
            approved_by_key[(page, review_id)] = ("code_actual_text", block)
            continue
        if _should_auto_artifact_grounded_text_block(block):
            approved_by_key[(page, review_id)] = ("artifact", block)
    return approved_by_key


def apply_grounded_text_resolutions_to_structure(
    structure_json: dict[str, object],
    approved_by_key: dict[tuple[int, str], tuple[str, dict[str, object]]],
) -> tuple[dict[str, object], dict[str, object]]:
    audit: dict[str, object] = {
        "applied": False,
        "reason": "",
        "applied_count": 0,
        "applied_actual_text_count": 0,
        "applied_code_text_count": 0,
        "applied_artifact_count": 0,
        "pages": [],
        "review_ids": [],
    }
    if not approved_by_key:
        audit["reason"] = "no_safe_resolutions"
        return structure_json, audit
    updated_structure = copy.deepcopy(structure_json)
    elements = updated_structure.get("elements")
    if not isinstance(elements, list):
        audit["reason"] = "missing_elements"
        return structure_json, audit

    applied_pages: set[int] = set()
    applied_review_ids: list[str] = []
    applied_count = 0
    applied_actual_text_count = 0
    applied_code_text_count = 0
    applied_artifact_count = 0
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        page_number = element.get("page")
        review_id = str(element.get("review_id") or f"review-{index}").strip()
        if not isinstance(page_number, int) or page_number < 0 or not review_id:
            continue
        approved_entry = approved_by_key.get((page_number + 1, review_id))
        if not approved_entry:
            continue
        resolution_type, approved = approved_entry
        resolved_text = str(approved.get("readable_text_hint") or "").strip()
        if resolution_type in {"actual_text", "code_actual_text"} and not resolved_text:
            continue
        element["review_id"] = review_id
        element["semantic_issue_type"] = str(approved.get("issue_type") or "").strip() or "spacing_only"
        element["semantic_blocking"] = False
        element["resolution_reason"] = str(approved.get("reason") or "").strip()
        chosen_source = str(approved.get("chosen_source") or "llm").strip() or "llm"
        if resolution_type == "artifact":
            element["type"] = "artifact"
            element.pop("actual_text", None)
            element.pop("resolved_text", None)
            element.pop("semantic_text_hint", None)
            element["resolution_source"] = f"pretag_artifact_{chosen_source}"
            applied_artifact_count += 1
        else:
            element["actual_text"] = resolved_text
            element["resolved_text"] = resolved_text
            element["semantic_text_hint"] = resolved_text
            if resolution_type == "code_actual_text":
                element["resolution_source"] = f"pretag_code_{chosen_source}"
                applied_code_text_count += 1
            else:
                element["resolution_source"] = f"pretag_{chosen_source}"
                applied_actual_text_count += 1
        applied_pages.add(int(approved["page"]))
        applied_review_ids.append(review_id)
        applied_count += 1

    if applied_count <= 0:
        audit["reason"] = "no_matching_structure_elements"
        return structure_json, audit

    audit["applied"] = True
    audit["applied_count"] = applied_count
    audit["applied_actual_text_count"] = applied_actual_text_count
    audit["applied_code_text_count"] = applied_code_text_count
    audit["applied_artifact_count"] = applied_artifact_count
    audit["pages"] = sorted(applied_pages)
    audit["review_ids"] = applied_review_ids
    audit["reason"] = "applied"
    return updated_structure, audit


def has_grounded_text_candidate_task(review_tasks: list[dict[str, object]]) -> bool:
    for task in review_tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("task_type") or "") != "content_fidelity":
            continue
        metadata = task.get("metadata")
        if isinstance(metadata, dict) and bool(metadata.get("grounded_text_candidate")):
            return True
    return False


def should_auto_apply_grounded_text_block(block: dict[str, object]) -> bool:
    readable_text = str(block.get("readable_text_hint") or "").strip()
    if not readable_text:
        return False
    if str(block.get("confidence") or "").strip() != "high":
        return False
    if not bool(block.get("should_block_accessibility", False)):
        return False
    if str(block.get("issue_type") or "").strip() != "spacing_only":
        return False
    role = str(block.get("role") or "").strip()
    if role not in PRETAG_GROUNDED_TEXT_ALLOWED_ROLES:
        return False
    if len(readable_text) > PRETAG_GROUNDED_TEXT_MAX_CHARS:
        return False
    original_text = str(
        block.get("original_text_candidate")
        or block.get("native_text_candidate")
        or block.get("extracted_text")
        or ""
    ).strip()
    if not original_text:
        return False
    dense_original = re.sub(r"\s+", "", original_text).lower()
    dense_readable = re.sub(r"\s+", "", readable_text).lower()
    if not dense_original or dense_original != dense_readable:
        return False
    chosen_source = str(block.get("chosen_source") or "").strip()
    return chosen_source in {"ocr", "llm_inferred"}


def should_auto_apply_grounded_encoding_block(block: dict[str, object]) -> bool:
    readable_text = str(block.get("readable_text_hint") or "").strip()
    if not readable_text or "\n" in readable_text:
        return False
    if str(block.get("confidence") or "").strip() != "high":
        return False
    if not bool(block.get("should_block_accessibility", False)):
        return False
    if str(block.get("issue_type") or "").strip() != "encoding_problem":
        return False
    role = str(block.get("role") or "").strip()
    if role not in PRETAG_GROUNDED_TEXT_ALLOWED_ROLES:
        return False
    if len(readable_text) > PRETAG_GROUNDED_TEXT_ENCODING_MAX_CHARS:
        return False
    original_text = str(
        block.get("original_text_candidate")
        or block.get("native_text_candidate")
        or block.get("extracted_text")
        or ""
    ).strip()
    if not original_text or "\n" in original_text:
        return False
    similarity = text_similarity_score(original_text, readable_text)
    if similarity < PRETAG_GROUNDED_TEXT_ENCODING_MIN_SIMILARITY:
        return False
    signals = block.get("signals")
    has_compact_signal = isinstance(signals, list) and any(
        str(signal).strip() == "very short token pattern"
        for signal in signals
    )
    if not has_compact_signal and len(readable_text.split()) > 10:
        return False
    chosen_source = str(block.get("chosen_source") or "").strip()
    return chosen_source in {"native", "ocr", "llm_inferred"}


def should_auto_apply_grounded_code_block(block: dict[str, object]) -> bool:
    readable_text = str(block.get("readable_text_hint") or "").strip()
    if not readable_text:
        return False
    if str(block.get("role") or "").strip() != "code":
        return False
    if str(block.get("confidence") or "").strip() != "high":
        return False
    if not bool(block.get("should_block_accessibility", False)):
        return False
    if str(block.get("issue_type") or "").strip() != "encoding_problem":
        return False
    chosen_source = str(block.get("chosen_source") or "").strip()
    if chosen_source not in {"ocr", "llm_inferred"}:
        return False
    if not _looks_like_code_resolution(readable_text):
        return False
    return _code_resolution_support_score(block, readable_text) >= PRETAG_GROUNDED_CODE_MIN_SUPPORT


def _normalized_dense_grounded_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", repair_text_candidate(value).lower())


def _strip_code_line_numbers(value: object) -> str:
    lines = []
    for raw_line in str(value or "").splitlines():
        line = re.sub(r"^\s*\d+[\]\|:\.]?\s*", "", raw_line).rstrip()
        lines.append(line)
    return "\n".join(lines).strip()


def _looks_like_code_resolution(value: object) -> bool:
    stripped = _strip_code_line_numbers(value)
    if not stripped or len(stripped) > PRETAG_GROUNDED_CODE_MAX_CHARS:
        return False
    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines) > PRETAG_GROUNDED_CODE_MAX_LINES:
        return False
    marker_lines = 0
    for line in lines:
        if re.search(r"(?:\bdef\b|\bfor\b|\bif\b|\breturn\b|\bclass\b|\bwith\b|\bimport\b|#|[=\[\]\(\)\{\}:])", line):
            marker_lines += 1
    return marker_lines >= min(2, len(lines))


def _code_resolution_support_score(block: dict[str, object], readable_text: str) -> float:
    stripped_readable = _strip_code_line_numbers(readable_text)
    if not stripped_readable:
        return 0.0
    native_candidate = _strip_code_line_numbers(
        block.get("native_text_candidate")
        or block.get("original_text_candidate")
        or block.get("extracted_text")
        or ""
    )
    ocr_candidate = _strip_code_line_numbers(block.get("ocr_text_candidate") or "")
    return max(
        text_similarity_score(stripped_readable, native_candidate),
        text_similarity_score(stripped_readable, ocr_candidate),
    )


def _neighbor_matches_grounded_hint(block: dict[str, object], readable_text: str) -> bool:
    readable_dense = _normalized_dense_grounded_text(readable_text)
    if not readable_dense:
        return False
    neighbors = (
        (
            str(block.get("previous_text") or "").strip(),
            str(block.get("previous_role") or "").strip(),
        ),
        (
            str(block.get("next_text") or "").strip(),
            str(block.get("next_role") or "").strip(),
        ),
    )
    for neighbor_text, neighbor_role in neighbors:
        if neighbor_role not in PRETAG_GROUNDED_TEXT_ALLOWED_DUPLICATE_ROLES:
            continue
        if not neighbor_text:
            continue
        if text_similarity_score(neighbor_text, readable_text) >= 0.97:
            return True
        if _normalized_dense_grounded_text(neighbor_text) == readable_dense:
            return True
    return False


def _should_auto_artifact_grounded_text_block(block: dict[str, object]) -> bool:
    suggested_action = str(block.get("suggested_action") or "").strip()
    readable_text = str(block.get("readable_text_hint") or "").strip()
    if str(block.get("confidence") or "").strip() != "high":
        return False
    if not bool(block.get("should_block_accessibility", False)):
        return False
    role = str(block.get("role") or "").strip()
    if role not in PRETAG_GROUNDED_TEXT_ALLOWED_ARTIFACT_ROLES:
        return False
    chosen_source = str(block.get("chosen_source") or "").strip()
    if chosen_source not in {"ocr", "llm_inferred"}:
        return False
    if suggested_action == "mark_decorative":
        original_text = str(
            block.get("original_text_candidate")
            or block.get("native_text_candidate")
            or block.get("extracted_text")
            or ""
        ).strip()
        if not original_text or "\n" in original_text:
            return False
        return len(original_text) <= PRETAG_GROUNDED_TEXT_ARTIFACT_MAX_CHARS
    if not readable_text:
        return False
    if str(block.get("issue_type") or "").strip() != "encoding_problem":
        return False
    if "\n" in readable_text or len(readable_text) > PRETAG_GROUNDED_TEXT_ARTIFACT_MAX_CHARS:
        return False
    if len(readable_text.split()) > 14:
        return False
    original_text = str(
        block.get("original_text_candidate")
        or block.get("native_text_candidate")
        or block.get("extracted_text")
        or ""
    ).strip()
    if not original_text:
        return False
    if text_similarity_score(original_text, readable_text) >= 0.6:
        return False
    return _neighbor_matches_grounded_hint(block, readable_text)
