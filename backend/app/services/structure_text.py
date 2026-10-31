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
from app.pipeline.pdfium_text import PageText, read_page_text
from app.services.pdf_objects import pdf_integer, pdf_name, pdf_page_key

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
                if pdf_integer(owner.get(key)) > 1:
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
    role = pdf_name(node.get("/S"))
    namespace = node.get("/NS")
    for _ in range(10):
        if role is None:
            break
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
        role = pdf_name(mapped)
    return str(role or "").lstrip("/")


class MarkedContentReader:
    """Shared PDFium reading and word-boundary state for tag-tree consumers.

    Callers reset ``last_read`` after block boundaries or element ActualText,
    exactly as the screen-reader traversal does. Form paths and marked-content
    ActualText use the same glyph reader in both HTML and plain-text exports.
    """

    def __init__(self, pdf: pikepdf.Pdf, glyphs: PdfGlyphReader, pages_text: list[PageText]):
        self.pdf = pdf
        self.glyphs = glyphs
        self.pages_text = pages_text
        self.marked: dict[int, dict[tuple[StreamKey, int], list[MeasuredGlyph | str]]] = {}
        self.form_paths: dict[int, dict[tuple[int, int], StreamKey]] = {}
        self.last_read: tuple[int, int] | None = None

    def read(self, page: int, stream: pikepdf.Object | None, mcid: int) -> str | None:
        parts: list[str] = []
        if page not in self.marked:
            self.marked[page] = self.glyphs.page(page).marked
            self.form_paths[page] = _form_paths(self.pdf.pages[page].obj)
        path = () if stream is None else self.form_paths[page].get(stream.objgen)
        if path is None:
            return None
        page_text = self.pages_text[page]
        indices = page_text.marked.get((path, mcid))
        if not indices:
            # Marked content that draws no text may still carry /ActualText.
            actual_texts = [
                item for item in self.marked[page].get((path, mcid), []) if isinstance(item, str)
            ]
            if actual_texts:
                if self.last_read is not None:
                    parts.append(" ")
                parts.append(" ".join(actual_texts))
                self.last_read = None
            return "".join(parts) if actual_texts else None
        previous = self.last_read
        if previous is not None and (
            previous[0] != page or page_text.breaks_between(previous[1], indices[0])
        ):
            parts.append(" ")
        actual_texts = [
            item for item in self.marked[page].get((path, mcid), []) if isinstance(item, str)
        ]
        parts.append(page_text.text(indices, actual_texts))
        self.last_read = (page, indices[-1])

        return "".join(parts)


def screen_reader_text(pdf_path: Path, *, figure_text: bool = False, markdown: bool = False) -> str:
    return _screen_reader_text(pdf_path, figure_text=figure_text, markdown=markdown)[0]


def screen_reader_text_with_pages(pdf_path: Path) -> tuple[str, list[str]]:
    """The full tag-order reading and its text on each page (zero-based).

    An /ActualText spanning pages belongs to its element's /Pg, or its first
    descendant page when /Pg is absent. It is read once, as in the full reading.
    """
    text, pages, _, _ = _screen_reader_text(pdf_path)
    return text, pages


def screen_reader_coverage_text(
    pdf_path: Path,
) -> tuple[str, list[str], list[set[int]], list[set[int]]]:
    """Reading plus raw character indices inside Figures/Formulas.

    Descendant MCIDs inherit /Pg, and MCRs can override it and name a Form
    XObject with /Stm. Match their (stream path, MCID) to PDFium's character
    map, rather than exempting a figure's rectangle or matching its words.
    The last result identifies marked characters, to prefer a marked copy
    when PDFium retains coincident duplicate drawings of the same glyphs.
    """
    return _screen_reader_text(pdf_path, exclude_figure_interiors=True)


def _screen_reader_text(
    pdf_path: Path, *, figure_text: bool = False, markdown: bool = False,
    exclude_figure_interiors: bool = False,
) -> tuple[str, list[str], list[set[int]], list[set[int]]]:
    with PdfGlyphReader(pdf_path) as glyphs, pikepdf.open(pdf_path) as pdf:
        page_index = {page.obj.objgen: index for index, page in enumerate(pdf.pages)}
        page_blocks: list[list[str]] = [[] for _ in pdf.pages]
        replaced: list[set[int]] = [set() for _ in pdf.pages]
        root = pdf.Root.get("/StructTreeRoot")
        if root is None:
            return "", ["" for _ in pdf.pages], replaced, [set() for _ in pdf.pages]
        pages_text = read_page_text(pdf_path)
        role_map = root.get("/RoleMap")
        blocks: list[str] = []
        reader = MarkedContentReader(pdf, glyphs, pages_text)
        form_paths = reader.form_paths
        last_page: list[int | None] = [None]

        def emit(text: str, page: int | None, out: list[str]) -> None:
            out.append(text)
            if out is blocks and page is not None:
                page_blocks[page].append(text)
                last_page[0] = page

        def first_page(node) -> int | None:
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    found = first_page(kid)
                    if found is not None:
                        return found
            elif isinstance(node, pikepdf.Dictionary):
                if (page_key := pdf_page_key(node.get("/Pg"))) is not None:
                    return page_index.get(page_key)
                return first_page(node.get("/K"))
            return None

        def read_mcid(page: int, stream: pikepdf.Object | None, mcid: int, out: list[str]) -> None:
            text = reader.read(page, stream, mcid)
            if text is not None:
                emit(text, page, out)

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
            if (page_key := pdf_page_key(node.get("/Pg"))) is not None:
                page = page_index.get(page_key, page)
            role = _standard_role(node, role_map)
            if exclude_figure_interiors and role in {"Figure", "Formula"}:
                collect_replaced(node.get("/K"), page)
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
                emit(str(node.ActualText), page if page is not None else first_page(node), out)
                reader.last_read = None
                if role in BLOCK_ROLES:
                    emit("\n", page if page is not None else first_page(node), out)
                return
            if role == "Artifact" or (
                role == "Figure" and not (figure_text and "/Alt" not in node)
            ):
                return
            kids = node.get("/K")
            if kids is not None:
                walk(kids, page, out)
            if role in BLOCK_ROLES:
                emit("\n\n" if markdown else "\n", page if page is not None else last_page[0], out)
                reader.last_read = None

        def collect_replaced(node, page) -> None:
            if isinstance(node, pikepdf.Array):
                for kid in node:
                    collect_replaced(kid, page)
                return
            ref = marked_ref(node, page)
            if ref is not None:
                number, stream, mcid = ref
                if number is None:
                    return
                if number not in form_paths:
                    form_paths[number] = _form_paths(pdf.pages[number].obj)
                path = () if stream is None else form_paths[number].get(stream.objgen)
                if path is not None:
                    replaced[number].update(pages_text[number].marked.get((path, mcid), []))
                return
            if isinstance(node, pikepdf.Dictionary):
                if (page_key := pdf_page_key(node.get("/Pg"))) is not None:
                    page = page_index.get(page_key, page)
                collect_replaced(node.get("/K"), page)

        walk(root.get("/K"), None, blocks)
    text = "".join(blocks)
    return (
        re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip(),
        ["".join(parts).strip() for parts in page_blocks],
        replaced,
        [{index for indices in page.marked.values() for index in indices} for page in pages_text],
    )
