from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from app.config import get_settings
from app.services.gemini_direct import (
    create_direct_gemini_pdf_cache,
    delete_direct_gemini_pdf_cache,
    request_direct_gemini_cached_json,
)
from app.services.intelligence_llm_utils import (
    context_json_part,
    page_preview_parts,
    preferred_cache_breakpoint_index,
    request_llm_json,
)
from app.services.llm_client import LlmClient
from app.services.local_semantic import local_semantic_enabled

BOOKMARK_DOCUMENT_CANDIDATE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_document_candidate_plan"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "front_matter_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page": {"type": "integer", "minimum": 1},
                    "label": {
                        "type": "string",
                        "enum": ["Cover", "Inside-Cover page", "Series Information"],
                    },
                },
                "required": ["page", "label"],
            },
        },
        "outline_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "candidate_id": {"type": "string"},
                    "level": {"type": "integer", "minimum": 1, "maximum": 6},
                    "supported_label": {"type": "string"},
                },
                "required": ["candidate_id", "level", "supported_label"],
            },
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "reason",
        "front_matter_entries",
        "outline_entries",
    ],
}

BOOKMARK_DOCUMENT_CANDIDATE_PLAN_PROMPT = """You are a PDF accessibility bookmark planning assistant.

The cached PDF is the primary source of truth. The JSON context contains an exhaustive inventory of bookmark candidates
derived from Docling-visible evidence.

Your job is to adjudicate that candidate inventory and return the final bookmark outline.

Rules:
- Use the cached PDF as the main evidence. The JSON context is only an index into candidate IDs and page anchors.
- Use only the provided candidate_id values in outline_entries. Do not invent bookmark entries there.
- Treat TOC candidates as the baseline skeleton unless the cached PDF makes one clearly invalid.
- Heading, heading_variant, and landmark candidates are optional evidence-backed suggestions.
- Keep optional candidates when the cached PDF shows that they materially improve navigation.
- Repeated visible labels on different pages are distinct candidates and may each be kept when they mark different visible sections.
- Only suppress a candidate as redundant when the cached PDF shows it points to the same visible section as another kept candidate.
- Use supported_label only when choosing one of that candidate's supported_labels.
- For TOC candidates, preserve preferred_label because it is the TOC-visible label; use heading variants as separate heading evidence, not as rewrites of the TOC label.
- For non-TOC candidates, choose the label variant that best matches the visible document evidence.
- For higher-order pre-TOC roles, use front_matter_entries with a 1-based page and one of these exact labels:
  - Cover
  - Inside-Cover page
  - Series Information
- Do not invent labels that are not visibly supported by the cached PDF.
- Return the final outline in document order.
"""

BOOKMARK_DOCUMENT_LANDMARK_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_document_landmark_plan"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "selected_landmarks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page": {"type": "integer", "minimum": 1},
                    "label": {"type": "string"},
                    "anchor_candidate_id": {"type": "string"},
                    "level": {"type": "integer", "minimum": 1, "maximum": 6},
                },
                "required": ["page", "label", "anchor_candidate_id", "level"],
            },
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "reason",
        "selected_landmarks",
    ],
}

BOOKMARK_DOCUMENT_LANDMARK_PLAN_PROMPT = """You are a PDF accessibility bookmark landmark planning assistant.

The cached PDF is the primary source of truth. You already have a selected bookmark skeleton from the TOC and visible headings.

Your job is to identify additional useful non-heading visible landmarks that should be inserted beneath that skeleton.

Rules:
- Use the cached PDF as the main evidence.
- Return only non-heading landmarks that are visibly distinct and materially improve navigation beneath the selected skeleton.
- Do not return ordinary running prose, captions, or page furniture.
- Use the provided anchor_candidate_id values to attach each extra landmark beneath the most appropriate selected outline entry.
- Return the exact visible label from the document.
- Keep the list selective. Do not omit a visibly supported landmark that materially improves navigation.
"""

BOOKMARK_DOCUMENT_HEADING_SUPPLEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_document_heading_supplement"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "outline_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "candidate_id": {"type": "string"},
                    "level": {"type": "integer", "minimum": 1, "maximum": 6},
                    "supported_label": {"type": "string"},
                },
                "required": ["candidate_id", "level", "supported_label"],
            },
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "reason",
        "outline_entries",
    ],
}

BOOKMARK_DOCUMENT_HEADING_SUPPLEMENT_PROMPT = """You are a PDF accessibility bookmark supplement planning assistant.

The cached PDF is the primary source of truth. You already have a selected bookmark skeleton.
Your job is to review the remaining heading candidates only and return any additional visible headings that materially improve navigation.

Rules:
- Use the cached PDF as the main evidence.
- Use only the provided candidate_id values in outline_entries.
- Return only additional heading or heading_variant candidates that should be added to the existing skeleton.
- Repeated visible labels on different pages are distinct candidates and may each be kept when they mark different visible sections.
- Short visible subsection headings may still be useful when they clearly identify a navigable subsection.
- Use supported_label only when choosing one of that candidate's supported_labels.
- Do not invent labels or candidates that are not visibly supported by the cached PDF.
- Keep the list selective, but do not omit a visibly supported heading only because its label resembles another heading elsewhere in the document.
"""

BOOKMARK_DIRECT_GEMINI_SYSTEM_INSTRUCTION = (
    "You are evaluating PDF accessibility and bookmark/navigation semantics. "
    "Stay grounded in the provided document evidence and return only valid JSON."
)

MAX_BOOKMARK_TOC_ITEMS = 80
BOOKMARK_HEADING_TYPES = {"heading"}
BOOKMARK_LANDMARK_TYPES = {"paragraph", "list_item", "note"}
BOOKMARK_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
FRONT_MATTER_AUTO_CONFIDENCE = {"high", "medium"}
TOC_TRAILING_PAGE_RE = re.compile(
    r"(?:\.{2,}\s*|(?:\.\s*){2,}|\s{2,}|\t+)(?:\d+|[ivxlcdm]+)\s*$|(?:\s+)(?:\d+)\s*$",
    re.IGNORECASE,
)
APPENDIX_BOOKMARK_KEY_RE = re.compile(r"^(appendix\s+[a-z0-9]+(?:\.\d+)*)\b", re.IGNORECASE)
NUMERIC_BOOKMARK_KEY_RE = re.compile(r"^(\d+(?:\.\d+)*)\b")
FRAGMENTED_WORD_RE = re.compile(r"\b([A-Za-z])\s+([a-z]{2,})\b")


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _int_or_default(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_bookmark_label(value: Any) -> str:
    label = _normalize_text(value)
    if not label:
        return ""
    label = TOC_TRAILING_PAGE_RE.sub("", label).rstrip(" .\t")
    return label.strip()


def _normalize_title_like_text(value: Any) -> str:
    text = _normalize_text(value).lower()
    text = "".join(ch for ch in text if ch.isalnum() or ch.isspace())
    return " ".join(text.split()).strip()


def _collapse_fragmented_words(value: Any) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    previous = None
    current = text
    while current != previous:
        previous = current
        current = FRAGMENTED_WORD_RE.sub(lambda match: match.group(1) + match.group(2), current)
    return current


def _bookmark_section_key(value: Any) -> str:
    label = _clean_bookmark_label(value)
    if not label:
        return ""

    appendix_match = APPENDIX_BOOKMARK_KEY_RE.match(label)
    if appendix_match:
        return re.sub(r"\s+", " ", appendix_match.group(1).lower()).strip()

    numeric_match = NUMERIC_BOOKMARK_KEY_RE.match(label)
    if numeric_match:
        return numeric_match.group(1)

    return ""


def _candidate_merge_key(value: Any) -> str:
    section_key = _bookmark_section_key(value)
    if section_key:
        return f"section:{section_key}"
    normalized = _normalize_title_like_text(value)
    return f"text:{normalized}" if normalized else ""


def _parent_section_key(value: Any) -> str:
    section_key = _bookmark_section_key(value)
    if not section_key:
        return ""
    if section_key.startswith("appendix "):
        prefix, _, suffix = section_key.partition(" ")
        if "." in suffix:
            return f"{prefix} {suffix.rsplit('.', 1)[0]}".strip()
        return section_key
    if "." in section_key:
        return section_key.rsplit(".", 1)[0]
    return section_key


def _dedupe_supported_labels(labels: list[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for label in labels:
        for variant in (_normalize_text(label), _collapse_fragmented_words(label)):
            normalized = _normalize_title_like_text(variant)
            if not variant or not normalized or normalized in seen:
                continue
            deduped.append(variant[:240])
            seen.add(normalized)
    return deduped


def _preferred_supported_label(labels: list[str], *, fallback: str) -> str:
    deduped = _dedupe_supported_labels(labels)
    if not deduped:
        return _clean_bookmark_label(fallback)
    return deduped[0]


def _resolve_supported_label(supported_labels: list[str], requested_label: Any, *, fallback: str) -> str:
    requested_raw = _normalize_text(requested_label)
    requested_clean = _clean_bookmark_label(requested_label)
    requested_raw_norm = _normalize_title_like_text(requested_raw)
    requested_clean_norm = _normalize_title_like_text(requested_clean)
    if not requested_raw_norm and not requested_clean_norm:
        return _preferred_supported_label(supported_labels, fallback=fallback)

    for label in supported_labels:
        if requested_raw_norm and _normalize_title_like_text(_normalize_text(label)) == requested_raw_norm:
            return label

    for label in supported_labels:
        if requested_clean_norm and _normalize_title_like_text(_clean_bookmark_label(label)) == requested_clean_norm:
            return label

    return _preferred_supported_label(supported_labels, fallback=fallback)


def _sample_preview_pages(pages: list[int], *, limit: int = 4) -> list[int]:
    unique_pages = sorted({page for page in pages if isinstance(page, int) and page > 0})
    if len(unique_pages) <= limit:
        return unique_pages
    sampled: list[int] = []
    for slot in range(limit):
        index = round(slot * (len(unique_pages) - 1) / max(limit - 1, 1))
        page = unique_pages[index]
        if page not in sampled:
            sampled.append(page)
    return sampled


def _bookmark_prompt_for_local(preview_prompt: str) -> str:
    return preview_prompt.replace("cached PDF", "page preview images").replace(
        "cached pdf", "page preview images"
    )


def _bookmark_job_for_preview(pdf_path, original_filename: str) -> Any:
    return SimpleNamespace(
        original_filename=original_filename,
        input_path=str(pdf_path),
        output_path=str(pdf_path),
    )


def _pages_from_outline_entries(entries: list[dict[str, Any]]) -> list[int]:
    pages: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page_index = _int_or_default(entry.get("page_index"), -1)
        if page_index >= 0:
            pages.append(page_index + 1)
    return pages


def _pages_from_outline_candidates(entries: list[dict[str, Any]]) -> list[int]:
    pages: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source_page = _int_or_default(entry.get("source_page"), -1)
        if source_page > 0:
            pages.append(source_page)
            continue
        page_index = _int_or_default(entry.get("target_page_index"), -1)
        if page_index >= 0:
            pages.append(page_index + 1)
    return pages


def _local_bookmark_preview_pages(
    *,
    base_pages: list[int],
    extra_pages: list[int] | None = None,
) -> list[int]:
    pages = list(base_pages or [])
    if extra_pages:
        pages.extend(extra_pages)
    sampled = _sample_preview_pages(
        pages,
        limit=max(1, int(get_settings().local_semantic_bookmark_preview_pages or 4)),
    )
    return sampled or [1]


async def _request_bookmark_json(
    *,
    pdf_path,
    original_filename: str,
    llm_client: LlmClient,
    prompt: str,
    context_payload: dict[str, Any],
    response_schema: dict[str, Any],
    preview_pages: list[int] | None = None,
    cache_handle: Any | None = None,
) -> dict[str, Any]:
    if local_semantic_enabled():
        job = _bookmark_job_for_preview(pdf_path, original_filename)
        content = [
            {"type": "text", "text": _bookmark_prompt_for_local(prompt)},
            *page_preview_parts(job, preview_pages or [1]),
            context_json_part(context_payload),
        ]
        return await request_llm_json(
            llm_client=llm_client,
            content=content,
            schema_name=response_schema["properties"]["task_type"]["enum"][0],
            response_schema=response_schema,
            cache_breakpoint_index=preferred_cache_breakpoint_index(content),
        )

    if cache_handle is None:
        raise RuntimeError("Gemini bookmark request requires a cache handle")
    return await request_direct_gemini_cached_json(
        cache_handle=cache_handle,
        prompt=prompt,
        context_payload=context_payload,
        response_schema=response_schema,
    )


def _compact_llm_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        compacted[key] = value
    return compacted


def _serialize_outline_candidates_for_direct_llm(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        preferred_label = _normalize_text(entry.get("preferred_label") or entry.get("text"))
        supported_labels = [
            str(label).strip()
            for label in (entry.get("supported_labels") or [])
            if str(label).strip()
        ]
        serialized.append(
            _compact_llm_mapping({
                "candidate_id": entry.get("candidate_id"),
                "source_kind": entry.get("source_kind"),
                "source_index": entry.get("source_index"),
                "preferred_label": preferred_label,
                "supported_labels": supported_labels,
                "raw_text": entry.get("raw_text"),
                "target_page_index": entry.get("target_page_index"),
                "source_page": entry.get("source_page"),
                "default_level": entry.get("default_level"),
                "previous_visible_label": entry.get("previous_visible_label"),
                "next_visible_label": entry.get("next_visible_label"),
                "anchor_heading_text": entry.get("anchor_heading_text"),
                "previous_body_text": entry.get("previous_body_text"),
                "following_body_text": entry.get("following_body_text"),
            })
        )
    return serialized


def _serialize_heading_supplement_candidates_for_direct_llm(
    entries: list[dict[str, Any]],
    *,
    selected_outline_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected_by_page = sorted(
        [
            entry
            for entry in selected_outline_entries
            if isinstance(entry, dict) and isinstance(entry.get("page_index"), int)
        ],
        key=lambda item: (
            _int_or_default(item.get("page_index"), 10**9),
            _int_or_default(item.get("level"), 1),
            _normalize_text(item.get("text")),
        ),
    )
    serialized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page_index = _int_or_default(entry.get("target_page_index"), -1)
        previous_outline_label = ""
        next_outline_label = ""
        for outline_entry in selected_by_page:
            outline_page_index = _int_or_default(outline_entry.get("page_index"), -1)
            if outline_page_index < 0:
                continue
            if outline_page_index <= page_index:
                previous_outline_label = _normalize_text(outline_entry.get("text"))
                continue
            next_outline_label = _normalize_text(outline_entry.get("text"))
            break
        serialized.append(
            _compact_llm_mapping({
                "candidate_id": entry.get("candidate_id"),
                "source_kind": entry.get("source_kind"),
                "source_index": entry.get("source_index"),
                "preferred_label": _normalize_text(entry.get("preferred_label") or entry.get("text")),
                "supported_labels": [
                    str(label).strip()
                    for label in (entry.get("supported_labels") or [])
                    if str(label).strip()
                ],
                "raw_text": entry.get("raw_text"),
                "target_page_index": entry.get("target_page_index"),
                "source_page": entry.get("source_page"),
                "default_level": entry.get("default_level"),
                "previous_outline_label": previous_outline_label,
                "next_outline_label": next_outline_label,
                "previous_visible_label": entry.get("previous_visible_label"),
                "next_visible_label": entry.get("next_visible_label"),
                "previous_body_text": entry.get("previous_body_text"),
                "following_body_text": entry.get("following_body_text"),
            })
        )
    return serialized

def _match_section_landmark_selections(
    selected_landmarks: Any,
    *,
    landmark_candidates: list[dict[str, Any]],
) -> tuple[set[int], dict[str | int, str]]:
    matched_indexes: set[int] = set()
    label_overrides: dict[str | int, str] = {}
    if not isinstance(selected_landmarks, list):
        return matched_indexes, label_overrides
    for item in selected_landmarks:
        if not isinstance(item, dict):
            continue
        page = _int_or_default(item.get("page"), -1)
        label = _clean_bookmark_label(item.get("label"))
        normalized = _normalize_title_like_text(label)
        if page <= 0 or not normalized:
            continue
        page_matches = [
            entry
            for entry in landmark_candidates
            if isinstance(entry.get("page"), int) and int(entry["page"]) == page
        ]
        best: dict[str, Any] | None = None
        for entry in page_matches:
            entry_text = _clean_bookmark_label(entry.get("text"))
            entry_norm = _normalize_title_like_text(entry_text)
            if entry_norm == normalized:
                best = entry
                break
        if best is None:
            for entry in page_matches:
                entry_text = _clean_bookmark_label(entry.get("text"))
                entry_norm = _normalize_title_like_text(entry_text)
                if normalized in entry_norm or entry_norm in normalized:
                    best = entry
                    break
        if best is None:
            continue
        index = _int_or_default(best.get("index"), -1)
        if index < 0:
            continue
        matched_indexes.add(index)
        if label and _normalize_text(label) != _normalize_text(best.get("text")):
            label_overrides[str(index)] = label
            label_overrides[index] = label
    return matched_indexes, label_overrides


def _materialize_outline_entries_from_plan(
    outline_entries_raw: Any,
    *,
    outline_candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    candidates_by_id = {
        str(candidate["candidate_id"]): candidate
        for candidate in outline_candidates
        if isinstance(candidate, dict) and str(candidate.get("candidate_id") or "").strip()
    }
    outline_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if not isinstance(outline_entries_raw, list):
        outline_entries_raw = []
    for item in outline_entries_raw:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "").strip()
        if not candidate_id or candidate_id in seen_ids:
            continue
        candidate = candidates_by_id.get(candidate_id)
        if candidate is None:
            continue
        try:
            level = int(item.get("level", candidate.get("default_level", 1)) or 1)
        except (TypeError, ValueError):
            level = int(candidate.get("default_level", 1) or 1)
        if str(candidate.get("source_kind") or "").strip() == "toc":
            label = _clean_bookmark_label(candidate.get("preferred_label") or candidate.get("text"))
        else:
            label = _resolve_supported_label(
                list(candidate.get("supported_labels") or []),
                item.get("supported_label", item.get("label_override")),
                fallback=str(candidate.get("preferred_label") or candidate["text"]),
            )
        if not label:
            continue
        outline_entries.append({
            "candidate_id": candidate_id,
            "source_kind": candidate.get("source_kind"),
            "source_index": candidate.get("source_index"),
            "text": label,
            "raw_text": candidate.get("raw_text"),
            "preferred_label": candidate.get("preferred_label"),
            "supported_labels": list(candidate.get("supported_labels") or []),
            "page_index": int(candidate["target_page_index"]),
            "level": max(1, min(6, level)),
            "previous_visible_label": candidate.get("previous_visible_label"),
            "next_visible_label": candidate.get("next_visible_label"),
            "anchor_heading_text": candidate.get("anchor_heading_text"),
            "previous_body_text": candidate.get("previous_body_text"),
            "following_body_text": candidate.get("following_body_text"),
        })
        seen_ids.add(candidate_id)
    return outline_entries, seen_ids


def _merge_outline_entries(
    primary_entries: list[dict[str, Any]],
    supplemental_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in [*primary_entries, *supplemental_entries]:
        if not isinstance(entry, dict):
            continue
        candidate_id = str(entry.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        if candidate_id not in merged_by_id:
            order.append(candidate_id)
            merged_by_id[candidate_id] = dict(entry)
            continue
        merged_by_id[candidate_id].update(entry)
    merged = [merged_by_id[candidate_id] for candidate_id in order]
    merged.sort(
        key=lambda item: (
            _int_or_default(item.get("page_index"), 10**9),
            _int_or_default(item.get("level"), 1),
            _int_or_default(item.get("source_index"), 10**9),
            _normalize_text(item.get("text")),
        )
    )
    return merged


def _materialize_front_matter_entries(
    front_matter_entries_raw: Any,
    *,
    front_matter_page_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    valid_front_pages = {
        int(page.get("page_number")): int(page.get("page_index"))
        for page in front_matter_page_candidates
        if isinstance(page, dict)
        and isinstance(page.get("page_number"), int)
        and isinstance(page.get("page_index"), int)
    }
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    if not isinstance(front_matter_entries_raw, list):
        front_matter_entries_raw = []
    for order_index, item in enumerate(front_matter_entries_raw):
        if not isinstance(item, dict):
            continue
        page_number = _int_or_default(item.get("page"), -1)
        label = _normalize_text(item.get("label"))
        page_index = valid_front_pages.get(page_number)
        if page_index is None or label not in {"Cover", "Inside-Cover page", "Series Information"}:
            continue
        key = (_normalize_title_like_text(label), page_index)
        if key in seen:
            continue
        entries.append({
            "candidate_id": f"front:auto:{order_index}",
            "source_kind": "front_matter",
            "source_index": page_index,
            "text": label,
            "page_index": page_index,
            "level": 1,
        })
        seen.add(key)
    return entries


def _serialize_selected_outline_for_landmark_llm(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    ordered = sorted(
        [entry for entry in entries if isinstance(entry, dict)],
        key=lambda item: (
            _int_or_default(item.get("page_index"), 10**9),
            _int_or_default(item.get("level"), 1),
            _normalize_text(item.get("text")),
        ),
    )
    for idx, entry in enumerate(ordered):
        page_index = _int_or_default(entry.get("page_index"), -1)
        if page_index < 0:
            continue
        next_page_index = _int_or_default(ordered[idx + 1].get("page_index"), -1) if idx + 1 < len(ordered) else None
        serialized.append(
            _compact_llm_mapping({
                "candidate_id": entry.get("candidate_id"),
                "source_kind": entry.get("source_kind"),
                "label": entry.get("text"),
                "raw_text": entry.get("raw_text"),
                "supported_labels": entry.get("supported_labels"),
                "level": entry.get("level"),
                "page": page_index + 1,
                "next_outline_page": (next_page_index + 1) if next_page_index is not None and next_page_index >= 0 else None,
                "previous_visible_label": entry.get("previous_visible_label"),
                "next_visible_label": entry.get("next_visible_label"),
                "anchor_heading_text": entry.get("anchor_heading_text"),
                "previous_body_text": entry.get("previous_body_text"),
                "following_body_text": entry.get("following_body_text"),
            })
        )
    return serialized


def _materialize_landmark_entries_from_plan(
    selected_landmarks_raw: Any,
    *,
    landmark_candidates: list[dict[str, Any]],
    anchor_level_by_id: dict[str, int],
) -> tuple[list[dict[str, Any]], set[int]]:
    if not isinstance(selected_landmarks_raw, list):
        return [], set()
    entries: list[dict[str, Any]] = []
    selected_indexes: set[int] = set()
    seen: set[tuple[str, int]] = set()
    for item in selected_landmarks_raw:
        if not isinstance(item, dict):
            continue
        anchor_candidate_id = str(item.get("anchor_candidate_id") or "").strip()
        if anchor_candidate_id not in anchor_level_by_id:
            continue
        matched_indexes, landmark_label_overrides = _match_section_landmark_selections(
            [{"page": item.get("page"), "label": item.get("label")}],
            landmark_candidates=landmark_candidates,
        )
        if not matched_indexes:
            continue
        source_index = min(matched_indexes)
        candidate = next(
            (
                entry
                for entry in landmark_candidates
                if isinstance(entry, dict) and _int_or_default(entry.get("index"), -1) == source_index
            ),
            None,
        )
        if candidate is None:
            continue
        label = _normalize_text(
            landmark_label_overrides.get(str(source_index))
            or landmark_label_overrides.get(source_index)
            or candidate.get("text")
        )
        page_index = _int_or_default(candidate.get("page"), 1) - 1
        if not label or page_index < 0:
            continue
        try:
            requested_level = int(item.get("level") or (anchor_level_by_id[anchor_candidate_id] + 1))
        except (TypeError, ValueError):
            requested_level = anchor_level_by_id[anchor_candidate_id] + 1
        level = max(1, min(6, requested_level))
        key = (_normalize_title_like_text(label), page_index)
        if key in seen:
            continue
        entries.append({
            "candidate_id": f"landmark:{source_index}",
            "source_kind": "landmark",
            "source_index": source_index,
            "text": label,
            "page_index": page_index,
            "level": level,
            "anchor_candidate_id": anchor_candidate_id,
        })
        selected_indexes.add(source_index)
        seen.add(key)
    return entries, selected_indexes


def _merge_outline_with_landmarks(
    outline_entries: list[dict[str, Any]],
    landmark_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not landmark_entries:
        return list(outline_entries)
    by_anchor: dict[str, list[dict[str, Any]]] = {}
    for entry in landmark_entries:
        if not isinstance(entry, dict):
            continue
        anchor = str(entry.get("anchor_candidate_id") or "").strip()
        by_anchor.setdefault(anchor, []).append(entry)
    for anchor_entries in by_anchor.values():
        anchor_entries.sort(
            key=lambda item: (
                _int_or_default(item.get("page_index"), 10**9),
                _normalize_text(item.get("text")),
            )
        )
    merged: list[dict[str, Any]] = []
    for entry in outline_entries:
        merged.append(entry)
        anchor_id = str(entry.get("candidate_id") or "").strip()
        merged.extend(by_anchor.pop(anchor_id, []))
    for leftovers in by_anchor.values():
        merged.extend(leftovers)
    return merged

def _merge_bookmark_plan_entries(
    front_matter_entries: list[dict[str, Any]],
    outline_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for entry in [*front_matter_entries, *outline_entries]:
        if not isinstance(entry, dict):
            continue
        text = _normalize_text(entry.get("text"))
        page_index = _int_or_default(entry.get("page_index"), -1)
        if not text or page_index < 0:
            continue
        key = (_normalize_title_like_text(text), page_index)
        if key in seen:
            continue
        merged.append({**entry, "text": text, "page_index": page_index})
        seen.add(key)
    return merged


def _neighbor_body_text(
    elements: list[dict[str, Any]],
    *,
    start_index: int,
    direction: int,
) -> str:
    cursor = start_index + direction
    while 0 <= cursor < len(elements):
        element = elements[cursor]
        if not isinstance(element, dict):
            cursor += direction
            continue
        element_type = str(element.get("type") or "").strip()
        if element_type in BOOKMARK_HEADING_TYPES:
            return ""
        text = _clean_bookmark_label(element.get("text"))
        if element_type in BOOKMARK_LANDMARK_TYPES and text:
            return text[:240]
        cursor += direction
    return ""


def _best_confidence_label(labels: list[str]) -> str:
    best = "low"
    best_rank = -1
    for label in labels:
        rank = BOOKMARK_CONFIDENCE_RANK.get(str(label or "").strip().lower(), -1)
        if rank > best_rank:
            best = str(label or "").strip().lower() or "low"
            best_rank = rank
    return best


def _heading_target_entries(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict) or str(element.get("type") or "").strip() != "heading":
            continue
        raw_text = _normalize_text(element.get("text"))
        text = _clean_bookmark_label(element.get("bookmark_text_override") or raw_text)
        if not text:
            continue
        page = element.get("page")
        if not isinstance(page, int) or page < 0:
            continue
        entries.append({
            "index": index,
            "text": text[:240],
            "raw_text": raw_text[:240],
            "page_index": page,
            "level": int(element.get("level", 1) or 1),
            "section_key": _bookmark_section_key(text),
        })
    return entries


def _select_heading_target(
    label: str,
    heading_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    normalized_label = _normalize_title_like_text(label)
    if not normalized_label:
        return None
    for entry in heading_entries:
        if _normalize_title_like_text(entry.get("text")) == normalized_label:
            return entry

    section_key = _bookmark_section_key(label)
    if section_key:
        for entry in heading_entries:
            if str(entry.get("section_key") or "").strip() == section_key:
                return entry
    return None


def _build_outline_candidates(
    *,
    structure_json: dict[str, Any],
    candidate_payload: dict[str, Any],
    selected_heading_indexes: set[int],
    selected_landmark_indexes: set[int],
) -> list[dict[str, Any]]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []

    heading_entries = _heading_target_entries(elements)
    candidates: list[dict[str, Any]] = []
    heading_context_by_index = {
        int(entry["index"]): entry
        for entry in candidate_payload.get("heading_candidates") or []
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }
    landmark_context_by_index = {
        int(entry["index"]): entry
        for entry in candidate_payload.get("landmark_candidates") or []
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }

    def _upsert_candidate(candidate: dict[str, Any]) -> None:
        supported_labels = _dedupe_supported_labels(candidate.get("supported_labels") or [candidate.get("text")])
        candidate["supported_labels"] = supported_labels
        candidate["text"] = _preferred_supported_label(
            supported_labels,
            fallback=str(candidate.get("text") or ""),
        )
        candidate["preferred_label"] = candidate["text"]
        candidates.append(candidate)

    for toc_order, toc_entry in enumerate(candidate_payload.get("toc_entries") or []):
        if not isinstance(toc_entry, dict):
            continue
        entry_index = toc_entry.get("index")
        if not isinstance(entry_index, int):
            continue
        label = _clean_bookmark_label(toc_entry.get("text"))
        if not label:
            continue
        target = _select_heading_target(label, heading_entries)
        target_page_index = target["page_index"] if target is not None else max(
            0,
            int(toc_entry.get("page") or 1) - 1,
        )
        default_level = 1 if str(toc_entry.get("type") or "").strip() == "toc_caption" else (
            int(target["level"]) if target is not None else 2
        )
        _upsert_candidate({
            "candidate_id": f"toc:{toc_order}",
            "source_kind": "toc",
            "source_index": entry_index,
            "text": label[:240],
            "raw_text": _normalize_text(toc_entry.get("text")),
            "target_page_index": target_page_index,
            "source_page": toc_entry.get("page"),
            "default_level": max(1, min(6, int(default_level))),
            "merge_key": _candidate_merge_key(label),
            "section_key": _bookmark_section_key(label),
            "supported_labels": [
                label,
                target.get("text") if target is not None else "",
                target.get("raw_text") if target is not None else "",
            ],
        })
        raw_target_text = _normalize_text(target.get("raw_text")) if target is not None else ""
        if (
            target is not None
            and raw_target_text
            and _normalize_title_like_text(raw_target_text) != _normalize_title_like_text(label)
        ):
            context_entry = heading_context_by_index.get(int(target.get("index", -1))) or {}
            _upsert_candidate({
                "candidate_id": f"heading_variant:{entry_index}",
                "source_kind": "heading_variant",
                "source_index": int(target.get("index", -1)),
                "text": raw_target_text[:240],
                "raw_text": raw_target_text[:240],
                "target_page_index": target_page_index,
                "source_page": (target_page_index + 1),
                "default_level": max(1, min(6, int(default_level))),
                "merge_key": _candidate_merge_key(raw_target_text),
                "section_key": _bookmark_section_key(raw_target_text),
                "supported_labels": [raw_target_text, target.get("text") or "", label],
                "previous_visible_label": context_entry.get("previous_heading_text"),
                "next_visible_label": context_entry.get("next_heading_text"),
                "previous_body_text": context_entry.get("previous_body_text"),
                "following_body_text": context_entry.get("following_body_text"),
            })

    for index in sorted(selected_heading_indexes):
        if not 0 <= index < len(elements):
            continue
        element = elements[index]
        if not isinstance(element, dict):
            continue
        raw_visible_label = _normalize_text(element.get("text"))
        raw_label = _clean_bookmark_label(raw_visible_label)
        label = _clean_bookmark_label(element.get("bookmark_text_override") or raw_label)
        page = element.get("page")
        if not label or not isinstance(page, int) or page < 0:
            continue
        context_entry = heading_context_by_index.get(index) or {}
        _upsert_candidate({
            "candidate_id": f"heading:{index}",
            "source_kind": "heading",
            "source_index": index,
            "text": label[:240],
            "raw_text": raw_visible_label[:240],
            "target_page_index": page,
            "source_page": page + 1,
            "default_level": max(1, min(6, int(element.get("level", 1) or 1))),
            "merge_key": _candidate_merge_key(label),
            "section_key": _bookmark_section_key(label),
            "supported_labels": [label, raw_visible_label, raw_label],
            "previous_visible_label": context_entry.get("previous_heading_text"),
            "next_visible_label": context_entry.get("next_heading_text"),
            "previous_body_text": context_entry.get("previous_body_text"),
            "following_body_text": context_entry.get("following_body_text"),
        })

    for index in sorted(selected_landmark_indexes):
        if not 0 <= index < len(elements):
            continue
        element = elements[index]
        if not isinstance(element, dict):
            continue
        label = _clean_bookmark_label(element.get("text"))
        page = element.get("page")
        if not label or not isinstance(page, int) or page < 0:
            continue
        context_entry = landmark_context_by_index.get(index) or {}
        _upsert_candidate({
            "candidate_id": f"landmark:{index}",
            "source_kind": "landmark",
            "source_index": index,
            "text": label[:240],
            "raw_text": _normalize_text(element.get("text"))[:240],
            "target_page_index": page,
            "source_page": page + 1,
            "default_level": 2,
            "merge_key": _candidate_merge_key(label),
            "section_key": _bookmark_section_key(label),
            "supported_labels": [label],
            "previous_visible_label": context_entry.get("previous_visible_text"),
            "next_visible_label": context_entry.get("next_visible_text"),
            "anchor_heading_text": context_entry.get("anchor_heading_text"),
            "previous_body_text": context_entry.get("previous_body_text"),
            "following_body_text": context_entry.get("following_body_text"),
        })

    return candidates


def _valid_index_set(entries: list[dict[str, Any]]) -> set[int]:
    valid: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if isinstance(index, int):
            valid.add(index)
    return valid


def _front_matter_page_candidates(candidate_payload: dict[str, Any], elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    toc_pages = sorted({
        int(entry.get("page"))
        for entry in candidate_payload.get("toc_entries") or []
        if isinstance(entry, dict) and isinstance(entry.get("page"), int)
    })
    if not toc_pages:
        return []

    first_toc_page = min(toc_pages)
    if first_toc_page <= 1:
        return []

    candidate_pages = list(range(1, first_toc_page))
    page_candidates: list[dict[str, Any]] = []
    for page_number in candidate_pages[:3]:
        page_index = page_number - 1
        snippets: list[dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            if _int_or_default(element.get("page"), -1) != page_index:
                continue
            elem_type = str(element.get("type") or "").strip()
            if elem_type not in {"heading", "paragraph", "note", "list_item"}:
                continue
            text = _clean_bookmark_label(element.get("text"))
            if not text:
                continue
            snippets.append({
                "type": elem_type,
                "level": element.get("level"),
                "text": text[:240],
            })
            if len(snippets) >= 8:
                break
        if snippets:
            page_candidates.append({
                "page_index": page_index,
                "page_number": page_number,
                "snippets": snippets,
            })
    return page_candidates


def collect_bookmark_heading_candidates(structure_json: dict[str, Any]) -> dict[str, Any]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return {
            "toc_entries": [],
            "heading_candidates": [],
            "landmark_candidates": [],
            "pages": [],
        }

    toc_entries: list[dict[str, Any]] = []
    toc_pages: set[int] = set()
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        element_type = str(element.get("type") or "").strip()
        if element_type not in {"toc_caption", "toc_item", "toc_item_table"}:
            continue
        text = _clean_bookmark_label(element.get("text"))
        if not text:
            continue
        page = element.get("page")
        if isinstance(page, int):
            toc_pages.add(page + 1)
        toc_entries.append({
            "index": index,
            "type": element_type,
            "page": (page + 1) if isinstance(page, int) else None,
            "text": text[:240],
        })
        if len(toc_entries) >= MAX_BOOKMARK_TOC_ITEMS:
            break

    if not toc_entries:
        return {
            "toc_entries": [],
            "heading_candidates": [],
            "landmark_candidates": [],
            "pages": [],
        }

    last_toc_page = max(toc_pages) if toc_pages else 0
    all_headings: list[dict[str, Any]] = []
    visible_blocks: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        element_type = str(element.get("type") or "").strip()
        page = element.get("page")
        if not isinstance(page, int):
            continue
        text = _clean_bookmark_label(element.get("text"))
        if not text:
            continue
        page_number = page + 1
        if page_number <= last_toc_page:
            if element_type in BOOKMARK_HEADING_TYPES:
                all_headings.append({
                    "index": index,
                    "page": page_number,
                    "level": element.get("level"),
                    "raw_text": _normalize_text(element.get("text")),
                    "text": text[:240],
                    "section_key": _bookmark_section_key(text),
                    "parent_section_key": _parent_section_key(text),
                })
            continue
        if element_type in BOOKMARK_HEADING_TYPES:
            all_headings.append({
                "index": index,
                "page": page_number,
                "level": element.get("level"),
                "raw_text": _normalize_text(element.get("text")),
                "text": text[:240],
                "section_key": _bookmark_section_key(text),
                "parent_section_key": _parent_section_key(text),
            })
        if element_type in BOOKMARK_HEADING_TYPES or element_type in BOOKMARK_LANDMARK_TYPES:
            visible_blocks.append({
                "index": index,
                "page": page_number,
                "type": element_type,
                "level": element.get("level"),
                "raw_text": _normalize_text(element.get("text")),
                "text": text[:240],
                "section_key": _bookmark_section_key(text),
                "parent_section_key": _parent_section_key(text),
            })

    heading_candidates: list[dict[str, Any]] = []
    for heading_pos, heading in enumerate(all_headings):
        page = heading["page"]
        if page <= last_toc_page:
            continue
        heading_index = int(heading["index"])
        heading_candidates.append({
            "index": heading_index,
            "page": page,
            "level": heading.get("level"),
            "text": heading["text"],
            "raw_text": heading.get("raw_text"),
            "section_key": heading.get("section_key"),
            "parent_section_key": heading.get("parent_section_key"),
            "previous_heading_text": all_headings[heading_pos - 1]["text"] if heading_pos > 0 else "",
            "next_heading_text": (
                all_headings[heading_pos + 1]["text"]
                if heading_pos + 1 < len(all_headings)
                else ""
            ),
            "previous_body_text": _neighbor_body_text(elements, start_index=heading_index, direction=-1),
            "following_body_text": _neighbor_body_text(elements, start_index=heading_index, direction=1),
        })
    landmark_candidates: list[dict[str, Any]] = []
    for block_pos, block in enumerate(visible_blocks):
        if block["type"] not in BOOKMARK_LANDMARK_TYPES:
            continue
        landmark_candidates.append({
            "index": block["index"],
            "page": block["page"],
            "type": block["type"],
            "text": block["text"],
            "raw_text": block.get("raw_text"),
            "section_key": block.get("section_key"),
            "parent_section_key": block.get("parent_section_key"),
            "previous_visible_text": visible_blocks[block_pos - 1]["text"] if block_pos > 0 else "",
            "next_visible_text": (
                visible_blocks[block_pos + 1]["text"]
                if block_pos + 1 < len(visible_blocks)
                else ""
            ),
            "previous_body_text": _neighbor_body_text(elements, start_index=int(block["index"]), direction=-1),
            "following_body_text": _neighbor_body_text(elements, start_index=int(block["index"]), direction=1),
        })
    heading_entries = [
        {
            "index": heading["index"],
            "text": heading["text"],
            "page_index": heading["page"] - 1,
            "level": heading.get("level"),
            "section_key": heading.get("section_key"),
        }
        for heading in all_headings
        if isinstance(heading.get("page"), int) and heading["page"] > 0
    ]
    for toc_entry in toc_entries:
        target = _select_heading_target(str(toc_entry.get("text") or ""), heading_entries)
        toc_entry["target_page"] = (int(target["page_index"]) + 1) if target is not None else None
        toc_entry["target_index"] = int(target["index"]) if target is not None else None

    candidate_pages = sorted({entry["page"] for entry in heading_candidates if isinstance(entry.get("page"), int)})
    candidate_pages.extend(
        entry["page"] for entry in landmark_candidates if isinstance(entry.get("page"), int)
    )
    preview_pages = sorted(toc_pages)[:3]
    for page in _sample_preview_pages(candidate_pages):
        if page not in preview_pages:
            preview_pages.append(page)

    return {
        "toc_entries": toc_entries,
        "heading_candidates": heading_candidates,
        "landmark_candidates": landmark_candidates,
        "pages": preview_pages,
    }


async def enhance_bookmark_structure_with_intelligence(
    *,
    pdf_path,
    structure_json: dict[str, Any],
    original_filename: str,
    llm_client: LlmClient,
    prefetched_front_matter_entries: list[dict[str, Any]] | None = None,
    prefetched_front_matter_audit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_payload = collect_bookmark_heading_candidates(structure_json)
    if not candidate_payload["toc_entries"]:
        return structure_json, {
            "attempted": False,
            "applied": False,
            "reason": "no_toc_entries",
            "selected_heading_count": 0,
        }
    all_heading_indexes = _valid_index_set(candidate_payload["heading_candidates"])
    document_outline_candidates = _build_outline_candidates(
        structure_json=structure_json,
        candidate_payload=candidate_payload,
        selected_heading_indexes=all_heading_indexes,
        selected_landmark_indexes=set(),
    )
    front_matter_page_candidates = _front_matter_page_candidates(
        candidate_payload,
        structure_json.get("elements") or [],
    )
    use_local_bookmark_backend = local_semantic_enabled()
    cache_handle = None
    if not use_local_bookmark_backend:
        cache_handle = await create_direct_gemini_pdf_cache(
            pdf_path=pdf_path,
            system_instruction=BOOKMARK_DIRECT_GEMINI_SYSTEM_INSTRUCTION,
            ttl="1800s",
        )
    try:
        candidate_context = {
            "job_filename": original_filename,
            "outline_candidates": _serialize_outline_candidates_for_direct_llm(document_outline_candidates),
        }
        if prefetched_front_matter_entries is None and prefetched_front_matter_audit is None:
            candidate_context["front_matter_page_candidates"] = front_matter_page_candidates

        parsed = await _request_bookmark_json(
            pdf_path=pdf_path,
            original_filename=original_filename,
            llm_client=llm_client,
            cache_handle=cache_handle,
            prompt=BOOKMARK_DOCUMENT_CANDIDATE_PLAN_PROMPT,
            context_payload=candidate_context,
            response_schema=BOOKMARK_DOCUMENT_CANDIDATE_PLAN_SCHEMA,
            preview_pages=_local_bookmark_preview_pages(
                base_pages=list(candidate_payload.get("pages") or []),
            ),
        )
        confidence = str(parsed.get("confidence") or "").strip().lower()
        candidate_plan_reason = str(parsed.get("reason") or "").strip()
        outline_entries, _ = _materialize_outline_entries_from_plan(
            parsed.get("outline_entries"),
            outline_candidates=document_outline_candidates,
        )

        if prefetched_front_matter_entries is not None or prefetched_front_matter_audit is not None:
            front_matter_entries = list(prefetched_front_matter_entries or [])
            front_matter_audit = dict(
                prefetched_front_matter_audit
                or {
                    "attempted": False,
                    "applied": False,
                    "reason": "no_prefetched_front_matter",
                    "entry_count": 0,
                }
            )
        else:
            front_matter_entries = _materialize_front_matter_entries(
                parsed.get("front_matter_entries"),
                front_matter_page_candidates=front_matter_page_candidates,
            )
            front_matter_audit = {
                "attempted": bool(front_matter_page_candidates),
                "applied": bool(front_matter_entries),
                "reason": candidate_plan_reason if front_matter_entries else "",
                "confidence": confidence or "low",
                "entry_count": len(front_matter_entries),
            }

        supplement_confidence = "low"
        supplement_reason = ""
        if outline_entries:
            selected_outline_ids = {
                str(entry.get("candidate_id") or "").strip()
                for entry in outline_entries
                if isinstance(entry, dict)
            }
            remaining_heading_candidates = [
                candidate
                for candidate in document_outline_candidates
                if candidate.get("source_kind") in {"heading", "heading_variant"}
                and str(candidate.get("candidate_id") or "").strip() not in selected_outline_ids
            ]
            if remaining_heading_candidates:
                heading_supplement_parsed = await _request_bookmark_json(
                    pdf_path=pdf_path,
                    original_filename=original_filename,
                    llm_client=llm_client,
                    cache_handle=cache_handle,
                    prompt=BOOKMARK_DOCUMENT_HEADING_SUPPLEMENT_PROMPT,
                    context_payload={
                        "job_filename": original_filename,
                        "selected_outline_entries": _serialize_selected_outline_for_landmark_llm(outline_entries),
                        "remaining_heading_candidates": _serialize_heading_supplement_candidates_for_direct_llm(
                            remaining_heading_candidates,
                            selected_outline_entries=outline_entries,
                        ),
                    },
                    response_schema=BOOKMARK_DOCUMENT_HEADING_SUPPLEMENT_SCHEMA,
                    preview_pages=_local_bookmark_preview_pages(
                        base_pages=_pages_from_outline_entries(outline_entries),
                        extra_pages=_pages_from_outline_candidates(remaining_heading_candidates),
                    ),
                )
                supplement_confidence = (
                    str(heading_supplement_parsed.get("confidence") or "").strip().lower() or "low"
                )
                supplement_reason = str(heading_supplement_parsed.get("reason") or "").strip()
                if supplement_confidence in {"high", "medium"}:
                    supplement_entries, _ = _materialize_outline_entries_from_plan(
                        heading_supplement_parsed.get("outline_entries"),
                        outline_candidates=remaining_heading_candidates,
                    )
                    outline_entries = _merge_outline_entries(outline_entries, supplement_entries)

        selected_heading_indexes = {
            _int_or_default(entry.get("source_index"), -1)
            for entry in outline_entries
            if entry.get("source_kind") in {"heading", "heading_variant"}
            and _int_or_default(entry.get("source_index"), -1) >= 0
        }
        landmark_entries: list[dict[str, Any]] = []
        selected_landmark_indexes: set[int] = set()
        landmark_confidence = "low"
        landmark_reason = ""
        if outline_entries and candidate_payload.get("landmark_candidates"):
            landmark_parsed = await _request_bookmark_json(
                pdf_path=pdf_path,
                original_filename=original_filename,
                llm_client=llm_client,
                cache_handle=cache_handle,
                prompt=BOOKMARK_DOCUMENT_LANDMARK_PLAN_PROMPT,
                context_payload={
                    "job_filename": original_filename,
                    "selected_outline_entries": _serialize_selected_outline_for_landmark_llm(outline_entries),
                },
                response_schema=BOOKMARK_DOCUMENT_LANDMARK_PLAN_SCHEMA,
                preview_pages=_local_bookmark_preview_pages(
                    base_pages=_pages_from_outline_entries(outline_entries),
                    extra_pages=[
                        _int_or_default(entry.get("page"), -1)
                        for entry in (candidate_payload.get("landmark_candidates") or [])
                        if isinstance(entry, dict)
                    ],
                ),
            )
            landmark_confidence = str(landmark_parsed.get("confidence") or "").strip().lower() or "low"
            landmark_reason = str(landmark_parsed.get("reason") or "").strip()
            if landmark_confidence in {"high", "medium"}:
                landmark_entries, selected_landmark_indexes = _materialize_landmark_entries_from_plan(
                    landmark_parsed.get("selected_landmarks"),
                    landmark_candidates=candidate_payload.get("landmark_candidates") or [],
                    anchor_level_by_id={
                        str(entry.get("candidate_id") or ""): _int_or_default(entry.get("level"), 1)
                        for entry in outline_entries
                        if isinstance(entry, dict) and str(entry.get("candidate_id") or "").strip()
                    },
                )
    finally:
        if cache_handle is not None:
            await delete_direct_gemini_pdf_cache(cache_handle)

    overall_confidence = _best_confidence_label([confidence, supplement_confidence, landmark_confidence])
    merged_outline_entries = _merge_outline_with_landmarks(outline_entries, landmark_entries)
    plan_entries = _merge_bookmark_plan_entries(front_matter_entries, merged_outline_entries)
    if plan_entries:
        structure_json["bookmark_plan"] = plan_entries
    else:
        structure_json.pop("bookmark_plan", None)
    return structure_json, {
        "attempted": True,
        "applied": bool(plan_entries),
        "reason": " | ".join(
            part
            for part in [
                front_matter_audit.get("reason") if isinstance(front_matter_audit, dict) else "",
                candidate_plan_reason,
                supplement_reason,
                landmark_reason,
            ]
            if part
        ),
        "confidence": overall_confidence or "low",
        "selected_heading_count": len(selected_heading_indexes),
        "selected_landmark_count": len(selected_landmark_indexes),
        "chunk_count": 0,
        "heading_chunks_reviewed": 0,
        "heading_chunk_shortlist_applied": False,
        "heading_chunk_failures": 0,
        "landmark_chunk_count": 0,
        "landmark_chunks_reviewed": 0,
        "landmark_chunk_shortlist_applied": False,
        "landmark_chunk_failures": 0,
        "outline_plan_applied": bool(plan_entries),
        "outline_entry_count": len(plan_entries),
        "front_matter_applied": bool(front_matter_audit.get("applied", False)),
        "front_matter_entry_count": int(front_matter_audit.get("entry_count", 0) or 0),
    }
