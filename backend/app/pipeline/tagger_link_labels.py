"""Infer visible link and widget labels from captured page evidence."""

import math
import re
from typing import Any

import pikepdf

from app.pipeline.tagger_geometry import (
    LINK_SAFE_MAX_CHARS,
    LINK_SAFE_MAX_WORDS,
    LINK_TEXT_ELEMENT_TYPES,
    _bbox_from_points,
    _bbox_intersection,
    _bbox_iou,
    _containment_ratio,
    _element_boxes,
    _expand_bbox,
    _rect_array_to_bbox,
)
from app.pipeline.tagger_ownership import _best_bbox_match
from app.pipeline.tagger_semantics import _element_accessible_text
from app.services.pdf_objects import pdf_dictionary, pdf_text


def _visible_link_contents_from_page_elements(
    annotation: pikepdf.Object,
    page_elements: list[dict[str, Any]] | None,
) -> tuple[str, str]:
    """Infer link contents from overlapping visible text on the same page."""
    if not page_elements:
        return "", ""
    try:
        annotation_bbox = _rect_array_to_bbox(annotation.get("/Rect"))
    except Exception:
        annotation_bbox = None
    if not annotation_bbox:
        return "", ""

    best_text = ""
    best_score = 0.0
    best_element_type = ""
    best_element_bbox: dict[str, float] | None = None
    expanded_bbox = _expand_bbox(annotation_bbox, 6.0)
    for element in page_elements:
        if not isinstance(element, dict):
            continue
        elem_type = str(element.get("type") or "").strip()
        if elem_type not in LINK_TEXT_ELEMENT_TYPES:
            continue
        text = " ".join(_element_accessible_text(element).split()).strip()
        if not text:
            continue
        for element_bbox in _element_boxes(element):
            overlap = _bbox_intersection(annotation_bbox, element_bbox)
            relaxed_overlap = _bbox_intersection(expanded_bbox, element_bbox)
            score = max(
                _bbox_iou(annotation_bbox, element_bbox),
                _containment_ratio(annotation_bbox, element_bbox),
                _containment_ratio(element_bbox, annotation_bbox),
            )
            if overlap <= 0 and relaxed_overlap <= 0 and score < 0.5:
                continue
            if score > best_score or (
                math.isclose(score, best_score) and len(text) > len(best_text)
            ):
                best_text = text
                best_score = score
                best_element_type = elem_type
                best_element_bbox = element_bbox

    if not best_text:
        return "", ""
    if not _is_safe_inferred_link_label(
        best_text,
        annotation_bbox=annotation_bbox,
        source_bbox=best_element_bbox,
        source_type=best_element_type,
    ):
        return "", ""

    return best_text, best_element_type


def _visible_link_contents_from_page_words(
    annotation: pikepdf.Object,
    page_words: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, float] | None]:
    """Infer link contents from overlapping word boxes on the same page."""
    if not page_words:
        return "", None
    try:
        annotation_bbox = _rect_array_to_bbox(annotation.get("/Rect"))
    except Exception:
        annotation_bbox = None
    if not annotation_bbox:
        return "", None

    expanded_bbox = _expand_bbox(annotation_bbox, 3.0)
    y_center = (annotation_bbox["b"] + annotation_bbox["t"]) / 2.0
    y_tolerance = max(6.0, (annotation_bbox["t"] - annotation_bbox["b"]) * 1.25)
    matched_words: list[tuple[float, float, str, dict[str, float]]] = []

    for word in page_words:
        if not isinstance(word, dict):
            continue
        bbox = word.get("bbox")
        if not isinstance(bbox, dict):
            continue
        text = " ".join(
            str(word.get("display_text") or word.get("text") or "").split()
        ).strip()
        if not text:
            continue

        overlap = _bbox_intersection(annotation_bbox, bbox)
        relaxed_overlap = _bbox_intersection(expanded_bbox, bbox)
        score = max(
            _bbox_iou(annotation_bbox, bbox),
            _containment_ratio(annotation_bbox, bbox),
            _containment_ratio(bbox, annotation_bbox),
        )
        word_y = (float(bbox.get("b", 0.0)) + float(bbox.get("t", 0.0))) / 2.0
        if abs(word_y - y_center) > y_tolerance and relaxed_overlap <= 0:
            continue
        if overlap <= 0 and relaxed_overlap <= 0 and score < 0.5:
            continue
        matched_words.append((word_y, float(bbox.get("l", 0.0)), text, bbox))

    if not matched_words:
        return "", None

    matched_words.sort(key=lambda item: (-item[0], item[1]))
    parts = [text for _, _, text, _ in matched_words if text]
    if not parts:
        return "", None

    xs = [float(bbox["l"]) for _, _, _, bbox in matched_words] + [
        float(bbox["r"]) for _, _, _, bbox in matched_words
    ]
    ys = [float(bbox["b"]) for _, _, _, bbox in matched_words] + [
        float(bbox["t"]) for _, _, _, bbox in matched_words
    ]
    return " ".join(parts).strip(), _bbox_from_points(xs, ys)


def _visible_link_contents_from_page_hyperlinks(
    annotation: pikepdf.Object,
    page_hyperlinks: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, float] | None]:
    """Infer link contents from docling-parse hyperlink metadata when present."""
    try:
        annotation_bbox = _rect_array_to_bbox(annotation.get("/Rect"))
    except Exception:
        annotation_bbox = None
    matched = _best_bbox_match(annotation_bbox, page_hyperlinks, margin=6.0)
    if not matched:
        return "", None

    for key in ("widget_text", "widget_description"):
        text = " ".join(str(matched.get(key) or "").split()).strip()
        if text:
            return text, matched.get("bbox")
    return "", matched.get("bbox")


def _is_safe_inferred_link_label(
    text: str,
    *,
    annotation_bbox: dict[str, float] | None,
    source_bbox: dict[str, float] | None = None,
    source_type: str = "",
) -> bool:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return False
    words = [word for word in normalized.split(" ") if word]
    if len(normalized) > LINK_SAFE_MAX_CHARS or len(words) > LINK_SAFE_MAX_WORDS:
        return False
    if re.search(r"[.!?]\s+[A-Z]", normalized):
        return False
    if not any(char.isalpha() for char in normalized):
        return False
    if not annotation_bbox or not source_bbox:
        return True

    ann_width = max(float(annotation_bbox["r"]) - float(annotation_bbox["l"]), 1.0)
    src_width = max(float(source_bbox["r"]) - float(source_bbox["l"]), 1.0)
    ann_height = max(float(annotation_bbox["t"]) - float(annotation_bbox["b"]), 1.0)
    src_height = max(float(source_bbox["t"]) - float(source_bbox["b"]), 1.0)
    ann_area = ann_width * ann_height
    src_area = src_width * src_height

    if source_type in {"paragraph", "word"} and (
        src_width > ann_width * 2.5 or src_area > ann_area * 4.0
    ):
        return False

    if source_type == "paragraph":
        if normalized.endswith((".", "!", "?")) and len(words) > 6:
            return False
    return True


def _infer_link_contents(
    annotation: pikepdf.Object,
    page_elements: list[dict[str, Any]] | None = None,
    page_words: list[dict[str, Any]] | None = None,
    page_hyperlinks: list[dict[str, Any]] | None = None,
) -> str:
    """Create a useful /Contents description for link annotations."""
    try:
        existing = pdf_text(annotation.get("/Contents")).strip()
    except Exception:
        existing = ""

    action = None
    try:
        action = pdf_dictionary(annotation.get("/A"))
        uri = pdf_text(action.get("/URI")).strip() if action is not None else ""
    except Exception:
        uri = ""
    try:
        dest = annotation.get("/Dest")
        has_dest = bool(dest)
    except Exception:
        has_dest = False

    try:
        annotation_bbox = _rect_array_to_bbox(annotation.get("/Rect"))
    except Exception:
        annotation_bbox = None

    visible_text = ""
    visible_type = ""
    hyperlink_text, hyperlink_bbox = _visible_link_contents_from_page_hyperlinks(
        annotation,
        page_hyperlinks,
    )
    if hyperlink_text and _is_safe_inferred_link_label(
        hyperlink_text,
        annotation_bbox=annotation_bbox,
        source_bbox=hyperlink_bbox,
        source_type="hyperlink",
    ):
        visible_text = hyperlink_text
        visible_type = "hyperlink"

    if not visible_text:
        word_text, word_bbox = _visible_link_contents_from_page_words(annotation, page_words)
        if word_text and _is_safe_inferred_link_label(
            word_text,
            annotation_bbox=annotation_bbox,
            source_bbox=word_bbox,
            source_type="word",
        ):
            visible_text = word_text
            visible_type = "word"

    if not visible_text:
        visible_text, visible_type = _visible_link_contents_from_page_elements(annotation, page_elements)
    existing_is_generated = (
        not existing
        or existing == "Link"
        or existing == "Link to destination"
        or (uri and existing == f"Link to {uri}")
        or (has_dest and existing.startswith("Link to "))
    )
    if visible_text and existing_is_generated:
        if uri and existing == f"Link to {uri}" and visible_type != "hyperlink":
            return existing
        return visible_text
    if existing:
        return existing

    try:
        if action is not None and hasattr(action, "get"):
            if uri:
                return f"Link to {uri}"
    except Exception:
        pass

    try:
        dest = annotation.get("/Dest")
        if dest:
            return "Link to destination"
    except Exception:
        pass

    return "Link"


def _infer_widget_accessible_name(
    annotation: pikepdf.Object,
    page_widgets: list[dict[str, Any]] | None = None,
) -> str:
    """Infer a widget accessible name from matched docling-parse widget metadata."""
    try:
        annotation_bbox = _rect_array_to_bbox(annotation.get("/Rect"))
    except Exception:
        annotation_bbox = None
    matched = _best_bbox_match(annotation_bbox, page_widgets, margin=6.0)
    if not matched:
        return ""

    for key in ("description", "field_name", "text"):
        value = " ".join(str(matched.get(key) or "").split()).strip()
        if value and len(value) <= 200:
            return value
    return ""


def _best_annotation_source_element(
    annotation: pikepdf.Object,
    page_elements: list[dict[str, Any]] | None,
    *,
    allowed_types: frozenset[str],
    margin: float = 6.0,
) -> dict[str, Any] | None:
    """Find the visible structure-json element that contains an annotation."""
    if not page_elements:
        return None
    try:
        annotation_bbox = _rect_array_to_bbox(annotation.get("/Rect"))
    except Exception:
        annotation_bbox = None
    if not annotation_bbox:
        return None

    candidates = [
        element
        for element in page_elements
        if isinstance(element, dict) and str(element.get("type") or "") in allowed_types
    ]
    return _best_bbox_match(annotation_bbox, candidates, margin=margin)
