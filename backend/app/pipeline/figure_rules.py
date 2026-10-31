"""Conservative local evidence for suppressing non-content figures.

Unknown visibility stays unknown. Bounding-box overlap alone never proves
occlusion: only a later, opaque rectangular PDF fill can cover an image.
"""

import hashlib
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pikepdf
from PIL import Image

from app.pipeline.structure import FigureInfo
from app.services.pdf_objects import pdf_dictionary, pdf_named_resource

TINY_SIDE_PT = 12.0
POSITION_TOLERANCE_PT = 2.0
REPEATED_MIN_PAGES = 3
_REFERENCE = re.compile(r"\b(?:fig(?:ure)?s?\.?|diagram|chart|photo(?:graph)?)\s*\d+", re.I)


def bounds(bbox: dict | None) -> tuple[float, float, float, float] | None:
    try:
        box = tuple(float(bbox[key]) for key in ("l", "b", "r", "t"))
        if all(math.isfinite(value) for value in box) and box[2] > box[0] and box[3] > box[1]:
            return box
    except (TypeError, KeyError, ValueError):
        pass
    return None


def content_protected(figure: FigureInfo, structure: dict[str, Any]) -> bool:
    if str(figure.caption or "").strip():
        return True
    # Protect references anywhere on the same page, even when the structure
    # missed a caption or the figure's classification. This errs toward content.
    if figure.classification and figure.classification not in {"logo", "icon", "other"}:
        return True
    return any(
        element.get("page") == figure.page
        and (element.get("figure_index") == figure.index and element.get("caption")
             or _REFERENCE.search(str(element.get("text") or "")))
        for element in structure.get("elements", [])
        if isinstance(element, dict)
    )


def _pixel_hash(path: Path) -> tuple[str, bool]:
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        digest = hashlib.sha256(str(rgba.size).encode() + rgba.tobytes()).hexdigest()
        invisible = rgba.getchannel("A").getextrema()[1] == 0
    return digest, invisible


def _covered_boxes(pdf_path: Path | None) -> dict[int, list[tuple[float, ...]]]:
    """Prove coverage only for simple, unclipped, opaque page streams.

    Skip pages with optional content, graphics-state overrides, clipping,
    rotated/sheared transforms or nested Form XObjects rather than guessing.
    """
    result: dict[int, list[tuple[float, ...]]] = {}
    supported_operators = {
        "q", "Q", "cm", "Do", "re", "f", "F", "f*", "n", "S", "s", "B", "B*", "b", "b*",
        "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "i",
        "BT", "ET", "Tc", "Tw", "Tz", "TL", "Tf", "Ts", "Td", "TD", "Tm", "T*",
        "Tj", "TJ", "'", '"', "BMC", "EMC", "MP", "DP",
    }
    if pdf_path is None:
        return result
    try:
        with pikepdf.open(pdf_path) as pdf:
            if pdf.Root.get("/OCProperties"):
                return result
            for page_index, page in enumerate(pdf.pages):
                ctm = (1.0, 1.0, 0.0, 0.0)  # axis-aligned scale and translation
                stack = []
                images: list[tuple[float, ...]] = []
                covered: list[tuple[float, ...]] = []
                rectangles: list[tuple[float, ...]] = []
                supported = True
                for instruction in pikepdf.parse_content_stream(page):
                    op = str(instruction.operator)
                    operands = instruction.operands
                    # In particular, pattern fills (cs/scn) may contain holes.
                    if op not in supported_operators:
                        supported = False
                        break
                    if op == "q":
                        stack.append(ctm)
                    elif op == "Q":
                        if not stack:
                            supported = False
                            break
                        ctm = stack.pop()
                    elif op == "cm":
                        a, b, c, d, e, f = map(float, operands)
                        if b or c or a <= 0 or d <= 0:
                            supported = False
                            break
                        sx, sy, tx, ty = ctm
                        ctm = (sx * a, sy * d, sx * e + tx, sy * f + ty)
                    elif op == "Do":
                        resources = pdf_dictionary(page.get("/Resources"))
                        obj = pdf_named_resource(
                            resources.get("/XObject") if resources is not None else None,
                            operands[0],
                        )
                        if obj is None or obj.get("/Subtype") != pikepdf.Name("/Image"):
                            supported = False
                            break
                        sx, sy, tx, ty = ctm
                        images.append((tx, ty, tx + sx, ty + sy))
                    elif op == "re":
                        x, y, width, height = map(float, operands)
                        sx, sy, tx, ty = ctm
                        if width > 0 and height > 0:
                            rectangles.append((tx + sx*x, ty + sy*y, tx + sx*(x+width), ty + sy*(y+height)))
                        else:
                            supported = False
                            break
                    elif op in {"f", "F", "f*"}:
                        if len(rectangles) == 1:
                            left, bottom, right, top = rectangles[0]
                            covered.extend(box for box in images if left <= box[0] and bottom <= box[1] and right >= box[2] and top >= box[3])
                        rectangles.clear()
                    elif op in {"n", "S", "s", "B", "B*", "b", "b*"}:
                        rectangles.clear()
                if supported:
                    # A later visible occurrence at the same location would
                    # invalidate this proof, so require a unique image box.
                    result[page_index] = [box for box in covered if images.count(box) == 1]
    except (OSError, TypeError, ValueError, pikepdf.PdfError):
        return {}
    return result


def _page_bounds(pdf_path: Path | None) -> dict[int, tuple[float, ...]]:
    if pdf_path is None:
        return {}
    try:
        with pikepdf.open(pdf_path) as pdf:
            return {index: tuple(map(float, page.cropbox)) for index, page in enumerate(pdf.pages)}
    except (OSError, TypeError, ValueError, pikepdf.PdfError):
        return {}


def _page_furniture(figure: FigureInfo, page_boxes: dict[int, tuple[float, ...]]) -> bool:
    box = bounds(figure.bbox)
    page = page_boxes.get(figure.page)
    if not box or not page:
        return False
    width, height = page[2] - page[0], page[3] - page[1]
    area = (box[2] - box[0]) * (box[3] - box[1])
    return (
        width > 0 and height > 0 and area <= width * height * 0.02
        and (box[3] <= page[1] + height * 0.1 or box[1] >= page[3] - height * 0.1)
        and page[0] <= box[0] < box[2] <= page[2]
        and page[1] <= box[1] < box[3] <= page[3]
    )


def decorative_reasons(
    figures: list[FigureInfo], *, structure: dict[str, Any], pdf_path: Path | None = None,
) -> dict[int, str]:
    reasons: dict[int, str] = {}
    repeated: dict[str, list[FigureInfo]] = defaultdict(list)
    covered = _covered_boxes(pdf_path)
    page_boxes = _page_bounds(pdf_path)
    for figure in figures:
        if not figure.path.is_file() or content_protected(figure, structure):
            continue
        box = bounds(figure.bbox)
        if box and box[2] - box[0] < TINY_SIDE_PT and box[3] - box[1] < TINY_SIDE_PT:
            reasons[figure.index] = "tiny_image: both displayed sides are under 12 pt."
            continue
        try:
            pixel_hash, invisible = _pixel_hash(figure.path)
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
        if invisible:
            reasons[figure.index] = "invisible_image: every crop pixel has zero alpha."
        elif box and any(
            all(abs(a - b) <= 0.01 for a, b in zip(box, hidden, strict=True))
            for hidden in (covered.get(figure.page, []) if figure.page is not None else [])
        ):
            reasons[figure.index] = "fully_covered: a later opaque PDF rectangle covers the entire image."
        elif box and isinstance(figure.page, int) and _page_furniture(figure, page_boxes):
            repeated[pixel_hash].append(figure)

    for group in repeated.values():
        for figure in group:
            box = bounds(figure.bbox)
            matches = [other for other in group if all(
                abs(a - b) <= POSITION_TOLERANCE_PT
                for a, b in zip(box, bounds(other.bbox), strict=True)
            )]
            pages = {other.page for other in matches}
            if len(pages) >= REPEATED_MIN_PAGES:
                reasons[figure.index] = (
                    f"repeated_image: identical pixels on {len(pages)} pages, "
                    "all four bbox coordinates within 2 pt; at most 2% of page area "
                    "and inside the top or bottom 10% margin."
                )
    return reasons
