"""Recover table structure after tagging, without changing content ownership.

A table that continues on the next page is one Table (ISO 32000-1, Table
337: its header rows "may be redrawn at the top of each table fragment",
and there is still one THead). Its rows on the next page are tagged under
the Table of the page before, so that page's column headers apply to them,
and a header row the next page repeats is an Artifact, as the PDF
Association's Tagged PDF Best Practice Guide (4.2.6) asks.

A table continues when it is the last body block on its page (headers,
footers and other artifacts aside) and the next page's first body block is
a table, near the bottom and top edges, with the same number of columns,
column starts and sides, no caption of its own (or one saying it
continues), and no header row except a repeat of the first table's. The
first table must get a header row: from its extractor, the tagger's
inference, the headings above it, or the model decision that sets its
headers. This follows Docling's open proposal (docling#2976) and MinerU's
and Azure Document Intelligence's merges: the last and first body blocks,
column count, aligned left edges, repeated header rows stripped.
"""

import re
from statistics import median
from typing import TYPE_CHECKING

import pikepdf

from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import MeasuredGlyph, union_bbox
from app.pipeline.tagger_ownership import _content_targets, _owner_of
from app.services.table_decisions import header_selection, table_action

if TYPE_CHECKING:
    from app.pipeline.tagger_structure import StructTreeBuilder


HEADER_FLAGS = ("is_header", "column_header", "row_header")
# Docling's frame, in points: the bottom and top bands a continued table
# ends and starts in, and how far its column starts and sides may move.
CONTINUATION_EDGE = 108.0
CONTINUATION_COLUMN_SLACK = 12.0
CONTINUATION_SIDE_SLACK = 8.0
CONTINUED_CAPTION = re.compile(r"\bcont(?:inued|['’]d|\.)|续", re.IGNORECASE)
NON_BODY_TYPES = {"artifact", "page_header", "page_footer"}


def _column_starts(table: dict, frame: PageFrame | None = None) -> list[float]:
    """Each column's median text start, in user space or, given the page's
    frame, in Docling's (the page as displayed)."""
    columns = []
    for col in range(table.get("num_cols", 0)):
        xs = [
            (frame.to_docling(c["bbox"]) if frame else c["bbox"])["l"]
            for c in table.get("cells", [])
            if c.get("col", 0) == col
            and c.get("col_span", 1) == 1
            and c.get("bbox")
            and str(c.get("text") or "").strip()
        ]
        if not xs:
            return []
        columns.append(median(xs))
    return columns


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _table_review_id(table: dict) -> str:
    """The id the table's review and fidelity give it: its own, or its index
    among the structure's elements."""
    return str(table.get("review_id") or f"review-{table.get('_docling_order')}")


def _scope(cell: dict) -> str | None:
    """The scope the tagger gives a header cell (see _ensure_fragmented_table)."""
    if not any(cell.get(flag) for flag in HEADER_FLAGS):
        return None
    if cell.get("author_scope") in {"Row", "Column", "Both"}:
        return cell["author_scope"]
    if cell.get("row_header"):
        return "Row"
    if cell.get("column_header"):
        return "Column"
    return "Row" if cell.get("col", 0) == 0 else "Column"


def _cells(table: dict) -> list[dict]:
    return [c for c in table.get("cells", []) if isinstance(c, dict)]


def _row(cells: list[dict], row: int) -> list[tuple[int, int, str]]:
    return sorted(
        (c.get("col", 0), c.get("col_span", 1), _text(c.get("text")))
        for c in cells
        if c.get("row", 0) == row
    )


def _column_header_rows(table: dict) -> list[int]:
    return sorted({c.get("row", 0) for c in _cells(table) if _scope(c) in {"Column", "Both"}})


def _header_rows(table: dict) -> list[list[tuple[int, int, str]]] | None:
    """The header rows the tagger will give a table, each as its cells'
    (column, span, text): the rows its extractor or inference marked, the
    headings recovered above it, or the rows of the first model decision
    that will set its headers (see _apply_model_table_headers). None when
    it gets no header row."""
    if table.get("_layout_table") or table.get("_table_readability"):
        return None
    cells = _cells(table)
    if any(c.get(flag) for c in cells for flag in HEADER_FLAGS):
        rows = _column_header_rows(table)
    elif headings := table.get("_external_table_headers"):
        return [[(col, 1, _text(h.get("text"))) for col, h in enumerate(headings)]]
    else:
        rows = next(
            (
                sorted(set(item.get("header_rows") or []))
                for item in table.get("table_llm_decisions", [])
                if table_action(item) == "set_table_headers" and header_selection(table, item)
            ),
            [],
        )
    header = [_row(cells, row) for row in rows]
    return header if any(text for row in header for _, _, text in row) else None


def _repeated_rows(header: list[list[tuple[int, int, str]]], table: dict) -> int:
    """How many leading rows of ``table`` repeat ``header`` exactly."""
    cells = _cells(table)
    if all(_row(cells, row) == cells_of for row, cells_of in enumerate(header)):
        return len(header)
    return 0


def _body_blocks(elements: list[dict]) -> list[dict]:
    return [
        e
        for e in elements
        if isinstance(e.get("bbox"), dict) and e.get("type") not in NON_BODY_TYPES
    ]


def _page_height(frame: PageFrame) -> float:
    if frame.rotation in (90, 270):
        return frame.right - frame.left
    return frame.top - frame.bottom


def _continued_table(
    previous: list[dict], current: list[dict], frames: tuple[PageFrame, PageFrame]
) -> tuple[dict, dict] | None:
    """The table the first body block of this page continues, and that
    block, judged before its own headers are inferred: the first table of
    the run (it carries the header rows), and this page's part."""
    before, after = _body_blocks(previous), _body_blocks(current)
    if not before or not after:
        return None
    previous_frame, frame = frames
    last = min(before, key=lambda e: previous_frame.to_docling(e["bbox"])["b"])
    first = max(after, key=lambda e: frame.to_docling(e["bbox"])["t"])
    if last.get("type") != "table" or first.get("type") != "table":
        return None
    root = last.get("_continues_table") or last
    header = _header_rows(root)
    if (
        header is None
        or first.get("_table_readability")
        or not 2 <= first.get("num_cols", 0) == last.get("num_cols", 0) == root.get("num_cols", 0)
        or (
            _text(first.get("caption"))
            and not CONTINUED_CAPTION.search(_text(first.get("caption")))
        )
        or not set(_column_header_rows(first)) <= set(range(_repeated_rows(header, first)))
    ):
        return None
    a, b = previous_frame.to_docling(last["bbox"]), frame.to_docling(first["bbox"])
    if a["b"] > CONTINUATION_EDGE or _page_height(frame) - b["t"] > CONTINUATION_EDGE:
        return None
    if any(abs(a[side] - b[side]) > CONTINUATION_SIDE_SLACK for side in ("l", "r")):
        return None
    xs, ys = _column_starts(last, previous_frame), _column_starts(first, frame)
    if (
        len(xs) < 2
        or len(xs) != len(ys)
        or any(abs(x - y) > CONTINUATION_COLUMN_SLACK for x, y in zip(xs, ys, strict=True))
    ):
        return None
    return root, first


def _mark_table_continuation(root: dict, table: dict, elements: list[dict]) -> bool:
    """Once this page's headers are inferred, mark ``table`` as rows of
    ``root`` (_finish_table_structure joins them), unless it now has a
    header row of its own. A header row repeating the root's becomes an
    Artifact element over its cells, and the rows after it are renumbered."""
    if table.get("_layout_table") or table.get("_external_table_headers"):
        return False
    header = _header_rows(root)
    if header is None:
        return False
    repeated = _repeated_rows(header, table)
    if not set(_column_header_rows(table)) <= set(range(repeated)):
        return False
    cells = _cells(table)
    if repeated:
        repeat = [c for c in cells if c.get("row", 0) < repeated]
        kept = [c for c in cells if c.get("row", 0) >= repeated]
        if (
            not kept
            or not all(isinstance(c.get("bbox"), dict) for c in cells)
            or any(c.get("row", 0) + c.get("row_span", 1) > repeated for c in repeat)
        ):
            return False
        table["cells"] = [{**c, "row": c.get("row", 0) - repeated} for c in kept]
        table["num_rows"] = table.get("num_rows", 0) - repeated
        elements.append(
            {
                "type": "artifact",
                "artifact_type": "pagination",
                "text": " ".join(_text(c.get("text")) for c in repeat),
                "page": table.get("page"),
                "bbox": union_bbox(c["bbox"] for c in repeat),
                "reading_order": table.get("reading_order"),
                "_docling_order": table.get("_docling_order"),
            }
        )
    table["_continues_table"] = root
    return True


def _plan_external_table_headers(elements: list[dict], glyphs: list[MeasuredGlyph]) -> None:
    """Require a short bold fragment for every column, on the same nearby line."""
    eligible = [
        t
        for t in elements
        if t.get("type") == "table"
        and t.get("bbox")
        and not t.get("_table_readability")
        and not t.get("_layout_table")
        and not any(c.get(f) for c in t.get("cells", []) for f in HEADER_FLAGS)
    ]
    if not any(
        e.get("type") in {"paragraph", "heading"}
        and e.get("bbox")
        and 0 <= e["bbox"]["b"] - t["bbox"]["t"] <= 16
        for e in elements
        for t in eligible
    ):
        return
    targets = _content_targets(elements)
    drawn: dict[int, list[MeasuredGlyph]] = {}
    for g in glyphs:
        if g.text.strip() and (owner := _owner_of(g.bbox, targets, g.turn)):
            drawn.setdefault(owner.elem_idx, []).append(g)
    for table in eligible:
        starts = _column_starts(table)
        box = table.get("bbox")
        if len(starts) < 2 or not box:
            continue
        candidates = []
        for index, elem in enumerate(elements):
            b, text = elem.get("bbox"), str(elem.get("text") or "").strip()
            gs = drawn.get(index, [])
            if (
                elem.get("type") not in {"paragraph", "heading"}
                or not b
                or not text
                or elem.get("continuation")
                or elem.get("continued_ref")
                or len(text.split()) > 12
                or len(text) > 100
                or not 0 <= b["b"] - box["t"] <= 16
                or not gs
                or any(g.turn for g in gs)
                or sum("bold" in g.font_name.lower() for g in gs) < 0.8 * len(gs)
            ):
                continue
            col = min(range(len(starts)), key=lambda c: abs(b["l"] - starts[c]))
            right = starts[col + 1] if col + 1 < len(starts) else box["r"] + 12
            if abs(b["l"] - starts[col]) <= 16 and b["r"] <= right:
                candidates.append((col, elem))
        if (
            len(candidates) == len(starts)
            and sorted(c for c, _ in candidates) == list(range(len(starts)))
            and max(e["bbox"]["t"] for _, e in candidates)
            - min(e["bbox"]["t"] for _, e in candidates)
            <= 4
        ):
            table["_external_table_headers"] = [e for _, e in sorted(candidates)]


def _table_has_headers(node) -> bool:
    return any(c.get("/S") == "/TH" for row in node.K for c in row.K)


def _associate_columns(node) -> None:
    headers = []
    for row in node.K:
        col = 0
        for cell in row.K:
            attrs = cell.get("/A", {})
            span = int(attrs.get("/ColSpan", 1))
            if cell.get("/S") == "/TH" and attrs.get("/Scope") == "/Column" and cell.get("/ID"):
                headers.append((col, col + span, cell.ID))
            col += span
    for row in node.K:
        col = 0
        for cell in row.K:
            attrs = cell.get("/A", {})
            span = int(attrs.get("/ColSpan", 1))
            ids = [ident for start, end, ident in headers if start < col + span and end > col]
            if cell.get("/S") == "/TD" and ids:
                if not isinstance(attrs, pikepdf.Dictionary):
                    attrs = pikepdf.Dictionary()
                attrs["/O"] = pikepdf.Name.Table
                existing = list(attrs.get("/Headers") or [])
                attrs["/Headers"] = pikepdf.Array(existing + [i for i in ids if i not in existing])
                cell.A = attrs
            col += span


def _finish_table_structure(builder: "StructTreeBuilder", pages: dict[int, list[dict]]) -> int:
    """Reparent existing owners; ParentTree entries and MCRs stay intact."""
    nodes = {}
    changed = {}
    for elems in pages.values():
        for table in elems:
            if (
                table.get("type") != "table"
                or table.get("_layout_table")
                or table.get("_table_readability")
            ):
                continue
            cells = builder._fragmented_table_cache.get(id(table), {})
            if cells:
                nodes[id(table)] = next(iter(cells.values())).P.P
    for elems in pages.values():
        for table in elems:
            node = nodes.get(id(table))
            headings = table.get("_external_table_headers", [])
            owners = [builder.source_element_struct(e) for e in headings]
            if (
                node is None
                or not owners
                or any(o is None or o.P != builder.doc_elem for o in owners)
            ):
                continue
            if len({o.objgen for o in owners}) != len(owners):
                continue
            row = builder.pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.StructElem,
                    S=pikepdf.Name.TR,
                    P=node,
                    K=pikepdf.Array([]),
                )
            )
            builder._struct_elems_created += 1
            for col, owner in enumerate(owners):
                builder.doc_elem.K = pikepdf.Array([k for k in builder.doc_elem.K if k != owner])
                owner.S, owner.P = pikepdf.Name.TH, row
                owner.ID = pikepdf.String(f"t{node.objgen[0]}-external-c{col}")
                owner.A = pikepdf.Dictionary(O=pikepdf.Name.Table, Scope=pikepdf.Name.Column)
                row.K.append(owner)
            node.K = pikepdf.Array([row, *list(node.K)])
            changed[node.objgen] = node
            for heading in headings:
                heading["_table_header_recovered"] = True
            builder._headings = [
                h
                for h in builder._headings
                if not any(
                    h["page_index"] == e["page"] and h["text"] == e.get("text") for e in headings
                )
            ]
            _associate_columns(node)
    # The rows of a continued table (see _continued_table) join the Table it
    # continues, before model decisions set that Table's headers.
    merged = 0
    for page_index in sorted(pages):
        for table in pages[page_index]:
            if (root := table.get("_continues_table")) is None:
                continue
            a, b = nodes.get(id(root)), nodes.get(id(table))
            if a is None or b is None or a == b:
                continue
            table["_joined_row_offset"] = len(a.K)
            for row in b.K:
                row.P = a
                a.K.append(row)
            b.K = pikepdf.Array([])
            builder.doc_elem.K = pikepdf.Array([k for k in builder.doc_elem.K if k != b])
            nodes[id(table)] = a
            changed[a.objgen] = a
            table["_joined_table"] = True
            merged += 1
            _associate_columns(a)
    for node in changed.values():
        attrs = node.get("/A", {})
        if attrs.get("/Summary"):
            attrs["/Summary"] = pikepdf.String(
                re.sub(
                    r"^Table with \d+ rows? and",
                    f"Table with {len(node.K)} rows and",
                    str(attrs["/Summary"]),
                )
            )
    return merged
