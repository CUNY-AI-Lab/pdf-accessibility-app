"""The text a screen reader hears from a tagged PDF.

Walks the structure tree in logical order. An element with ``/ActualText``
contributes that text and hides its descendants; otherwise each marked-content
reference contributes what that marked content reads as: its text as pdfium,
Chrome's PDF engine, extracts it (``app/pipeline/pdfium_text.py``), or the ``/ActualText`` of
marked content that carries one (``app/pipeline/page_glyphs.py``). Artifacts and untagged content are not in the structure tree, so
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
from pathlib import Path

import pikepdf

from app.pipeline.page_glyphs import MeasuredGlyph, PdfGlyphReader, StreamKey
from app.pipeline.pdfium_text import read_page_text

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


def _form_paths(page: pikepdf.Object) -> dict[tuple[int, int], StreamKey]:
    """The name path by which the page draws each Form XObject (see
    page_glyphs.StreamKey), keyed by the form's object."""
    paths: dict[tuple[int, int], StreamKey] = {}

    def visit(owner: pikepdf.Object, prefix: StreamKey) -> None:
        resources = owner.get("/Resources")
        xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for name, xobject in xobjects.items():
            if xobject.get("/Subtype") != "/Form" or xobject.objgen in paths:
                continue
            paths[xobject.objgen] = (*prefix, name.lstrip("/"))
            visit(xobject, paths[xobject.objgen])

    visit(page, ())
    return paths


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
    with PdfGlyphReader(pdf_path) as glyphs, pikepdf.open(pdf_path) as pdf:
        page_index = {page.objgen: index for index, page in enumerate(pdf.pages)}
        root = pdf.Root.get("/StructTreeRoot")
        if root is None:
            return ""
        pages_text = read_page_text(pdf_path)
        role_map = root.get("/RoleMap")
        blocks: list[str] = []
        marked: dict[int, dict[tuple[StreamKey, int], list[MeasuredGlyph | str]]] = {}
        form_paths: dict[int, dict[tuple[int, int], StreamKey]] = {}
        # Where the last marked content read ended (page, character index),
        # to decide whether the next starts a new word; None after a block
        # break or /ActualText.
        last_read: list[tuple[int, int] | None] = [None]

        def read_mcid(page: int, stream: pikepdf.Object | None, mcid: int, out: list[str]) -> None:
            if page not in marked:
                marked[page] = glyphs.page(page).marked
                form_paths[page] = _form_paths(pdf.pages[page].obj)
            path = () if stream is None else form_paths[page].get(stream.objgen)
            if path is None:
                return
            page_text = pages_text[page]
            indices = page_text.marked.get((path, mcid))
            if not indices:
                # Marked content that draws no text may still carry /ActualText.
                actual_texts = [
                    item for item in marked[page].get((path, mcid), []) if isinstance(item, str)
                ]
                if actual_texts:
                    if last_read[0] is not None:
                        out.append(" ")
                    out.append(" ".join(actual_texts))
                    last_read[0] = None
                return
            previous = last_read[0]
            if previous is not None and (
                previous[0] != page or page_text.breaks_between(previous[1], indices[0])
            ):
                out.append(" ")
            actual_texts = [
                item for item in marked[page].get((path, mcid), []) if isinstance(item, str)
            ]
            out.append(page_text.text(indices, actual_texts))
            last_read[0] = (page, indices[-1])

        def inline(node, page) -> str:
            parts: list[str] = []
            if "/ActualText" in node:
                parts.append(str(node.ActualText))
            elif node.get("/K") is not None:
                walk(node.K, page, parts)
            return " ".join("".join(parts).split())

        def marked_ref(kid, inherited_page):
            """(page, content stream or None for the page's own, MCID)."""
            if isinstance(kid, int):
                return inherited_page, None, kid
            if isinstance(kid, pikepdf.Dictionary) and kid.get("/Type") == "/MCR":
                page = kid.get("/Pg")
                index = page_index.get(page.objgen) if page is not None else inherited_page
                return index, kid.get("/Stm"), int(kid.MCID)
            return None

        def walk(node, page, out: list[str]) -> None:
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    walk(kid, page, out)
                return
            ref = marked_ref(node, page)
            if ref is not None:
                if ref[0] is not None:
                    read_mcid(*ref, out)
                return
            if not isinstance(node, pikepdf.Dictionary):
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
                last_read[0] = None
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
                last_read[0] = None

        walk(root.get("/K"), None, blocks)
    text = "".join(blocks)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
