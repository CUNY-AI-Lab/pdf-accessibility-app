"""Step 5: Write PDF/UA accessibility tags using pikepdf.

Builds a real StructTreeRoot with StructElems for headings, paragraphs,
tables, lists, and figures. Inserts BDC/EMC marked content operators into
page content streams and constructs the ParentTree NumberTree.

Uses position-based matching to correlate content stream operations with
Docling's bounding box data, so it handles multi-column layouts and
non-sequential content stream ordering.
"""

import asyncio
import logging
import math
import mimetypes
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import pikepdf
from docling_core.types.doc.page import TextCellUnit
from docling_parse.pdf_parser import DoclingPdfParser
from PIL import Image
from rtree import index as rtree_index
from scipy.optimize import linear_sum_assignment

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants and result dataclass
# ──────────────────────────────────────────────────────────────────────────────

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

TEXT_ELEMENT_TYPES = frozenset({
    "heading", "paragraph", "list_item", "code", "artifact", "table", "formula", "note", "toc_caption", "toc_item", "toc_item_table",
})
MATCH_ACCEPT_THRESHOLD = 0.05
LARGE_COST = 1e6
BLANK_PAGE_NONWHITE_THRESHOLD = 245
BLANK_PAGE_MAX_INK_RATIO = 0.001
OCR_NOISE_ONLY_OPERATORS = frozenset({
    "Do", "re", "W*", "n", "INLINE IMAGE", "w", "m", "l", "S",
})


@dataclass
class TaggingResult:
    output_path: Path
    tags_added: int = 0
    lang_set: bool = False
    marked: bool = False
    struct_elems_created: int = 0
    figures_tagged: int = 0
    headings_tagged: int = 0
    tables_tagged: int = 0
    lists_tagged: int = 0
    links_tagged: int = 0
    annotations_tagged: int = 0
    decorative_figures_artifacted: int = 0
    bookmarks_added: int = 0
    title_set: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Shared pikepdf helpers
# ──────────────────────────────────────────────────────────────────────────────


def _obj_key(obj) -> tuple[int, int] | None:
    """Return the (objgen) identity tuple for a pikepdf indirect object."""
    try:
        obj_num, gen_num = obj.objgen
        if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
            return obj_num, gen_num
    except Exception:
        return None
    return None


def _resolve_dictionary(obj):
    """Resolve a pikepdf reference to a Dictionary (or dict-like) object."""
    if obj is None:
        return None
    try:
        if isinstance(obj, pikepdf.Dictionary):
            return obj
        if hasattr(obj, "keys") and hasattr(obj, "get"):
            return obj
        return obj.get_object()
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Graphics state helpers
# ──────────────────────────────────────────────────────────────────────────────

def _mat_multiply(a: tuple, b: tuple) -> tuple:
    """Multiply two 3x3 affine matrices in PDF format (a,b,c,d,e,f)."""
    a1, b1, c1, d1, e1, f1 = a
    a2, b2, c2, d2, e2, f2 = b
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _transform_point(mat: tuple, x: float, y: float) -> tuple[float, float]:
    """Transform point (x,y) by affine matrix (a,b,c,d,e,f)."""
    a, b, c, d, e, f = mat
    return (a * x + c * y + e, b * x + d * y + f)


def _safe_float(val) -> float:
    """Safely convert a pikepdf operand to float."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _normalize_text(text: str) -> str:
    """Lowercase and collapse non-word characters for fuzzy text matching."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def _decode_pdf_text_operand(value: Any) -> str:
    """Decode a content stream text operand into a best-effort string."""
    if isinstance(value, pikepdf.String):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("latin-1", errors="ignore")
    if isinstance(value, (int, float)):
        return ""
    return str(value) if value is not None else ""


def _extract_text_from_operands(op: str, operands: list[Any]) -> str:
    """Extract visible text from Tj/TJ/'/\" operators."""
    if op == "Tj":
        return _decode_pdf_text_operand(operands[0]) if operands else ""
    if op == "TJ" and operands:
        arr = operands[0]
        if isinstance(arr, pikepdf.Array):
            return "".join(
                _decode_pdf_text_operand(item)
                for item in arr
                if not isinstance(item, (int, float))
            )
        return _decode_pdf_text_operand(arr)
    if op == "'":
        return _decode_pdf_text_operand(operands[0]) if operands else ""
    if op == '"' and len(operands) >= 3:
        return _decode_pdf_text_operand(operands[2])
    return ""


def _rect_to_bbox(rect: Any) -> dict[str, float] | None:
    """Convert a docling-parse BoundingRectangle into {l,b,r,t}."""
    if not rect or not hasattr(rect, "model_dump"):
        return None
    data = rect.model_dump()
    xs = [
        _safe_float(data.get("r_x0")),
        _safe_float(data.get("r_x1")),
        _safe_float(data.get("r_x2")),
        _safe_float(data.get("r_x3")),
    ]
    ys = [
        _safe_float(data.get("r_y0")),
        _safe_float(data.get("r_y1")),
        _safe_float(data.get("r_y2")),
        _safe_float(data.get("r_y3")),
    ]
    return {
        "l": min(xs),
        "b": min(ys),
        "r": max(xs),
        "t": max(ys),
    }


def _bbox_from_points(xs: list[float], ys: list[float]) -> dict[str, float] | None:
    """Build a bbox from point lists."""
    if not xs or not ys:
        return None
    return {
        "l": min(xs),
        "b": min(ys),
        "r": max(xs),
        "t": max(ys),
    }


def _bbox_from_center(cx: float, cy: float, width: float, height: float) -> dict[str, float]:
    """Build a bbox from center and dimensions."""
    w = max(width, 1.0)
    h = max(height, 1.0)
    return {
        "l": cx - w / 2,
        "b": cy - h / 2,
        "r": cx + w / 2,
        "t": cy + h / 2,
    }


def _region_bbox(region: "ContentRegion") -> dict[str, float] | None:
    """Return explicit region bbox when available."""
    return region.bbox


def _bbox_tuple(bbox: dict[str, float]) -> tuple[float, float, float, float]:
    """Convert bbox dict to an (l,b,r,t) tuple."""
    return (bbox["l"], bbox["b"], bbox["r"], bbox["t"])


def _bbox_area(bbox: dict[str, float] | None) -> float:
    """Compute bbox area."""
    if not bbox:
        return 0.0
    return max(0.0, bbox["r"] - bbox["l"]) * max(0.0, bbox["t"] - bbox["b"])


def _bbox_intersection(a: dict[str, float] | None, b: dict[str, float] | None) -> float:
    """Compute intersection area between bboxes."""
    if not a or not b:
        return 0.0
    x0 = max(a["l"], b["l"])
    y0 = max(a["b"], b["b"])
    x1 = min(a["r"], b["r"])
    y1 = min(a["t"], b["t"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _bbox_iou(a: dict[str, float] | None, b: dict[str, float] | None) -> float:
    """Compute intersection-over-union score."""
    inter = _bbox_intersection(a, b)
    if inter <= 0:
        return 0.0
    union = _bbox_area(a) + _bbox_area(b) - inter
    return inter / union if union > 0 else 0.0


def _containment_ratio(inner: dict[str, float] | None, outer: dict[str, float] | None) -> float:
    """Compute how much of inner lies inside outer."""
    inter = _bbox_intersection(inner, outer)
    inner_area = _bbox_area(inner)
    if inner_area <= 0:
        return 0.0
    return inter / inner_area


def _expand_bbox(bbox: dict[str, float], margin: float) -> dict[str, float]:
    """Expand bbox by a scalar margin."""
    return {
        "l": bbox["l"] - margin,
        "b": bbox["b"] - margin,
        "r": bbox["r"] + margin,
        "t": bbox["t"] + margin,
    }


def _as_positive_int(value: Any, default: int = 1) -> int:
    """Best-effort positive integer coercion."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalize_heading_hierarchy(elements: list[dict]) -> None:
    """Normalize heading levels to start at H1 and avoid large level jumps."""
    headings = [el for el in elements if el.get("type") == "heading"]
    if not headings:
        return

    sorted_headings = sorted(
        headings,
        key=lambda h: (
            _as_positive_int(h.get("page", 0), default=0),
            -_safe_float((h.get("bbox") or {}).get("t", 0.0)),
            _safe_float((h.get("bbox") or {}).get("l", 0.0)),
        ),
    )
    first_level = _as_positive_int(sorted_headings[0].get("level", 1), default=1)
    shift = first_level - 1 if first_level > 1 else 0

    previous_level = 1
    for heading in sorted_headings:
        level = _as_positive_int(heading.get("level", 1), default=1)
        if shift:
            level = max(1, level - shift)
        if previous_level == 1 and level > 1:
            level = 1
        if level > previous_level + 1:
            level = previous_level + 1
        heading["level"] = max(1, min(6, level))
        previous_level = heading["level"]


def _infer_link_contents(annotation: pikepdf.Object) -> str:
    """Create a useful /Contents description for link annotations."""
    try:
        existing = str(annotation.get("/Contents", "")).strip()
    except Exception:
        existing = ""
    if existing:
        return existing

    try:
        action = annotation.get("/A")
        if action is not None and hasattr(action, "get"):
            uri = str(action.get("/URI", "")).strip()
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


def _table_has_consistent_column_count(cells: list[dict]) -> bool:
    """Check whether table rows appear to have a consistent column count."""
    if not cells:
        return False

    row_widths: dict[int, int] = {}
    for cell in cells:
        row = _as_positive_int(cell.get("row", 0), default=0)
        col_span = _as_positive_int(cell.get("col_span", 1), default=1)
        row_widths[row] = row_widths.get(row, 0) + col_span

    if len(row_widths) <= 1:
        return True

    widths = list(row_widths.values())
    return min(widths) == max(widths)


# ──────────────────────────────────────────────────────────────────────────────
# Content region extraction — positions from content streams
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ContentRegion:
    """A region in the content stream with its computed page position."""
    kind: str  # "text" or "image"
    start_idx: int  # index into instructions list
    end_idx: int  # exclusive end index
    instructions: list  # the actual instructions
    cx: float = 0.0  # center x in page coordinates
    cy: float = 0.0  # center y in page coordinates
    width: float = 0.0
    height: float = 0.0
    xobject_name: str = ""  # for image regions
    bbox: dict[str, float] | None = None  # {l,b,r,t}
    text: str = ""


def _update_text_pos(pos, first_text_pos, last_text_pos):
    """Track first and last text positions in a block."""
    if first_text_pos is None:
        first_text_pos = pos
    last_text_pos = pos
    return first_text_pos, last_text_pos


def _extract_content_regions(instructions: list, page) -> list[ContentRegion]:
    """Walk a content stream tracking graphics state to extract positioned regions.

    Returns a list of ContentRegion objects, each representing either a text
    block (BT..ET) or an image placement (Do), with approximate page coordinates
    computed from the CTM and text matrix.
    """
    regions = []
    ctm = IDENTITY
    ctm_stack = []
    font_size = 12.0

    i = 0
    while i < len(instructions):
        instr = instructions[i]
        op = str(instr.operator)

        # ── Graphics state operators ──
        if op == "q":
            ctm_stack.append(ctm)
        elif op == "Q":
            if ctm_stack:
                ctm = ctm_stack.pop()
        elif op == "cm":
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            if len(operands) >= 6:
                m = tuple(_safe_float(x) for x in operands[:6])
                ctm = _mat_multiply(m, ctm)

        # ── Text block ──
        elif op == "BT":
            bt_start = i
            bt_block = [instr]
            text_matrix = IDENTITY
            first_text_pos = None
            last_text_pos = None
            block_font_size = font_size
            text_chunks: list[str] = []
            xs: list[float] = []
            ys: list[float] = []
            i += 1

            while i < len(instructions):
                inner = instructions[i]
                inner_op = str(inner.operator)
                bt_block.append(inner)

                if inner_op == "ET":
                    i += 1
                    break

                inner_ops = list(inner.operands) if hasattr(inner, "operands") else []

                if inner_op == "Tm" and len(inner_ops) >= 6:
                    text_matrix = tuple(_safe_float(x) for x in inner_ops[:6])
                    pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                    xs.append(pos[0])
                    ys.append(pos[1])
                    first_text_pos, last_text_pos = _update_text_pos(
                        pos, first_text_pos, last_text_pos,
                    )

                elif inner_op in ("Td", "TD") and len(inner_ops) >= 2:
                    tx, ty = _safe_float(inner_ops[0]), _safe_float(inner_ops[1])
                    text_matrix = (
                        text_matrix[0], text_matrix[1],
                        text_matrix[2], text_matrix[3],
                        text_matrix[4] + tx, text_matrix[5] + ty,
                    )
                    pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                    xs.append(pos[0])
                    ys.append(pos[1])
                    first_text_pos, last_text_pos = _update_text_pos(
                        pos, first_text_pos, last_text_pos,
                    )

                elif inner_op == "T*":
                    text_matrix = (
                        text_matrix[0], text_matrix[1],
                        text_matrix[2], text_matrix[3],
                        text_matrix[4], text_matrix[5] - block_font_size * 1.2,
                    )
                    pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                    xs.append(pos[0])
                    ys.append(pos[1])
                    first_text_pos, last_text_pos = _update_text_pos(
                        pos, first_text_pos, last_text_pos,
                    )

                elif inner_op == "Tf" and len(inner_ops) >= 2:
                    block_font_size = abs(_safe_float(inner_ops[1])) or 12.0
                    font_size = block_font_size

                elif inner_op in ("Tj", "TJ", "'", '"'):
                    if first_text_pos is None:
                        pos = _transform_point(ctm, text_matrix[4], text_matrix[5])
                        xs.append(pos[0])
                        ys.append(pos[1])
                        first_text_pos, last_text_pos = pos, pos
                    chunk = _extract_text_from_operands(inner_op, inner_ops)
                    if chunk:
                        text_chunks.append(chunk)

                i += 1

            # Compute approximate center and bbox of text block
            if first_text_pos and last_text_pos:
                cx = (first_text_pos[0] + last_text_pos[0]) / 2
                cy = (first_text_pos[1] + last_text_pos[1]) / 2
                h = abs(first_text_pos[1] - last_text_pos[1]) + block_font_size
            elif first_text_pos:
                cx, cy = first_text_pos
                h = block_font_size
            else:
                cx, cy, h = 0, 0, 0

            text = _normalize_text(" ".join(text_chunks))
            bbox = _bbox_from_points(xs, ys)
            if bbox:
                # Expand bbox to include approximate glyph extents.
                text_width_guess = max(block_font_size * 0.5 * max(len(text), 1), block_font_size * 2)
                if bbox["r"] - bbox["l"] < block_font_size * 0.5:
                    bbox["r"] = bbox["l"] + text_width_guess
                bbox["t"] += block_font_size
                bbox["b"] -= block_font_size * 0.2
            else:
                bbox = _bbox_from_center(cx, cy, block_font_size * max(len(text), 1), h or block_font_size)

            regions.append(ContentRegion(
                kind="text",
                start_idx=bt_start,
                end_idx=i,
                instructions=bt_block,
                cx=cx,
                cy=cy,
                width=max(0.0, bbox["r"] - bbox["l"]) if bbox else 0.0,
                height=max(0.0, bbox["t"] - bbox["b"]) if bbox else h,
                bbox=bbox,
                text=text,
            ))
            continue  # already incremented i

        # ── Image/XObject placement ──
        elif op == "Do":
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            if operands:
                name = str(operands[0])
                is_image = False
                try:
                    resources = page.get("/Resources", {})
                    xobjects = resources.get("/XObject", {})
                    xobj = xobjects.get(name)
                    if xobj is not None and hasattr(xobj, "get"):
                        is_image = xobj.get("/Subtype") == pikepdf.Name("/Image")
                except Exception:
                    pass

                if is_image:
                    x0, y0 = _transform_point(ctm, 0, 0)
                    x1, y1 = _transform_point(ctm, 1, 1)
                    bbox = {
                        "l": min(x0, x1),
                        "b": min(y0, y1),
                        "r": max(x0, x1),
                        "t": max(y0, y1),
                    }
                    regions.append(ContentRegion(
                        kind="image",
                        start_idx=i,
                        end_idx=i + 1,
                        instructions=[instr],
                        cx=(x0 + x1) / 2,
                        cy=(y0 + y1) / 2,
                        width=abs(x1 - x0),
                        height=abs(y1 - y0),
                        xobject_name=name,
                        bbox=bbox,
                    ))

        i += 1

    return regions


# ──────────────────────────────────────────────────────────────────────────────
# Spatial matching — correlate content regions with Docling elements
# ──────────────────────────────────────────────────────────────────────────────

def _bbox_center(bbox: dict) -> tuple[float, float]:
    """Get center point of a Docling bounding box {l, b, r, t}."""
    return (
        (bbox["l"] + bbox["r"]) / 2,
        (bbox["b"] + bbox["t"]) / 2,
    )


def _distance(x1: float, y1: float, x2: float, y2: float) -> float:
    """Euclidean distance between two points."""
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def _text_similarity(a: str, b: str) -> float:
    """Compute fuzzy text similarity for tie-breaking ambiguous spatial matches."""
    a_norm = _normalize_text(a)
    b_norm = _normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm in b_norm or b_norm in a_norm:
        shorter = min(len(a_norm), len(b_norm))
        longer = max(len(a_norm), len(b_norm))
        containment = shorter / longer if longer else 0.0
    else:
        containment = 0.0
    ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
    return max(containment, ratio)


def _distance_score(a: dict[str, float] | None, b: dict[str, float] | None) -> float:
    """Convert center distance into a normalized [0,1] score."""
    if not a or not b:
        return 0.0
    ax, ay = _bbox_center(a)
    bx, by = _bbox_center(b)
    dist = _distance(ax, ay, bx, by)
    diag = math.sqrt((b["r"] - b["l"]) ** 2 + (b["t"] - b["b"]) ** 2)
    norm = dist / max(diag * 2, 1.0)
    return max(0.0, 1.0 - norm)


def _matching_score(region: ContentRegion, element: dict) -> float:
    """Score a region-element pair using containment/IoU + text similarity."""
    rbbox = _region_bbox(region)
    ebbox = element.get("bbox")
    if not rbbox or not ebbox:
        return 0.0

    containment = _containment_ratio(rbbox, ebbox)
    reverse_containment = _containment_ratio(ebbox, rbbox)
    iou = _bbox_iou(rbbox, ebbox)
    dist = _distance_score(rbbox, ebbox)
    spatial = max(containment, 0.6 * iou + 0.4 * reverse_containment)

    if region.kind == "text":
        text = _text_similarity(region.text, element.get("text", ""))
        return 0.65 * spatial + 0.25 * text + 0.10 * dist

    text = _text_similarity(region.text, element.get("caption", ""))
    return 0.85 * spatial + 0.10 * dist + 0.05 * text


def _candidate_element_positions(
    region_bbox: dict[str, float],
    spatial_index: rtree_index.Index,
    bbox_element_positions: list[int],
) -> list[int]:
    """Query nearby candidate elements via R-tree with bounded expansion."""
    candidates = list(spatial_index.intersection(_bbox_tuple(_expand_bbox(region_bbox, 24))))
    if not candidates:
        candidates = list(spatial_index.intersection(_bbox_tuple(_expand_bbox(region_bbox, 96))))
    if not candidates:
        candidates = bbox_element_positions.copy()
    return list(dict.fromkeys(candidates))


def _optimal_match(
    regions: list[tuple[int, ContentRegion]],
    elements: list[tuple[int, dict]],
) -> dict[int, int]:
    """Globally optimal region-element assignment via Hungarian algorithm."""
    if not regions or not elements:
        return {}

    spatial_index = rtree_index.Index()
    bbox_element_positions: list[int] = []
    for epos, (_elem_idx, elem) in enumerate(elements):
        ebbox = elem.get("bbox")
        if ebbox:
            spatial_index.insert(epos, _bbox_tuple(ebbox))
            bbox_element_positions.append(epos)

    cost = np.full((len(regions), len(elements)), LARGE_COST, dtype=np.float64)
    for rpos, (_region_idx, region) in enumerate(regions):
        rbbox = _region_bbox(region)
        if not rbbox:
            continue
        candidates = _candidate_element_positions(
            rbbox,
            spatial_index,
            bbox_element_positions,
        )
        for epos in candidates:
            _elem_idx, elem = elements[epos]
            score = _matching_score(region, elem)
            cost[rpos, epos] = 1.0 - score

    row_idx, col_idx = linear_sum_assignment(cost)
    matches: dict[int, int] = {}
    for rpos, epos in zip(row_idx, col_idx, strict=False):
        score = 1.0 - cost[rpos, epos]
        if score < MATCH_ACCEPT_THRESHOLD:
            continue
        region_idx = regions[rpos][0]
        elem_idx = elements[epos][0]
        matches[region_idx] = elem_idx
    return matches


def _build_docling_parse_page_lines(
    parser_doc: Any | None,
    page_index: int,
    cache: dict[int, list[dict]],
) -> list[dict]:
    """Extract line-level text cells from docling-parse for one page."""
    if parser_doc is None:
        return []
    if page_index in cache:
        return cache[page_index]

    page_no = page_index + 1  # docling-parse is 1-indexed
    try:
        if page_no > parser_doc.number_of_pages():
            cache[page_index] = []
            return []
        page = parser_doc.get_page(page_no)
    except Exception:
        cache[page_index] = []
        return []

    lines: list[dict] = []
    for cell in page.iterate_cells(TextCellUnit.LINE):
        bbox = _rect_to_bbox(cell.rect)
        if not bbox:
            continue
        text = _normalize_text(getattr(cell, "text", "") or getattr(cell, "orig", ""))
        lines.append({
            "bbox": bbox,
            "text": text,
        })

    cache[page_index] = lines
    return lines


def _refine_text_regions_with_docling_parse(
    text_regions: list[tuple[int, ContentRegion]],
    page_lines: list[dict],
) -> None:
    """Use docling-parse line boxes to improve region bboxes in Docling coordinates."""
    if not text_regions or not page_lines:
        return

    spatial_index = rtree_index.Index()
    for i, line in enumerate(page_lines):
        spatial_index.insert(i, _bbox_tuple(line["bbox"]))

    for _, region in text_regions:
        rbbox = _region_bbox(region)
        if not rbbox:
            continue
        candidates = list(spatial_index.intersection(_bbox_tuple(_expand_bbox(rbbox, 24))))
        if not candidates:
            candidates = list(spatial_index.intersection(_bbox_tuple(_expand_bbox(rbbox, 96))))
        if not candidates:
            continue

        best_line = None
        best_score = 0.0
        best_spatial = 0.0
        best_text = 0.0
        for cand in candidates:
            line = page_lines[cand]
            lbbox = line["bbox"]
            spatial = max(
                _containment_ratio(rbbox, lbbox),
                _containment_ratio(lbbox, rbbox),
                _bbox_iou(rbbox, lbbox),
            )
            text = _text_similarity(region.text, line["text"])
            score = 0.6 * spatial + 0.4 * text
            if score > best_score:
                best_score = score
                best_line = line
                best_spatial = spatial
                best_text = text

        if not best_line:
            continue
        # Only trust docling-parse replacement on strong evidence.
        if best_text < 0.65 and best_spatial < 0.8:
            continue

        region.bbox = best_line["bbox"]
        region.cx, region.cy = _bbox_center(region.bbox)
        region.width = max(0.0, region.bbox["r"] - region.bbox["l"])
        region.height = max(0.0, region.bbox["t"] - region.bbox["b"])
        if not region.text and best_line["text"]:
            region.text = best_line["text"]


def _match_regions_to_elements(
    regions: list[ContentRegion],
    elements: list[dict],
    docling_page_lines: list[dict] | None = None,
) -> dict[int, int]:
    """Match content regions to Docling elements with robust global assignment.

    Returns: dict mapping region_index -> element_index.
    """
    text_elements = [(i, el) for i, el in enumerate(elements)
                     if el.get("type") in TEXT_ELEMENT_TYPES]
    figure_elements = [(i, el) for i, el in enumerate(elements)
                       if el.get("type") == "figure"]

    text_regions = [(i, r) for i, r in enumerate(regions) if r.kind == "text"]
    image_regions = [(i, r) for i, r in enumerate(regions) if r.kind == "image"]

    if docling_page_lines:
        _refine_text_regions_with_docling_parse(text_regions, docling_page_lines)

    matches: dict[int, int] = {}
    matches.update(_optimal_match(text_regions, text_elements))
    matches.update(_optimal_match(image_regions, figure_elements))
    return matches


# ──────────────────────────────────────────────────────────────────────────────
# Structure tree builder
# ──────────────────────────────────────────────────────────────────────────────

class StructTreeBuilder:
    """Manages MCID allocation, StructElem creation, and ParentTree construction."""

    def __init__(self, pdf: pikepdf.Pdf, doc_lang: str = "en"):
        self.pdf = pdf
        self.doc_lang = doc_lang.lower().strip()
        self.struct_tree_root = None
        self.doc_elem = None
        self._page_mcid_counter: dict[int, int] = {}
        self._page_mcids: dict[int, list[tuple[int, pikepdf.Object]]] = {}
        self._struct_parents_counter = 0
        self._object_parent_entries: list[tuple[int, pikepdf.Object]] = []
        self._headings: list[dict] = []
        self._struct_elems_created = 0
        self._list_cache: dict[str, tuple[pikepdf.Object, pikepdf.Object | None]] = {}
        self._note_counter = 0
        self._table_counter = 0
        self._toc_cache: dict[str, pikepdf.Object] = {}

    def setup(self):
        """Create StructTreeRoot and Document element."""
        self.struct_tree_root = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([]),
            "/ParentTree": self.pdf.make_indirect(pikepdf.Dictionary({
                "/Nums": pikepdf.Array([]),
            })),
        }))
        self.pdf.Root["/StructTreeRoot"] = self.struct_tree_root

        self.doc_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Document"),
            "/P": self.struct_tree_root,
            "/K": pikepdf.Array([]),
        }))
        self.struct_tree_root["/K"] = pikepdf.Array([self.doc_elem])

    def _alloc_mcid(self, page_index: int) -> int:
        """Allocate a new MCID for the given page."""
        mcid = self._page_mcid_counter.get(page_index, 0)
        self._page_mcid_counter[page_index] = mcid + 1
        return mcid

    def _alloc_struct_parent_key(self) -> int:
        """Allocate a StructParent key for ParentTree entries."""
        key = self._struct_parents_counter
        self._struct_parents_counter += 1
        return key

    def _register_mcid(self, page_index: int, mcid: int, elem: pikepdf.Object):
        """Register an MCID-to-StructElem mapping for ParentTree construction."""
        if page_index not in self._page_mcids:
            self._page_mcids[page_index] = []
        self._page_mcids[page_index].append((mcid, elem))

    def _make_mcr(self, mcid: int, page_ref: pikepdf.Object) -> pikepdf.Dictionary:
        """Create a Marked Content Reference (MCR) dictionary."""
        return pikepdf.Dictionary({
            "/Type": pikepdf.Name("/MCR"),
            "/Pg": page_ref,
            "/MCID": mcid,
        })

    def _append_child(self, parent: pikepdf.Object, child: pikepdf.Object):
        """Append a child element to a parent's /K array."""
        parent_k = parent.get("/K")
        if isinstance(parent_k, pikepdf.Array):
            parent_k.append(child)
        else:
            parent["/K"] = pikepdf.Array([child])

    def _add_struct_elem(
        self,
        struct_type: str,
        page_index: int,
        page_ref: pikepdf.Object,
        parent: pikepdf.Object | None = None,
        alt_text: str | None = None,
        lang: str | None = None,
    ) -> int:
        """Create a StructElem and return its MCID."""
        if parent is None:
            parent = self.doc_elem

        mcid = self._alloc_mcid(page_index)

        elem_dict = {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name(f"/{struct_type}"),
            "/P": parent,
            "/K": self._make_mcr(mcid, page_ref),
        }
        if alt_text:
            elem_dict["/Alt"] = pikepdf.String(alt_text)
        if lang and lang.lower().strip() != self.doc_lang:
            elem_dict["/Lang"] = pikepdf.String(lang)

        elem = self.pdf.make_indirect(pikepdf.Dictionary(elem_dict))
        self._register_mcid(page_index, mcid, elem)
        self._append_child(parent, elem)
        self._struct_elems_created += 1
        return mcid

    def add_heading(
        self, level: int, page_index: int, page_ref: pikepdf.Object,
        text: str, lang: str | None = None,
    ) -> int:
        level = max(1, min(6, level))
        mcid = self._add_struct_elem(f"H{level}", page_index, page_ref, lang=lang)
        self._headings.append({"level": level, "text": text, "page_index": page_index})
        return mcid

    def add_paragraph(
        self, page_index: int, page_ref: pikepdf.Object,
        lang: str | None = None,
    ) -> int:
        return self._add_struct_elem("P", page_index, page_ref, lang=lang)

    def add_figure(self, page_index: int, page_ref: pikepdf.Object, alt_text: str | None = None) -> int:
        return self._add_struct_elem("Figure", page_index, page_ref, alt_text=alt_text)

    def add_code(self, page_index: int, page_ref: pikepdf.Object, lang: str | None = None) -> int:
        return self._add_struct_elem("Code", page_index, page_ref, lang=lang)

    def add_formula(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        alt_text: str | None = None,
    ) -> int:
        return self._add_struct_elem("Formula", page_index, page_ref, alt_text=alt_text)

    def add_note(self, page_index: int, page_ref: pikepdf.Object) -> int:
        mcid = self._alloc_mcid(page_index)
        self._note_counter += 1
        note_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Note"),
            "/P": self.doc_elem,
            "/ID": pikepdf.String(f"note-{self._note_counter}"),
            "/K": self._make_mcr(mcid, page_ref),
        }))
        self._register_mcid(page_index, mcid, note_elem)
        self._append_child(self.doc_elem, note_elem)
        self._struct_elems_created += 1
        return mcid

    def _get_or_create_toc(self, toc_group_ref: str | None) -> pikepdf.Object:
        if toc_group_ref and toc_group_ref in self._toc_cache:
            return self._toc_cache[toc_group_ref]

        toc_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TOC"),
            "/P": self.doc_elem,
            "/K": pikepdf.Array([]),
        }))
        self._append_child(self.doc_elem, toc_elem)
        self._struct_elems_created += 1
        if toc_group_ref:
            self._toc_cache[toc_group_ref] = toc_elem
        return toc_elem

    def add_toc_caption(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        toc_group_ref: str | None,
    ) -> int:
        toc_elem = self._get_or_create_toc(toc_group_ref)
        mcid = self._alloc_mcid(page_index)
        caption_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Caption"),
            "/P": toc_elem,
            "/K": self._make_mcr(mcid, page_ref),
        }))
        toc_k = toc_elem.get("/K")
        if isinstance(toc_k, pikepdf.Array):
            toc_elem["/K"] = pikepdf.Array([caption_elem, *list(toc_k)])
        else:
            toc_elem["/K"] = pikepdf.Array([caption_elem])
        self._register_mcid(page_index, mcid, caption_elem)
        self._struct_elems_created += 1
        return mcid

    def add_toc_item(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        toc_group_ref: str | None,
    ) -> int:
        toc_elem = self._get_or_create_toc(toc_group_ref)
        mcid = self._alloc_mcid(page_index)
        toci_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TOCI"),
            "/P": toc_elem,
            "/K": self._make_mcr(mcid, page_ref),
        }))
        self._append_child(toc_elem, toci_elem)
        self._register_mcid(page_index, mcid, toci_elem)
        self._struct_elems_created += 1
        return mcid

    def add_table(self, page_index: int, page_ref: pikepdf.Object, table_data: dict) -> int:
        """Add a Table StructElem with rows, cells, /ID on THs, and /Headers on TDs."""
        self._table_counter += 1
        table_n = self._table_counter

        table_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/P": self.doc_elem,
            "/K": pikepdf.Array([]),
        }))
        self._append_child(self.doc_elem, table_elem)
        self._struct_elems_created += 1

        cells = table_data.get("cells", [])
        num_rows = table_data.get("num_rows", 0)

        if not cells or num_rows == 0 or not _table_has_consistent_column_count(cells):
            mcid = self._alloc_mcid(page_index)
            table_elem["/K"] = self._make_mcr(mcid, page_ref)
            self._register_mcid(page_index, mcid, table_elem)
            return mcid

        rows: dict[int, list[dict]] = {}
        for cell in cells:
            row_idx = cell.get("row", 0)
            if row_idx not in rows:
                rows[row_idx] = []
            rows[row_idx].append(cell)

        # Collected during the build pass so we can set /Headers on TDs
        # after all TH /IDs have been assigned.
        th_info: list[tuple[int, int, int, int, str]] = []
        # (row, col, row_span, col_span, cell_id)
        td_info: list[tuple[int, int, pikepdf.Object]] = []
        # (row, col, cell_elem)

        first_mcid = None
        for row_idx in sorted(rows.keys()):
            row_elem = self.pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/TR"),
                "/P": table_elem,
                "/K": pikepdf.Array([]),
            }))
            table_elem["/K"].append(row_elem)
            self._struct_elems_created += 1

            for cell in sorted(rows[row_idx], key=lambda c: c.get("col", 0)):
                cell_type = "TH" if cell.get("is_header", False) else "TD"
                col_idx = _as_positive_int(cell.get("col", 0), default=0)
                mcid = self._alloc_mcid(page_index)
                if first_mcid is None:
                    first_mcid = mcid
                cell_elem_dict: dict = {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name(f"/{cell_type}"),
                    "/P": row_elem,
                    "/K": self._make_mcr(mcid, page_ref),
                }
                attrs = pikepdf.Dictionary({
                    "/O": pikepdf.Name("/Table"),
                })
                row_span = _as_positive_int(cell.get("row_span", 1), default=1)
                col_span = _as_positive_int(cell.get("col_span", 1), default=1)
                if row_span > 1:
                    attrs["/RowSpan"] = row_span
                if col_span > 1:
                    attrs["/ColSpan"] = col_span

                scope = ""
                if cell_type == "TH":
                    scope = "/Column"
                    if bool(cell.get("row_header", False)):
                        scope = "/Row"
                    elif bool(cell.get("column_header", False)):
                        scope = "/Column"
                    elif col_idx == 0:
                        scope = "/Row"
                    attrs["/Scope"] = pikepdf.Name(scope)

                    # Assign a unique /ID so TD cells can reference it.
                    cell_id = f"t{table_n}-r{row_idx}-c{col_idx}"
                    cell_elem_dict["/ID"] = pikepdf.String(cell_id)
                    th_info.append((row_idx, col_idx, row_span, col_span, cell_id))

                if len(attrs) > 1:
                    cell_elem_dict["/A"] = attrs

                cell_elem = self.pdf.make_indirect(pikepdf.Dictionary(cell_elem_dict))
                row_elem["/K"].append(cell_elem)
                self._register_mcid(page_index, mcid, cell_elem)
                self._struct_elems_created += 1

                if cell_type == "TD":
                    td_info.append((row_idx, col_idx, cell_elem))

        # Second pass: set /Headers on each TD pointing to applicable THs.
        if th_info and td_info:
            for td_row, td_col, td_elem in td_info:
                header_ids: list[str] = []
                for th_row, th_col, th_rspan, th_cspan, th_id in th_info:
                    # Column header: TH spans TD's column and is above it
                    if th_col <= td_col < th_col + th_cspan and th_row + th_rspan <= td_row:
                        header_ids.append(th_id)
                    # Row header: TH spans TD's row and is to the left
                    elif th_row <= td_row < th_row + th_rspan and th_col + th_cspan <= td_col:
                        header_ids.append(th_id)
                if header_ids:
                    td_attrs = td_elem.get("/A")
                    if td_attrs is None:
                        td_attrs = pikepdf.Dictionary({"/O": pikepdf.Name("/Table")})
                        td_elem["/A"] = td_attrs
                    td_attrs["/Headers"] = pikepdf.Array(
                        [pikepdf.String(h) for h in header_ids],
                    )

        return first_mcid if first_mcid is not None else 0

    def add_list_item(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        list_group_ref: str | None,
        parent_list_group_ref: str | None = None,
    ) -> int:
        """Add a single list item to a list group, creating the list on first use.

        List items sharing the same list_group_ref are grouped under a single
        /L StructElem. The list is created lazily on the first item and reused
        for subsequent items in the same group.

        For nested lists, parent_list_group_ref identifies the enclosing list
        group. The nested /L is attached as a child of the parent list's most
        recent /LI element, producing the PDF structure::

            /L (outer)
              /LI
                /LBody …
                /L (nested)      ← attached here
                  /LI
                    /LBody …
        """
        if list_group_ref and list_group_ref in self._list_cache:
            list_elem, _ = self._list_cache[list_group_ref]
        else:
            list_elem = self.pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/L"),
                "/K": pikepdf.Array([]),
            }))

            # Determine where to attach this /L element.
            # If it belongs to a nested list, place it inside the parent
            # list's most recent /LI; otherwise attach to the document root.
            attach_parent = self.doc_elem
            if parent_list_group_ref and parent_list_group_ref in self._list_cache:
                _, parent_last_li = self._list_cache[parent_list_group_ref]
                if parent_last_li is not None:
                    attach_parent = parent_last_li

            list_elem["/P"] = attach_parent
            self._append_child(attach_parent, list_elem)
            self._struct_elems_created += 1
            if list_group_ref:
                self._list_cache[list_group_ref] = (list_elem, None)

        li_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/LI"),
            "/P": list_elem,
            "/K": pikepdf.Array([]),
        }))
        list_elem["/K"].append(li_elem)
        self._struct_elems_created += 1

        # Track the most recent LI so nested lists can attach to it.
        if list_group_ref and list_group_ref in self._list_cache:
            self._list_cache[list_group_ref] = (
                self._list_cache[list_group_ref][0],
                li_elem,
            )

        mcid = self._alloc_mcid(page_index)
        lbody_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/LBody"),
            "/P": li_elem,
            "/K": self._make_mcr(mcid, page_ref),
        }))
        li_elem["/K"].append(lbody_elem)
        self._register_mcid(page_index, mcid, lbody_elem)
        self._struct_elems_created += 1

        return mcid

    def add_link_annotation(self, page_ref: pikepdf.Object, annotation: pikepdf.Object) -> bool:
        """Attach a /Link StructElem to an existing link annotation via OBJR."""
        try:
            if annotation.get("/Subtype") != pikepdf.Name("/Link"):
                return False
            if not getattr(annotation, "is_indirect", False):
                return False
        except Exception:
            return False

        struct_parent_key = self._alloc_struct_parent_key()
        annotation["/StructParent"] = struct_parent_key
        if not str(annotation.get("/Contents", "")).strip():
            annotation["/Contents"] = pikepdf.String(_infer_link_contents(annotation))

        link_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Link"),
            "/P": self.doc_elem,
            "/K": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/OBJR"),
                "/Obj": annotation,
                "/Pg": page_ref,
            }),
        }))
        self._append_child(self.doc_elem, link_elem)
        self._object_parent_entries.append((struct_parent_key, link_elem))
        self._struct_elems_created += 1
        return True

    def add_widget_annotation(self, page_ref: pikepdf.Object, annotation: pikepdf.Object) -> bool:
        """Attach a /Form StructElem to a widget annotation via OBJR."""
        try:
            if annotation.get("/Subtype") != pikepdf.Name("/Widget"):
                return False
            if not getattr(annotation, "is_indirect", False):
                return False
        except Exception:
            return False

        struct_parent_key = self._alloc_struct_parent_key()
        annotation["/StructParent"] = struct_parent_key

        form_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Form"),
            "/P": self.doc_elem,
            "/K": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/OBJR"),
                "/Obj": annotation,
                "/Pg": page_ref,
            }),
        }))
        self._append_child(self.doc_elem, form_elem)
        self._object_parent_entries.append((struct_parent_key, form_elem))
        self._struct_elems_created += 1
        return True

    def add_generic_annotation(self, page_ref: pikepdf.Object, annotation: pikepdf.Object) -> bool:
        """Attach a generic /Annot StructElem to a non-link, non-widget annotation."""
        try:
            subtype = annotation.get("/Subtype")
            if subtype in (pikepdf.Name("/Link"), pikepdf.Name("/Widget"), pikepdf.Name("/PrinterMark")):
                return False
            if not getattr(annotation, "is_indirect", False):
                return False
        except Exception:
            return False

        struct_parent_key = self._alloc_struct_parent_key()
        annotation["/StructParent"] = struct_parent_key

        annot_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Annot"),
            "/P": self.doc_elem,
            "/K": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/OBJR"),
                "/Obj": annotation,
                "/Pg": page_ref,
            }),
        }))
        self._append_child(self.doc_elem, annot_elem)
        self._object_parent_entries.append((struct_parent_key, annot_elem))
        self._struct_elems_created += 1
        return True

    def finalize(self):
        """Build the ParentTree NumberTree and set StructParents on pages."""
        parent_tree_nums = pikepdf.Array([])

        for page_index in sorted(self._page_mcids.keys()):
            if page_index >= len(self.pdf.pages):
                continue

            page = self.pdf.pages[page_index]
            struct_parents_idx = self._alloc_struct_parent_key()
            page["/StructParents"] = struct_parents_idx

            mcid_entries = self._page_mcids[page_index]
            elem_array = pikepdf.Array([])
            for _mcid, elem in sorted(mcid_entries, key=lambda x: x[0]):
                elem_array.append(elem if elem is not None else pikepdf.Null())

            parent_tree_nums.append(struct_parents_idx)
            parent_tree_nums.append(self.pdf.make_indirect(elem_array))

        for struct_parent_key, elem in sorted(self._object_parent_entries, key=lambda x: x[0]):
            parent_tree_nums.append(struct_parent_key)
            parent_tree_nums.append(elem)

        parent_tree = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Nums": parent_tree_nums,
        }))
        self.struct_tree_root["/ParentTree"] = parent_tree
        self.struct_tree_root["/ParentTreeNextKey"] = self._struct_parents_counter


# ──────────────────────────────────────────────────────────────────────────────
# Content stream rewriting — position-based
# ──────────────────────────────────────────────────────────────────────────────

def _strip_existing_markers(instructions: list) -> list:
    """Remove existing BDC/BMC/EMC markers from content stream instructions."""
    stripped = []
    depth = 0
    for instr in instructions:
        op = str(instr.operator) if hasattr(instr, 'operator') else ""
        if op in ("BDC", "BMC"):
            depth += 1
        elif op == "EMC":
            if depth > 0:
                depth -= 1
        else:
            stripped.append(instr)
    return stripped


def _rewrite_content_stream(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    page_index: int,
    elements: list[dict],
    builder: StructTreeBuilder,
    page_ref: pikepdf.Object,
    alt_lookup: dict[int, str],
    decorative_figures: set[int] | None = None,
    docling_page_lines: list[dict] | None = None,
):
    """Insert BDC/EMC markers into the page's content stream using position-based matching."""
    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except Exception as e:
        logger.warning(f"Could not parse content stream for page {page_index}: {e}")
        return

    instructions = _strip_existing_markers(instructions)
    if not instructions:
        return

    regions = _extract_content_regions(instructions, page)
    matches = _match_regions_to_elements(
        regions,
        elements,
        docling_page_lines=docling_page_lines,
    )

    # Map instruction start indices to region indices
    region_by_start: dict[int, int] = {
        region.start_idx: ri for ri, region in enumerate(regions)
    }

    # Walk instructions, inserting markers
    new_instructions = []
    i = 0
    while i < len(instructions):
        if i in region_by_start:
            ri = region_by_start[i]
            region = regions[ri]
            elem_idx = matches.get(ri)

            if elem_idx is not None:
                _emit_tagged_region(
                    new_instructions, region, elements[elem_idx],
                    builder, page_index, page_ref, alt_lookup, decorative_figures or set(),
                )
            else:
                new_instructions.append(_make_bmc_artifact())
                new_instructions.extend(region.instructions)
                new_instructions.append(_make_emc())

            i = region.end_idx
        else:
            artifact_chunk = []
            while i < len(instructions) and i not in region_by_start:
                artifact_chunk.append(instructions[i])
                i += 1
            if artifact_chunk:
                new_instructions.append(_make_bmc_artifact())
                new_instructions.extend(artifact_chunk)
                new_instructions.append(_make_emc())

    try:
        new_stream = pikepdf.unparse_content_stream(new_instructions)
        page["/Contents"] = pdf.make_stream(new_stream)
    except Exception as e:
        logger.error(f"Failed to write content stream for page {page_index}: {e}")


def _emit_tagged_region(
    new_instructions: list,
    region: ContentRegion,
    elem: dict,
    builder: StructTreeBuilder,
    page_index: int,
    page_ref: pikepdf.Object,
    alt_lookup: dict[int, str],
    decorative_figures: set[int],
):
    """Emit a tagged content region based on the matched element type."""
    elem_type = elem.get("type", "")
    actual_text = _element_actual_text(elem)
    accessible_text = _element_accessible_text(elem)

    # Types that wrap content as artifact (no struct elem needed)
    if elem_type == "artifact":
        new_instructions.append(_make_bmc_artifact())
        new_instructions.extend(region.instructions)
        new_instructions.append(_make_emc())
        return

    # Tables: struct tree built separately, content stream wrapped as artifact
    if elem_type == "table":
        builder.add_table(page_index, page_ref, elem)
        new_instructions.append(_make_bmc_artifact())
        new_instructions.extend(region.instructions)
        new_instructions.append(_make_emc())
        return

    # List items: use cached list groups to avoid duplicates
    if elem_type == "list_item":
        mcid = builder.add_list_item(
            page_index, page_ref, elem.get("list_group_ref"),
            parent_list_group_ref=elem.get("parent_list_group_ref"),
        )
        new_instructions.append(_make_bdc("LBody", mcid, actual_text=actual_text))
        new_instructions.extend(region.instructions)
        new_instructions.append(_make_emc())
        return

    # All other types: allocate struct elem, wrap with BDC/EMC
    elem_lang = elem.get("lang")  # per-element language (may be None)

    if elem_type == "heading":
        level = elem.get("level", 1)
        mcid = builder.add_heading(level, page_index, page_ref, accessible_text, lang=elem_lang)
        tag = f"H{level}"
    elif elem_type == "figure":
        fig_idx = elem.get("figure_index")
        if fig_idx in decorative_figures:
            new_instructions.append(_make_bmc_artifact())
            new_instructions.extend(region.instructions)
            new_instructions.append(_make_emc())
            return
        alt = (
            alt_lookup.get(fig_idx)
            or elem.get("caption")
            or elem.get("text")
        )
        if isinstance(alt, str):
            alt = alt.strip()
        mcid = builder.add_figure(page_index, page_ref, alt_text=alt or None)
        tag = "Figure"
    elif elem_type == "code":
        mcid = builder.add_code(page_index, page_ref, lang=elem_lang)
        tag = "Code"
    elif elem_type == "formula":
        alt = accessible_text
        if isinstance(alt, str):
            alt = alt.strip()
        mcid = builder.add_formula(page_index, page_ref, alt_text=alt or None)
        tag = "Formula"
    elif elem_type == "note":
        mcid = builder.add_note(page_index, page_ref)
        tag = "Note"
    elif elem_type == "toc_caption":
        mcid = builder.add_toc_caption(page_index, page_ref, elem.get("toc_group_ref"))
        tag = "Caption"
    elif elem_type in {"toc_item", "toc_item_table"}:
        mcid = builder.add_toc_item(page_index, page_ref, elem.get("toc_group_ref"))
        tag = "TOCI"
    else:
        # paragraph or unknown → /P
        mcid = builder.add_paragraph(page_index, page_ref, lang=elem_lang)
        tag = "P"

    new_instructions.append(_make_bdc(tag, mcid, actual_text=actual_text))
    new_instructions.extend(region.instructions)
    new_instructions.append(_make_emc())


# ──────────────────────────────────────────────────────────────────────────────
# BDC/EMC instruction constructors
# ──────────────────────────────────────────────────────────────────────────────

def _element_accessible_text(elem: dict) -> str:
    return str(
        elem.get("actual_text")
        or elem.get("resolved_text")
        or elem.get("semantic_text_hint")
        or elem.get("text")
        or ""
    ).strip()


def _element_actual_text(elem: dict) -> str | None:
    actual_text = _element_accessible_text(elem)
    return actual_text or None


def _make_bdc(
    struct_type: str,
    mcid: int,
    *,
    actual_text: str | None = None,
) -> pikepdf.ContentStreamInstruction:
    attributes = pikepdf.Dictionary({"/MCID": mcid})
    normalized_actual_text = str(actual_text or "").strip()
    if normalized_actual_text:
        attributes["/ActualText"] = pikepdf.String(normalized_actual_text)
    return pikepdf.ContentStreamInstruction(
        [pikepdf.Name(f"/{struct_type}"), attributes],
        pikepdf.Operator("BDC"),
    )


def _make_emc() -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC"))


def _make_bmc_artifact() -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(
        [pikepdf.Name("/Artifact")], pikepdf.Operator("BMC"),
    )


def _render_page_ink_ratio(pdf_path: Path, page_number: int) -> float | None:
    from app.services.pdf_preview import render_page_png_bytes

    try:
        page_png = render_page_png_bytes(pdf_path, page_number, dpi=72, max_width=900)
    except Exception:
        return None

    with Image.open(BytesIO(page_png)) as image:
        grayscale = image.convert("L")
        histogram = grayscale.histogram()

    total_pixels = sum(histogram)
    if total_pixels <= 0:
        return 0.0
    nonwhite_pixels = sum(histogram[:BLANK_PAGE_NONWHITE_THRESHOLD])
    return nonwhite_pixels / total_pixels


def _should_artifact_nonsemantic_page_content(
    input_path: Path,
    page: pikepdf.Page,
    page_index: int,
) -> bool:
    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except Exception:
        return False

    instructions = _strip_existing_markers(instructions)
    if not instructions:
        return False

    regions = _extract_content_regions(instructions, page)
    if any(region.kind == "text" for region in regions):
        return False

    meaningful_ops: list[str] = []
    has_ocr_form = False
    has_image_xobject = False
    xobjects = {}
    try:
        resources = page.get("/Resources", {})
        xobjects = resources.get("/XObject", {}) or {}
    except Exception:
        xobjects = {}

    for instr in instructions:
        if not hasattr(instr, "operator"):
            continue
        op = str(instr.operator)
        if op in {"q", "Q", "cm"}:
            continue
        meaningful_ops.append(op)
        if op != "Do":
            continue
        operands = list(instr.operands) if hasattr(instr, "operands") else []
        if not operands:
            continue
        name = operands[0]
        xobject = xobjects.get(name) if hasattr(xobjects, "get") else None
        if xobject is None or not hasattr(xobject, "get"):
            continue
        subtype = xobject.get("/Subtype")
        if subtype == pikepdf.Name("/Image"):
            has_image_xobject = True
        elif subtype == pikepdf.Name("/Form") and str(name).startswith("/OCR-"):
            has_ocr_form = True

    ink_ratio = None
    if meaningful_ops and (
        all(op == "Do" for op in meaningful_ops)
        or (
            all(op in OCR_NOISE_ONLY_OPERATORS for op in meaningful_ops)
            and has_ocr_form
            and has_image_xobject
        )
    ):
        ink_ratio = _render_page_ink_ratio(input_path, page_index + 1)

    if meaningful_ops and all(op == "Do" for op in meaningful_ops):
        if ink_ratio is not None and ink_ratio <= BLANK_PAGE_MAX_INK_RATIO:
            return True

    return (
        bool(meaningful_ops)
        and all(op in OCR_NOISE_ONLY_OPERATORS for op in meaningful_ops)
        and has_ocr_form
        and has_image_xobject
        and ink_ratio is not None
        and ink_ratio <= BLANK_PAGE_MAX_INK_RATIO
    )


# ──────────────────────────────────────────────────────────────────────────────
# Document metadata
# ──────────────────────────────────────────────────────────────────────────────

def _reset_xmp_metadata(pdf: pikepdf.Pdf):
    """Drop source XMP metadata to avoid carrying malformed metadata packets."""
    try:
        if "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
    except Exception as e:
        logger.warning(f"Could not reset existing metadata stream: {e}")


def _set_document_title(pdf: pikepdf.Pdf, structure_json: dict, original_filename: str):
    """Set the document title in XMP metadata and enable title display."""
    existing_title = None
    try:
        if "/Title" in pdf.docinfo:
            existing_title = str(pdf.docinfo["/Title"]).strip()
    except Exception:
        pass

    title = existing_title or structure_json.get("title")
    if not title and original_filename:
        title = Path(original_filename).stem.replace("_", " ").replace("-", " ")
    if not title:
        title = "Untitled Document"

    try:
        pdf.docinfo["/Title"] = pikepdf.String(title)
    except Exception as e:
        logger.warning(f"Could not set Info dictionary title: {e}")

    try:
        with pdf.open_metadata() as meta:
            meta["{http://purl.org/dc/elements/1.1/}title"] = title
    except Exception as e:
        logger.warning(f"Could not set XMP title: {e}")

    if "/ViewerPreferences" not in pdf.Root:
        pdf.Root["/ViewerPreferences"] = pikepdf.Dictionary()
    pdf.Root["/ViewerPreferences"]["/DisplayDocTitle"] = True

    return title


def _set_pdfua_identification(pdf: pikepdf.Pdf):
    """Set PDF/UA identification metadata in XMP."""
    try:
        with pdf.open_metadata() as meta:
            meta.register_xml_namespace("http://www.aiim.org/pdfua/ns/id/", "pdfuaid")
            meta["{http://www.aiim.org/pdfua/ns/id/}part"] = "1"
    except Exception as e:
        logger.warning(f"Could not set PDF/UA identification metadata: {e}")


def _normalize_optional_content_configs(pdf: pikepdf.Pdf) -> int:
    """Normalize optional content configs to satisfy PDF/UA syntax requirements."""
    ocprops = pdf.Root.get("/OCProperties")
    if not isinstance(ocprops, pikepdf.Dictionary):
        return 0

    changes = 0

    def _normalize_config(config, default_name: str) -> None:
        nonlocal changes
        if not isinstance(config, pikepdf.Dictionary):
            return

        try:
            name = str(config.get("/Name", "")).strip()
        except Exception:
            name = ""
        if not name:
            config["/Name"] = pikepdf.String(default_name)
            changes += 1
        if "/AS" in config:
            del config["/AS"]
            changes += 1

    _normalize_config(ocprops.get("/D"), "Default")

    configs = ocprops.get("/Configs")
    if isinstance(configs, pikepdf.Array):
        for idx, config in enumerate(configs):
            _normalize_config(config, f"Optional Content Config {idx + 1}")

    return changes


def _remove_dynamic_xfa(pdf: pikepdf.Pdf) -> bool:
    """Strip dynamic XFA packets from AcroForm dictionaries."""
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, pikepdf.Dictionary):
        return False
    if "/XFA" not in acroform:
        return False
    del acroform["/XFA"]
    return True


def _normalize_embedded_file_specs(pdf: pikepdf.Pdf) -> int:
    """Ensure embedded file specs carry non-empty F/UF filename keys."""
    names = pdf.Root.get("/Names")
    if not isinstance(names, pikepdf.Dictionary):
        return 0

    embedded_files = names.get("/EmbeddedFiles")
    if not isinstance(embedded_files, pikepdf.Dictionary):
        return 0

    changes = 0

    def _string_or_empty(value) -> str:
        if value is None:
            return ""
        try:
            text = str(value).strip()
        except Exception:
            return ""
        return "" if text == "None" else text

    def _walk_name_tree(node) -> list[tuple[str, pikepdf.Object]]:
        results: list[tuple[str, pikepdf.Object]] = []
        if not isinstance(node, pikepdf.Dictionary):
            return results
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                results.extend(_walk_name_tree(kid))
        names_array = node.get("/Names")
        if isinstance(names_array, pikepdf.Array):
            for idx in range(0, len(names_array) - 1, 2):
                results.append((str(names_array[idx]), names_array[idx + 1]))
        return results

    for entry_name, file_spec in _walk_name_tree(embedded_files):
        if not isinstance(file_spec, pikepdf.Dictionary):
            continue
        ef = file_spec.get("/EF")
        if not isinstance(ef, pikepdf.Dictionary) or len(ef) == 0:
            continue

        candidate = ""
        for value in (
            file_spec.get("/UF"),
            file_spec.get("/F"),
            entry_name,
            file_spec.get("/Desc"),
            "attachment",
        ):
            candidate = _string_or_empty(value)
            if candidate:
                break
        if not candidate:
            candidate = "attachment"

        if not _string_or_empty(file_spec.get("/F")):
            file_spec["/F"] = pikepdf.String(candidate)
            changes += 1
        if not _string_or_empty(file_spec.get("/UF")):
            file_spec["/UF"] = pikepdf.String(candidate)
            changes += 1

    return changes


def _normalize_type1_font_charsets(pdf: pikepdf.Pdf) -> int:
    """Remove stale CharSet entries from embedded Type1 font descriptors.

    CharSet is optional. When present but incomplete it triggers PDF/UA font
    failures, especially on subset fonts carried through OCR or producer
    pipelines. Removing an invalid CharSet is safer than keeping incorrect
    glyph metadata.
    """
    seen_resources: set[tuple[int, int]] = set()
    seen_fonts: set[tuple[int, int]] = set()
    seen_appearances: set[tuple[int, int]] = set()
    changes = 0

    def _is_mapping_like(obj) -> bool:
        return isinstance(obj, pikepdf.Dictionary) or (hasattr(obj, "keys") and hasattr(obj, "get"))

    def _has_embedded_font(descriptor) -> bool:
        if not isinstance(descriptor, pikepdf.Dictionary):
            return False
        return any(
            key in descriptor
            for key in (
                pikepdf.Name("/FontFile"),
                pikepdf.Name("/FontFile2"),
                pikepdf.Name("/FontFile3"),
            )
        )

    def _walk_resources(resources) -> None:
        nonlocal changes
        resources = _resolve_dictionary(resources)
        if not _is_mapping_like(resources):
            return
        resources_key = _obj_key(resources)
        if resources_key and resources_key in seen_resources:
            return
        if resources_key:
            seen_resources.add(resources_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        if _is_mapping_like(fonts):
            for _, font_obj in fonts.items():
                font_dict = _resolve_dictionary(font_obj)
                if not _is_mapping_like(font_dict):
                    continue
                font_key = _obj_key(font_dict)
                if font_key and font_key in seen_fonts:
                    continue
                if font_key:
                    seen_fonts.add(font_key)

                subtype = font_dict.get("/Subtype")
                if subtype not in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1")):
                    continue
                descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                if not _has_embedded_font(descriptor):
                    continue
                if isinstance(descriptor, pikepdf.Dictionary) and pikepdf.Name("/CharSet") in descriptor:
                    del descriptor[pikepdf.Name("/CharSet")]
                    changes += 1

        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not _is_mapping_like(xobjects):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if not _is_mapping_like(xobject_dict):
                continue
            if xobject_dict.get("/Subtype") != pikepdf.Name("/Form"):
                continue
            _walk_resources(xobject_dict.get("/Resources"))

    def _walk_appearance_object(obj) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_mapping_like(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_appearances:
            return
        if appearance_key:
            seen_appearances.add(appearance_key)

        _walk_resources(appearance_obj.get("/Resources"))
        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _walk_appearance_object(child)

    for page in pdf.pages:
        _walk_resources(page.get("/Resources"))
        annots = page.get("/Annots")
        if isinstance(annots, pikepdf.Array):
            for annot in annots:
                ap = _resolve_dictionary(annot.get("/AP")) if hasattr(annot, "get") else None
                if isinstance(ap, pikepdf.Dictionary):
                    for key in ("/N", "/R", "/D"):
                        child = ap.get(key)
                        if child is not None:
                            _walk_appearance_object(child)

    acroform = _resolve_dictionary(pdf.Root.get("/AcroForm"))
    if _is_mapping_like(acroform):
        _walk_resources(acroform.get("/DR"))

    return changes


def _normalize_media_clip_data_dicts(pdf: pikepdf.Pdf) -> int:
    """Backfill syntax-critical CT/Alt entries on media clip data dictionaries."""
    changes = 0
    seen: set[tuple[int, int]] = set()

    def _string_or_empty(value) -> str:
        if value is None:
            return ""
        try:
            text = str(value).strip()
        except Exception:
            return ""
        if text == "None":
            return ""
        if text.startswith("/"):
            text = text[1:]
        return re.sub(
            r"#([0-9A-Fa-f]{2})",
            lambda match: chr(int(match.group(1), 16)),
            text,
        )

    def _is_mapping(obj) -> bool:
        return isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream))

    def _derive_media_clip_label(media_clip) -> str:
        label = _string_or_empty(media_clip.get("/N"))
        if label:
            return label
        data = media_clip.get("/D")
        if _is_mapping(data):
            for key in ("/UF", "/F", "/Desc"):
                label = _string_or_empty(data.get(key))
                if label:
                    return label
        return "Embedded media"

    def _derive_media_clip_type(media_clip) -> str | None:
        data = media_clip.get("/D")
        if not _is_mapping(data):
            return "application/octet-stream"
        try:
            if data.get("/Subtype") == pikepdf.Name("/Form"):
                return None
        except Exception:
            pass

        subtype = _string_or_empty(data.get("/Subtype"))
        if "/" in subtype:
            return subtype

        for key in ("/UF", "/F", "/Desc"):
            guess = _string_or_empty(data.get(key))
            if not guess:
                continue
            guessed_type, _ = mimetypes.guess_type(guess)
            if guessed_type:
                return guessed_type

        ef = data.get("/EF")
        if _is_mapping(ef):
            for key in ("/UF", "/F"):
                embedded_stream = ef.get(key)
                if not _is_mapping(embedded_stream):
                    continue
                stream_subtype = _string_or_empty(embedded_stream.get("/Subtype"))
                if "/" in stream_subtype:
                    return stream_subtype

        return "application/octet-stream"

    def _walk(obj) -> None:
        nonlocal changes
        if isinstance(obj, pikepdf.Array):
            for item in obj:
                _walk(item)
            return
        if not _is_mapping(obj):
            return

        objgen = getattr(obj, "objgen", None)
        if objgen and objgen != (0, 0):
            if objgen in seen:
                return
            seen.add(objgen)

        try:
            is_media_clip_data = obj.get("/S") == pikepdf.Name("/MCD")
        except Exception:
            is_media_clip_data = False

        if is_media_clip_data:
            ct_value = _derive_media_clip_type(obj)
            if ct_value and not _string_or_empty(obj.get("/CT")):
                obj["/CT"] = pikepdf.String(ct_value)
                changes += 1
            alt = obj.get("/Alt")
            if not isinstance(alt, pikepdf.Array) or len(alt) < 2:
                obj["/Alt"] = pikepdf.Array([
                    pikepdf.String(""),
                    pikepdf.String(_derive_media_clip_label(obj)),
                ])
                changes += 1

        for _, value in obj.items():
            _walk(value)

    _walk(pdf.Root)
    return changes


def _add_bookmarks(pdf: pikepdf.Pdf, headings: list[dict]):
    """Build bookmarks (outlines) from heading elements."""
    if not headings:
        return 0

    valid_headings = [h for h in headings if 0 <= h["page_index"] < len(pdf.pages)]
    if not valid_headings:
        return 0

    items = [pikepdf.OutlineItem(h["text"], h["page_index"]) for h in valid_headings]
    if not items:
        return 0

    root_items = []
    stack: list[tuple[int, pikepdf.OutlineItem]] = []

    for h, item in zip(valid_headings, items, strict=True):
        level = h["level"]

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


def _tag_link_annotations(
    page: pikepdf.Page,
    page_ref: pikepdf.Object,
    builder: StructTreeBuilder,
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
        if not str(annotation.get("/Contents", "")).strip():
            annotation["/Contents"] = pikepdf.String(_infer_link_contents(annotation))

        if not getattr(annotation, "is_indirect", False):
            try:
                annotation = builder.pdf.make_indirect(pikepdf.Dictionary(annotation))
                annots[idx] = annotation
            except Exception:
                continue

        if builder.add_link_annotation(page_ref, annotation):
            tagged += 1

    if has_link_annotation:
        page["/Tabs"] = pikepdf.Name("/S")

    return tagged


def _tag_widget_annotations(
    page: pikepdf.Page,
    page_ref: pikepdf.Object,
    builder: StructTreeBuilder,
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

        if not getattr(annotation, "is_indirect", False):
            try:
                annotation = builder.pdf.make_indirect(pikepdf.Dictionary(annotation))
                annots[idx] = annotation
            except Exception:
                continue

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
            contents = str(annotation.get("/Contents", "")).strip()
            if contents:
                return contents
        except Exception:
            pass
        return "Text note"

    # Free text annotation
    if subtype_str == "/FreeText":
        try:
            contents = str(annotation.get("/Contents", "")).strip()
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
            name = str(annotation.get("/Name", "")).strip().lstrip("/")
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
            subtype = annotation.get("/Subtype")
            if subtype == pikepdf.Name("/Widget"):
                continue
            if str(annotation.get("/Contents", "")).strip():
                continue
            if subtype == pikepdf.Name("/Link"):
                annotation["/Contents"] = pikepdf.String(_infer_link_contents(annotation))
            else:
                annotation["/Contents"] = pikepdf.String(
                    _infer_annotation_contents(annotation, subtype)
                )
        except Exception:
            continue


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

async def tag_pdf(
    input_path: Path,
    output_path: Path,
    structure_json: dict,
    alt_texts: list[dict] | None = None,
    language: str = "en",
    original_filename: str = "",
) -> TaggingResult:
    """Write PDF/UA structure tags into the PDF."""

    def _tag():
        tags_added = 0
        parser_doc = None
        docling_parse_cache: dict[int, list[dict]] = {}

        try:
            parser_doc = DoclingPdfParser(loglevel="fatal").load(
                str(input_path),
                lazy=True,
            )
        except Exception as e:
            logger.warning(f"docling-parse unavailable for {input_path.name}: {e}")

        with pikepdf.open(str(input_path)) as pdf:
            # 1. Mark PDF as tagged
            if "/MarkInfo" not in pdf.Root:
                pdf.Root["/MarkInfo"] = pikepdf.Dictionary({
                    "/Marked": True,
                    "/Suspects": False,
                })
            else:
                pdf.Root["/MarkInfo"]["/Marked"] = True
                pdf.Root["/MarkInfo"]["/Suspects"] = False
            tags_added += 1

            # 2. Set document language
            pdf.Root["/Lang"] = pikepdf.String(language)
            tags_added += 1

            # 3. Set document title
            _reset_xmp_metadata(pdf)
            title = _set_document_title(pdf, structure_json, original_filename)
            title_set = bool(title)
            _set_pdfua_identification(pdf)
            _normalize_optional_content_configs(pdf)
            _remove_dynamic_xfa(pdf)
            _normalize_embedded_file_specs(pdf)
            _normalize_type1_font_charsets(pdf)
            _normalize_media_clip_data_dicts(pdf)

            # 4. Build structure tree
            builder = StructTreeBuilder(pdf, doc_lang=language)
            builder.setup()
            tags_added += 1

            # Build figure override maps from review decisions.
            alt_lookup: dict[int, str] = {}
            decorative_figures: set[int] = set()
            for alt in alt_texts or []:
                fig_idx = alt.get("figure_index")
                if not isinstance(fig_idx, int):
                    continue
                status = str(alt.get("status", "")).strip().lower()
                if status == "rejected" or bool(alt.get("decorative")):
                    decorative_figures.add(fig_idx)
                    continue
                text = alt.get("text")
                if isinstance(text, str) and text.strip():
                    alt_lookup[fig_idx] = text.strip()

            # Group elements by page
            elements = structure_json.get("elements", [])
            _normalize_heading_hierarchy(elements)
            pages_elements: dict[int, list[dict]] = {}
            for elem in elements:
                pg = elem.get("page", 0)
                if pg not in pages_elements:
                    pages_elements[pg] = []
                pages_elements[pg].append(elem)

            figures_tagged = 0
            headings_tagged = 0
            tables_tagged = 0
            lists_tagged = 0
            links_tagged = 0
            annotations_tagged = 0
            decorative_figures_artifacted = 0

            for page_index, page in enumerate(pdf.pages):
                page_elems = pages_elements.get(page_index, [])
                if page_elems:
                    page_lines = _build_docling_parse_page_lines(
                        parser_doc,
                        page_index,
                        docling_parse_cache,
                    )
                    _rewrite_content_stream(
                        pdf, page, page_index, page_elems,
                        builder, page.obj, alt_lookup,
                        decorative_figures=decorative_figures,
                        docling_page_lines=page_lines,
                    )

                    for elem in page_elems:
                        t = elem.get("type", "")
                        if t == "figure":
                            if elem.get("figure_index") in decorative_figures:
                                decorative_figures_artifacted += 1
                            else:
                                figures_tagged += 1
                        elif t == "heading":
                            headings_tagged += 1
                        elif t == "table":
                            tables_tagged += 1
                        elif t == "list_item":
                            lists_tagged += 1
                elif _should_artifact_nonsemantic_page_content(input_path, page, page_index):
                    _rewrite_content_stream(
                        pdf, page, page_index, [],
                        builder, page.obj, alt_lookup,
                        decorative_figures=decorative_figures,
                        docling_page_lines=None,
                    )

                _ensure_annotation_baseline(page)
                links_tagged += _tag_link_annotations(page, page.obj, builder)
                _tag_widget_annotations(page, page.obj, builder)
                annotations_tagged += _tag_generic_annotations(page, page.obj, builder)

            # 5. Finalize structure tree
            builder.finalize()

            # 6. Add bookmarks from headings
            bookmarks_added = _add_bookmarks(pdf, builder._headings)

            pdf.save(str(output_path))

        total_tags = tags_added + builder._struct_elems_created
        logger.info(
            f"Tagged PDF saved: {output_path.name} "
            f"({total_tags} tags, {headings_tagged} headings, "
            f"{figures_tagged} figures, {decorative_figures_artifacted} decorative figures, "
            f"{tables_tagged} tables, {links_tagged} links, "
            f"{annotations_tagged} annotations, "
            f"{bookmarks_added} bookmarks)"
        )

        return TaggingResult(
            output_path=output_path,
            tags_added=total_tags,
            lang_set=True,
            marked=True,
            struct_elems_created=builder._struct_elems_created,
            figures_tagged=figures_tagged,
            headings_tagged=headings_tagged,
            tables_tagged=tables_tagged,
            lists_tagged=lists_tagged,
            links_tagged=links_tagged,
            annotations_tagged=annotations_tagged,
            decorative_figures_artifacted=decorative_figures_artifacted,
            bookmarks_added=bookmarks_added,
            title_set=title_set,
        )

    return await asyncio.to_thread(_tag)
