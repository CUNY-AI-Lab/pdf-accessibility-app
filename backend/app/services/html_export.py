"""Semantic, offline HTML from the final PDF's reviewed structure tree.

No extraction model or source JSON is used. Figure alternatives are attributes,
so (like structure_text) body text does not include them. Two intentional text
additions are a missing document H1 and a Formula's Alt when ActualText is absent.
"""

from __future__ import annotations

import base64
import html
import math
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import pikepdf

from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.pdfium_text import Box, PdfiumText, read_page_text
from app.services.pdf_objects import pdf_integer, pdf_page_key
from app.services.structure_text import (
    BLOCK_ROLES,
    HEADING_LEVELS,
    TABLE_HTML,
    MarkedContentReader,
    _form_paths,
    _standard_role,
)

FIGURE_PPI = 150
MAX_FIGURE_SIDE = 1600
MAX_FIGURE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024  # Base64 data, including its URI prefix.

CSS = """
body { max-width: 72ch; margin: 2rem auto; padding: 0 1rem;
       font: 1.125rem/1.6 system-ui, sans-serif; color: #171717; background: white; }
table { border-collapse: collapse; margin: 1rem 0; }
th, td { border: 1px solid #666; padding: .4rem .65rem; text-align: start; }
caption { font-weight: bold; text-align: start; }
img { max-width: 100%; height: auto; }
code { white-space: pre-wrap; overflow-wrap: anywhere; }
a { overflow-wrap: anywhere; }
.pdf-labels { list-style: none; padding-inline-start: 1.5rem; }
.image-placeholder { display: inline-block; padding: .5rem; border: 1px solid #666; }
aside { border-inline-start: .25rem solid #666; padding-inline-start: 1rem; }
""".strip()


@dataclass
class _Element:
    role: str
    node: pikepdf.Dictionary
    page: int | None
    lang: str
    id: str
    children: list[_Element | str] = field(default_factory=list)


def _elements(parts: list[_Element | str]):
    for part in parts:
        if isinstance(part, _Element):
            yield part
            yield from _elements(part.children)


def _text(parts: list[_Element | str]) -> str:
    return "".join(part if isinstance(part, str) else _text(part.children) for part in parts)


def _attributes(pdf: pikepdf.Pdf, node: pikepdf.Dictionary) -> dict:
    """Resolve class attributes first, then explicit attributes (which override)."""
    result = {}

    def add(value):
        if isinstance(value, pikepdf.Array):
            for item in value:
                add(item)
        elif isinstance(value, pikepdf.Dictionary):
            result.update(value.items())

    classes = node.get("/C")
    classes = classes if isinstance(classes, pikepdf.Array) else [classes]
    class_map = pdf.Root.StructTreeRoot.get("/ClassMap") or {}
    for name in classes:
        if isinstance(name, pikepdf.Name):
            add(class_map.get(str(name)))
    add(node.get("/A"))
    return result


def _marked_ref(node, page, page_index):
    if isinstance(node, int):
        return page, None, node
    if isinstance(node, pikepdf.Dictionary) and node.get("/Type") == "/MCR":
        key = pdf_page_key(node.get("/Pg"))
        return page_index.get(key, page), node.get("/Stm"), pdf_integer(node.get("/MCID"), -1)
    return None


class _Export:
    def __init__(self, pdf: pikepdf.Pdf, reader: MarkedContentReader, visuals: PdfiumText):
        self.pdf = pdf
        self.reader = reader
        self.visuals = visuals
        self.root = pdf.Root.StructTreeRoot
        self.page_index = {page.obj.objgen: i for i, page in enumerate(pdf.pages)}
        self.count = 0
        self.image_bytes = 0
        self.bounds: dict[int, dict] = {}
        self.ids: dict[str, str] = {}
        self.page_ids: dict[int, str] = {}
        self.has_h1 = False

    def page_number(self, node, inherited: int | None) -> int | None:
        key = pdf_page_key(node.get("/Pg"))
        return self.page_index.get(key, inherited) if key is not None else inherited

    def read(self, node, page: int | None, lang: str) -> list[_Element | str]:
        if isinstance(node, pikepdf.Array):
            return [part for kid in node for part in self.read(kid, page, lang)]
        ref = _marked_ref(node, page, self.page_index)
        if ref is not None:
            number, stream, mcid = ref
            return [self.reader.read(number, stream, mcid) or ""] if number is not None and mcid >= 0 else []
        if not isinstance(node, pikepdf.Dictionary) or node.get("/Type") == "/OBJR":
            return []
        role = _standard_role(node, self.root.get("/RoleMap"))
        if role == "Artifact":
            return []
        page = self.page_number(node, page)
        lang = str(node.get("/Lang") or lang)
        self.count += 1
        elem = _Element(role, node, page, lang, f"pdf-{self.count}")
        if role in HEADING_LEVELS and HEADING_LEVELS[role] == 1:
            self.has_h1 = True
        if "/ActualText" in node:
            elem.children = [str(node.ActualText)]
            self.reader.last_read = None
        elif role == "Formula" and "/Alt" in node:
            elem.children = [str(node.Alt)]
            self.reader.last_read = None
        elif role != "Figure":
            elem.children = self.read(node.get("/K"), page, lang)
        # MCRs may be the only place a page is recorded.
        if elem.page is None:
            elem.page = next((child.page for child in _elements(elem.children) if child.page is not None), None)
            if elem.page is None:
                elem.page = self.first_page(node.get("/K"))
        if role in BLOCK_ROLES:
            self.reader.last_read = None
            return [elem, "\n"]
        return [elem]

    def first_page(self, node) -> int | None:
        if isinstance(node, pikepdf.Array):
            return next((page for kid in node if (page := self.first_page(kid)) is not None), None)
        if isinstance(node, pikepdf.Dictionary):
            key = pdf_page_key(node.get("/Pg"))
            return self.page_index.get(key) if key is not None else self.first_page(node.get("/K"))
        return None

    def index(self, parts: list[_Element | str]) -> None:
        for elem in _elements(parts):
            if "/ID" in elem.node:
                self.ids.setdefault(str(elem.node.ID), elem.id)
            if elem.page is not None and elem.role not in {"Document", "Part", "Sect", "Div"}:
                self.page_ids.setdefault(elem.page, elem.id)

    def href(self, node) -> str | None:
        if isinstance(node, pikepdf.Array):
            return next((href for kid in node if (href := self.href(kid))), None)
        if not isinstance(node, pikepdf.Dictionary):
            return None
        if node.get("/Type") != "/OBJR":
            return self.href(node.get("/K"))
        annotation = node.get("/Obj")
        if not isinstance(annotation, pikepdf.Dictionary):
            return None
        action = annotation.get("/A") or {}
        if action.get("/S") == "/URI":
            uri = str(action.get("/URI") or "")
            if any(ord(char) < 32 or ord(char) == 127 for char in uri):
                return None
            try:
                if urlsplit(uri).scheme.lower() in {"https", "http", "mailto", "tel"}:
                    return uri
            except ValueError:
                return None
        dest = annotation.get("/Dest")
        if action.get("/S") == "/GoTo":
            dest = action.get("/D")
        # Explicit local destinations only; named destinations can be added later.
        if isinstance(dest, pikepdf.Array) and len(dest):
            page = self.page_index.get(pdf_page_key(dest[0]))
            if page in self.page_ids:
                return f"#{self.page_ids[page]}"
        return None

    def figure_boxes(self, node, page) -> list[tuple[int, Box]]:
        if isinstance(node, pikepdf.Array):
            return [box for kid in node for box in self.figure_boxes(kid, page)]
        ref = _marked_ref(node, page, self.page_index)
        if ref is not None:
            number, stream, mcid = ref
            if number is None:
                return []
            if number not in self.bounds:
                self.bounds[number] = self.visuals.marked_bounds(number, self.pdf.pages[number].obj)
            path = () if stream is None else _form_paths(self.pdf.pages[number].obj).get(stream.objgen)
            return [(number, box) for box in self.bounds[number].get((path, mcid), [])]
        if isinstance(node, pikepdf.Dictionary):
            page = self.page_number(node, page)
            return self.figure_boxes(node.get("/K"), page)
        return []

    def figure(self, elem: _Element, attrs: dict[str, str]) -> str:
        # ActualText overrides Alt; show that replacement once, adjacent to a
        # decorative rendering, rather than announcing it twice as alt and text.
        actual = "/ActualText" in elem.node
        alt = "" if actual else str(elem.node.get("/Alt") or "")
        image_attrs = {} if actual else attrs
        reason = "figure region unavailable"
        src = None
        if self.image_bytes >= MAX_TOTAL_IMAGE_BYTES:
            reason = "image size budget exceeded"
        else:
            try:
                boxes = self.figure_boxes(elem.node.get("/K"), elem.page)
                attributes = _attributes(self.pdf, elem.node)
                box = attributes.get("/BBox")
                if isinstance(box, pikepdf.Array) and len(box) == 4 and elem.page is not None:
                    boxes = [(elem.page, tuple(float(value) for value in box))]
                if boxes:
                    number = boxes[0][0]
                    region = [box for page, box in boxes if page == number]
                    # A multi-page figure is not a single trustworthy crop.
                    if all(page == number for page, _ in boxes):
                        bounds = (min(b[0] for b in region), min(b[1] for b in region),
                                  max(b[2] for b in region), max(b[3] for b in region))
                        png = self.render_region(number, bounds)
                        if png is not None:
                            uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                            if len(uri) <= min(MAX_FIGURE_BYTES, MAX_TOTAL_IMAGE_BYTES - self.image_bytes):
                                src = uri
                                self.image_bytes += len(uri)
                            else:
                                reason = "image size budget exceeded"
            except (ValueError, RuntimeError, pikepdf.PdfError, OSError, OverflowError):
                # Retain alternatives when a damaged/unsupported region cannot render.
                reason = "figure rendering failed"
        if src is None:
            placeholder = {**image_attrs, "class": "image-placeholder", "role": "img", "aria-label": alt}
            if not alt:
                placeholder = {**image_attrs, "class": "image-placeholder", "aria-hidden": "true"}
            result = f'<!-- Image omitted: {reason}. -->' + _tag("span", placeholder, "")
        else:
            result = "<img" + _attrs({**image_attrs, "src": src, "alt": alt}) + ">"
        return _tag("span", attrs, result + html.escape(_text(elem.children))) if actual else result

    def render_region(self, number: int, box: Box) -> bytes | None:
        if not all(math.isfinite(value) for value in box):
            return None
        page = self.visuals.document[number]
        try:
            left, bottom, right, top = page.get_bbox()
            x0, y0, x1, y1 = max(left, box[0]), max(bottom, box[1]), min(right, box[2]), min(top, box[3])
            if x0 >= x1 or y0 >= y1:
                return None
            # Crop is in displayed canvas units, after the page's own rotation.
            rotation = page.get_rotation()
            crop = (x0 - left, y0 - bottom, right - x1, top - y1)
            if rotation == 90:
                crop = (crop[1], crop[2], crop[3], crop[0])
            elif rotation == 180:
                crop = (crop[2], crop[3], crop[0], crop[1])
            elif rotation == 270:
                crop = (crop[3], crop[0], crop[1], crop[2])
            scale = min(FIGURE_PPI / 72, MAX_FIGURE_SIDE / max(x1 - x0, y1 - y0))
            bitmap = page.render(scale=scale, crop=crop, draw_annots=False)
            try:
                with bitmap.to_pil() as image:
                    image.thumbnail((MAX_FIGURE_SIDE, MAX_FIGURE_SIDE))
                    output = BytesIO()
                    image.save(output, format="PNG")
                    return output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()

    def render(self, parts: list[_Element | str], lang: str, parent: str = "") -> str:
        return "".join(html.escape(part) if isinstance(part, str) else self.element(part, lang, parent) for part in parts)

    def element(self, elem: _Element, lang: str, parent: str) -> str:
        role, node = elem.role, elem.node
        attrs = {"id": elem.id}
        if elem.lang != lang:
            attrs["lang"] = elem.lang
        if role == "Figure":
            return self.figure(elem, attrs)
        tag = {"P": "p", "LI": "li", "LBody": "div", "Lbl": "span",
               "Note": "aside", "BlockQuote": "blockquote", "Code": "code",
               "Document": "div", "Part": "div", "Sect": "section", "Div": "div",
               **TABLE_HTML}.get(role, "span")
        if role in HEADING_LEVELS:
            tag = f"h{HEADING_LEVELS[role]}"
        if role == "Caption":
            tag = "caption" if parent == "Table" else "p"
        if role == "L":
            labels = []
            for item in elem.children:
                if not isinstance(item, _Element) or item.role != "LI":
                    continue
                label = next((child for child in item.children if isinstance(child, _Element) and child.role == "Lbl"), None)
                if label is not None:
                    labels.append(_text(label.children).strip())
                elif match := re.match(r"(\(?\d+[.)]|\(?[A-Za-z]+[.)]|[•*−-])\s+", _text(item.children).strip()):
                    # Our tagger can retain the marker in LBody, without a Lbl.
                    labels.append(match[1])
            numbered = labels and all(re.fullmatch(r"\(?(?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)[.)]?", label) for label in labels)
            numbering = str(_attributes(self.pdf, node).get("/ListNumbering"))
            tag = "ol" if numbered or (not labels and numbering in {"/Decimal", "/UpperRoman", "/LowerRoman", "/UpperAlpha", "/LowerAlpha"}) else "ul"
            if labels:
                attrs.update({"class": "pdf-labels", "role": "list"})
        if role == "Link" and (href := self.href(node.get("/K"))):
            tag, attrs["href"] = "a", href
        if role in {"TH", "TD"}:
            attributes = _attributes(self.pdf, node)
            scope = {"/Row": "row", "/Column": "col"}.get(str(attributes.get("/Scope")))
            if role == "TH" and scope:
                attrs["scope"] = scope
            for pdf_key, key in (("/RowSpan", "rowspan"), ("/ColSpan", "colspan")):
                if (span := pdf_integer(attributes.get(pdf_key))) > 1:
                    attrs[key] = str(span)
            headers = attributes.get("/Headers", node.get("/Headers"))
            if isinstance(headers, pikepdf.Array):
                mapped = [self.ids[str(header)] for header in headers if str(header) in self.ids]
                if mapped:
                    attrs["headers"] = " ".join(mapped)
        return _tag(tag, attrs, self.render(elem.children, elem.lang, role))


def _attrs(attributes: dict[str, str]) -> str:
    return "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in attributes.items())


def _tag(tag: str, attributes: dict[str, str], content: str) -> str:
    return f"<{tag}{_attrs(attributes)}>{content}</{tag}>"


def export_html(tagged_pdf: Path) -> str:
    """Export a final tagged PDF as one semantic HTML5 document, without I/O beyond the PDF.

    Untagged input is rejected rather than silently exporting an empty body.
    PNG data URIs are capped at 150 ppi, 1600 px on either side, 2 MiB each
    and 8 MiB total (after base64). Text is never truncated to meet that budget.
    """
    with pikepdf.open(tagged_pdf) as pdf, PdfGlyphReader(tagged_pdf) as glyphs, PdfiumText(tagged_pdf) as visuals:
        if not isinstance(pdf.Root.get("/StructTreeRoot"), pikepdf.Dictionary):
            raise ValueError("HTML export requires a tagged PDF structure tree")
        lang = str(pdf.Root.get("/Lang") or "und")
        with pdf.open_metadata(set_pikepdf_as_editor=False) as metadata:
            title = str(metadata.get("dc:title") or pdf.docinfo.get("/Title") or tagged_pdf.stem)
        exporter = _Export(pdf, MarkedContentReader(pdf, glyphs, read_page_text(tagged_pdf)), visuals)
        parts = exporter.read(pdf.Root.StructTreeRoot.get("/K"), None, lang)
        exporter.index(parts)
        body = exporter.render(parts, lang)
        if not exporter.has_h1:
            body = _tag("h1", {"class": "document-title"}, html.escape(title)) + "\n" + body
    return (
        '<!doctype html>\n<html' + _attrs({"lang": lang}) + '>\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">\n"
        f"<title>{html.escape(title)}</title>\n<style>{CSS}</style>\n</head>\n"
        f"<body><main>{body}</main></body>\n</html>\n"
    )
