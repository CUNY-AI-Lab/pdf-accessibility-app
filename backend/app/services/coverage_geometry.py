"""Page-scoped Figure rectangles for the tagged-text coverage denominator."""

from __future__ import annotations

import math
from pathlib import Path

import pikepdf

from app.pipeline.page_glyphs import StreamKey
from app.pipeline.pdfium_text import Box, PdfiumText
from app.services.pdf_objects import pdf_page_key
from app.services.structure_text import _form_paths, _standard_role


def _layout_box(node: pikepdf.Dictionary) -> Box | None:
    attributes = node.get("/A")
    owners = attributes if isinstance(attributes, pikepdf.Array) else [attributes]
    rectangles = [node.get("/BBox")] + [
        owner.get("/BBox") for owner in owners if isinstance(owner, pikepdf.Dictionary)
    ]
    for rect in rectangles:
        if isinstance(rect, pikepdf.Array) and len(rect) == 4:
            values = tuple(float(value) for value in rect)
            if all(math.isfinite(value) for value in values) and values[0] < values[2] and values[1] < values[3]:
                return values
    return None


def figure_boxes_for_coverage(pdf_path: Path, raw: PdfiumText) -> list[list[Box]]:
    """Prefer a Figure's direct or Layout BBox. Otherwise union its painted
    descendant MCID bounds on each referenced page, including clipped images.
    Page and Form MCIDs remain scoped separately; an unreferenced picture
    or an extraction-layout rectangle is not a tagged Figure."""
    with pikepdf.open(pdf_path) as pdf:
        boxes: list[list[Box]] = [[] for _ in pdf.pages]
        root = pdf.Root.get("/StructTreeRoot")
        if root is None:
            return boxes
        pages = {page.obj.objgen: index for index, page in enumerate(pdf.pages)}
        role_map = root.get("/RoleMap")
        paths: dict[int, dict[tuple[int, int], StreamKey]] = {}
        bounds: dict[int, dict[tuple[StreamKey, int], list[Box]]] = {}

        def refs(node, page):
            if isinstance(node, int):
                if page is not None:
                    yield page, (), node
            elif isinstance(node, pikepdf.Array):
                for kid in node:
                    yield from refs(kid, page)
            elif isinstance(node, pikepdf.Dictionary):
                if (page_key := pdf_page_key(node.get("/Pg"))) is not None:
                    page = pages.get(page_key, page)
                if node.get("/Type") == "/MCR":
                    if page is not None:
                        stream = node.get("/Stm")
                        if page not in paths:
                            paths[page] = _form_paths(pdf.pages[page].obj)
                        path = paths[page].get(stream.objgen) if stream is not None else ()
                        if path is not None:
                            yield page, path, int(node.MCID)
                else:
                    yield from refs(node.get("/K"), page)

        def walk(node, page=None):
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    walk(kid, page)
                return
            if not isinstance(node, pikepdf.Dictionary):
                return
            if (page_key := pdf_page_key(node.get("/Pg"))) is not None:
                page = pages.get(page_key, page)
            if _standard_role(node, role_map) == "Figure":
                references = list(refs(node.get("/K"), page))
                rect = _layout_box(node)
                rect_pages = {page} if page is not None else {ref[0] for ref in references}
                if rect is not None and len(rect_pages) == 1:
                    boxes[next(iter(rect_pages))].append(rect)
                else:
                    painted: dict[int, list[Box]] = {}
                    for number, path, mcid in references:
                        if number not in bounds:
                            bounds[number] = raw.marked_bounds(number, pdf.pages[number].obj)
                        painted.setdefault(number, []).extend(bounds[number].get((path, mcid), []))
                    for number, parts in painted.items():
                        if parts:
                            boxes[number].append((
                                min(box[0] for box in parts), min(box[1] for box in parts),
                                max(box[2] for box in parts), max(box[3] for box in parts),
                            ))
            walk(node.get("/K"), page)

        walk(root.get("/K"))
    return boxes
