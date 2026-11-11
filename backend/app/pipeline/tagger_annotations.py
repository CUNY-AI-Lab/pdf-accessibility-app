"""Prepare and tag page links, widgets and other annotations."""

import math
from typing import Any

import pikepdf
from docling_core.types.doc.page import TextCellUnit

from app.pipeline.tagger_geometry import (
    LINK_TEXT_ELEMENT_TYPES,
    _as_positive_int,
    _bbox_area,
    _element_boxes,
    _normalize_annotation_rect,
    _normalize_text,
    _rect_array_to_bbox,
    _rect_to_bbox,
    _table_cell_bbox,
)
from app.pipeline.tagger_link_labels import (
    _best_annotation_source_element,
    _infer_link_contents,
    _infer_widget_accessible_name,
)
from app.pipeline.tagger_ownership import _best_bbox_match
from app.pipeline.tagger_semantics import _element_accessible_text
from app.pipeline.tagger_structure import StructTreeBuilder
from app.services.pdf_objects import pdf_text


def _get_docling_parse_page(parser_doc: Any | None, page_index: int) -> Any | None:
    """Return a docling-parse page object for a 0-indexed page, if available."""
    if parser_doc is None:
        return None

    page_no = page_index + 1  # docling-parse is 1-indexed
    try:
        if page_no > parser_doc.number_of_pages():
            return None
        return parser_doc.get_page(page_no)
    except Exception:
        return None


def _extract_docling_parse_text_cells(page: Any, unit: TextCellUnit) -> list[dict]:
    """Extract normalized text cells from a docling-parse page."""
    cells: list[dict] = []
    try:
        iterator = page.iterate_cells(unit)
    except Exception:
        return cells

    for cell in iterator:
        bbox = _rect_to_bbox(cell.rect)
        if not bbox:
            continue
        raw_text = " ".join(str(getattr(cell, "text", "") or getattr(cell, "orig", "")).split()).strip()
        text = _normalize_text(raw_text)
        cells.append({
            "bbox": bbox,
            "display_text": raw_text,
            "text": text,
            "index": _as_positive_int(getattr(cell, "index", len(cells) + 1), default=len(cells) + 1),
        })
    return cells


def _build_docling_parse_page_words(
    parser_doc: Any | None,
    page_index: int,
    cache: dict[int, list[dict]],
) -> list[dict]:
    """Extract word-level text cells from docling-parse for one page."""
    if parser_doc is None:
        return []
    if page_index in cache:
        return cache[page_index]

    page = _get_docling_parse_page(parser_doc, page_index)
    if page is None:
        cache[page_index] = []
        return []

    words = _extract_docling_parse_text_cells(page, TextCellUnit.WORD)
    cache[page_index] = words
    return words


def _build_docling_parse_page_hyperlinks(
    parser_doc: Any | None,
    page_index: int,
    cache: dict[int, list[dict]],
) -> list[dict]:
    """Extract hyperlink rectangles and metadata from docling-parse for one page."""
    if parser_doc is None:
        return []
    if page_index in cache:
        return cache[page_index]

    page = _get_docling_parse_page(parser_doc, page_index)
    if page is None:
        cache[page_index] = []
        return []

    hyperlinks: list[dict] = []
    for link in getattr(page, "hyperlinks", []) or []:
        bbox = _rect_to_bbox(getattr(link, "rect", None))
        if not bbox:
            continue
        hyperlinks.append({
            "bbox": bbox,
            "uri": str(getattr(link, "uri", "") or "").strip(),
            "widget_text": " ".join(str(getattr(link, "widget_text", "") or "").split()).strip(),
            "widget_description": " ".join(str(getattr(link, "widget_description", "") or "").split()).strip(),
            "index": _as_positive_int(getattr(link, "index", len(hyperlinks) + 1), default=len(hyperlinks) + 1),
        })

    cache[page_index] = hyperlinks
    return hyperlinks


def _build_docling_parse_page_widgets(
    parser_doc: Any | None,
    page_index: int,
    cache: dict[int, list[dict]],
) -> list[dict]:
    """Extract widget rectangles and metadata from docling-parse for one page."""
    if parser_doc is None:
        return []
    if page_index in cache:
        return cache[page_index]

    page = _get_docling_parse_page(parser_doc, page_index)
    if page is None:
        cache[page_index] = []
        return []

    widgets: list[dict] = []
    for widget in getattr(page, "widgets", []) or []:
        bbox = _rect_to_bbox(getattr(widget, "rect", None))
        if not bbox:
            continue
        widgets.append({
            "bbox": bbox,
            "field_name": " ".join(str(getattr(widget, "widget_field_name", "") or "").split()).strip(),
            "description": " ".join(str(getattr(widget, "widget_description", "") or "").split()).strip(),
            "text": " ".join(str(getattr(widget, "widget_text", "") or "").split()).strip(),
            "field_type": str(getattr(widget, "widget_field_type", "") or "").strip(),
            "index": _as_positive_int(getattr(widget, "index", len(widgets) + 1), default=len(widgets) + 1),
        })

    cache[page_index] = widgets
    return widgets


def _tag_link_annotations(
    page: pikepdf.Page,
    page_ref: pikepdf.Object,
    builder: StructTreeBuilder,
    page_elements: list[dict[str, Any]] | None = None,
    docling_page_words: list[dict[str, Any]] | None = None,
    docling_page_hyperlinks: list[dict[str, Any]] | None = None,
) -> int:
    """Tag link annotations as /Link struct elements using OBJR."""
    annots = page.get("/Annots")
    if not isinstance(annots, pikepdf.Array):
        return 0

    has_link_annotation = False
    tagged = 0
    for idx, annotation in enumerate(annots):
        try:
            if annotation.get("/Subtype") != pikepdf.Name("/Link"):
                continue
        except Exception:
            continue
        has_link_annotation = True
        _normalize_annotation_rect(annotation)
        current_contents = pdf_text(annotation.get("/Contents")).strip()
        inferred_contents = _infer_link_contents(
            annotation,
            page_elements,
            page_words=docling_page_words,
            page_hyperlinks=docling_page_hyperlinks,
        )
        if inferred_contents and inferred_contents != current_contents:
            annotation["/Contents"] = pikepdf.String(inferred_contents)

        if not getattr(annotation, "is_indirect", False):
            try:
                annotation = builder.pdf.make_indirect(pikepdf.Dictionary(annotation))
                annots[idx] = annotation
            except Exception:
                continue

        parent_element = _best_annotation_source_element(
            annotation,
            page_elements,
            allowed_types=LINK_TEXT_ELEMENT_TYPES,
        )
        parent_struct = builder.source_element_struct(parent_element)
        if builder.add_link_annotation(page_ref, annotation, parent=parent_struct):
            tagged += 1

    if has_link_annotation:
        page["/Tabs"] = pikepdf.Name("/S")

    return tagged


def _prepare_link_targets(page: pikepdf.Page, builder: StructTreeBuilder, elements: list[dict]) -> None:
    """Make link text a nested ownership target before splitting text runs.
    QuadPoints describe each line of a wrapped link; its Rect alone would
    also capture unrelated words between those lines."""
    annots = page.get("/Annots")
    if not isinstance(annots, pikepdf.Array):
        return
    for index, annotation in enumerate(annots):
        try:
            if annotation.get("/Subtype") != pikepdf.Name("/Link"):
                continue
        except Exception:
            continue
        _normalize_annotation_rect(annotation)
        if not annotation.is_indirect:
            try:
                annotation = builder.pdf.make_indirect(pikepdf.Dictionary(annotation))
                annots[index] = annotation
            except Exception:
                continue
        parent = _best_annotation_source_element(
            annotation, elements, allowed_types=LINK_TEXT_ELEMENT_TYPES | {"table"},
        )
        if parent is None:
            continue
        if parent.get("type") == "table" and parent.get("cells"):
            parent = _best_bbox_match(
                _rect_array_to_bbox(annotation.get("/Rect")),
                [dict(cell, bbox=_table_cell_bbox(parent, cell), source=cell)
                 for cell in parent["cells"] if isinstance(cell, dict)],
            )
            if parent is None:
                continue
            parent = parent["source"]
        boxes = []
        quads = annotation.get("/QuadPoints")
        if isinstance(quads, pikepdf.Array) and len(quads) % 8 == 0:
            try:
                for offset in range(0, len(quads), 8):
                    points = [float(quads[offset + i]) for i in range(8)]
                    box = {"l": min(points[::2]), "r": max(points[::2]),
                           "b": min(points[1::2]), "t": max(points[1::2])}
                    if all(math.isfinite(value) for value in points) and _bbox_area(box) > 0:
                        boxes.append(box)
            except (TypeError, ValueError):
                boxes = []
        if not boxes:
            box = _rect_array_to_bbox(annotation.get("/Rect"))
            if box:
                boxes.append(box)
        parent.setdefault("_link_targets", []).extend((annotation, box) for box in boxes)


def _widget_label_order(annotation: pikepdf.Object, elements: list[dict]) -> float | None:
    """Place a field just after the closest label to its left on the same
    line, or, failing that, the closest text above it."""
    rect = _rect_array_to_bbox(annotation.get("/Rect"))
    if rect is None:
        return None
    same_line = []
    above = []
    for element in elements:
        if element.get("type") not in LINK_TEXT_ELEMENT_TYPES or not _element_accessible_text(element):
            continue
        order = element.get("reading_order")
        if order is None:
            continue
        for box in _element_boxes(element):
            middle = (box["b"] + box["t"]) / 2
            if rect["b"] <= middle <= rect["t"] and box["l"] < rect["l"]:
                same_line.append((max(0, rect["l"] - box["r"]), -order, order))
            elif box["b"] >= rect["t"]:
                horizontal_gap = max(box["l"] - rect["r"], rect["l"] - box["r"], 0)
                above.append((box["b"] - rect["t"], horizontal_gap, -order, order))
    if same_line:
        return min(same_line)[-1] + 0.5
    if above:
        return min(above)[-1] + 0.5
    return None


def _tag_widget_annotations(
    page: pikepdf.Page,
    page_ref: pikepdf.Object,
    builder: StructTreeBuilder,
    docling_page_widgets: list[dict[str, Any]] | None = None,
    page_elements: list[dict[str, Any]] | None = None,
) -> int:
    """Tag widget annotations as /Form struct elements using OBJR."""
    annots = page.get("/Annots")
    if not isinstance(annots, pikepdf.Array):
        return 0

    tagged = 0
    for idx, annotation in enumerate(annots):
        try:
            if annotation.get("/Subtype") != pikepdf.Name("/Widget"):
                continue
        except Exception:
            continue

        _normalize_annotation_rect(annotation)
        if not pdf_text(annotation.get("/TU")).strip():
            inferred_name = _infer_widget_accessible_name(annotation, docling_page_widgets)
            if inferred_name:
                annotation["/TU"] = pikepdf.String(inferred_name)

        if not getattr(annotation, "is_indirect", False):
            try:
                annotation = builder.pdf.make_indirect(pikepdf.Dictionary(annotation))
                annots[idx] = annotation
            except Exception:
                continue

        with builder.at_reading_order(_widget_label_order(annotation, page_elements or [])):
            if builder.add_widget_annotation(page_ref, annotation):
                tagged += 1

    return tagged


def _tag_generic_annotations(
    page: pikepdf.Page,
    page_ref: pikepdf.Object,
    builder: StructTreeBuilder,
) -> int:
    """Tag non-link, non-widget annotations as /Annot struct elements using OBJR."""
    annots = page.get("/Annots")
    if not isinstance(annots, pikepdf.Array):
        return 0

    tagged = 0
    for idx, annotation in enumerate(annots):
        try:
            subtype = annotation.get("/Subtype")
            if subtype in (
                pikepdf.Name("/Link"),
                pikepdf.Name("/TrapNet"),
                pikepdf.Name("/Widget"),
                pikepdf.Name("/PrinterMark"),
            ):
                continue
        except Exception:
            continue

        _normalize_annotation_rect(annotation)
        if not getattr(annotation, "is_indirect", False):
            try:
                annotation = builder.pdf.make_indirect(pikepdf.Dictionary(annotation))
                annots[idx] = annotation
            except Exception:
                continue

        if builder.add_generic_annotation(page_ref, annotation):
            tagged += 1

    return tagged


def _prune_incidental_annotations(page: pikepdf.Page) -> int:
    """Remove annotation subtypes that PDF/UA does not permit as accessible content."""
    annots = page.get("/Annots")
    if not isinstance(annots, pikepdf.Array) or len(annots) == 0:
        return 0

    filtered = pikepdf.Array()
    removed = 0
    incidental_subtypes = {
        pikepdf.Name("/TrapNet"),
        pikepdf.Name("/PrinterMark"),
    }

    for annotation in annots:
        try:
            if annotation.get("/Subtype") in incidental_subtypes:
                removed += 1
                continue
        except Exception:
            pass
        filtered.append(annotation)

    if removed == 0:
        return 0

    if len(filtered) == 0:
        try:
            del page["/Annots"]
        except Exception:
            page["/Annots"] = pikepdf.Array()
    else:
        page["/Annots"] = filtered

    return removed


def _infer_annotation_contents(
    annotation: pikepdf.Object,
    subtype: pikepdf.Object | None,
) -> str:
    """Generate a meaningful /Contents string based on annotation subtype."""
    subtype_str = str(subtype) if subtype else ""

    # Markup annotations
    if subtype_str == "/Highlight":
        return "Highlighted text"
    if subtype_str == "/StrikeOut":
        return "Strikethrough text"
    if subtype_str == "/Underline":
        return "Underlined text"
    if subtype_str == "/Squiggly":
        return "Squiggly-underlined text"

    # Text note — extract user comment if present
    if subtype_str in ("/Text", "/Note"):
        try:
            contents = pdf_text(annotation.get("/Contents")).strip()
            if contents:
                return contents
        except Exception:
            pass
        return "Text note"

    # Free text annotation
    if subtype_str == "/FreeText":
        try:
            contents = pdf_text(annotation.get("/Contents")).strip()
            if contents:
                return contents
        except Exception:
            pass
        return "Free text annotation"

    # File attachment — extract filename
    if subtype_str == "/FileAttachment":
        try:
            fs = annotation.get("/FS")
            if fs is not None and hasattr(fs, "get"):
                filename = str(fs.get("/UF") or fs.get("/F") or "").strip()
                if filename:
                    return f"File attachment: {filename}"
        except Exception:
            pass
        return "File attachment"

    # Stamp
    if subtype_str == "/Stamp":
        try:
            name = pdf_text(annotation.get("/Name")).strip().lstrip("/")
            if name:
                return f"Stamp: {name}"
        except Exception:
            pass
        return "Stamp annotation"

    # Caret (insertion point)
    if subtype_str == "/Caret":
        return "Insertion mark"

    # Ink (freehand drawing)
    if subtype_str == "/Ink":
        return "Freehand drawing"

    # Fallback — use cleaned subtype name
    if subtype_str.startswith("/"):
        return f"{subtype_str[1:]} annotation"
    return "Annotation"


def _ensure_annotation_baseline(page: pikepdf.Page):
    """Apply baseline PDF/UA annotation requirements on a page."""
    _prune_incidental_annotations(page)
    annots = page.get("/Annots")
    if not isinstance(annots, pikepdf.Array) or len(annots) == 0:
        return

    page["/Tabs"] = pikepdf.Name("/S")

    for annotation in annots:
        try:
            _normalize_annotation_rect(annotation)
            subtype = annotation.get("/Subtype")
            if subtype == pikepdf.Name("/Widget"):
                continue
            if pdf_text(annotation.get("/Contents")).strip():
                continue
            if subtype == pikepdf.Name("/Link"):
                annotation["/Contents"] = pikepdf.String(_infer_link_contents(annotation))
            else:
                annotation["/Contents"] = pikepdf.String(
                    _infer_annotation_contents(annotation, subtype)
                )
        except Exception:
            continue
