from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.pipeline.structure import _expand_toc_item_tables
from app.services.intelligence_gemini_toc import generate_toc_group_intelligence
from app.services.llm_client import LlmClient

TOC_HEADING_TEXTS = {
    "contents",
    "table of contents",
}
MAX_TOC_GROUPS = 3
MAX_TOC_PAGES = 3
TOC_CHUNK_SIZE = 18
TOC_AUTO_CONFIDENCE = {"high", "medium"}
TOC_ALLOWED_ENTRY_TYPES = {"paragraph", "list_item", "heading", "table", "toc_item", "toc_item_table"}
TOC_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _raw_text(value: Any) -> str:
    return str(value or "").strip()


def _table_preview(element: dict[str, Any]) -> list[str]:
    rows: dict[int, list[dict[str, Any]]] = {}
    for cell in element.get("cells") or []:
        try:
            row = int(cell.get("row", 0))
            col = int(cell.get("col", 0))
        except Exception:
            continue
        rows.setdefault(row, []).append({"col": col, "text": _normalize_text(cell.get("text"))})

    previews: list[str] = []
    for row_index in sorted(rows.keys())[:5]:
        texts = [cell["text"] for cell in sorted(rows[row_index], key=lambda item: item["col"]) if cell["text"]]
        if texts:
            previews.append(" | ".join(texts[:4])[:240])
    return previews


def _chunk_candidate_elements(
    candidate_elements: list[dict[str, Any]],
    *,
    chunk_size: int = TOC_CHUNK_SIZE,
) -> list[list[dict[str, Any]]]:
    if chunk_size <= 0:
        return [candidate_elements]
    return [
        candidate_elements[start : start + chunk_size]
        for start in range(0, len(candidate_elements), chunk_size)
    ]


def _best_confidence_label(labels: list[str]) -> str:
    best = "low"
    best_rank = -1
    for label in labels:
        normalized = str(label or "").strip().lower()
        rank = TOC_CONFIDENCE_RANK.get(normalized, -1)
        if rank > best_rank:
            best = normalized or "low"
            best_rank = rank
    return best


def _merge_toc_group_intelligence(
    *,
    candidate_group: dict[str, Any],
    chunk_results: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible_chunks = [
        chunk
        for chunk in chunk_results
        if bool(chunk.get("is_toc"))
        and str(chunk.get("confidence") or "").strip().lower() in TOC_AUTO_CONFIDENCE
    ]
    merged_entry_indexes: set[int] = set()
    merged_entry_types: dict[str, str] = {}
    merged_entry_text_overrides: dict[str, str] = {}
    caption_text_override = ""
    reasons: list[str] = []

    for chunk in eligible_chunks:
        if not caption_text_override:
            caption_text_override = _normalize_text(chunk.get("caption_text_override"))
        for entry_index in chunk.get("entry_indexes") or []:
            if isinstance(entry_index, int):
                merged_entry_indexes.add(entry_index)
        entry_types = chunk.get("entry_types")
        if isinstance(entry_types, dict):
            for key, value in entry_types.items():
                key_text = str(key).strip()
                value_text = str(value).strip()
                if key_text and value_text:
                    merged_entry_types[key_text] = value_text
        entry_text_overrides = chunk.get("entry_text_overrides")
        if isinstance(entry_text_overrides, dict):
            for key, value in entry_text_overrides.items():
                key_text = str(key).strip()
                value_text = _normalize_text(value)
                if key_text and value_text:
                    merged_entry_text_overrides[key_text] = value_text
        reason = _normalize_text(chunk.get("reason"))
        if reason:
            reasons.append(reason)

    return {
        "caption_index": int(candidate_group.get("caption_index"))
        if isinstance(candidate_group.get("caption_index"), int)
        else -1,
        "is_toc": bool(eligible_chunks),
        "confidence": _best_confidence_label(
            [str(chunk.get("confidence") or "") for chunk in chunk_results]
        ),
        "reason": " ".join(reasons[:3]),
        "entry_indexes": sorted(merged_entry_indexes),
        "entry_types": merged_entry_types,
        "caption_text_override": caption_text_override,
        "entry_text_overrides": merged_entry_text_overrides,
        "chunk_count": len(chunk_results),
    }


def _collect_existing_toc_groups(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None

    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        element_type = str(element.get("type") or "")
        if element_type not in {"toc_caption", "toc_item", "toc_item_table"}:
            current_group = None
            continue

        if element_type == "toc_caption":
            current_group = {
                "caption_index": index,
                "caption_text": _normalize_text(element.get("text"))[:240],
                "caption_raw_text": _raw_text(element.get("text"))[:400],
                "pages": [],
                "candidate_elements": [],
            }
            page_number = element.get("page")
            if isinstance(page_number, int):
                current_group["pages"].append(page_number + 1)
            groups.append(current_group)
            continue

        if current_group is None:
            continue

        page_number = element.get("page")
        if isinstance(page_number, int):
            current_group["pages"].append(page_number + 1)

        candidate_entry = {
            "index": index,
            "type": element_type,
            "page": (page_number + 1) if isinstance(page_number, int) else 1,
            "raw_text": _raw_text(element.get("text"))[:400],
            "text": _normalize_text(element.get("text"))[:240],
            "caption": _normalize_text(element.get("caption"))[:240],
            "raw_caption": _raw_text(element.get("caption"))[:400],
            "bbox": element.get("bbox"),
            "lang": element.get("lang"),
        }
        if element_type == "toc_item_table":
            candidate_entry["table_preview_rows"] = _table_preview(element)
            candidate_entry["row_count"] = int(element.get("num_rows", 0) or 0)
            candidate_entry["col_count"] = int(element.get("num_cols", 0) or 0)
        current_group["candidate_elements"].append(candidate_entry)

    normalized_groups: list[dict[str, Any]] = []
    for group in groups:
        candidate_elements = group.get("candidate_elements")
        if not isinstance(candidate_elements, list) or not candidate_elements:
            continue
        pages = sorted({page for page in group.get("pages", []) if isinstance(page, int) and page > 0})
        normalized_groups.append({
            "caption_index": group["caption_index"],
            "caption_text": group["caption_text"],
            "caption_raw_text": group.get("caption_raw_text", ""),
            "pages": pages[:MAX_TOC_PAGES],
            "candidate_elements": candidate_elements,
        })

    return normalized_groups[:MAX_TOC_GROUPS]


def collect_toc_candidates(
    structure_json: dict[str, Any],
    *,
    include_existing: bool = False,
) -> list[dict[str, Any]]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []
    has_existing_toc = any(
        isinstance(element, dict) and element.get("type") in {"toc_caption", "toc_item", "toc_item_table"}
        for element in elements
    )
    if has_existing_toc and include_existing:
        return _collect_existing_toc_groups(elements)
    if has_existing_toc:
        return []

    candidates: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        if element.get("type") not in {"heading", "toc_caption"}:
            continue
        heading_text = _normalize_text(element.get("text")).lower()
        if heading_text not in TOC_HEADING_TEXTS:
            continue

        start_page = element.get("page")
        if not isinstance(start_page, int):
            start_page = 0

        candidate_elements: list[dict[str, Any]] = []
        cursor = index + 1
        while cursor < len(elements):
            candidate = elements[cursor]
            if not isinstance(candidate, dict):
                cursor += 1
                continue
            candidate_page = candidate.get("page")
            if not isinstance(candidate_page, int):
                candidate_page = start_page
            if candidate_page > start_page + (MAX_TOC_PAGES - 1):
                break
            candidate_type = str(candidate.get("type") or "")
            if candidate_type == "artifact":
                cursor += 1
                continue
            candidate_entry = {
                "index": cursor,
                "type": candidate_type,
                "page": candidate_page + 1,
                "raw_text": _raw_text(candidate.get("text"))[:400],
                "text": _normalize_text(candidate.get("text"))[:240],
                "caption": _normalize_text(candidate.get("caption"))[:240],
                "raw_caption": _raw_text(candidate.get("caption"))[:400],
                "bbox": candidate.get("bbox"),
                "lang": candidate.get("lang"),
            }
            if candidate_type == "table":
                candidate_entry["table_preview_rows"] = _table_preview(candidate)
                candidate_entry["row_count"] = int(candidate.get("num_rows", 0) or 0)
                candidate_entry["col_count"] = int(candidate.get("num_cols", 0) or 0)
            candidate_elements.append(candidate_entry)
            cursor += 1

        if not candidate_elements:
            continue

        pages = sorted({start_page + 1, *[
            candidate["page"]
            for candidate in candidate_elements
            if isinstance(candidate.get("page"), int)
        ]})
        candidates.append({
            "caption_index": index,
            "caption_text": _normalize_text(element.get("text"))[:240],
            "caption_raw_text": _raw_text(element.get("text"))[:400],
            "pages": pages[:MAX_TOC_PAGES],
            "candidate_elements": candidate_elements,
        })

    return candidates[:MAX_TOC_GROUPS]


def apply_toc_intelligence(
    structure_json: dict[str, Any],
    intelligence: dict[str, Any],
) -> dict[str, Any]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return {
            "attempted": True,
            "applied": False,
            "reason": "missing_elements",
            "groups_applied": 0,
        }

    candidate_groups = {
        group["caption_index"]: group
        for group in collect_toc_candidates(structure_json, include_existing=True)
    }
    groups = intelligence.get("groups")
    if not isinstance(groups, list):
        return {
            "attempted": True,
            "applied": False,
            "reason": "no_groups",
            "groups_applied": 0,
        }

    groups_applied = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        caption_index = group.get("caption_index")
        confidence = str(group.get("confidence") or "").strip().lower()
        if not isinstance(caption_index, int) or caption_index not in candidate_groups:
            continue
        if not bool(group.get("is_toc")) or confidence not in TOC_AUTO_CONFIDENCE:
            continue

        candidate_group = candidate_groups[caption_index]
        allowed_indexes = {
            int(item["index"]): str(item.get("type") or "")
            for item in candidate_group.get("candidate_elements", [])
            if isinstance(item, dict) and isinstance(item.get("index"), int)
        }
        entry_indexes = group.get("entry_indexes")
        if not isinstance(entry_indexes, list):
            continue
        valid_entry_indexes = [idx for idx in entry_indexes if isinstance(idx, int) and idx in allowed_indexes]
        if not valid_entry_indexes:
            continue

        toc_group_ref = f"toc-intelligence-{caption_index}"
        elements[caption_index]["type"] = "toc_caption"
        elements[caption_index]["toc_group_ref"] = toc_group_ref
        caption_text_override = _normalize_text(group.get("caption_text_override"))
        if caption_text_override:
            elements[caption_index]["text"] = caption_text_override

        entry_types = group.get("entry_types")
        if not isinstance(entry_types, dict):
            entry_types = {}
        entry_text_overrides = group.get("entry_text_overrides")
        if not isinstance(entry_text_overrides, dict):
            entry_text_overrides = {}

        for entry_index in valid_entry_indexes:
            source_type = allowed_indexes[entry_index]
            requested = str(entry_types.get(str(entry_index)) or entry_types.get(entry_index) or "").strip()
            if requested not in {"toc_item", "toc_item_table"}:
                requested = "toc_item_table" if source_type == "table" else "toc_item"
            if requested == "toc_item_table" and source_type != "table":
                requested = "toc_item"
            if requested == "toc_item" and source_type == "table":
                requested = "toc_item_table"
            elements[entry_index]["type"] = requested
            elements[entry_index]["toc_group_ref"] = toc_group_ref
            text_override = _normalize_text(
                entry_text_overrides.get(str(entry_index)) or entry_text_overrides.get(entry_index)
            )
            if text_override and source_type != "table":
                elements[entry_index]["text"] = text_override

        groups_applied += 1

    structure_json["elements"] = _expand_toc_item_tables(elements)

    return {
        "attempted": True,
        "applied": groups_applied > 0,
        "reason": "" if groups_applied > 0 else "no_eligible_groups",
        "groups_applied": groups_applied,
    }


async def enhance_toc_structure_with_intelligence(
    *,
    pdf_path: Path,
    structure_json: dict[str, Any],
    original_filename: str,
    llm_client: LlmClient,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_groups = collect_toc_candidates(structure_json, include_existing=True)
    if not candidate_groups:
        return structure_json, {
            "attempted": False,
            "applied": False,
            "reason": "no_candidates",
            "groups_considered": 0,
            "groups_applied": 0,
        }

    groups: list[dict[str, Any]] = []
    total_chunks = 0
    for group in candidate_groups:
        candidate_elements = group.get("candidate_elements")
        if not isinstance(candidate_elements, list) or not candidate_elements:
            continue
        chunk_results: list[dict[str, Any]] = []
        for chunk in _chunk_candidate_elements(candidate_elements):
            chunk_pages = sorted({
                int(item["page"])
                for item in chunk
                if isinstance(item, dict) and isinstance(item.get("page"), int) and int(item["page"]) > 0
            })
            chunk_group = {
                **group,
                "pages": chunk_pages[:MAX_TOC_PAGES] or list(group.get("pages") or [])[:MAX_TOC_PAGES],
                "candidate_elements": chunk,
            }
            chunk_results.append(
                await generate_toc_group_intelligence(
                    pdf_path=pdf_path,
                    original_filename=original_filename,
                    candidate_group=chunk_group,
                    llm_client=llm_client,
                )
            )
        total_chunks += len(chunk_results)
        groups.append(
            _merge_toc_group_intelligence(
                candidate_group=group,
                chunk_results=chunk_results,
            )
        )

    intelligence = {
        "groups": groups,
        "generated_at": datetime.now(UTC).isoformat(),
        "model": llm_client.model,
    }
    audit = apply_toc_intelligence(structure_json, intelligence)
    audit["groups_considered"] = len(candidate_groups)
    audit["chunk_count"] = total_chunks
    audit["intelligence"] = intelligence
    return structure_json, audit
