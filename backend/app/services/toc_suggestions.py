import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.pipeline.structure import _expand_toc_item_tables
from app.services.llm_client import LlmClient
from app.services.pdf_preview import render_page_png_data_url

TOC_HEADING_TEXTS = {
    "contents",
    "table of contents",
}
MAX_TOC_GROUPS = 3
MAX_TOC_PAGES = 3
MAX_TOC_ELEMENTS = 18
TOC_AUTO_CONFIDENCE = {"high", "medium"}
TOC_ALLOWED_ENTRY_TYPES = {"paragraph", "list_item", "heading", "table", "toc_item", "toc_item_table"}

TOC_REVIEW_PROMPT = """You are assisting PDF accessibility remediation for a PDF/UA workflow.

You will receive:
- full-page previews of candidate table-of-contents pages
- a JSON payload describing extracted structural elements near headings like "Contents" or "Table of Contents"

Respond with strict JSON only using this schema:
{
  "groups": [
    {
      "caption_index": 4,
      "is_toc": true,
      "confidence": "high" | "medium" | "low",
      "reason": "short explanation",
      "entry_indexes": [5, 6, 7],
      "entry_types": {
        "5": "toc_item",
        "6": "toc_item_table"
      }
    }
  ]
}

Rules:
- Only return groups for caption_index values present in candidate_groups.
- Use is_toc=false and leave entry_indexes empty when the page is not actually a table of contents.
- entry_indexes must only refer to indices listed in that group's candidate_elements.
- Use toc_item_table only for candidate elements whose source type is table.
- Use toc_item for heading, paragraph, or list-style entries.
- Prefer precision over recall. If unsure, omit the group or set confidence to low.
- Do not invent entries that are not represented in candidate_elements.
- Do not include markdown fences or commentary outside the JSON object.
"""


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


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


def collect_toc_candidates(structure_json: dict[str, Any]) -> list[dict[str, Any]]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []
    if any(
        isinstance(element, dict) and element.get("type") in {"toc_caption", "toc_item", "toc_item_table"}
        for element in elements
    ):
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
        while cursor < len(elements) and len(candidate_elements) < MAX_TOC_ELEMENTS:
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
                "text": _normalize_text(candidate.get("text"))[:240],
                "caption": _normalize_text(candidate.get("caption"))[:240],
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
            "pages": pages[:MAX_TOC_PAGES],
            "candidate_elements": candidate_elements,
        })

    return candidates[:MAX_TOC_GROUPS]


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty LLM response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON was not an object")
    return parsed


def build_toc_llm_payload(
    *,
    pdf_path: Path,
    structure_json: dict[str, Any],
    original_filename: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_groups = collect_toc_candidates(structure_json)
    page_numbers: list[int] = []
    for group in candidate_groups:
        for page_number in group.get("pages", []):
            if isinstance(page_number, int) and page_number > 0 and page_number not in page_numbers:
                page_numbers.append(page_number)
    images: list[dict[str, Any]] = []
    for page_number in page_numbers[:MAX_TOC_PAGES]:
        try:
            image_url = render_page_png_data_url(pdf_path, page_number)
        except Exception:
            continue
        images.append({
            "type": "image_url",
            "image_url": {"url": image_url},
        })
    payload = {
        "job_filename": original_filename,
        "candidate_groups": candidate_groups,
        "page_numbers": page_numbers[:MAX_TOC_PAGES],
    }
    return payload, images


def apply_toc_llm_suggestion(
    structure_json: dict[str, Any],
    suggestion: dict[str, Any],
) -> dict[str, Any]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return {
            "attempted": True,
            "applied": False,
            "reason": "missing_elements",
            "groups_applied": 0,
        }

    candidate_groups = {group["caption_index"]: group for group in collect_toc_candidates(structure_json)}
    groups = suggestion.get("groups")
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

        toc_group_ref = f"toc-llm-{caption_index}"
        elements[caption_index]["type"] = "toc_caption"
        elements[caption_index]["toc_group_ref"] = toc_group_ref

        entry_types = group.get("entry_types")
        if not isinstance(entry_types, dict):
            entry_types = {}

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

        groups_applied += 1

    structure_json["elements"] = _expand_toc_item_tables(elements)

    return {
        "attempted": True,
        "applied": groups_applied > 0,
        "reason": "" if groups_applied > 0 else "no_eligible_groups",
        "groups_applied": groups_applied,
    }


async def enhance_toc_structure_with_llm(
    *,
    pdf_path: Path,
    structure_json: dict[str, Any],
    original_filename: str,
    llm_client: LlmClient,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, images = build_toc_llm_payload(
        pdf_path=pdf_path,
        structure_json=structure_json,
        original_filename=original_filename,
    )
    candidate_groups = payload.get("candidate_groups", [])
    if not candidate_groups:
        return structure_json, {
            "attempted": False,
            "applied": False,
            "reason": "no_candidates",
            "groups_considered": 0,
            "groups_applied": 0,
        }

    prompt_text = (
        f"{TOC_REVIEW_PROMPT}\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
    )
    content = [{"type": "text", "text": prompt_text}, *images]

    try:
        response = await llm_client.chat_completion(
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception:
        response = await llm_client.chat_completion(
            messages=[{"role": "user", "content": content}],
            temperature=0,
        )

    try:
        message_content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected LLM response format: {exc}") from exc

    suggestion = _extract_json_object(str(message_content))
    suggestion["generated_at"] = datetime.now(UTC).isoformat()
    suggestion["model"] = llm_client.model
    audit = apply_toc_llm_suggestion(structure_json, suggestion)
    audit["groups_considered"] = len(candidate_groups)
    audit["suggestion"] = suggestion
    return structure_json, audit
