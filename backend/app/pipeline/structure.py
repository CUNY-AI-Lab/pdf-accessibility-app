"""Step 3: Extract document structure using IBM Docling."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.pipeline.language import (
    detect_language as _detect_language,
)
from app.pipeline.language import (
    normalize_lang_tag as _normalize_lang_tag,
)
from app.services.runtime_paths import enriched_subprocess_env, resolve_binary

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from PIL import Image

TOC_HEADING_TEXTS = {
    "contents",
    "table of contents",
}
TOC_TRAILING_PAGE_RE = re.compile(
    r"(?:\.{2,}\s*|(?:\.\s*){2,}|\s{2,}|\t+)(?:\d+|[ivxlcdm]+)\s*$|(?:\s+)(?:\d+)\s*$",
    re.IGNORECASE,
)
TITLE_SPACED_CAPS_RE = re.compile(r"\b(?:[A-Z]\s+){2,}[A-Z](?:\s+\d+)*\b")
TITLE_TABLE_CAPTION_RE = re.compile(r"^(?:table|figure)\s+\d+\s*[:.]", re.IGNORECASE)
TOC_GROUP_HEADING_EXCLUDE = {
    "page",
    "lists",
}
DOCLING_SERVE_REQUEST_ATTEMPTS = 3
DOCLING_SERVE_RETRY_DELAY_SECONDS = 2.0
DOCLING_SERVE_RETRY_STATUS_CODES = {502, 503, 504}


@dataclass
class FigureInfo:
    index: int
    path: Path
    caption: str | None = None
    page: int | None = None
    bbox: dict | None = None


@dataclass
class StructureResult:
    document_json: dict
    figures: list[FigureInfo] = field(default_factory=list)
    processed_pdf_path: Path | None = None
    page_count: int = 0
    headings_count: int = 0
    tables_count: int = 0
    figures_count: int = 0


def _is_retryable_docling_serve_error(exc: Exception) -> bool:
    import httpx

    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ),
    )


async def _docling_serve_request(
    client: Any,
    method: str,
    url: str,
    *,
    attempts: int = DOCLING_SERVE_REQUEST_ATTEMPTS,
    retry_delay: float = DOCLING_SERVE_RETRY_DELAY_SECONDS,
    **kwargs: Any,
):
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except Exception as exc:
            if attempt >= attempts or not _is_retryable_docling_serve_error(exc):
                raise
            logger.warning(
                "docling-serve %s request failed on attempt %d/%d: %s; retrying",
                method.upper(),
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(retry_delay)
            continue

        if (
            response.status_code in DOCLING_SERVE_RETRY_STATUS_CODES
            and attempt < attempts
        ):
            logger.warning(
                "docling-serve %s request returned HTTP %d on attempt %d/%d; retrying",
                method.upper(),
                response.status_code,
                attempt,
                attempts,
            )
            await asyncio.sleep(retry_delay)
            continue
        return response

    raise RuntimeError("docling-serve request retries were exhausted")


def _normalize_native_toc(node: Any) -> dict[str, Any] | None:
    """Convert a docling-parse table-of-contents tree into plain JSON-safe dicts."""
    if node is None:
        return None
    if hasattr(node, "model_dump"):
        data = node.model_dump()
    elif isinstance(node, dict):
        data = node
    else:
        return None

    text = " ".join(str(data.get("text") or "").split()).strip()
    orig = " ".join(str(data.get("orig") or "").split()).strip()
    marker = " ".join(str(data.get("marker") or "").split()).strip()
    children: list[dict[str, Any]] = []
    for child in data.get("children") or []:
        normalized_child = _normalize_native_toc(child)
        if normalized_child is not None:
            children.append(normalized_child)

    normalized: dict[str, Any] = {"text": text, "children": children}
    if orig:
        normalized["orig"] = orig
    if marker:
        normalized["marker"] = marker
    if text or children:
        return normalized
    return None


def _extract_native_toc_from_pdf(pdf_path: Path) -> dict[str, Any] | None:
    """Read the parser-native TOC tree when the source PDF exposes one."""
    try:
        from docling_parse.pdf_parser import DoclingPdfParser
    except ImportError as exc:
        # docling-parse is an optional dependency; absence is expected in some
        # deployments (e.g. slim images without the native parser).
        logger.debug("docling-parse unavailable; skipping native TOC extraction: %s", exc)
        return None

    try:
        parser_doc = DoclingPdfParser(loglevel="fatal").load(str(pdf_path), lazy=True)
        native_toc = parser_doc.get_table_of_contents()
    except Exception as exc:
        logger.info("Native TOC extraction unavailable for %s: %s", pdf_path.name, exc)
        return None

    normalized = _normalize_native_toc(native_toc)
    if not normalized or not normalized.get("children"):
        return None
    return normalized


def _resolve_ref(doc_dict: dict, ref: str):
    """Resolve a JSON pointer like '#/texts/0' into the actual item from doc_dict."""
    if not ref or not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    obj = doc_dict
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if obj is None:
            return None
    return obj


def _walk_body_tree(doc_dict: dict) -> list[dict]:
    """Walk the body tree in reading order and return a flat list of content items.

    Docling stores document structure as a tree rooted at doc_dict["body"],
    with children referenced via JSON pointers. We do a depth-first traversal
    to get elements in reading order, which maps directly to PDF structure order.
    """
    items = []

    def _visit(ref_str: str):
        item = _resolve_ref(doc_dict, ref_str)
        if item is None:
            return

        # If it's a group (list, section, etc.), visit children in order
        if ref_str.startswith("#/groups") or ref_str in ("#/body", "#/furniture"):
            for child_ref in item.get("children", []):
                _visit(child_ref.get("$ref", ""))
            return

        # It's a content item — add it
        items.append(item)

    body = doc_dict.get("body", {})
    for child_ref in body.get("children", []):
        _visit(child_ref.get("$ref", ""))

    return items


def _extract_bbox(prov: list[dict]) -> dict | None:
    """Extract bounding box from Docling provenance data.

    Returns bbox in bottom-left origin coordinates: {l, b, r, t}
    where (l,b) is bottom-left and (r,t) is top-right.
    """
    if not prov:
        return None
    first = prov[0]
    if isinstance(first, dict):
        bbox = first.get("bbox")
    else:
        bbox = getattr(first, "bbox", None)
    if not bbox:
        return None

    if isinstance(bbox, dict):
        left = bbox.get("l", 0)
        bottom = bbox.get("b", 0)
        right = bbox.get("r", 0)
        top = bbox.get("t", 0)
    else:
        left = getattr(bbox, "l", 0)
        bottom = getattr(bbox, "b", 0)
        right = getattr(bbox, "r", 0)
        top = getattr(bbox, "t", 0)
    return {
        "l": left,
        "b": bottom,
        "r": right,
        "t": top,
    }


def _normalize_docling_elements(doc_dict: dict) -> list[dict]:
    """Convert Docling's rich document model into a flat elements array for the tagger.

    Each element has:
      - type: heading | paragraph | figure | table | list_item | code | formula
      - page: 0-indexed page number
      - bbox: {l, b, r, t} bounding box in bottom-left coordinates (for spatial matching)
      - text: text content (for text-based elements)
      - level: heading level (for headings)
      - And type-specific fields (table data, list markers, etc.)
    """
    elements = []
    figure_index = 0

    for item in _walk_body_tree(doc_dict):
        label = item.get("label", "")
        prov = item.get("prov", [])
        # Docling uses 1-based page numbers; convert to 0-based for pikepdf
        page = (prov[0]["page_no"] - 1) if prov else 0
        text = item.get("text", item.get("orig", ""))
        bbox = _extract_bbox(prov)

        # Docling may provide per-element language via Watson NLP metadata
        _docling_meta = item.get("meta") or item.get("metadata") or {}
        _docling_lang = (
            _docling_meta.get("language")
            or _docling_meta.get("detected_language")
            or item.get("language")
        )
        _item_lang = _normalize_lang_tag(_docling_lang)

        if label == "title":
            elem_dict: dict = {
                "type": "heading",
                "level": 1,
                "text": text,
                "page": page,
                "bbox": bbox,
                "_is_title": True,  # internal flag for level inference
            }
            if _item_lang:
                elem_dict["lang"] = _item_lang
            elements.append(elem_dict)

        elif label == "section_header":
            level = item.get("level", 1)
            level = max(1, min(6, level))
            elem_dict = {
                "type": "heading",
                "level": level,
                "text": text,
                "page": page,
                "bbox": bbox,
            }
            if _item_lang:
                elem_dict["lang"] = _item_lang
            elements.append(elem_dict)

        elif label in ("text", "paragraph"):
            if text.strip():
                elem_dict = {
                    "type": "paragraph",
                    "text": text,
                    "page": page,
                    "bbox": bbox,
                }
                if _item_lang:
                    elem_dict["lang"] = _item_lang
                elements.append(elem_dict)

        elif label == "caption":
            if text.strip():
                elem_dict = {
                    "type": "caption",
                    "text": text,
                    "page": page,
                    "bbox": bbox,
                }
                if _item_lang:
                    elem_dict["lang"] = _item_lang
                elements.append(elem_dict)

        elif label == "reference":
            if text.strip():
                elem_dict = {
                    "type": "bib_entry",
                    "text": text,
                    "page": page,
                    "bbox": bbox,
                }
                if _item_lang:
                    elem_dict["lang"] = _item_lang
                elements.append(elem_dict)

        elif label == "footnote":
            if text.strip():
                elem_dict = {
                    "type": "note",
                    "text": text,
                    "page": page,
                    "bbox": bbox,
                }
                if _item_lang:
                    elem_dict["lang"] = _item_lang
                elements.append(elem_dict)

        elif label == "list_item":
            parent_ref = item.get("parent", {}).get("$ref", "")
            parent = _resolve_ref(doc_dict, parent_ref)
            parent_label = parent.get("label", "") if parent else ""
            list_group_ref = parent_ref if parent_label == "list" else None

            # Detect nesting: if this list group's parent is also a list
            # group, record it so the tagger can build nested /L elements.
            parent_list_group_ref = None
            if list_group_ref and parent:
                gp_ref = parent.get("parent", {}).get("$ref", "")
                gp = _resolve_ref(doc_dict, gp_ref)
                if gp and gp.get("label") == "list":
                    parent_list_group_ref = gp_ref

            elements.append({
                "type": "list_item",
                "text": text,
                "page": page,
                "bbox": bbox,
                "enumerated": item.get("enumerated", False),
                "marker": item.get("marker", ""),
                "list_group_ref": list_group_ref,
                "parent_list_group_ref": parent_list_group_ref,
            })

        elif label in ("picture", "chart"):
            elements.append({
                "type": "figure",
                "page": page,
                "bbox": bbox,
                "figure_index": figure_index,
                "caption": _get_caption_text(item, doc_dict),
            })
            figure_index += 1

        elif label in ("table", "document_index"):
            table_data = item.get("data", {})
            elements.append({
                "type": "table",
                "page": page,
                "bbox": bbox,
                "caption": _get_caption_text(item, doc_dict),
                "num_rows": table_data.get("num_rows", 0),
                "num_cols": table_data.get("num_cols", 0),
                "cells": _normalize_table_cells(table_data),
            })

        elif label == "code":
            elements.append({
                "type": "code",
                "text": text,
                "page": page,
                "bbox": bbox,
                "language": item.get("code_language", ""),
            })

        elif label == "formula":
            elements.append({
                "type": "formula",
                "text": text,
                "page": page,
                "bbox": bbox,
            })

        elif label in ("page_header", "page_footer"):
            elements.append({
                "type": "artifact",
                "text": text,
                "page": page,
                "bbox": bbox,
                "artifact_type": label,
            })

    return elements


def _mark_toc_sequences(elements: list[dict]) -> None:
    """Convert obvious table-of-contents runs into TOC caption/item elements."""
    toc_counter = 0
    index = 0

    while index < len(elements):
        element = elements[index]
        if element.get("type") != "heading":
            index += 1
            continue

        heading_text = " ".join(str(element.get("text", "")).split()).strip().lower()
        if heading_text not in TOC_HEADING_TEXTS:
            index += 1
            continue

        entry_indexes: list[int] = []
        cursor = index + 1
        while cursor < len(elements):
            candidate = elements[cursor]
            candidate_type = candidate.get("type")
            if candidate_type == "artifact":
                cursor += 1
                continue
            if candidate_type in {"paragraph", "list_item", "heading"} and _looks_like_toc_entry(candidate.get("text", "")):
                entry_indexes.append(cursor)
                cursor += 1
                continue
            if candidate_type == "table" and _looks_like_toc_table(candidate):
                entry_indexes.append(cursor)
                cursor += 1
                continue
            break

        if len(entry_indexes) >= 1:
            toc_group_ref = f"toc-{toc_counter}"
            toc_counter += 1
            element["type"] = "toc_caption"
            element["toc_group_ref"] = toc_group_ref
            for toc_index in entry_indexes:
                if elements[toc_index].get("type") == "table":
                    elements[toc_index]["type"] = "toc_item_table"
                else:
                    elements[toc_index]["type"] = "toc_item"
                elements[toc_index]["toc_group_ref"] = toc_group_ref

        index = max(cursor, index + 1)


def _expand_toc_item_tables(elements: list[dict]) -> list[dict]:
    """Expand TOC tables into row-level toc_item elements.

    Docling often emits a visible table of contents as one or more table blocks.
    Treating each whole table as a single TOCI is too coarse for assistive
    technology. We instead convert each logical row into its own toc_item while
    keeping the group association created by TOC detection.
    """
    expanded: list[dict] = []
    for element in elements:
        if element.get("type") != "toc_item_table":
            expanded.append(element)
            continue

        row_items = _toc_row_items_from_table(element)
        if row_items:
            expanded.extend(row_items)
        else:
            fallback = dict(element)
            fallback["type"] = "toc_item"
            fallback["text"] = _toc_table_text(element)
            expanded.append(fallback)
    return expanded


def _toc_row_items_from_table(element: dict) -> list[dict]:
    """Build row-level TOC entries from a table element."""
    cells = element.get("cells") or []
    if not isinstance(cells, list) or not cells:
        return []

    rows: dict[int, list[dict]] = {}
    for cell in cells:
        try:
            row = int(cell.get("row", 0))
        except Exception:
            row = 0
        rows.setdefault(row, []).append(cell)

    if not rows:
        return []

    bbox = element.get("bbox")
    min_row = min(rows)
    max_row = max(rows)
    row_span = max(max_row - min_row + 1, 1)
    row_height = None
    if isinstance(bbox, dict):
        height = max(float(bbox.get("t", 0)) - float(bbox.get("b", 0)), 0.0)
        if height > 0:
            row_height = height / row_span

    toc_items: list[dict] = []
    for row_index in sorted(rows):
        row_cells = sorted(
            rows[row_index],
            key=lambda cell: int(cell.get("col", 0) or 0),
        )
        texts = [" ".join(str(cell.get("text", "")).split()).strip() for cell in row_cells]
        texts = [text for text in texts if text]
        if not texts:
            continue
        row_text = " ".join(texts).strip()
        if not _looks_like_toc_entry(row_text):
            continue

        row_bbox = None
        if row_height and isinstance(bbox, dict):
            row_offset = row_index - min_row
            top = float(bbox.get("t", 0)) - (row_offset * row_height)
            bottom = max(float(bbox.get("b", 0)), top - row_height)
            row_bbox = {
                "l": float(bbox.get("l", 0)),
                "b": bottom,
                "r": float(bbox.get("r", 0)),
                "t": top,
            }

        toc_items.append({
            "type": "toc_item",
            "page": element.get("page"),
            "bbox": row_bbox or bbox,
            "text": row_text,
            "toc_group_ref": element.get("toc_group_ref"),
            "toc_row_index": row_index,
            "toc_source": "table_row",
        })

    return toc_items


def _toc_table_text(element: dict) -> str:
    """Collapse a TOC table's visible rows into one fallback text string."""
    cells = element.get("cells") or []
    if not isinstance(cells, list) or not cells:
        return ""

    rows: dict[int, list[str]] = {}
    for cell in cells:
        try:
            row = int(cell.get("row", 0))
        except Exception:
            row = 0
        text = " ".join(str(cell.get("text", "")).split()).strip()
        if text:
            rows.setdefault(row, []).append(text)

    parts: list[str] = []
    for row_index in sorted(rows):
        parts.append(" ".join(rows[row_index]))
    return " ".join(parts).strip()


def _looks_like_toc_entry(text: str) -> bool:
    """Heuristic TOC entry detector: title-like text followed by a page marker."""
    collapsed = " ".join(str(text or "").split()).strip()
    if len(collapsed) < 4 or not re.search(r"[A-Za-z]", collapsed):
        return False
    match = TOC_TRAILING_PAGE_RE.search(collapsed)
    if not match:
        return False
    prefix = collapsed[:match.start()].rstrip(" .\t")
    if len(prefix.split()) < 1:
        return False
    return True


def _looks_like_toc_table(element: dict) -> bool:
    """Heuristic TOC table detector based on row-wise title/page patterns."""
    cells = element.get("cells") or []
    if not isinstance(cells, list) or len(cells) < 2:
        return False

    rows: dict[int, list[dict]] = {}
    for cell in cells:
        try:
            row = int(cell.get("row", 0))
        except Exception:
            row = 0
        rows.setdefault(row, []).append(cell)

    if len(rows) < 2:
        return False

    matching_rows = 0
    for row_cells in rows.values():
        ordered = sorted(row_cells, key=lambda c: int(c.get("col", 0) or 0))
        texts = [" ".join(str(c.get("text", "")).split()).strip() for c in ordered]
        texts = [text for text in texts if text]
        if len(texts) < 2:
            continue
        page_candidate = texts[-1]
        title_candidate = " ".join(texts[:-1]).strip()
        if not title_candidate or not re.search(r"[A-Za-z]", title_candidate):
            continue
        if re.fullmatch(r"(?:\d+(?:\.\d+)*)|(?:[ivxlcdm]+)", page_candidate, re.IGNORECASE):
            matching_rows += 1

    return matching_rows >= max(1, len(rows) // 2)


def _rect_to_bbox(rect: Any) -> dict[str, float] | None:
    """Convert a docling-parse rectangle into a bottom-left bbox dict."""
    if rect is None or not hasattr(rect, "model_dump"):
        return None
    data = rect.model_dump()
    xs = [float(data.get(key, 0.0)) for key in ("r_x0", "r_x1", "r_x2", "r_x3")]
    ys = [float(data.get(key, 0.0)) for key in ("r_y0", "r_y1", "r_y2", "r_y3")]
    return {
        "l": min(xs),
        "b": min(ys),
        "r": max(xs),
        "t": max(ys),
    }


def _toc_rows_from_word_cells(word_cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group word cells into visual TOC rows using shared y-position."""
    rows: list[dict[str, Any]] = []
    sorted_cells = sorted(
        (
            cell for cell in word_cells
            if isinstance(cell, dict)
            and isinstance(cell.get("bbox"), dict)
            and str(cell.get("text") or "").strip()
        ),
        key=lambda cell: (
            -float((cell["bbox"]["b"] + cell["bbox"]["t"]) / 2.0),
            float(cell["bbox"]["l"]),
        ),
    )

    for cell in sorted_cells:
        bbox = cell["bbox"]
        text = " ".join(str(cell.get("text") or "").split()).strip()
        y_center = (float(bbox["b"]) + float(bbox["t"])) / 2.0
        target_row = None
        for row in rows:
            if abs(float(row["y_center"]) - y_center) <= 4.0:
                target_row = row
                break
        if target_row is None:
            target_row = {
                "y_center": y_center,
                "parts": [],
            }
            rows.append(target_row)
        target_row["parts"].append({
            "text": text,
            "bbox": bbox,
        })

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        parts = sorted(row["parts"], key=lambda part: float(part["bbox"]["l"]))
        if not parts:
            continue
        texts = [part["text"] for part in parts if part["text"]]
        if not texts:
            continue
        merged_text = " ".join(texts).strip()
        bbox = {
            "l": min(float(part["bbox"]["l"]) for part in parts),
            "b": min(float(part["bbox"]["b"]) for part in parts),
            "r": max(float(part["bbox"]["r"]) for part in parts),
            "t": max(float(part["bbox"]["t"]) for part in parts),
        }
        normalized_rows.append({
            "text": merged_text,
            "bbox": bbox,
            "y_center": row["y_center"],
        })

    return normalized_rows


def _looks_like_toc_group_heading(text: str) -> bool:
    """Detect short section labels on TOC pages that group later entries."""
    collapsed = " ".join(str(text or "").split()).strip()
    if not collapsed or _looks_like_toc_entry(collapsed):
        return False
    lowercase = collapsed.lower()
    if lowercase in TOC_GROUP_HEADING_EXCLUDE:
        return False
    if len(collapsed) > 80:
        return False
    words = [word for word in collapsed.split() if word]
    if not 1 <= len(words) <= 4:
        return False
    if not re.search(r"[A-Za-z]", collapsed):
        return False
    return True


def _looks_like_structured_toc_section(text: str) -> bool:
    """Detect TOC entries that already carry an explicit section prefix."""
    collapsed = " ".join(str(text or "").split()).strip()
    if not collapsed:
        return False
    return bool(re.match(r"^(?:appendix\s+[a-z0-9]+|\d+(?:\.\d+)*)\b", collapsed, re.IGNORECASE))


def _extract_docling_parse_toc_page_rows(page: Any) -> list[dict[str, Any]]:
    """Build plausible TOC rows from docling-parse word cells on a page."""
    try:
        from docling_core.types.doc.page import TextCellUnit
    except Exception:
        return []

    word_cells: list[dict[str, Any]] = []
    try:
        iterator = page.iterate_cells(TextCellUnit.WORD)
    except Exception:
        return []

    for cell in iterator:
        bbox = _rect_to_bbox(getattr(cell, "rect", None))
        if not bbox:
            continue
        text = " ".join(str(getattr(cell, "text", "") or getattr(cell, "orig", "")).split()).strip()
        if not text:
            continue
        word_cells.append({
            "text": text,
            "bbox": bbox,
        })

    return _toc_rows_from_word_cells(word_cells)


def _rebuild_toc_elements_from_page_rows(
    elements: list[dict[str, Any]],
    toc_page_rows: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Replace malformed TOC items with rows reconstructed from visible page words."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        toc_group_ref = element.get("toc_group_ref")
        if toc_group_ref is None:
            continue
        if element.get("type") not in {"toc_caption", "toc_item", "toc_item_table"}:
            continue
        groups.setdefault(str(toc_group_ref), []).append(element)

    if not groups:
        return elements

    replacements: dict[str, list[dict[str, Any]]] = {}
    for toc_group_ref, group_entries in groups.items():
        pages = sorted({
            int(entry.get("page", 0) or 0)
            for entry in group_entries
            if isinstance(entry.get("page"), int) or str(entry.get("page", "")).isdigit()
        })
        if not pages:
            continue

        rebuilt_group: list[dict[str, Any]] = []
        found_caption = False
        pending_group_heading: str | None = None
        pending_group_bbox: dict[str, Any] | None = None

        for page_index in pages:
            page_rows = toc_page_rows.get(page_index) or []
            if not page_rows:
                continue

            started = found_caption
            for row in page_rows:
                row_text = " ".join(str(row.get("text") or "").split()).strip()
                if not row_text:
                    continue
                lowered = row_text.lower()
                if lowered in TOC_HEADING_TEXTS:
                    if not found_caption:
                        rebuilt_group.append({
                            "type": "toc_caption",
                            "page": page_index,
                            "bbox": row.get("bbox"),
                            "text": row_text,
                            "toc_group_ref": toc_group_ref,
                            "toc_source": "word_rows",
                        })
                        found_caption = True
                    started = True
                    pending_group_heading = None
                    pending_group_bbox = None
                    continue

                if not started:
                    continue

                if _looks_like_toc_entry(row_text):
                    if pending_group_heading:
                        if not _looks_like_structured_toc_section(row_text):
                            rebuilt_group.append({
                                "type": "toc_item",
                                "page": page_index,
                                "bbox": pending_group_bbox,
                                "text": pending_group_heading,
                                "toc_group_ref": toc_group_ref,
                                "toc_group_heading": True,
                                "toc_source": "word_rows",
                            })
                        pending_group_heading = None
                        pending_group_bbox = None
                    rebuilt_group.append({
                        "type": "toc_item",
                        "page": page_index,
                        "bbox": row.get("bbox"),
                        "text": row_text,
                        "toc_group_ref": toc_group_ref,
                        "toc_source": "word_rows",
                    })
                    continue

                if _looks_like_toc_group_heading(row_text):
                    pending_group_heading = row_text
                    pending_group_bbox = row.get("bbox")
                    continue

        if rebuilt_group:
            replacements[toc_group_ref] = rebuilt_group

    if not replacements:
        return elements

    rebuilt_elements: list[dict[str, Any]] = []
    emitted_groups: set[str] = set()
    for element in elements:
        toc_group_ref = str(element.get("toc_group_ref")) if element.get("toc_group_ref") is not None else None
        if toc_group_ref and toc_group_ref in replacements and element.get("type") in {"toc_caption", "toc_item", "toc_item_table"}:
            if toc_group_ref not in emitted_groups:
                rebuilt_elements.extend(replacements[toc_group_ref])
                emitted_groups.add(toc_group_ref)
            continue
        rebuilt_elements.append(element)

    return rebuilt_elements


def _rebuild_toc_with_docling_parse(elements: list[dict], pdf_path: Path) -> list[dict]:
    """Use docling-parse page words to rebuild visible TOC rows when available."""
    if not any(
        isinstance(element, dict) and element.get("type") == "toc_caption"
        for element in elements
    ):
        return elements

    try:
        from docling_parse.pdf_parser import DoclingPdfParser
    except Exception:
        return elements

    try:
        parser_doc = DoclingPdfParser(loglevel="fatal").load(str(pdf_path), lazy=True)
    except Exception as exc:
        logger.info("TOC word-row reconstruction unavailable for %s: %s", pdf_path.name, exc)
        return elements

    toc_pages = sorted({
        int(element.get("page", 0) or 0)
        for element in elements
        if isinstance(element, dict)
        and element.get("type") in {"toc_caption", "toc_item", "toc_item_table"}
        and (isinstance(element.get("page"), int) or str(element.get("page", "")).isdigit())
    })
    toc_page_rows: dict[int, list[dict[str, Any]]] = {}
    for page_index in toc_pages:
        page_no = page_index + 1
        try:
            if page_no > parser_doc.number_of_pages():
                continue
            page = parser_doc.get_page(page_no)
        except Exception:
            continue
        toc_page_rows[page_index] = _extract_docling_parse_toc_page_rows(page)

    return _rebuild_toc_elements_from_page_rows(elements, toc_page_rows)


def _get_caption_text(item: dict, doc_dict: dict) -> str | None:
    """Extract caption text from a figure or table's caption references."""
    captions = item.get("captions", [])
    if not captions:
        return None
    texts = []
    for ref in captions:
        cap_item = _resolve_ref(doc_dict, ref.get("$ref", ""))
        if cap_item:
            texts.append(cap_item.get("text", ""))
    return " ".join(texts).strip() or None


def _normalize_table_cells(table_data: dict) -> list[dict]:
    """Normalize Docling table cells into a simpler format for the tagger."""
    return [
        {
            "text": cell.get("text", ""),
            "row": cell.get("start_row_offset_idx", 0),
            "col": cell.get("start_col_offset_idx", 0),
            "row_span": cell.get("row_span", 1),
            "col_span": cell.get("col_span", 1),
            "column_header": bool(cell.get("column_header", False)),
            "row_header": bool(cell.get("row_header", False)),
            "is_header": bool(cell.get("column_header", False) or cell.get("row_header", False)),
        }
        for cell in table_data.get("table_cells", [])
    ]


def _clean_title_candidate_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _collapse_spaced_title_caps(value: Any) -> str:
    text = _clean_title_candidate_text(value)
    if not text:
        return ""

    def repl(match: re.Match[str]) -> str:
        tokens = match.group(0).split()
        letters = [token for token in tokens if len(token) == 1 and token.isalpha() and token.isupper()]
        suffix = tokens[len(letters):]
        collapsed = "".join(letters)
        if suffix:
            collapsed = " ".join([collapsed, *suffix])
        return collapsed

    return TITLE_SPACED_CAPS_RE.sub(repl, text)


def _extract_first_page_lines(pdf_path: Path) -> list[str]:
    """Best-effort visible first-page text lines for repairing Docling title artifacts."""
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(pdf_path))
        try:
            if len(document) < 1:
                return []
            page = document[0]
            text_page = page.get_textpage()
            text = text_page.get_text_range() or ""
        finally:
            document.close()
    except Exception:
        return []
    return [_clean_title_candidate_text(line) for line in text.splitlines() if line.strip()]


def _title_fingerprint(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).lower()


def _repair_title_from_first_page_lines(text: str, pdf_path: Path | None) -> str:
    if not pdf_path:
        return text
    expected = _title_fingerprint(text)
    if not expected:
        return text
    lines = _extract_first_page_lines(pdf_path)
    for line_count in range(1, min(len(lines), 4) + 1):
        candidate = _collapse_spaced_title_caps(" ".join(lines[:line_count]))
        if _title_fingerprint(candidate) == expected:
            return candidate
    return text


def _docling_title_page_index(item: dict) -> int | None:
    prov = item.get("prov")
    if not isinstance(prov, list) or not prov:
        return None
    first = prov[0]
    if not isinstance(first, dict):
        return None
    page_no = first.get("page_no")
    if isinstance(page_no, int):
        return max(page_no - 1, 0)
    return None


def _first_heading_title(elements: list[dict] | None) -> str | None:
    headings = [
        element
        for element in (elements or [])
        if isinstance(element, dict) and element.get("type") == "heading"
    ]
    if not headings:
        return None
    for element in headings:
        if element.get("level") == 1 and int(element.get("page") or 0) <= 1:
            text = _collapse_spaced_title_caps(element.get("text"))
            if text:
                return text
    for element in headings:
        text = _collapse_spaced_title_caps(element.get("text"))
        if text:
            return text
    return None


def _looks_like_late_caption_title(text: str, page_index: int | None) -> bool:
    return page_index is not None and page_index > 2 and bool(TITLE_TABLE_CAPTION_RE.match(text))


def _extract_title_from_docling(
    doc_dict: dict,
    elements: list[dict] | None = None,
    *,
    pdf_path: Path | None = None,
) -> str | None:
    """Extract a document title from Docling, rejecting common caption false positives."""
    for item in doc_dict.get("texts", []):
        if not isinstance(item, dict) or item.get("label") != "title":
            continue
        text = _collapse_spaced_title_caps(item.get("text"))
        if not text:
            continue
        if _looks_like_late_caption_title(text, _docling_title_page_index(item)):
            continue
        return _repair_title_from_first_page_lines(text, pdf_path)

    metadata_title = _collapse_spaced_title_caps(doc_dict.get("title"))
    if metadata_title:
        return _repair_title_from_first_page_lines(metadata_title, pdf_path)
    return _first_heading_title(elements)


def _infer_document_language(elements: list[dict], title: str | None = None) -> str | None:
    """Resolve a document language only from Docling-provided element metadata."""
    language_weights: dict[str, int] = {}

    for element in elements:
        if not isinstance(element, dict):
            continue
        text = " ".join(str(element.get("text") or "").split()).strip()
        lang = _normalize_lang_tag(element.get("lang"))
        if lang:
            language_weights[lang] = language_weights.get(lang, 0) + max(len(text.split()), 1)

    if language_weights:
        return max(
            language_weights.items(),
            key=lambda item: (
                item[1],
                item[0].count("-"),
                len(item[0]),
                item[0],
            ),
        )[0]
    visible_text = " ".join(
        text
        for text in [
            _clean_title_candidate_text(title),
            *[
                _clean_title_candidate_text(element.get("text"))
                for element in elements
                if isinstance(element, dict)
            ],
        ]
        if text
    )
    return _normalize_lang_tag(_detect_language(visible_text))


def _repair_pdf_with_ghostscript(
    input_path: Path, output_path: Path, timeout: int = 120
) -> bool:
    """Try to rewrite a damaged PDF into a parser-friendly file using Ghostscript."""
    gs = resolve_binary("gs", explicit=get_settings().ghostscript_path)
    if not gs:
        return False

    cmd = [
        gs,
        "-q",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=pdfwrite",
        "-o",
        str(output_path),
        str(input_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=enriched_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            f"Ghostscript repair timed out after {timeout}s for {input_path.name}"
        )
        return False
    except Exception as exc:
        logger.warning(f"Ghostscript repair execution failed for {input_path.name}: {exc}")
        return False

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err:
            logger.warning(f"Ghostscript repair failed for {input_path.name}: {err[:300]}")
        return False

    return output_path.exists() and output_path.stat().st_size > 0


async def _convert_via_docling_serve(
    pdf_path: Path,
    job_dir: Path,
    base_url: str,
    timeout: int,
    ocr_engine: str = "rapidocr",
    token: str = "",
    include_figure_images: bool = True,
) -> tuple[dict, list[FigureInfo]]:
    """Send PDF to a docling-serve instance and return (doc_dict, figures).

    Uses the async conversion endpoint with polling.  The response contains
    the same Docling ``export_to_dict()`` structure as the local converter.
    Figure images are cropped from embedded page renders using bbox data.
    """
    import base64
    import io
    import time

    import httpx

    base_url = base_url.rstrip("/")
    submit_url = f"{base_url}/v1/convert/file/async"

    auth_headers = {"Authorization": f"Bearer {token}"} if token else {}

    # Per-request timeout: generous but bounded; total timeout enforced by the polling loop
    per_request_timeout = httpx.Timeout(min(timeout, 120.0), connect=30.0)
    async with httpx.AsyncClient(timeout=per_request_timeout) as client:
        pdf_bytes = pdf_path.read_bytes()
        data = {
            "to_formats": "json",
            "ocr_engine": ocr_engine,
            "do_table_structure": "true",
            "do_picture_classification": "true",
        }
        if include_figure_images:
            data.update({
                "image_export_mode": "embedded",
                "include_images": "true",
                "images_scale": "2.0",
            })
        resp = await _docling_serve_request(
            client,
            "POST",
            submit_url,
            headers=auth_headers,
            files={"files": (pdf_path.name, pdf_bytes, "application/pdf")},
            data=data,
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]
        logger.info("docling-serve task submitted: %s", task_id)

        # Poll for completion
        poll_url = f"{base_url}/v1/status/poll/{task_id}"
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise TimeoutError(
                    f"docling-serve task {task_id} did not complete within {timeout}s"
                )
            await asyncio.sleep(2)
            poll_resp = await _docling_serve_request(
                client,
                "GET",
                poll_url,
                headers=auth_headers,
            )
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()
            status = poll_data.get("task_status")
            if status == "success":
                logger.info(
                    "docling-serve task %s completed in %.1fs",
                    task_id,
                    time.monotonic() - start,
                )
                break
            if status == "failure":
                raise RuntimeError(
                    f"docling-serve task {task_id} failed: "
                    f"{poll_data.get('error_message', 'unknown')}"
                )

        # Fetch result
        result_resp = await _docling_serve_request(
            client,
            "GET",
            f"{base_url}/v1/result/{task_id}",
            headers=auth_headers,
        )
        result_resp.raise_for_status()
        result_data = result_resp.json()

    doc_dict = result_data["document"]["json_content"]
    processing_time = result_data.get("processing_time")
    if processing_time is not None:
        logger.info("docling-serve processing_time=%.1fs", processing_time)

    # Extract figure images by cropping from embedded page renders
    figures: list[FigureInfo] = []
    if not include_figure_images:
        return doc_dict, figures

    figures_dir = job_dir / "figures"
    figures_dir.mkdir(exist_ok=True)

    dict_pictures = doc_dict.get("pictures", [])
    pages_dict = doc_dict.get("pages", {})

    # Build page image cache: page_no -> PIL Image
    page_images: dict[int, Image.Image] = {}

    for i, pic in enumerate(dict_pictures):
        prov = pic.get("prov", [])
        if not prov:
            continue
        page_no = prov[0].get("page_no")
        bbox_data = prov[0].get("bbox")
        if page_no is None or not bbox_data:
            continue

        caption = _get_caption_text(pic, doc_dict)
        bbox = _extract_bbox(prov)

        # Try to crop figure from page image
        fig_path = figures_dir / f"figure_{i}.png"
        if page_no not in page_images:
            page_key = str(page_no)
            page_info = pages_dict.get(page_key, {})
            page_img_data = page_info.get("image", {})
            uri = page_img_data.get("uri", "") if isinstance(page_img_data, dict) else ""
            if uri.startswith("data:image/"):
                try:
                    from PIL import Image as PILImage

                    # Strip data URI prefix
                    b64_str = uri.split(",", 1)[1]
                    img_bytes = base64.b64decode(b64_str)
                    page_images[page_no] = PILImage.open(io.BytesIO(img_bytes))
                except Exception as exc:
                    logger.warning("Failed to decode page %d image: %s", page_no, exc)

        page_img = page_images.get(page_no)
        if page_img is not None and bbox_data:
            try:
                page_size = pages_dict.get(str(page_no), {}).get("size", {})
                pdf_w = page_size.get("width", 612)
                pdf_h = page_size.get("height", 792)
                img_w, img_h = page_img.size

                # Convert bbox from bottom-left PDF coords to top-left pixel coords
                scale_x = img_w / pdf_w
                scale_y = img_h / pdf_h
                crop_l = bbox_data.get("l", 0) * scale_x
                crop_r = bbox_data.get("r", 0) * scale_x
                crop_t = (pdf_h - bbox_data.get("t", 0)) * scale_y
                crop_b = (pdf_h - bbox_data.get("b", 0)) * scale_y

                cropped = page_img.crop((
                    int(crop_l), int(crop_t), int(crop_r), int(crop_b),
                ))
                cropped.save(str(fig_path), "PNG")
            except Exception as exc:
                logger.warning("Failed to crop figure %d: %s", i, exc)
                fig_path = None  # type: ignore[assignment]
        else:
            fig_path = None  # type: ignore[assignment]

        if fig_path and fig_path.exists():
            figures.append(FigureInfo(
                index=i,
                path=fig_path,
                caption=caption,
                page=page_no - 1,  # Convert to 0-based
                bbox=bbox,
            ))

    # Close PIL images to free memory
    for img in page_images.values():
        img.close()
    page_images.clear()

    return doc_dict, figures


async def extract_structure(
    pdf_path: Path,
    job_dir: Path,
    *,
    include_figure_images: bool = True,
) -> StructureResult:
    """Extract document structure using Docling.

    Priority: docling-serve → local Docling.
    """
    settings = get_settings()
    use_docling_serve = bool(settings.docling_serve_url)

    if use_docling_serve:
        logger.info("Using docling-serve at %s for structure extraction", settings.docling_serve_url)
        doc_dict, figures = await _convert_via_docling_serve(
            pdf_path,
            job_dir,
            settings.docling_serve_url,
            settings.docling_serve_timeout,
            settings.docling_serve_ocr_engine,
            settings.docling_serve_token,
            include_figure_images,
        )
        processed_pdf_path = pdf_path
    else:
        def _convert():
            try:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import (
                    PdfPipelineOptions,
                    RapidOcrOptions,
                )
                from docling.document_converter import DocumentConverter, PdfFormatOption
            except ImportError:
                raise RuntimeError(
                    "Local Docling is not installed. Either set DOCLING_SERVE_URL to use "
                    "a remote docling-serve instance, or install with: "
                    "uv sync --extra local-docling"
                )

            def _convert_one(path: Path):
                pipeline_options = PdfPipelineOptions(
                    generate_picture_images=include_figure_images,
                    images_scale=2.0,
                    do_picture_classification=True,
                    do_table_structure=True,
                    ocr_options=RapidOcrOptions(backend="torch"),
                )
                converter = DocumentConverter(
                    format_options={
                        InputFormat.PDF: PdfFormatOption(
                            pipeline_options=pipeline_options,
                        ),
                    }
                )
                return converter.convert(str(path))

            source_pdf = pdf_path
            result_processed_pdf_path = source_pdf
            repaired_pdf = job_dir / "repaired_input.pdf"
            try:
                conv_result = _convert_one(source_pdf)
            except Exception:
                if _repair_pdf_with_ghostscript(source_pdf, repaired_pdf):
                    logger.warning(
                        "Docling failed on %s; retrying structure extraction with Ghostscript-repaired file",
                        source_pdf.name,
                    )
                    conv_result = _convert_one(repaired_pdf)
                    result_processed_pdf_path = repaired_pdf
                else:
                    raise

            doc = conv_result.document
            doc_dict = doc.export_to_dict()

            # Extract and save figure images
            figures = []
            if include_figure_images:
                figures_dir = job_dir / "figures"
                figures_dir.mkdir(exist_ok=True)

                dict_pictures = doc_dict.get("pictures", [])

                for i, element in enumerate(doc.pictures if hasattr(doc, 'pictures') else []):
                    try:
                        img = element.get_image(conv_result)
                        if img:
                            fig_path = figures_dir / f"figure_{i}.png"
                            img.save(str(fig_path), "PNG")
                            caption = None
                            if i < len(dict_pictures):
                                caption = _get_caption_text(dict_pictures[i], doc_dict)
                            figures.append(FigureInfo(
                                index=i,
                                path=fig_path,
                                caption=caption,
                                page=(element.prov[0].page_no - 1) if element.prov else None,
                                bbox=_extract_bbox(element.prov) if getattr(element, "prov", None) else None,
                            ))
                    except Exception as e:
                        logger.warning(f"Failed to extract figure {i}: {e}")

            return doc_dict, figures, result_processed_pdf_path

        doc_dict, figures, processed_pdf_path = await asyncio.to_thread(_convert)

    # Normalize into elements array and infer heading levels
    elements = _normalize_docling_elements(doc_dict)
    toc_source_pdf = processed_pdf_path or pdf_path
    _mark_toc_sequences(elements)
    elements = _expand_toc_item_tables(elements)
    elements = _rebuild_toc_with_docling_parse(elements, toc_source_pdf)
    title = _extract_title_from_docling(doc_dict, elements, pdf_path=pdf_path)
    document_language = _infer_document_language(elements, title)
    native_toc = await asyncio.to_thread(_extract_native_toc_from_pdf, toc_source_pdf)

    page_count = len(doc_dict.get("pages", {}))
    figures_count = len(figures)
    headings_count = sum(1 for el in elements if el["type"] == "heading")
    tables_count = sum(1 for el in elements if el["type"] == "table")

    structure = {
        "source": str(pdf_path.name),
        "page_count": page_count,
        "title": title,
        "language": document_language,
        "elements": elements,
        "figures_count": figures_count,
    }
    if native_toc:
        structure["native_toc"] = native_toc

    return StructureResult(
        document_json=structure,
        figures=figures,
        processed_pdf_path=processed_pdf_path,
        page_count=page_count,
        headings_count=headings_count,
        tables_count=tables_count,
        figures_count=figures_count,
    )
