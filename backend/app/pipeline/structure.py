"""Step 3: Extract document structure using IBM Docling."""

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TOC_HEADING_TEXTS = {
    "contents",
    "table of contents",
}
TOC_TRAILING_PAGE_RE = re.compile(
    r"(?:\.{2,}|\s{2,}|\t+)?\s*(?:\d+|[ivxlcdm]+)\s*$",
    re.IGNORECASE,
)


@dataclass
class FigureInfo:
    index: int
    path: Path
    caption: str | None = None
    page: int | None = None


@dataclass
class StructureResult:
    document_json: dict
    figures: list[FigureInfo] = field(default_factory=list)
    processed_pdf_path: Path | None = None
    page_count: int = 0
    headings_count: int = 0
    tables_count: int = 0
    figures_count: int = 0


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
    bbox = prov[0].get("bbox")
    if not bbox:
        return None
    return {
        "l": bbox.get("l", 0),
        "b": bbox.get("b", 0),
        "r": bbox.get("r", 0),
        "t": bbox.get("t", 0),
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

        if label == "title":
            elements.append({
                "type": "heading",
                "level": 1,
                "text": text,
                "page": page,
                "bbox": bbox,
                "_is_title": True,  # internal flag for level inference
            })

        elif label == "section_header":
            level = item.get("level", 1)
            level = max(1, min(6, level))
            elements.append({
                "type": "heading",
                "level": level,
                "text": text,
                "page": page,
                "bbox": bbox,
            })

        elif label in ("text", "paragraph", "caption", "reference"):
            if text.strip():
                elements.append({
                    "type": "paragraph",
                    "text": text,
                    "page": page,
                    "bbox": bbox,
                })

        elif label == "footnote":
            if text.strip():
                elements.append({
                    "type": "note",
                    "text": text,
                    "page": page,
                    "bbox": bbox,
                })

        elif label == "list_item":
            parent_ref = item.get("parent", {}).get("$ref", "")
            parent = _resolve_ref(doc_dict, parent_ref)
            parent_label = parent.get("label", "") if parent else ""
            elements.append({
                "type": "list_item",
                "text": text,
                "page": page,
                "bbox": bbox,
                "enumerated": item.get("enumerated", False),
                "marker": item.get("marker", ""),
                "list_group_ref": parent_ref if parent_label == "list" else None,
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
                "artifact_type": label,
            })

    _mark_toc_sequences(elements)
    return _expand_toc_item_tables(elements)


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


def _infer_heading_levels(elements: list[dict]):
    """Infer heading levels from bounding box heights when Docling doesn't provide them.

    Docling's PDF pipeline always outputs level=1 for all section_header elements.
    This function uses bbox height (a proxy for font size) to cluster headings into
    levels dynamically.

    Title elements (already level=1) are preserved. Section headers get levels
    assigned based on their relative bbox heights: tallest = highest level after
    title, descending from there.

    Modifies elements in place.
    """
    headings = [el for el in elements if el.get("type") == "heading"]
    if not headings:
        return

    # Check if levels are already meaningful (not all the same)
    levels = {h.get("level", 1) for h in headings}
    if len(levels) > 1:
        # Clean up internal flags and return — Docling provided real hierarchy
        for h in headings:
            h.pop("_is_title", None)
        return

    # Collect bbox heights for non-title headings
    section_headings = []
    for h in headings:
        if h.get("_is_title"):
            continue
        bbox = h.get("bbox")
        height = abs(bbox["t"] - bbox["b"]) if bbox else 0.0
        section_headings.append((h, height))

    if not section_headings:
        for h in headings:
            h.pop("_is_title", None)
        return

    # Cluster unique heights (rounded to nearest point) and sort descending
    heights = sorted({round(ht) for _, ht in section_headings}, reverse=True)

    # Build height -> level mapping
    has_title = any(h.get("_is_title") for h in headings)
    start_level = 2 if has_title else 1
    height_to_level = {ht: min(start_level + i, 6) for i, ht in enumerate(heights)}

    # Apply inferred levels
    for heading, ht in section_headings:
        heading["level"] = height_to_level.get(round(ht), start_level)

    # Clean up internal flag
    for h in headings:
        h.pop("_is_title", None)


def _extract_title_from_docling(doc_dict: dict, elements: list[dict] | None = None) -> str | None:
    """Extract the document title from Docling's output."""
    # Try Docling's texts array first
    for item in doc_dict.get("texts", []):
        if item.get("label") == "title":
            title = item.get("text", "").strip()
            if title:
                return title

    # Fall back to first H1 heading in normalized elements
    if elements:
        for el in elements:
            if el.get("type") == "heading" and el.get("level") == 1:
                title = el.get("text", "").strip()
                if title:
                    return title

    return None


def _repair_pdf_with_ghostscript(input_path: Path, output_path: Path) -> bool:
    """Try to rewrite a damaged PDF into a parser-friendly file using Ghostscript."""
    gs = shutil.which("gs")
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
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except Exception as exc:
        logger.warning(f"Ghostscript repair execution failed for {input_path.name}: {exc}")
        return False

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err:
            logger.warning(f"Ghostscript repair failed for {input_path.name}: {err[:300]}")
        return False

    return output_path.exists() and output_path.stat().st_size > 0


async def extract_structure(pdf_path: Path, job_dir: Path) -> StructureResult:
    """Extract document structure using Docling."""

    def _convert():
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        def _convert_one(path: Path):
            pipeline_options = PdfPipelineOptions(
                generate_picture_images=True,
                images_scale=2.0,
                do_picture_classification=True,
                do_table_structure=True,
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
        processed_pdf_path = source_pdf
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
                processed_pdf_path = repaired_pdf
            else:
                raise

        doc = conv_result.document
        doc_dict = doc.export_to_dict()

        # Extract and save figure images
        figures = []
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
                    ))
            except Exception as e:
                logger.warning(f"Failed to extract figure {i}: {e}")

        # Normalize into elements array and infer heading levels
        elements = _normalize_docling_elements(doc_dict)
        _infer_heading_levels(elements)
        title = _extract_title_from_docling(doc_dict, elements)

        page_count = len(doc_dict.get("pages", {}))
        figures_count = len(figures)
        headings_count = sum(1 for el in elements if el["type"] == "heading")
        tables_count = sum(1 for el in elements if el["type"] == "table")

        structure = {
            "source": str(pdf_path.name),
            "page_count": page_count,
            "title": title,
            "elements": elements,
            "figures_count": figures_count,
        }

        return StructureResult(
            document_json=structure,
            figures=figures,
            processed_pdf_path=processed_pdf_path,
            page_count=page_count,
            headings_count=headings_count,
            tables_count=tables_count,
            figures_count=figures_count,
        )

    return await asyncio.to_thread(_convert)
