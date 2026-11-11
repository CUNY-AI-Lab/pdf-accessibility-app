"""Shared PDF object, text, matrix and bounding-box helpers and target types."""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pikepdf

from app.pipeline.page_glyphs import MeasuredGlyph

if TYPE_CHECKING:
    from app.pipeline.tagger_content_geometry import ContentRegion

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants and result dataclass
# ──────────────────────────────────────────────────────────────────────────────

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

BLANK_PAGE_NONWHITE_THRESHOLD = 245
BLANK_PAGE_MAX_INK_RATIO = 0.001
OCR_NOISE_ONLY_OPERATORS = frozenset({
    "Do", "re", "W*", "n", "INLINE IMAGE", "w", "m", "l", "S",
})
LINK_TEXT_ELEMENT_TYPES = frozenset({
    "heading",
    "paragraph",
    "list_item",
    "caption",
    "note",
    "code",
    "formula",
    "reference",
    "bib_entry",
    "toc_caption",
    "toc_item",
    "toc_item_table",
})
TEXT_SHOW_OPERATORS = frozenset({"Tj", "TJ", "'", '"'})
CONTENT_PAINT_OPERATORS = frozenset({
    "Tj",
    "TJ",
    "'",
    '"',
    "Do",
    "f",
    "F",
    "f*",
    "S",
    "s",
    "B",
    "B*",
    "b",
    "b*",
    "sh",
    "INLINE IMAGE",
})
# Path construction, clipping, and painting operators: a path object runs
# from its first construction operator to its painting operator and is
# marked as a whole.
PATH_CONSTRUCTION_OPERATORS = frozenset({"m", "l", "c", "v", "y", "h", "re", "W", "W*"})
PATH_PAINTING_OPERATORS = frozenset({"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"})
LINK_SAFE_MAX_CHARS = 160
LINK_SAFE_MAX_WORDS = 18
BOOKMARK_SPACED_CAPS_RE = re.compile(r"\b(?:[A-Z]\s+){2,}[A-Z](?:\s+\d+)*\b")
WEAK_FIGURE_ALT_RE = re.compile(
    r"^(?:element\s+\d+|figure\s*\d*|image\s*\d*|graphic\s*\d*|picture\s*\d*|illustration\s*\d*|decorative)$",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────────
# Shared pikepdf helpers
# ──────────────────────────────────────────────────────────────────────────────

def _obj_key(obj) -> tuple[int, int] | None:
    """Return the (objgen) identity tuple for a pikepdf indirect object.

    Returns None for direct (inline) objects — they have no object number and
    ``obj.objgen`` raises. Genuine errors (malformed references) are logged at
    debug so unexpected failures are traceable without noise.
    """
    try:
        obj_num, gen_num = obj.objgen
        if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
            return obj_num, gen_num
    except Exception as exc:
        logger.debug("_obj_key: could not read objgen on %r: %s", type(obj).__name__, exc)
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


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_text(text: str) -> str:
    """Lowercase and collapse non-word characters for fuzzy text matching."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def _collapse_spaced_caps(text: Any) -> str:
    collapsed = " ".join(str(text or "").split()).strip()
    if not collapsed:
        return ""

    def repl(match: re.Match[str]) -> str:
        tokens = match.group(0).split()
        letters = [token for token in tokens if len(token) == 1 and token.isalpha() and token.isupper()]
        suffix = tokens[len(letters):]
        result = "".join(letters)
        if suffix:
            result = " ".join([result, *suffix])
        return result

    return BOOKMARK_SPACED_CAPS_RE.sub(repl, collapsed)


def _normalize_title_like_text(text: Any) -> str:
    return _normalize_text(_collapse_spaced_caps(text))


def _decode_pdf_text_operand(value: Any) -> str:
    """Decode a content stream text operand into a best-effort string."""
    if isinstance(value, pikepdf.String):
        text = str(value)
    elif isinstance(value, bytes):
        text = value.decode("latin-1", errors="ignore")
    elif isinstance(value, (int, float)):
        return ""
    else:
        text = str(value) if value is not None else ""

    if "\x00" in text:
        without_nuls = text.replace("\x00", "")
        if without_nuls.strip():
            text = without_nuls
    return "".join(
        char if char in "\n\r\t" or ord(char) >= 32 else " "
        for char in text
    )


def _extract_text_from_operands(op: str, operands: list[Any]) -> str:
    """Extract visible text from Tj/TJ/'/\" operators."""
    if op == "Tj":
        return _decode_pdf_text_operand(operands[0]) if operands else ""
    if op == "TJ" and operands:
        arr = operands[0]
        if isinstance(arr, pikepdf.Array):
            # pikepdf gives TJ position adjustments as int or Decimal.
            return "".join(
                _decode_pdf_text_operand(item)
                for item in arr
                if not isinstance(item, (int, float, Decimal))
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


def _rect_array_to_bbox(rect: Any) -> dict[str, float] | None:
    """Convert a PDF /Rect array into a bbox dict."""
    if not isinstance(rect, pikepdf.Array) or len(rect) < 4:
        return None
    try:
        x0 = float(rect[0])
        y0 = float(rect[1])
        x1 = float(rect[2])
        y1 = float(rect[3])
    except Exception:
        return None
    left = min(x0, x1)
    bottom = min(y0, y1)
    right = max(x0, x1)
    top = max(y0, y1)
    if right <= left or top <= bottom:
        return None
    return {
        "l": left,
        "b": bottom,
        "r": right,
        "t": top,
    }


def _normalize_annotation_rect(annotation: pikepdf.Object) -> bool:
    """Canonicalize annotation /Rect coordinates when producers wrote them reversed."""
    try:
        bbox = _rect_array_to_bbox(annotation.get("/Rect"))
    except Exception:
        return False
    if not bbox:
        return False
    normalized = [bbox["l"], bbox["b"], bbox["r"], bbox["t"]]
    try:
        current = [float(annotation["/Rect"][idx]) for idx in range(4)]
    except Exception:
        current = []
    if current == normalized:
        return False
    annotation["/Rect"] = pikepdf.Array(normalized)
    return True


def _region_bbox(region: "ContentRegion") -> dict[str, float] | None:
    """Return explicit region bbox when available."""
    return region.bbox


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


def _bbox_intersection_box(
    a: dict[str, float] | None,
    b: dict[str, float] | None,
) -> dict[str, float] | None:
    """Return the intersection bbox, if any."""
    if not a or not b:
        return None
    x0 = max(a["l"], b["l"])
    y0 = max(a["b"], b["b"])
    x1 = min(a["r"], b["r"])
    y1 = min(a["t"], b["t"])
    if x1 <= x0 or y1 <= y0:
        return None
    return {"l": x0, "b": y0, "r": x1, "t": y1}


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


def _element_boxes(item: dict[str, Any]) -> list[dict[str, float]]:
    """Every box an element occupies on the page. Docling gives a paragraph
    that continues in another column one box for each part."""
    return [
        box
        for box in (item.get("bbox"), *(item.get("extra_bboxes") or ()))
        if isinstance(box, dict)
    ]


@dataclass(frozen=True)
class ContentTarget:
    """A box content can belong to: one of a text or artifact element's
    boxes, a table cell, or a link nested inside either. Targets of the
    same element, cell, and link are the same owner."""

    elem_idx: int
    cell_key: tuple[int, int] | None = None
    box: dict[str, float] = field(default_factory=dict, compare=False, hash=False)
    cell: dict | None = field(default=None, compare=False, hash=False)
    list_label: bool = False
    link_key: tuple[int, int] | None = None
    annotation: pikepdf.Object | None = field(default=None, compare=False, hash=False)


def _glyph_key(glyph: MeasuredGlyph) -> tuple:
    box = glyph.bbox
    return (glyph.text, *(round(box[side], 2) for side in "lbrt"))


def _as_positive_int(value: Any, default: int = 1) -> int:
    """Best-effort positive integer coercion."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _table_cell_bbox(table: dict, cell: dict) -> dict[str, float] | None:
    """The cell's own box when the structure step has one (Docling's, fitted
    to the cell text); otherwise an even share of the table's box."""
    if isinstance(cell.get("bbox"), dict):
        return cell["bbox"]
    table_bbox = table.get("bbox")
    if not isinstance(table_bbox, dict):
        return None
    num_rows = _as_positive_int(table.get("num_rows", 0), default=0)
    num_cols = _as_positive_int(table.get("num_cols", 0), default=0)
    if num_rows <= 0 or num_cols <= 0:
        return None

    left = _safe_float(table_bbox.get("l"))
    bottom = _safe_float(table_bbox.get("b"))
    right = _safe_float(table_bbox.get("r"))
    top = _safe_float(table_bbox.get("t"))
    if right <= left or top <= bottom:
        return None

    row = _as_positive_int(cell.get("row", 0), default=0)
    col = _as_positive_int(cell.get("col", 0), default=0)
    row_span = _as_positive_int(cell.get("row_span", 1), default=1)
    col_span = _as_positive_int(cell.get("col_span", 1), default=1)
    if row >= num_rows or col >= num_cols:
        return None

    row_h = (top - bottom) / num_rows
    col_w = (right - left) / num_cols
    return {
        "l": left + col * col_w,
        "r": left + min(num_cols, col + col_span) * col_w,
        "t": top - row * row_h,
        "b": top - min(num_rows, row + row_span) * row_h,
    }


def _matrix_tuple(value: Any) -> tuple[float, float, float, float, float, float]:
    if isinstance(value, pikepdf.Array) and len(value) >= 6:
        return tuple(_safe_float(item) for item in list(value)[:6])
    if isinstance(value, (list, tuple)) and len(value) >= 6:
        return tuple(_safe_float(item) for item in list(value)[:6])
    return IDENTITY


def _lookup_xobject(resources, name: str) -> pikepdf.Object | None:
    if resources is None or not hasattr(resources, "get"):
        return None
    xobjects = resources.get("/XObject")
    if xobjects is None or not hasattr(xobjects, "get"):
        return None
    try:
        return xobjects.get(name) or xobjects.get(pikepdf.Name(name))
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Spatial matching — correlate content regions with Docling elements
# ──────────────────────────────────────────────────────────────────────────────

def _bbox_center(bbox: dict) -> tuple[float, float]:
    """Get center point of a Docling bounding box {l, b, r, t}."""
    return (
        (bbox["l"] + bbox["r"]) / 2,
        (bbox["b"] + bbox["t"]) / 2,
    )
