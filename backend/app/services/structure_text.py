"""The text a screen reader hears from a tagged PDF.

Walks the structure tree in logical order. An element with ``/ActualText``
contributes that text and hides its descendants; otherwise each marked-content
reference contributes the glyphs drawn inside it on its page, joined into words
by position. Artifacts and untagged content are not in the structure tree, so
they are not read. Figure ``/Alt`` text is left out: it describes an image, not
page text.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pikepdf
from pdfminer.converter import PDFLayoutAnalyzer
from pdfminer.layout import LTChar
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage

BLOCK_ROLES = {
    "P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "LI", "LBody", "Lbl", "TD",
    "TH", "Caption", "BlockQuote", "Note", "TOCI", "Title", "Formula", "Code",
}


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
        advance = super().render_char(
            matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate
        )
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


def page_mcid_text(pdf_path: Path) -> list[dict[int, str]]:
    """Text drawn inside each MCID, per page, decoded through the fonts."""
    pages: list[dict[int, str]] = []
    manager = PDFResourceManager()
    with pdf_path.open("rb") as handle:
        for page in PDFPage.get_pages(handle):
            device = MarkedContentChars(manager)
            PDFPageInterpreter(manager, device).process_page(page)
            pages.append(
                {mcid: glyphs_to_text(chars) for mcid, chars in device.chars.items()}
            )
    return pages


def screen_reader_text(pdf_path: Path) -> str:
    mcid_text = page_mcid_text(pdf_path)
    with pikepdf.open(pdf_path) as pdf:
        page_index = {page.objgen: index for index, page in enumerate(pdf.pages)}
        root = pdf.Root.get("/StructTreeRoot")
        if root is None:
            return ""
        blocks: list[str] = []

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
            role = str(node.get("/S", "")).lstrip("/")
            if "/ActualText" in node:
                out.append(str(node.ActualText))
                if role in BLOCK_ROLES:
                    out.append("\n")
                return
            if role == "Figure" or role == "Artifact":
                return
            kids = node.get("/K")
            if kids is not None:
                walk(kids, page, out)
            if role in BLOCK_ROLES:
                out.append("\n")

        walk(root.get("/K"), None, blocks)
    text = "".join(blocks)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()

