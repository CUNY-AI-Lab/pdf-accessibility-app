from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

from app.services.intelligence_llm_utils import (
    context_json_part,
    page_preview_parts,
    preferred_cache_breakpoint_index,
    request_llm_json,
)
from app.services.llm_client import LlmClient

BOOKMARK_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_heading_selection"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "selected_heading_indexes": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
        },
        "label_overrides": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "reason",
        "selected_heading_indexes",
        "label_overrides",
    ],
}

BOOKMARK_LANDMARK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_landmark_selection"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "selected_candidate_indexes": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
        },
        "label_overrides": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "reason",
        "selected_candidate_indexes",
        "label_overrides",
    ],
}

BOOKMARK_CHUNK_SHORTLIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_chunk_shortlist"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "selected_chunk_indexes": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "reason",
        "selected_chunk_indexes",
    ],
}

BOOKMARK_OUTLINE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_outline_plan"]},
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
                    "label_override": {"type": "string"},
                },
                "required": ["candidate_id", "level", "label_override"],
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

BOOKMARK_FRONT_MATTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["bookmark_front_matter"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page_index": {"type": "integer", "minimum": 0},
                    "label": {
                        "type": "string",
                        "enum": ["Cover", "Inside-Cover page", "Series Information"],
                    },
                },
                "required": ["page_index", "label"],
            },
        },
    },
    "required": ["task_type", "summary", "confidence", "reason", "entries"],
}

BOOKMARK_INTELLIGENCE_PROMPT = """You are a PDF accessibility bookmark planning assistant.

The document already has explicit TOC-derived bookmark entries. Your task is to decide which additional visible headings
after the TOC should also become bookmarks to preserve useful navigation.

Rules:
- Use the provided TOC-derived labels as the primary navigation plan.
- Treat the visible heading evidence as authoritative.
- Select extra heading indexes when they would be useful landmarks for assistive navigation beyond the TOC-derived entries.
- Do not aggressively compress. If a visible heading looks like a real navigational landmark and is not boilerplate or page furniture, prefer keeping it.
- Use the local TOC slice and neighboring heading context to judge whether the heading already has enough navigation support.
- Use the nearby body-text excerpt when available. A concise heading can still be a strong bookmark when the following body text shows that it introduces a substantive subsection, requirement, or workflow step.
- Preserve consistent visible section patterns. When nearby sections use numbered reviewer-comments headings, keep the matching numbered reviewer-comments heading for the current section as well.
- Generic headings like Special Comments or References should not replace a visible numbered section heading that serves as the primary navigation landmark for that section. If both are useful, keep both.
- Use label_overrides only for light cleanup grounded in the visible heading text itself.
- Do not add parenthetical context, parent-section names, or other words that are not visibly present in the heading text.
- Do not append inferred parent context to generic headings like References or Special Comments when that context is not visibly present in the heading.
- Preserve visible meaning and numbering. Do not invent headings that are not supported by the provided evidence.
- If no extra headings should become bookmarks, return an empty selected_heading_indexes list.
"""

BOOKMARK_LANDMARK_PROMPT = """You are a PDF accessibility bookmark landmark planning assistant.

The document already has explicit TOC-derived bookmark entries and model-selected headings.
Your task is to decide which additional visible non-heading blocks after the TOC should also become bookmarks.

Rules:
- Use the TOC-derived entries as the primary navigation skeleton.
- Each landmark candidate appears inside the span after one of the listed heading candidates and before the next anchor heading.
- Use that anchor heading span as the primary local context for deciding whether a non-heading block is a useful bookmark.
- Treat the visible non-heading block evidence as authoritative.
- Use the nearby body-text excerpts when available. A short paragraph or list item can still be a strong bookmark when it behaves like a visible subsection title and is followed by substantive explanatory text or command steps.
- Select a block only when it clearly functions as a standalone navigational landmark in the visible document.
- Do not aggressively compress, but do reject ordinary running text, captions, or page furniture that would not help navigation.
- Preserve visible meaning and numbering.
- Use label_overrides only for light cleanup grounded in the visible candidate text or nearby document evidence.
- Do not invent landmarks that are not supported by the provided evidence.
- If no extra non-heading landmarks should become bookmarks, return an empty selected_candidate_indexes list.
"""

BOOKMARK_CHUNK_SHORTLIST_PROMPT = """You are a PDF accessibility bookmark chunk triage assistant.

Your task is to decide which chunk summaries need detailed bookmark review by a later model pass.

Rules:
- Use the TOC slice as the primary navigation baseline.
- Select chunk indexes only when the chunk summary suggests there may be useful additional bookmark detail beyond what the TOC already provides.
- It is acceptable to select many chunks when the document appears navigation-dense.
- It is acceptable to select no chunks when the TOC already appears to cover the chunk sufficiently and the summaries do not suggest extra navigation value.
- Prefer selecting a chunk when unsure rather than risking a navigation miss.
- Use only the provided chunk indexes.
- Do not decide the final bookmarks here. This is only a shortlist for more detailed review.
"""

BOOKMARK_OUTLINE_PROMPT = """You are a PDF accessibility bookmark outline planning assistant.

You will receive bookmark candidates derived from visible TOC entries and model-selected heading or landmark entries.
Return the final bookmark outline to write into the PDF.

Rules:
- Use only the provided candidate_id values. Do not invent entries.
- All candidate_id values listed as required must appear exactly once in outline_entries.
- Required candidates include TOC/front-matter entries and heading candidates already selected by an earlier model pass. Preserve them unless the input itself is invalid.
- Candidate_id values listed as optional are suggestions and may be omitted when they do not improve navigation.
- Preserve visible meaning and numbering.
- Use TOC-derived entries as the primary navigation skeleton when they are present.
- Treat the optional heading and landmark candidates as evidence-backed suggestions from earlier model passes.
- Use each candidate's local evidence fields, especially anchor headings and nearby body-text excerpts, when deciding whether a selected landmark adds real navigational value beyond the TOC.
- Preserve useful navigation rather than compressing aggressively. Omit optional candidates only when they are clearly redundant, noisy, or not useful to navigation.
- Do not omit a selected landmark merely because it is not tagged as a heading. Keep it when the local evidence shows it functions like a visible subsection title, numbered subsection, or procedural waypoint.
- Preserve model-selected numbered subsection landmarks when they add clear granularity beneath a broader TOC section.
- Repeated visible labels like References can still be useful bookmarks when they occur under different sections or on different pages. Do not omit a candidate solely because another candidate uses the same label.
- Multiple candidates may refer to the same visible section from different evidence sources. When that happens, keep the strongest candidate or the smallest useful set rather than mechanically keeping duplicates.
- Prefer a clean, useful hierarchy over mechanically copying every visible line at the same level.
- Keep top-level outline depth stable and understandable for assistive technology.
- Each candidate includes a preferred_label and supported_labels.
- Use label_override only when choosing one of that candidate's supported_labels.
- Prefer the visible label that best matches the document evidence. When supported_labels include a plain visible heading and a more elaborated paraphrase, prefer the plain visible heading unless the elaboration is also visibly present.
- When supported_labels include both a cleaned TOC-style label and a raw visible heading label, prefer the raw visible heading label if the visible heading evidence clearly supports it.
- When a candidate is not worth keeping in the final outline, omit it.
- If no outline should be written, return an empty outline_entries list.
"""

BOOKMARK_FRONT_MATTER_PROMPT = """You are a PDF accessibility bookmark front-matter planning assistant.

You will receive only the pages that appear before the visible table of contents.
Your task is to infer whether any of these pages should become higher-order bookmark roles.

Canonical role labels:
- Cover
- Inside-Cover page
- Series Information

Rules:
- Use only the provided canonical role labels.
- Add an entry only when the page's visible content clearly supports that role.
- Prefer Cover for the primary title/cover page.
- Prefer Inside-Cover page for the publication-details/title-verso page that immediately follows a cover.
- Prefer Series Information for an editorial-notes, publication-series, or document-information page that appears before the TOC.
- Return entries in page order.
- Do not invent page roles that are not supported by the visible evidence.
- If none of the pages clearly support these roles, return an empty entries list.
"""

MAX_BOOKMARK_TOC_ITEMS = 80
BOOKMARK_HEADING_TYPES = {"heading"}
BOOKMARK_LANDMARK_TYPES = {"paragraph", "list_item", "note"}
BOOKMARK_SELECTION_CHUNK_SIZE = 8
BOOKMARK_LANDMARK_CHUNK_SIZE = 16
BOOKMARK_LANDMARK_HEADING_GROUP_SIZE = 6
BOOKMARK_SELECTION_TOC_CONTEXT_ITEMS = 12
BOOKMARK_SELECTED_HEADING_CONTEXT_ITEMS = 8
BOOKMARK_SHORTLIST_MIN_CHUNKS = 3
BOOKMARK_SHORTLIST_SAMPLE_ITEMS = 6
BOOKMARK_HEADING_CHUNK_TIMEOUT_SECONDS = 120
BOOKMARK_LANDMARK_CHUNK_TIMEOUT_SECONDS = 120
BOOKMARK_OUTLINE_TIMEOUT_SECONDS = 120
BOOKMARK_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
FRONT_MATTER_AUTO_CONFIDENCE = {"high", "medium"}
TOC_TRAILING_PAGE_RE = re.compile(
    r"(?:\.{2,}\s*|(?:\.\s*){2,}|\s{2,}|\t+)(?:\d+|[ivxlcdm]+)\s*$|(?:\s+)(?:\d+)\s*$",
    re.IGNORECASE,
)
APPENDIX_BOOKMARK_KEY_RE = re.compile(r"^(appendix\s+[a-z0-9]+(?:\.\d+)*)\b", re.IGNORECASE)
NUMERIC_BOOKMARK_KEY_RE = re.compile(r"^(\d+(?:\.\d+)*)\b")


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
        normalized_label = _normalize_text(label)
        normalized = _normalize_title_like_text(normalized_label)
        if not normalized_label or not normalized or normalized in seen:
            continue
        deduped.append(normalized_label[:240])
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


def _page_bounds(pages: list[int]) -> tuple[int, int] | None:
    valid = sorted(page for page in pages if isinstance(page, int) and page > 0)
    if not valid:
        return None
    return valid[0], valid[-1]


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


def _serialize_toc_entries_for_llm(toc_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for entry in toc_entries:
        if not isinstance(entry, dict):
            continue
        serialized.append(
            _compact_llm_mapping({
                "index": entry.get("index"),
                "type": entry.get("type"),
                "toc_page": entry.get("page"),
                "target_page": entry.get("target_page"),
                "text": entry.get("text"),
            })
        )
    return serialized


def _serialize_heading_candidates_for_llm(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        serialized.append(
            _compact_llm_mapping({
                "index": entry.get("index"),
                "page": entry.get("page"),
                "level": entry.get("level"),
                "text": entry.get("text"),
                "previous_heading_text": entry.get("previous_heading_text"),
                "next_heading_text": entry.get("next_heading_text"),
                "previous_body_text": entry.get("previous_body_text"),
                "following_body_text": entry.get("following_body_text"),
            })
        )
    return serialized


def _serialize_landmark_candidates_for_llm(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        serialized.append(
            _compact_llm_mapping({
                "index": entry.get("index"),
                "page": entry.get("page"),
                "type": entry.get("type"),
                "text": entry.get("text"),
                "anchor_heading_text": entry.get("anchor_heading_text"),
                "anchor_heading_page": entry.get("anchor_heading_page"),
                "previous_visible_text": entry.get("previous_visible_text"),
                "next_visible_text": entry.get("next_visible_text"),
                "previous_body_text": entry.get("previous_body_text"),
                "following_body_text": entry.get("following_body_text"),
            })
        )
    return serialized


def _serialize_outline_candidates_for_llm(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    labels = [
        _normalize_text(entry.get("preferred_label") or entry.get("text"))
        for entry in entries
        if isinstance(entry, dict)
    ]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        preferred_label = entry.get("preferred_label")
        supported_labels = [
            str(label).strip()
            for label in (entry.get("supported_labels") or [])
            if str(label).strip()
        ]
        if len(supported_labels) <= 1 and (
            not supported_labels or supported_labels[0] == str(preferred_label or "").strip()
        ):
            supported_labels_payload: list[str] | None = None
        else:
            supported_labels_payload = supported_labels
        serialized.append(
            _compact_llm_mapping({
                "candidate_id": entry.get("candidate_id"),
                "source_kind": entry.get("source_kind"),
                "source_index": entry.get("source_index"),
                "preferred_label": preferred_label,
                "supported_labels": supported_labels_payload,
                "target_page_index": entry.get("target_page_index"),
                "source_page": entry.get("source_page"),
                "default_level": entry.get("default_level"),
                "previous_candidate_label": labels[index - 1] if index > 0 else "",
                "next_candidate_label": labels[index + 1] if index + 1 < len(labels) else "",
                "previous_visible_label": entry.get("previous_visible_label"),
                "next_visible_label": entry.get("next_visible_label"),
                "anchor_heading_text": entry.get("anchor_heading_text"),
                "previous_body_text": entry.get("previous_body_text"),
                "following_body_text": entry.get("following_body_text"),
            })
        )
    return serialized


def _summarize_heading_chunk_for_shortlist(
    heading_chunk: list[dict[str, Any]],
) -> dict[str, Any]:
    pages = [entry.get("page") for entry in heading_chunk if isinstance(entry.get("page"), int)]
    page_range = _page_bounds([int(page) for page in pages if isinstance(page, int)])
    return _compact_llm_mapping({
        "chunk_size": len(heading_chunk),
        "chunk_page_range": list(page_range) if page_range else None,
        "headings": [
            _compact_llm_mapping({
                "index": entry.get("index"),
                "page": entry.get("page"),
                "level": entry.get("level"),
                "text": entry.get("text"),
            })
            for entry in heading_chunk[:BOOKMARK_SHORTLIST_SAMPLE_ITEMS]
            if isinstance(entry, dict)
        ],
    })


def _summarize_landmark_chunk_for_shortlist(
    landmark_chunk: dict[str, Any],
) -> dict[str, Any]:
    heading_candidates = [
        entry
        for entry in (landmark_chunk.get("heading_candidates") or [])
        if isinstance(entry, dict)
    ]
    landmark_candidates = [
        entry
        for entry in (landmark_chunk.get("landmark_candidates") or [])
        if isinstance(entry, dict)
    ]
    pages = [
        int(page)
        for page in (landmark_chunk.get("pages") or [])
        if isinstance(page, int)
    ]
    page_range = _page_bounds(pages)
    return _compact_llm_mapping({
        "chunk_size": len(landmark_candidates),
        "chunk_page_range": list(page_range) if page_range else None,
        "anchor_headings": [
            _compact_llm_mapping({
                "index": entry.get("index"),
                "page": entry.get("page"),
                "level": entry.get("level"),
                "text": entry.get("text"),
            })
            for entry in heading_candidates[:BOOKMARK_SHORTLIST_SAMPLE_ITEMS]
        ],
        "landmarks": [
            _compact_llm_mapping({
                "index": entry.get("index"),
                "page": entry.get("page"),
                "type": entry.get("type"),
                "text": entry.get("text"),
            })
            for entry in landmark_candidates[:BOOKMARK_SHORTLIST_SAMPLE_ITEMS]
        ],
    })


def _nearest_entries_by_page(
    entries: list[dict[str, Any]],
    *,
    chunk_pages: list[int],
    page_key: str,
    max_items: int,
) -> list[dict[str, Any]]:
    if max_items <= 0 or not entries:
        return []
    bounds = _page_bounds(chunk_pages)
    if bounds is None:
        return entries[:max_items]
    start_page, end_page = bounds
    center = (start_page + end_page) / 2
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for order, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        page_value = _int_or_default(entry.get(page_key), -1)
        if page_value > 0:
            distance = abs(page_value - center)
        else:
            distance = float("inf")
        scored.append((distance, order, entry))
    if not scored:
        return entries[:max_items]
    scored.sort(key=lambda item: (item[0], item[1]))
    selected_orders = sorted(order for _distance, order, _entry in scored[:max_items])
    return [entries[order] for order in selected_orders]


def _local_toc_entries_for_chunk(
    toc_entries: list[dict[str, Any]],
    *,
    chunk_pages: list[int],
    max_items: int,
) -> list[dict[str, Any]]:
    if max_items <= 0 or not toc_entries:
        return []
    bounds = _page_bounds(chunk_pages)
    if bounds is None:
        return toc_entries[:max_items]
    start_page, end_page = bounds
    matched_indexes = [
        idx
        for idx, entry in enumerate(toc_entries)
        if start_page - 4 <= _int_or_default(entry.get("target_page"), -1) <= end_page + 4
    ]
    if matched_indexes:
        first = max(0, matched_indexes[0] - 1)
        last = min(len(toc_entries), matched_indexes[-1] + 2)
        window = toc_entries[first:last]
        if len(window) <= max_items:
            return window
        return _nearest_entries_by_page(window, chunk_pages=chunk_pages, page_key="target_page", max_items=max_items)
    return _nearest_entries_by_page(
        toc_entries,
        chunk_pages=chunk_pages,
        page_key="target_page",
        max_items=max_items,
    )


def _local_selected_headings_for_chunk(
    heading_candidates: list[dict[str, Any]],
    *,
    selected_indexes: set[int],
    chunk_pages: list[int],
    max_items: int,
) -> list[dict[str, Any]]:
    selected = [
        entry
        for entry in heading_candidates
        if isinstance(entry, dict) and entry.get("index") in selected_indexes
    ]
    return _nearest_entries_by_page(
        selected,
        chunk_pages=chunk_pages,
        page_key="page",
        max_items=max_items,
    )


async def _shortlist_bookmark_chunks(
    *,
    llm_client: LlmClient,
    original_filename: str,
    stage_label: str,
    toc_entries: list[dict[str, Any]],
    chunk_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(chunk_payloads) < BOOKMARK_SHORTLIST_MIN_CHUNKS:
        return {
            "attempted": False,
            "applied": False,
            "reason": "too_few_chunks",
            "confidence": "low",
            "selected_chunk_indexes": list(range(len(chunk_payloads))),
        }

    content = [
        {"type": "text", "text": BOOKMARK_CHUNK_SHORTLIST_PROMPT},
        context_json_part(
            {
                "job_filename": original_filename,
                "stage": stage_label,
                "toc_entries": _serialize_toc_entries_for_llm(toc_entries),
                "chunk_summaries": chunk_payloads,
            },
            prefix="Bookmark chunk shortlist context:\n",
        ),
    ]
    parsed = await request_llm_json(
        llm_client=llm_client,
        content=content,
        schema_name="bookmark_chunk_shortlist",
        response_schema=BOOKMARK_CHUNK_SHORTLIST_SCHEMA,
        cache_breakpoint_index=preferred_cache_breakpoint_index(content),
    )
    confidence = str(parsed.get("confidence") or "").strip().lower()
    valid_indexes = {int(item.get("chunk_index")) for item in chunk_payloads if isinstance(item.get("chunk_index"), int)}
    selected = sorted(
        {
            int(index)
            for index in (parsed.get("selected_chunk_indexes") or [])
            if isinstance(index, int) and index in valid_indexes
        }
    )
    applied = confidence in {"high", "medium"} and bool(selected) and len(selected) < len(chunk_payloads)
    return {
        "attempted": True,
        "applied": applied,
        "reason": str(parsed.get("reason") or "").strip(),
        "confidence": confidence or "low",
        "selected_chunk_indexes": selected if applied else list(range(len(chunk_payloads))),
    }


def _landmark_anchor_entries(
    candidate_payload: dict[str, Any],
    *,
    selected_indexes: set[int],
) -> list[dict[str, Any]]:
    heading_by_index = {
        int(entry["index"]): entry
        for entry in candidate_payload.get("heading_candidates") or []
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }
    anchor_indexes = set(selected_indexes)
    for toc_entry in candidate_payload.get("toc_entries") or []:
        if not isinstance(toc_entry, dict):
            continue
        target_index = toc_entry.get("target_index")
        if isinstance(target_index, int) and target_index in heading_by_index:
            anchor_indexes.add(target_index)
    return [heading_by_index[index] for index in sorted(anchor_indexes) if index in heading_by_index]


def _build_landmark_candidate_chunks(
    candidate_payload: dict[str, Any],
    *,
    selected_indexes: set[int],
    group_size: int = BOOKMARK_LANDMARK_HEADING_GROUP_SIZE,
) -> list[dict[str, Any]]:
    landmark_candidates = [
        entry
        for entry in candidate_payload.get("landmark_candidates") or []
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    ]
    if not landmark_candidates:
        return []

    anchor_entries = _landmark_anchor_entries(candidate_payload, selected_indexes=selected_indexes)
    if not anchor_entries:
        fallback_chunks = _chunk_heading_candidates(
            landmark_candidates,
            chunk_size=BOOKMARK_LANDMARK_CHUNK_SIZE,
        )
        return [
            {
                "heading_candidates": [],
                "landmark_candidates": list(chunk),
                "pages": sorted({
                    int(entry["page"])
                    for entry in chunk
                    if isinstance(entry.get("page"), int)
                }),
            }
            for chunk in fallback_chunks
            if chunk
        ]

    sorted_landmarks = sorted(landmark_candidates, key=lambda entry: int(entry["index"]))
    windows: list[dict[str, Any]] = []
    landmark_pos = 0
    for anchor_pos, anchor_entry in enumerate(anchor_entries):
        anchor_index = int(anchor_entry["index"])
        next_anchor_index = (
            int(anchor_entries[anchor_pos + 1]["index"])
            if anchor_pos + 1 < len(anchor_entries)
            else None
        )
        while landmark_pos < len(sorted_landmarks) and int(sorted_landmarks[landmark_pos]["index"]) <= anchor_index:
            landmark_pos += 1
        probe = landmark_pos
        window_landmarks: list[dict[str, Any]] = []
        while probe < len(sorted_landmarks):
            landmark_entry = sorted_landmarks[probe]
            landmark_index = int(landmark_entry["index"])
            if next_anchor_index is not None and landmark_index >= next_anchor_index:
                break
            window_landmarks.append({
                **landmark_entry,
                "anchor_heading_text": anchor_entry.get("text"),
                "anchor_heading_page": anchor_entry.get("page"),
            })
            probe += 1
        landmark_pos = probe
        if not window_landmarks:
            continue
        windows.append({
            "heading_candidates": [anchor_entry],
            "landmark_candidates": window_landmarks,
        })

    if not windows:
        return []

    effective_group_size = max(1, int(group_size or 1))
    grouped_chunks: list[dict[str, Any]] = []
    for start in range(0, len(windows), effective_group_size):
        window_chunk = windows[start : start + effective_group_size]
        heading_candidates: list[dict[str, Any]] = []
        seen_heading_indexes: set[int] = set()
        chunk_landmarks: list[dict[str, Any]] = []
        pages: set[int] = set()
        for window in window_chunk:
            for heading_entry in window["heading_candidates"]:
                heading_index = int(heading_entry["index"])
                if heading_index not in seen_heading_indexes:
                    heading_candidates.append(heading_entry)
                    seen_heading_indexes.add(heading_index)
                if isinstance(heading_entry.get("page"), int):
                    pages.add(int(heading_entry["page"]))
            for landmark_entry in window["landmark_candidates"]:
                chunk_landmarks.append(landmark_entry)
                if isinstance(landmark_entry.get("page"), int):
                    pages.add(int(landmark_entry["page"]))
        if not chunk_landmarks:
            continue
        grouped_chunks.append({
            "heading_candidates": heading_candidates,
            "landmark_candidates": chunk_landmarks,
            "pages": sorted(pages),
        })
    return grouped_chunks


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


def _chunk_heading_candidates(
    heading_candidates: list[dict[str, Any]],
    *,
    chunk_size: int = BOOKMARK_SELECTION_CHUNK_SIZE,
) -> list[list[dict[str, Any]]]:
    if chunk_size <= 0:
        return [heading_candidates]
    return [
        heading_candidates[start : start + chunk_size]
        for start in range(0, len(heading_candidates), chunk_size)
    ]


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


def _filtered_label_overrides(
    overrides: Any,
    *,
    valid_indexes: set[int],
) -> dict[str | int, str]:
    if not isinstance(overrides, dict):
        return {}
    filtered: dict[str | int, str] = {}
    for key, value in overrides.items():
        try:
            normalized_index = int(key)
        except (TypeError, ValueError):
            continue
        if normalized_index not in valid_indexes:
            continue
        label = _normalize_text(value)
        if not label:
            continue
        filtered[str(normalized_index)] = label
        filtered[normalized_index] = label
    return filtered


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


async def _generate_front_matter_entries(
    *,
    pdf_path,
    structure_json: dict[str, Any],
    candidate_payload: dict[str, Any],
    original_filename: str,
    llm_client: LlmClient,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return [], {"attempted": False, "applied": False, "reason": "no_elements", "entry_count": 0}

    page_candidates = _front_matter_page_candidates(candidate_payload, elements)
    if not page_candidates:
        return [], {"attempted": False, "applied": False, "reason": "no_front_matter_pages", "entry_count": 0}

    preview_pages = [page["page_number"] for page in page_candidates]
    job = SimpleNamespace(
        original_filename=original_filename,
        input_path=str(pdf_path),
        output_path=str(pdf_path),
    )
    content = [
        {"type": "text", "text": BOOKMARK_FRONT_MATTER_PROMPT},
        *page_preview_parts(job, preview_pages),
        context_json_part(
            {
                "job_filename": original_filename,
                "front_matter_pages": page_candidates,
            },
            prefix="Front-matter bookmark context:\n",
        ),
    ]
    parsed = await request_llm_json(
        llm_client=llm_client,
        content=content,
        schema_name="bookmark_front_matter",
        response_schema=BOOKMARK_FRONT_MATTER_SCHEMA,
        cache_breakpoint_index=preferred_cache_breakpoint_index(content),
    )
    confidence = str(parsed.get("confidence") or "").strip().lower()
    if confidence not in FRONT_MATTER_AUTO_CONFIDENCE:
        return [], {
            "attempted": True,
            "applied": False,
            "reason": str(parsed.get("reason") or "").strip(),
            "confidence": confidence,
            "entry_count": 0,
        }

    valid_pages = {page["page_index"] for page in page_candidates}
    entries: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    for order_index, entry in enumerate(parsed.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        page_index = _int_or_default(entry.get("page_index"), -1)
        label = str(entry.get("label") or "").strip()
        if page_index not in valid_pages or page_index in seen_pages or not label:
            continue
        entries.append({
            "candidate_id": f"front:{order_index}",
            "source_kind": "front_matter",
            "source_index": page_index,
            "text": label,
            "page_index": page_index,
            "level": 1,
        })
        seen_pages.add(page_index)

    return entries, {
        "attempted": True,
        "applied": bool(entries),
        "reason": str(parsed.get("reason") or "").strip(),
        "confidence": confidence,
        "entry_count": len(entries),
    }


async def _generate_bookmark_outline_plan(
    *,
    pdf_path,
    original_filename: str,
    outline_candidates: list[dict[str, Any]],
    preview_pages: list[int],
    llm_client: LlmClient,
) -> dict[str, Any]:
    if not outline_candidates:
        return {
            "attempted": False,
            "applied": False,
            "reason": "no_outline_candidates",
            "outline_entry_count": 0,
        }
    required_candidate_ids = [
        str(candidate["candidate_id"])
        for candidate in outline_candidates
        if (
            isinstance(candidate, dict)
            and str(candidate.get("candidate_id") or "").strip()
            and str(candidate.get("source_kind") or "").strip() in {"toc", "front_matter", "heading"}
        )
    ]
    optional_candidate_ids = [
        str(candidate["candidate_id"])
        for candidate in outline_candidates
        if (
            isinstance(candidate, dict)
            and str(candidate.get("candidate_id") or "").strip()
            and str(candidate.get("source_kind") or "").strip() not in {"toc", "front_matter"}
        )
    ]
    content = [
        {
            "type": "text",
            "text": BOOKMARK_OUTLINE_PROMPT,
        },
        context_json_part(
            {
                "job_filename": original_filename,
                "required_candidate_ids": required_candidate_ids,
                "optional_candidate_ids": optional_candidate_ids,
                "outline_candidates": _serialize_outline_candidates_for_llm(outline_candidates),
            },
            prefix="Bookmark outline context:\n",
        ),
    ]
    candidates_by_id = {
        str(candidate["candidate_id"]): candidate
        for candidate in outline_candidates
        if isinstance(candidate, dict) and str(candidate.get("candidate_id") or "").strip()
    }

    def _materialize_outline(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
        outline_entries_raw = parsed.get("outline_entries")
        if not isinstance(outline_entries_raw, list):
            outline_entries_raw = []

        outline_entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
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
            label = _resolve_supported_label(
                list(candidate.get("supported_labels") or []),
                item.get("label_override"),
                fallback=str(candidate.get("preferred_label") or candidate["text"]),
            )
            if not label:
                continue
            outline_entries.append({
                "candidate_id": candidate_id,
                "source_kind": candidate.get("source_kind"),
                "source_index": candidate.get("source_index"),
                "text": label,
                "page_index": int(candidate["target_page_index"]),
                "level": max(1, min(6, level)),
            })
            seen_ids.add(candidate_id)
        return outline_entries, seen_ids

    repair_note = None
    for attempt in range(2):
        if attempt == 0:
            request_content = content
        else:
            request_content = [
                content[0],
                {
                    "type": "text",
                    "text": (
                        "Your previous outline omitted required candidate_ids. "
                        "Return a corrected outline that includes every required candidate_id exactly once.\n\n"
                        f"Required candidate_ids: {required_candidate_ids}\n"
                        f"Previous issue: {repair_note}"
                    ),
                },
                *content[1:],
            ]
        try:
            parsed = await asyncio.wait_for(
                request_llm_json(
                    llm_client=llm_client,
                    content=request_content,
                    schema_name="bookmark_outline_plan",
                    response_schema=BOOKMARK_OUTLINE_PLAN_SCHEMA,
                    cache_breakpoint_index=1,
                ),
                timeout=BOOKMARK_OUTLINE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return {
                "attempted": True,
                "applied": False,
                "reason": "outline_plan_timeout",
                "confidence": "low",
                "outline_entry_count": 0,
            }
        confidence = str(parsed.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium"}:
            return {
                "attempted": True,
                "applied": False,
                "reason": str(parsed.get("reason") or "").strip(),
                "confidence": confidence,
                "outline_entry_count": 0,
            }
        outline_entries, seen_ids = _materialize_outline(parsed)
        missing_required_ids = [
            candidate_id for candidate_id in required_candidate_ids if candidate_id not in seen_ids
        ]
        if not missing_required_ids:
            return {
                "attempted": True,
                "applied": bool(outline_entries),
                "reason": str(parsed.get("reason") or "").strip(),
                "confidence": confidence,
                "outline_entry_count": len(outline_entries),
                "outline_entries": outline_entries,
            }
        repair_note = f"missing required TOC candidate_ids: {missing_required_ids}"

    return {
        "attempted": True,
        "applied": False,
        "reason": repair_note or "missing_required_toc_candidates",
        "confidence": "low",
        "outline_entry_count": 0,
    }


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
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_payload = collect_bookmark_heading_candidates(structure_json)
    if not candidate_payload["toc_entries"]:
        return structure_json, {
            "attempted": False,
            "applied": False,
            "reason": "no_toc_entries",
            "selected_heading_count": 0,
        }
    front_matter_entries, front_matter_audit = await _generate_front_matter_entries(
        pdf_path=pdf_path,
        structure_json=structure_json,
        candidate_payload=candidate_payload,
        original_filename=original_filename,
        llm_client=llm_client,
    )
    if (
        not candidate_payload["heading_candidates"]
        and not candidate_payload.get("landmark_candidates")
        and not front_matter_entries
    ):
        return structure_json, {
            "attempted": False,
            "applied": False,
            "reason": "no_bookmark_candidates",
            "selected_heading_count": 0,
            "selected_landmark_count": 0,
            "front_matter_applied": False,
            "front_matter_entry_count": 0,
        }

    selected_indexes: set[int] = set()
    selected_landmark_indexes: set[int] = set()
    label_overrides: dict[str | int, str] = {}
    landmark_label_overrides: dict[str | int, str] = {}
    chunk_confidences: list[str] = []
    landmark_chunk_confidences: list[str] = []
    heading_chunk_failures = 0
    landmark_chunk_failures = 0
    valid_heading_indexes = _valid_index_set(candidate_payload["heading_candidates"])
    valid_landmark_indexes = _valid_index_set(candidate_payload.get("landmark_candidates") or [])

    heading_chunks = _chunk_heading_candidates(candidate_payload["heading_candidates"])
    heading_chunk_shortlist = {
        "attempted": False,
        "applied": False,
        "reason": "too_few_chunks",
        "confidence": "low",
        "selected_chunk_indexes": list(range(len(heading_chunks))),
    }
    heading_chunk_payloads = [
        {
            "chunk_index": chunk_index,
            **_summarize_heading_chunk_for_shortlist(heading_chunk),
            "toc_entries": _serialize_toc_entries_for_llm(
                _local_toc_entries_for_chunk(
                    candidate_payload["toc_entries"],
                    chunk_pages=[
                        entry["page"]
                        for entry in heading_chunk
                        if isinstance(entry.get("page"), int)
                    ],
                    max_items=BOOKMARK_SELECTION_TOC_CONTEXT_ITEMS,
                )
            ),
        }
        for chunk_index, heading_chunk in enumerate(heading_chunks)
    ]
    heading_chunk_shortlist = await _shortlist_bookmark_chunks(
        llm_client=llm_client,
        original_filename=original_filename,
        stage_label="heading_selection",
        toc_entries=candidate_payload["toc_entries"],
        chunk_payloads=heading_chunk_payloads,
    )
    heading_chunk_indexes_to_review = [
        int(index)
        for index in heading_chunk_shortlist.get("selected_chunk_indexes") or []
        if isinstance(index, int) and 0 <= index < len(heading_chunks)
    ]
    if not heading_chunk_indexes_to_review:
        heading_chunk_indexes_to_review = list(range(len(heading_chunks)))

    async def _request_heading_chunk(
        chunk_index: int,
        heading_chunk: list[dict[str, Any]],
    ) -> dict[str, Any]:
        chunk_pages = [entry["page"] for entry in heading_chunk if isinstance(entry.get("page"), int)]
        chunk_page_range = _page_bounds(chunk_pages)
        local_toc_entries = _local_toc_entries_for_chunk(
            candidate_payload["toc_entries"],
            chunk_pages=chunk_pages,
            max_items=BOOKMARK_SELECTION_TOC_CONTEXT_ITEMS,
        )

        content = [
            {
                "type": "text",
                "text": BOOKMARK_INTELLIGENCE_PROMPT,
            },
            context_json_part(
                {
                    "job_filename": original_filename,
                    "chunk_index": chunk_index,
                    "chunk_count": len(heading_chunks),
                    "chunk_page_range": list(chunk_page_range) if chunk_page_range else None,
                    "toc_entries": _serialize_toc_entries_for_llm(local_toc_entries),
                    "heading_candidates": _serialize_heading_candidates_for_llm(heading_chunk),
                },
                prefix="Bookmark planning context:\n",
            ),
        ]
        return await asyncio.wait_for(
            request_llm_json(
                llm_client=llm_client,
                content=content,
                schema_name="bookmark_heading_selection",
                response_schema=BOOKMARK_DECISION_SCHEMA,
                cache_breakpoint_index=1,
            ),
            timeout=BOOKMARK_HEADING_CHUNK_TIMEOUT_SECONDS,
        )

    heading_results = await asyncio.gather(
        *[
            _request_heading_chunk(chunk_index, heading_chunk)
            for chunk_index, heading_chunk in enumerate(heading_chunks)
            if chunk_index in heading_chunk_indexes_to_review
        ],
        return_exceptions=True,
    )
    for parsed in heading_results:
        if isinstance(parsed, BaseException):
            heading_chunk_failures += 1
            continue
        confidence = str(parsed.get("confidence") or "").strip().lower()
        chunk_confidences.append(confidence)
        if confidence not in {"high", "medium"}:
            continue
        selected_indexes.update(
            int(index)
            for index in parsed.get("selected_heading_indexes") or []
            if isinstance(index, int) and index in valid_heading_indexes
        )
        label_overrides.update(
            _filtered_label_overrides(
                parsed.get("label_overrides"),
                valid_indexes=valid_heading_indexes,
            )
        )

    landmark_chunks = _build_landmark_candidate_chunks(
        candidate_payload,
        selected_indexes=selected_indexes,
    )
    landmark_chunk_shortlist = {
        "attempted": False,
        "applied": False,
        "reason": "too_few_chunks",
        "confidence": "low",
        "selected_chunk_indexes": list(range(len(landmark_chunks))),
    }
    landmark_chunk_payloads = [
        {
            "chunk_index": chunk_index,
            **_summarize_landmark_chunk_for_shortlist(landmark_chunk),
            "toc_entries": _serialize_toc_entries_for_llm(
                _local_toc_entries_for_chunk(
                    candidate_payload["toc_entries"],
                    chunk_pages=list(landmark_chunk.get("pages") or []),
                    max_items=BOOKMARK_SELECTION_TOC_CONTEXT_ITEMS,
                )
            ),
        }
        for chunk_index, landmark_chunk in enumerate(landmark_chunks)
    ]
    landmark_chunk_shortlist = await _shortlist_bookmark_chunks(
        llm_client=llm_client,
        original_filename=original_filename,
        stage_label="landmark_selection",
        toc_entries=candidate_payload["toc_entries"],
        chunk_payloads=landmark_chunk_payloads,
    )
    landmark_chunk_indexes_to_review = [
        int(index)
        for index in landmark_chunk_shortlist.get("selected_chunk_indexes") or []
        if isinstance(index, int) and 0 <= index < len(landmark_chunks)
    ]
    if not landmark_chunk_indexes_to_review:
        landmark_chunk_indexes_to_review = list(range(len(landmark_chunks)))

    async def _request_landmark_chunk(
        chunk_index: int,
        landmark_chunk: dict[str, Any],
    ) -> dict[str, Any]:
        chunk_pages = list(landmark_chunk.get("pages") or [])
        chunk_page_range = _page_bounds(chunk_pages)
        local_toc_entries = _local_toc_entries_for_chunk(
            candidate_payload["toc_entries"],
            chunk_pages=chunk_pages,
            max_items=BOOKMARK_SELECTION_TOC_CONTEXT_ITEMS,
        )

        content = [
            {
                "type": "text",
                "text": BOOKMARK_LANDMARK_PROMPT,
            },
            context_json_part(
                {
                    "job_filename": original_filename,
                    "chunk_index": chunk_index,
                    "chunk_count": len(landmark_chunks),
                    "chunk_page_range": list(chunk_page_range) if chunk_page_range else None,
                    "toc_entries": _serialize_toc_entries_for_llm(local_toc_entries),
                    "heading_candidates": _serialize_heading_candidates_for_llm(
                        landmark_chunk.get("heading_candidates") or []
                    ),
                    "landmark_candidates": _serialize_landmark_candidates_for_llm(
                        landmark_chunk.get("landmark_candidates") or []
                    ),
                },
                prefix="Bookmark landmark context:\n",
            ),
        ]
        return await asyncio.wait_for(
            request_llm_json(
                llm_client=llm_client,
                content=content,
                schema_name="bookmark_landmark_selection",
                response_schema=BOOKMARK_LANDMARK_SCHEMA,
                cache_breakpoint_index=1,
            ),
            timeout=BOOKMARK_LANDMARK_CHUNK_TIMEOUT_SECONDS,
        )

    landmark_results = await asyncio.gather(
        *[
            _request_landmark_chunk(chunk_index, landmark_chunk)
            for chunk_index, landmark_chunk in enumerate(landmark_chunks)
            if chunk_index in landmark_chunk_indexes_to_review
        ],
        return_exceptions=True,
    )
    for parsed in landmark_results:
        if isinstance(parsed, BaseException):
            landmark_chunk_failures += 1
            continue
        confidence = str(parsed.get("confidence") or "").strip().lower()
        landmark_chunk_confidences.append(confidence)
        if confidence not in {"high", "medium"}:
            continue
        selected_landmark_indexes.update(
            int(index)
            for index in parsed.get("selected_candidate_indexes") or []
            if isinstance(index, int) and index in valid_landmark_indexes
        )
        landmark_label_overrides.update(
            _filtered_label_overrides(
                parsed.get("label_overrides"),
                valid_indexes=valid_landmark_indexes,
            )
        )

    applied = False
    if selected_indexes:
        elements = structure_json.get("elements")
        if isinstance(elements, list):
            for index, element in enumerate(elements):
                if not isinstance(element, dict):
                    continue
                if index not in selected_indexes:
                    element.pop("bookmark_include", None)
                    element.pop("bookmark_text_override", None)
                    continue
                element["bookmark_include"] = True
                override = _normalize_text(
                    label_overrides.get(str(index)) or label_overrides.get(index)
                )
                if override:
                    element["bookmark_text_override"] = override
            applied = True

    outline_candidates = _build_outline_candidates(
        structure_json=structure_json,
        candidate_payload=candidate_payload,
        selected_heading_indexes=selected_indexes,
        selected_landmark_indexes=selected_landmark_indexes,
    )
    for candidate in outline_candidates:
        if not isinstance(candidate, dict) or candidate.get("source_kind") != "landmark":
            continue
        source_index = candidate.get("source_index")
        override = _normalize_text(
            landmark_label_overrides.get(str(source_index)) or landmark_label_overrides.get(source_index)
        )
        if not override:
            continue
        candidate["supported_labels"] = _dedupe_supported_labels([*candidate.get("supported_labels", []), override])
        candidate["text"] = _resolve_supported_label(
            list(candidate.get("supported_labels") or []),
            override,
            fallback=str(candidate.get("text") or ""),
        )
        candidate["preferred_label"] = candidate["text"]
    preview_pages = list(candidate_payload["pages"][:3])
    selected_pages = sorted({
        candidate["source_page"]
        for candidate in outline_candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("source_page"), int)
    })
    for page in _sample_preview_pages(selected_pages):
        if page not in preview_pages:
            preview_pages.append(page)
    outline_audit = await _generate_bookmark_outline_plan(
        pdf_path=pdf_path,
        original_filename=original_filename,
        outline_candidates=outline_candidates,
        preview_pages=preview_pages,
        llm_client=llm_client,
    )
    if outline_audit.get("applied"):
        structure_json["bookmark_plan"] = _merge_bookmark_plan_entries(
            front_matter_entries,
            outline_audit.get("outline_entries") or [],
        )
    else:
        structure_json.pop("bookmark_plan", None)

    return structure_json, {
        "attempted": True,
        "applied": applied or bool(selected_landmark_indexes) or bool(front_matter_entries),
        "reason": "chunked_bookmark_selection",
        "confidence": _best_confidence_label([*chunk_confidences, *landmark_chunk_confidences]),
        "selected_heading_count": len(selected_indexes),
        "selected_landmark_count": len(selected_landmark_indexes),
        "chunk_count": len(heading_chunks),
        "heading_chunks_reviewed": len(heading_chunk_indexes_to_review),
        "heading_chunk_shortlist_applied": bool(heading_chunk_shortlist.get("applied", False)),
        "heading_chunk_failures": heading_chunk_failures,
        "landmark_chunk_count": len(landmark_chunks),
        "landmark_chunks_reviewed": len(landmark_chunk_indexes_to_review),
        "landmark_chunk_shortlist_applied": bool(landmark_chunk_shortlist.get("applied", False)),
        "landmark_chunk_failures": landmark_chunk_failures,
        "outline_plan_applied": bool(outline_audit.get("applied", False)),
        "outline_entry_count": int(outline_audit.get("outline_entry_count", 0) or 0),
        "front_matter_applied": bool(front_matter_audit.get("applied", False)),
        "front_matter_entry_count": int(front_matter_audit.get("entry_count", 0) or 0),
    }
