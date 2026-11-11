"""Plan heading and TOC bookmarks and normalize outline leaf counts."""

import logging
import re
from typing import Any

import pikepdf

from app.pipeline.tagger_geometry import (
    _as_positive_int,
    _coerce_int,
    _collapse_spaced_caps,
    _normalize_text,
    _normalize_title_like_text,
    _obj_key,
)

logger = logging.getLogger(__name__)
TOC_TRAILING_PAGE_RE = re.compile(
    r"(?:\.{2,}\s*|(?:\.\s*){2,}|\s{2,}|\t+)(?:\d+|[ivxlcdm]+)\s*$|(?:\s+)(?:\d+)\s*$",
    re.IGNORECASE,
)
APPENDIX_BOOKMARK_KEY_RE = re.compile(r"^(appendix\s+[a-z0-9]+(?:\.\d+)*)\b", re.IGNORECASE)
NUMERIC_BOOKMARK_KEY_RE = re.compile(r"^(\d+(?:\.\d+)*)\b")


def _clean_bookmark_label(text: Any) -> str:
    label = " ".join(str(text or "").split()).strip()
    if not label:
        return ""
    label = TOC_TRAILING_PAGE_RE.sub("", label).rstrip(" .\t")
    return label.strip()


def _bookmark_section_key(text: Any) -> str:
    label = _clean_bookmark_label(text)
    if not label:
        return ""

    appendix_match = APPENDIX_BOOKMARK_KEY_RE.match(label)
    if appendix_match:
        return re.sub(r"\s+", " ", appendix_match.group(1).lower()).strip()

    numeric_match = NUMERIC_BOOKMARK_KEY_RE.match(label)
    if numeric_match:
        return numeric_match.group(1)

    return ""


def _heading_bookmark_entries(headings: list[dict], page_count: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for order_index, heading in enumerate(headings):
        text = _clean_bookmark_label(heading.get("text"))
        if not text:
            continue
        try:
            page_index = int(heading.get("page_index", -1))
        except (TypeError, ValueError):
            page_index = -1
        if not 0 <= page_index < page_count:
            continue
        entries.append({
            "text": text,
            "page_index": page_index,
            "level": max(1, min(6, _as_positive_int(heading.get("level", 1), default=1))),
            "order_index": order_index,
        })
    return entries


def _docling_heading_bookmark_entries(
    elements: list[dict[str, Any]] | None,
    page_count: int,
    *,
    selected_only: bool = False,
) -> list[dict[str, Any]]:
    if not elements:
        return []

    entries: list[dict[str, Any]] = []
    for order_index, element in enumerate(elements):
        if not isinstance(element, dict) or element.get("type") != "heading":
            continue
        if selected_only and not bool(element.get("bookmark_include")):
            continue
        text = _clean_bookmark_label(
            element.get("bookmark_text_override") or element.get("text")
        )
        if not text:
            continue
        try:
            page_index = int(element.get("page", -1))
        except (TypeError, ValueError):
            page_index = -1
        if not 0 <= page_index < page_count:
            continue
        entries.append({
            "text": text,
            "page_index": page_index,
            "level": max(1, min(6, _as_positive_int(element.get("level", 1), default=1))),
            "order_index": order_index,
        })
    return entries


def _sort_heading_bookmark_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (
            _coerce_int(entry.get("page_index", -1), default=-1),
            int(entry.get("order_index", 0) or 0),
            _normalize_text(entry.get("text")),
        ),
    )


def _maybe_synthesize_title_bookmark(
    heading_entries: list[dict[str, Any]],
    document_title: str | None,
    toc_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if toc_entries or not heading_entries or not document_title:
        return heading_entries

    normalized_title = _normalize_title_like_text(document_title)
    if not normalized_title:
        return heading_entries

    first_page_entries = [
        entry for entry in heading_entries
        if _coerce_int(entry.get("page_index", -1), default=-1) == 0
    ]
    if len(first_page_entries) < 2:
        return heading_entries

    fragment_indexes: list[int] = []
    for index, entry in enumerate(first_page_entries[:4]):
        normalized_entry = _normalize_title_like_text(entry.get("text"))
        if not normalized_entry:
            continue
        if normalized_entry == normalized_title:
            return heading_entries
        if normalized_entry in normalized_title:
            fragment_indexes.append(index)

    if len(fragment_indexes) < 2:
        return heading_entries

    title_entry = {
        "text": _collapse_spaced_caps(document_title),
        "page_index": 0,
        "level": 1,
        "order_index": -1,
    }

    filtered_entries: list[dict[str, Any]] = []
    for entry in heading_entries:
        if _coerce_int(entry.get("page_index", -1), default=-1) != 0:
            filtered_entries.append(entry)
            continue
        normalized_entry = _normalize_title_like_text(entry.get("text"))
        if normalized_entry and normalized_entry in normalized_title and normalized_entry != normalized_title:
            continue
        filtered_entries.append(entry)

    return [title_entry, *filtered_entries]


def _select_heading_bookmark_target(
    label: str,
    heading_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    normalized_label = _normalize_title_like_text(label)
    if not normalized_label:
        return None

    for entry in heading_entries:
        normalized_heading = _normalize_title_like_text(entry.get("text"))
        if normalized_heading == normalized_label:
            return entry

    return None


def _toc_bookmark_entries(
    elements: list[dict[str, Any]] | None,
    heading_entries: list[dict[str, Any]],
    page_count: int,
) -> list[dict[str, Any]]:
    if not elements:
        return []

    captioned_groups = {
        str(element.get("toc_group_ref"))
        for element in elements
        if isinstance(element, dict)
        and element.get("type") == "toc_caption"
        and element.get("toc_group_ref") is not None
    }

    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for element in elements:
        if not isinstance(element, dict):
            continue
        elem_type = str(element.get("type") or "").strip()
        if elem_type not in {"toc_caption", "toc_item", "toc_item_table"}:
            continue

        label = _clean_bookmark_label(element.get("text"))
        if not label:
            continue
        if not re.search(r"[A-Za-z]", label):
            continue

        if elem_type == "toc_caption":
            try:
                page_index = int(element.get("page", 0))
            except (TypeError, ValueError):
                page_index = 0
            if not 0 <= page_index < page_count:
                page_index = 0
            source_page_index = page_index
            level = 1
        else:
            captioned_group = str(element.get("toc_group_ref")) in captioned_groups
            target = _select_heading_bookmark_target(label, heading_entries)
            if target is None:
                try:
                    page_index = int(element.get("page", 0))
                except (TypeError, ValueError):
                    page_index = 0
                if not 0 <= page_index < page_count:
                    page_index = 0
                source_page_index = page_index
                base_level = 1
            else:
                page_index = target["page_index"]
                try:
                    source_page_index = int(element.get("page", page_index))
                except (TypeError, ValueError):
                    source_page_index = page_index
                if not 0 <= source_page_index < page_count:
                    source_page_index = page_index
                base_level = max(1, min(6, _as_positive_int(target.get("level", 1), default=1)))
            level = base_level + 1 if captioned_group else base_level

        key = (_normalize_text(label), page_index)
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "text": label,
            "page_index": page_index,
            "source_page_index": source_page_index,
            "level": max(1, min(6, level)),
            "section_key": _bookmark_section_key(label),
        })

    return entries


def _native_toc_bookmark_entries(
    native_toc: dict[str, Any] | None,
    heading_entries: list[dict[str, Any]],
    page_count: int,
) -> list[dict[str, Any]]:
    """Build bookmark entries from docling-parse's native TOC tree when available."""
    if not isinstance(native_toc, dict):
        return []

    children = native_toc.get("children")
    if not isinstance(children, list) or not children:
        return []

    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def _walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            label = _clean_bookmark_label(node.get("text"))
            if not label:
                continue

            target = _select_heading_bookmark_target(label, heading_entries)
            if target is None:
                _walk(node.get("children") or [], depth + 1)
                continue

            page_index = target["page_index"]
            if not 0 <= int(page_index) < page_count:
                _walk(node.get("children") or [], depth + 1)
                continue

            key = (_normalize_text(label), int(page_index))
            if key not in seen:
                seen.add(key)
                entries.append({
                    "text": label,
                    "page_index": int(page_index),
                    "source_page_index": int(page_index),
                    "level": max(1, min(6, depth)),
                })

            _walk(node.get("children") or [], depth + 1)

    _walk(children, 1)
    return entries


def _combine_bookmark_source_entries(
    primary_entries: list[dict[str, Any]],
    secondary_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge bookmark entry sources while preserving primary ordering."""
    combined = list(primary_entries)
    seen = {
        (_normalize_text(entry.get("text")), _coerce_int(entry.get("page_index", -1), default=-1))
        for entry in primary_entries
        if entry.get("text")
    }
    seen_section_keys = {
        str(entry.get("section_key") or "").strip()
        for entry in primary_entries
        if str(entry.get("section_key") or "").strip()
    }

    for entry in secondary_entries:
        text_key = (
            _normalize_text(entry.get("text")),
            _coerce_int(entry.get("page_index", -1), default=-1),
        )
        section_key = str(entry.get("section_key") or "").strip()
        if text_key in seen:
            continue
        if section_key and section_key in seen_section_keys:
            continue
        combined.append(entry)
        seen.add(text_key)
        if section_key:
            seen_section_keys.add(section_key)

    return combined


def _bookmark_plan_entries(
    bookmark_plan: list[dict[str, Any]] | None,
    page_count: int,
) -> list[dict[str, Any]]:
    if not isinstance(bookmark_plan, list):
        return []

    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for order_index, plan_entry in enumerate(bookmark_plan):
        if not isinstance(plan_entry, dict):
            continue
        text = " ".join(str(plan_entry.get("text") or "").split()).strip()
        if not text:
            continue
        page_index = _coerce_int(plan_entry.get("page_index", -1), default=-1)
        if not 0 <= page_index < page_count:
            continue
        level = max(1, min(6, _as_positive_int(plan_entry.get("level", 1), default=1)))
        key = (_normalize_text(text), page_index)
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "text": text,
            "page_index": page_index,
            "source_page_index": page_index,
            "level": level,
            "order_index": order_index,
        })
    return entries


def _add_bookmarks(
    pdf: pikepdf.Pdf,
    headings: list[dict],
    elements: list[dict[str, Any]] | None = None,
    bookmark_plan: list[dict[str, Any]] | None = None,
    native_toc: dict[str, Any] | None = None,
    document_title: str | None = None,
):
    """Build bookmarks from TOC semantics when available, else fall back to headings."""
    page_count = len(pdf.pages)
    plan_entries = _bookmark_plan_entries(bookmark_plan, page_count)
    heading_entries = _docling_heading_bookmark_entries(elements, page_count)
    selected_heading_entries = _docling_heading_bookmark_entries(
        elements,
        page_count,
        selected_only=True,
    )
    if not heading_entries:
        heading_entries = _heading_bookmark_entries(headings, page_count)
    native_toc_entries = _native_toc_bookmark_entries(native_toc, heading_entries, page_count)
    toc_entries = _toc_bookmark_entries(elements, heading_entries, page_count)
    if plan_entries:
        bookmark_entries = plan_entries
    elif native_toc_entries:
        bookmark_entries = native_toc_entries
    elif toc_entries:
        bookmark_entries = _combine_bookmark_source_entries(toc_entries, selected_heading_entries)
    else:
        bookmark_entries = _sort_heading_bookmark_entries(
            _maybe_synthesize_title_bookmark(heading_entries, document_title, [])
        )
    if not bookmark_entries:
        return 0

    items = [pikepdf.OutlineItem(entry["text"], entry["page_index"]) for entry in bookmark_entries]
    if not items:
        return 0

    root_items = []
    stack: list[tuple[int, pikepdf.OutlineItem]] = []

    for entry, item in zip(bookmark_entries, items, strict=True):
        level = entry["level"]

        while stack and stack[-1][0] >= level:
            stack.pop()

        if stack:
            stack[-1][1].children.append(item)
        else:
            root_items.append(item)

        stack.append((level, item))

    try:
        with pdf.open_outline() as outline:
            outline.root.extend(root_items)
    except Exception as e:
        logger.warning(f"Could not add bookmarks: {e}")
        return 0

    return len(items)


def _sanitize_outline_leaf_counts(pdf: pikepdf.Pdf) -> int:
    """Remove explicit /Count 0 from bookmark leaves for stricter viewer compatibility."""
    outlines = pdf.Root.get("/Outlines")
    if not isinstance(outlines, pikepdf.Dictionary):
        return 0

    changed = 0
    seen: set[tuple[int, int]] = set()

    def _walk(item) -> None:
        nonlocal changed
        while isinstance(item, pikepdf.Dictionary):
            item_key = _obj_key(item)
            if item_key is not None:
                if item_key in seen:
                    return
                seen.add(item_key)

            first = item.get("/First")
            if isinstance(first, pikepdf.Dictionary):
                _walk(first)
            else:
                try:
                    has_zero_count = int(item.get("/Count")) == 0
                except (TypeError, ValueError):
                    has_zero_count = False
                if has_zero_count and "/Count" in item:
                    del item["/Count"]
                    changed += 1

            next_item = item.get("/Next")
            if not isinstance(next_item, pikepdf.Dictionary):
                return
            item = next_item

    _walk(outlines.get("/First"))
    return changed
