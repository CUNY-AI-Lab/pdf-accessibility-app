"""The text a screen reader hears from a tagged PDF.

Walks the structure tree in logical order. An element with ``/ActualText``
contributes that text and hides its descendants; otherwise each marked-content
reference contributes the glyphs drawn inside it on its page, joined into words
by position. Artifacts and untagged content are not in the structure tree, so
they are not read. Figure ``/Alt`` text is left out: it describes an image, not
page text. Text drawn inside a Figure is not read either, unless
``figure_text`` asks for the reading some screen readers give a Figure that
has no ``/Alt``: its text, as if it were not a Figure. Custom structure types
are read as the standard types they are role-mapped to.

With ``markdown`` the structure a screen reader announces is kept as
Markdown: headings as ``#`` to ``######`` by level, list items as ``- ``, and
tables as HTML tables with header and data cells and spans. That is the form
structure benchmarks score.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from pathlib import Path

import pikepdf
from pdfminer.converter import PDFLayoutAnalyzer
from pdfminer.layout import LTChar
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage

from app.pipeline.pdf_repair import pdfminer_readable

BLOCK_ROLES = {
    "P",
    "H",
    "H1",
    "H2",
    "H3",
    "H4",
    "H5",
    "H6",
    "LI",
    "LBody",
    "Lbl",
    "TD",
    "TH",
    "Caption",
    "BlockQuote",
    "Note",
    "TOCI",
    "Title",
    "Formula",
    "Code",
}


TABLE_HTML = {
    "Table": "table",
    "THead": "thead",
    "TBody": "tbody",
    "TFoot": "tfoot",
    "TR": "tr",
    "TH": "th",
    "TD": "td",
}


def _cell_spans(node: pikepdf.Dictionary) -> str:
    attributes = node.get("/A")
    owners = attributes if isinstance(attributes, pikepdf.Array) else [attributes]
    spans = ""
    for owner in owners:
        if isinstance(owner, pikepdf.Dictionary) and owner.get("/O") == "/Table":
            for key, name in (("/RowSpan", "rowspan"), ("/ColSpan", "colspan")):
                if int(owner.get(key, 1)) > 1:
                    spans += f' {name}="{int(owner[key])}"'
    return spans


class MarkedContentChars(PDFLayoutAnalyzer):
    """Collects each glyph under the innermost marked-content ID drawing it."""

    def __init__(self, manager: PDFResourceManager) -> None:
        super().__init__(manager, laparams=None)
        self.stack: list[int | None] = []
        self.chars: dict[int, list[LTChar]] = defaultdict(list)

    def begin_tag(self, tag, props=None) -> None:
        mcid = props.get("MCID") if isinstance(props, dict) else None
        self.stack.append(int(mcid) if isinstance(mcid, int) else None)

    def end_tag(self) -> None:
        if self.stack:
            self.stack.pop()

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        advance = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        mcid = next((m for m in reversed(self.stack) if m is not None), None)
        if mcid is not None:
            self.chars[mcid].append(self.cur_item._objs[-1])
        return advance

    def receive_layout(self, ltpage) -> None:
        return None


def glyphs_to_text(chars: list[LTChar]) -> str:
    """Join glyphs in drawing order, adding a space at word and line gaps."""
    text: list[str] = []
    previous: LTChar | None = None
    for char in chars:
        if previous is not None:
            size = max(char.size, previous.size, 1.0)
            new_line = abs(char.y0 - previous.y0) > 0.5 * size
            gap = char.x0 - previous.x1
            if (new_line or gap > 0.15 * size) and not text[-1].endswith(" "):
                text.append(" ")
        text.append(char.get_text())
        previous = char
    return "".join(text)


def _parse_mcid_text(pdf_path: Path) -> list[dict[int, str]]:
    """Text drawn inside each MCID, per page, decoded through the fonts."""
    pages: list[dict[int, str]] = []
    manager = PDFResourceManager()
    with pdf_path.open("rb") as handle:
        for page in PDFPage.get_pages(handle):
            device = MarkedContentChars(manager)
            PDFPageInterpreter(manager, device).process_page(page)
            pages.append({mcid: glyphs_to_text(chars) for mcid, chars in device.chars.items()})
    return pages


def page_mcid_text(pdf_path: Path) -> list[dict[int, str]]:
    """Text drawn inside each MCID, per page."""
    with pdfminer_readable(pdf_path) as readable:
        return _parse_mcid_text(readable)


HEADING_LEVELS = {"Title": 1, "H": 1, **{f"H{level}": level for level in range(1, 7)}}


def _standard_role(node: pikepdf.Dictionary, role_map) -> str:
    """The structure type after following the role map (and, in PDF 2.0,
    the element's namespace role map)."""
    role = node.get("/S")
    namespace = node.get("/NS")
    for _ in range(10):
        mapped = None
        if isinstance(namespace, pikepdf.Dictionary):
            namespace_map = namespace.get("/RoleMapNS")
            if isinstance(namespace_map, pikepdf.Dictionary) and role in namespace_map:
                mapped = namespace_map[role]
                if isinstance(mapped, pikepdf.Array):
                    mapped, namespace = mapped[0], mapped[1] if len(mapped) > 1 else None
        if mapped is None and isinstance(role_map, pikepdf.Dictionary) and role in role_map:
            mapped = role_map[role]
        if mapped is None or mapped == role:
            break
        role = mapped
    return str(role or "").lstrip("/")


def screen_reader_text(pdf_path: Path, *, figure_text: bool = False, markdown: bool = False) -> str:
    mcid_text = page_mcid_text(pdf_path)
    with pikepdf.open(pdf_path) as pdf:
        page_index = {page.objgen: index for index, page in enumerate(pdf.pages)}
        root = pdf.Root.get("/StructTreeRoot")
        if root is None:
            return ""
        role_map = root.get("/RoleMap")
        blocks: list[str] = []

        def inline(node, page) -> str:
            parts: list[str] = []
            if "/ActualText" in node:
                parts.append(str(node.ActualText))
            elif node.get("/K") is not None:
                walk(node.K, page, parts)
            return " ".join("".join(parts).split())

        def mcid_of(kid, inherited_page):
            if isinstance(kid, int):
                return inherited_page, kid
            if isinstance(kid, pikepdf.Dictionary) and kid.get("/Type") == "/MCR":
                page = kid.get("/Pg")
                index = page_index.get(page.objgen) if page is not None else inherited_page
                return index, int(kid.MCID)
            return None

        def walk(node, page, out: list[str]) -> None:
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    walk(kid, page, out)
                return
            if not isinstance(node, pikepdf.Dictionary):
                ref = mcid_of(node, page)
                if ref and ref[0] is not None:
                    out.append(mcid_text[ref[0]].get(ref[1], ""))
                return
            ref = mcid_of(node, page)
            if ref is not None:
                if ref[0] is not None:
                    out.append(mcid_text[ref[0]].get(ref[1], ""))
                return
            if node.get("/Type") == "/OBJR":
                return
            if "/Pg" in node:
                page = page_index.get(node.Pg.objgen, page)
            role = _standard_role(node, role_map)
            if markdown and role in HEADING_LEVELS:
                text = inline(node, page)
                if text:
                    out.append(f"\n\n{'#' * HEADING_LEVELS[role]} {text}\n\n")
                return
            if markdown and role == "LI":
                text = inline(node, page)
                if text:
                    out.append(f"\n- {text}\n")
                return
            if markdown and role in TABLE_HTML:
                tag = TABLE_HTML[role]
                if tag in ("th", "td"):
                    text = html.escape(inline(node, page))
                    out.append(f"<{tag}{_cell_spans(node)}>{text}</{tag}>")
                else:
                    out.append(f"\n<{tag}>")
                    if node.get("/K") is not None:
                        walk(node.K, page, out)
                    out.append(f"</{tag}>\n")
                return
            if "/ActualText" in node:
                out.append(str(node.ActualText))
                if role in BLOCK_ROLES:
                    out.append("\n")
                return
            if role == "Artifact" or (
                role == "Figure" and not (figure_text and "/Alt" not in node)
            ):
                return
            kids = node.get("/K")
            if kids is not None:
                walk(kids, page, out)
            if role in BLOCK_ROLES:
                out.append("\n\n" if markdown else "\n")

        walk(root.get("/K"), None, blocks)
    text = "".join(blocks)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
