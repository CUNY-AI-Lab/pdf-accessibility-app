"""Recover live glyphs in decorative images and text boxes mistaken for figures."""

import logging
import re
from collections.abc import Callable
from pathlib import Path

from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import MeasuredTextOp
from app.pipeline.tagger_geometry import (
    ContentTarget,
    _bbox_area,
    _bbox_intersection_box,
)
from app.pipeline.tagger_ownership import _owner_of

logger = logging.getLogger(__name__)

# A figure is text set on a panel or banner, not a picture with labels, when
# hiding the page's live text leaves its region less than this share of its
# detail (a chart keeps its bars, a diagram its lines, an icon beside a
# handle its outline) ...
FIGURE_TEXT_GROUND_DETAIL_SHARE = 0.15
# ... and of its ink (a photo or a pie keeps most of its area) ...
FIGURE_TEXT_GROUND_INK_SHARE = 0.5
# ... and its text reads as words: at least this share of its characters are in
# words of two or more letters, which a chart plotted with glyphs is not.
FIGURE_TEXT_WORD_SHARE = 0.5
_WORD_RE = re.compile(r"[^\W\d_]{2,}")


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
    plain_ground: Callable[[list[dict]], set[int]] | None = None,
) -> list[dict]:
    """Separate a figure's live text from its image semantics.

    Dense glyph coverage is evidence of a text box, regardless of a model's
    classification, and so is a region that shows little but its live text
    (``plain_ground`` names those among the given figures, by position).
    Sparse labels remain readable alongside the figure's description,
    including text in OCR Form XObjects.
    The proxies are appended after ownership/order planning so existing
    elements and their MCIDs retain their identity.
    """
    recovered = []
    glyphs = [g for op in measured.values() for g in op.glyphs if g.text]
    with_text: list[tuple[dict, dict, bool]] = []
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
        with_text.append((elem, box, text_box))
    sparse = [
        elem
        for elem, _box, text_box in with_text
        if not text_box and elem.get("figure_index") not in decorative
    ]
    on_ground = plain_ground(sparse) if plain_ground is not None and sparse else set()
    grounded = {id(sparse[position]) for position in on_ground}
    for elem, box, text_box in with_text:
        text_box = text_box or id(elem) in grounded
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


def _ink(image, background: tuple[int, int, int]) -> int:
    """Pixels of ``image`` that differ from ``background``."""
    from PIL import Image, ImageChops

    red, green, blue = ImageChops.difference(
        image, Image.new("RGB", image.size, background)
    ).split()
    differs = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
        lambda value: 255 if value > 40 else 0
    )
    return differs.histogram()[255]


def _ground_of_text(shown, ground) -> bool:
    """Whether a region, ``shown`` with and ``ground`` without its live
    text, is text on a ground: little detail and little ink remain."""
    from app.pipeline.structure import _image_detail

    shown_detail = _image_detail(shown)
    if shown_detail <= 0 or _image_detail(ground) >= FIGURE_TEXT_GROUND_DETAIL_SHARE * shown_detail:
        return False
    shown, ground = shown.copy(), ground.copy()
    shown.thumbnail((128, 128))
    ground.thumbnail((128, 128))
    # The ground's most common colour, at five bits a channel, is its background.
    coarse = ground.point(lambda value: (value >> 3) * 8 + 4)
    _count, background = max(coarse.getcolors(coarse.width * coarse.height) or [(0, (255,) * 3)])
    shown_ink = _ink(shown, background)
    return shown_ink > 0 and _ink(ground, background) < FIGURE_TEXT_GROUND_INK_SHARE * shown_ink


def _reads_as_words(text: str) -> bool:
    characters = sum(1 for character in text if not character.isspace())
    words = sum(len(word) for word in _WORD_RE.findall(text))
    return characters > 0 and words >= FIGURE_TEXT_WORD_SHARE * characters


def _figures_on_plain_ground(
    pdf_path: Path, page_index: int, frame: PageFrame, figures: list[dict]
) -> set[int]:
    """Positions in ``figures`` whose region shows little but readable live text.

    The page is rendered as displayed, once as it is and once with every text
    object, in form XObjects too, drawn invisible, and each figure's region
    is compared between the two (see _ground_of_text); its text is what
    pdfium reads in the box. A page that cannot be rendered keeps its figures.
    """
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c

        document = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        logger.warning("Could not open the PDF to compare figures with their text", exc_info=True)
        return set()
    try:
        page = document[page_index]
        text_page = page.get_textpage()
        texts = [
            text_page.get_text_bounded(
                left=figure["bbox"]["l"],
                bottom=figure["bbox"]["b"],
                right=figure["bbox"]["r"],
                top=figure["bbox"]["t"],
            )
            for figure in figures
        ]
        scale = min(2.0, 4000 / max(*page.get_size(), 1.0))
        with_text = page.render(scale=scale, draw_annots=False).to_pil().convert("RGB")

        def hide(count: int, child) -> None:
            for index in range(count):
                obj = child(index)
                kind = pdfium_c.FPDFPageObj_GetType(obj)
                if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
                    pdfium_c.FPDFTextObj_SetTextRenderMode(
                        obj, pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE
                    )
                elif kind == pdfium_c.FPDF_PAGEOBJ_FORM:
                    hide(
                        pdfium_c.FPDFFormObj_CountObjects(obj),
                        lambda position, form=obj: pdfium_c.FPDFFormObj_GetObject(form, position),
                    )

        hide(
            pdfium_c.FPDFPage_CountObjects(page),
            lambda position: pdfium_c.FPDFPage_GetObject(page, position),
        )
        without_text = page.render(scale=scale, draw_annots=False).to_pil().convert("RGB")
        found = set()
        for position, figure in enumerate(figures):
            box = frame.to_docling(figure["bbox"])
            pixels = (
                max(0, round(box["l"] * scale)),
                max(0, round(with_text.height - box["t"] * scale)),
                min(with_text.width, round(box["r"] * scale)),
                min(with_text.height, round(with_text.height - box["b"] * scale)),
            )
            if pixels[2] - pixels[0] < 2 or pixels[3] - pixels[1] < 2:
                continue
            if _reads_as_words(texts[position]) and _ground_of_text(
                with_text.crop(pixels), without_text.crop(pixels)
            ):
                found.add(position)
        return found
    except Exception:
        logger.warning(
            "Could not compare figures with their text on page %s", page_index + 1, exc_info=True
        )
        return set()
    finally:
        document.close()


def _recover_figure_text_owners(
    owners: dict[tuple, ContentTarget | None],
    recovered: dict[tuple, ContentTarget | None],
) -> None:
    # Preserve paragraphs, tables, links, and explicit furniture ownership.
    # A decorative decision cannot claim their glyphs, even when boxes overlap.
    for key, target in recovered.items():
        if target is not None and owners.get(key) is None:
            owners[key] = target
