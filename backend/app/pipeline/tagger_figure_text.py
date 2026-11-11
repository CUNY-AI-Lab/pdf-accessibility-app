"""Recover live glyphs in decorative images and text boxes mistaken for figures."""

from app.pipeline.page_glyphs import MeasuredTextOp
from app.pipeline.tagger_geometry import (
    ContentTarget,
    _bbox_area,
    _bbox_intersection_box,
)
from app.pipeline.tagger_ownership import _owner_of


def _union_area(boxes: list[dict[str, float]]) -> float:
    """Rectangle union, so overlapping glyphs cannot inflate text coverage."""
    xs = sorted({box[side] for box in boxes for side in ("l", "r")})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted(
            (box["b"], box["t"]) for box in boxes if box["l"] < right and box["r"] > left
        )
        top = float("-inf")
        height = 0.0
        for bottom, end in intervals:
            height += max(0.0, end - max(top, bottom))
            top = max(top, end)
        area += (right - left) * height
    return area


def _figure_text_elements(
    elements: list[dict],
    measured: dict[int, MeasuredTextOp],
    decorative: set[int],
    described: set[int] | None = None,
) -> list[dict]:
    """Separate a figure's live text from its image semantics.

    Dense glyph coverage is evidence of a text box, regardless of a model's
    classification. Sparse labels remain readable alongside the figure's
    description, including text in OCR Form XObjects.
    The proxies are appended after ownership/order planning so existing
    elements and their MCIDs retain their identity.
    """
    recovered = []
    glyphs = [g for op in measured.values() for g in op.glyphs if g.text]
    for elem in elements:
        if elem.get("type") != "figure" or not isinstance(box := elem.get("bbox"), dict):
            continue
        area = _bbox_area(box)
        if area <= 0:
            continue
        target = ContentTarget(0, box=box)
        inside = [g for g in glyphs if _owner_of(g.bbox, [target], g.turn) is not None]
        if not any(g.text.strip() for g in inside):
            continue
        boxes = [
            clipped for g in inside if (clipped := _bbox_intersection_box(g.bbox, box)) is not None
        ]
        text_box = (
            sum(_bbox_area(box) for box in boxes) >= 0.5 * area and _union_area(boxes) >= 0.5 * area
        )
        if (
            not text_box
            and elem.get("figure_index") not in decorative
            and elem.get("figure_index") not in (described or set())
        ):
            continue
        paragraph = {
            "type": "paragraph",
            "page": elem.get("page"),
            "bbox": dict(box),
            "reading_order": elem.get("reading_order"),
            "_figure_text": True,
        }
        recovered.append(paragraph)
        if text_box:
            # Images within a text box don't warrant a separate Figure. Keep
            # any existing image MCID attached to this paragraph instead.
            elem["_figure_text_box"] = paragraph
    return recovered


def _recover_figure_text_owners(
    owners: dict[tuple, ContentTarget | None],
    recovered: dict[tuple, ContentTarget | None],
) -> None:
    # Preserve paragraphs, tables, links, and explicit furniture ownership.
    # A decorative decision cannot claim their glyphs, even when boxes overlap.
    for key, target in recovered.items():
        if target is not None and owners.get(key) is None:
            owners[key] = target
