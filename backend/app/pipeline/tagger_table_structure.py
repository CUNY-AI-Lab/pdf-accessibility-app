"""Recover table structure after tagging, without changing content ownership."""

import re
from statistics import median
from typing import TYPE_CHECKING

import pikepdf

from app.pipeline.page_glyphs import MeasuredGlyph
from app.pipeline.tagger_ownership import _content_targets, _owner_of

if TYPE_CHECKING:
    from app.pipeline.tagger_structure import StructTreeBuilder


HEADER_FLAGS = ("is_header", "column_header", "row_header")


def _column_starts(table: dict) -> list[float]:
    columns = []
    for col in range(table.get("num_cols", 0)):
        xs = [
            c["bbox"]["l"]
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
    merged = 0
    for page_index in sorted(pages):

        def semantic(e):
            return e.get("bbox") and e.get("type") not in {"artifact", "page_header", "page_footer"}

        current = [e for e in pages[page_index] if semantic(e)]
        previous = [e for e in pages.get(page_index - 1, []) if semantic(e)]
        if not current or not previous:
            continue
        first = max(current, key=lambda e: e["bbox"]["t"])
        last = min(previous, key=lambda e: e["bbox"]["b"])
        a, b = nodes.get(id(last)), nodes.get(id(first))
        if a is None or b is None or a == b or not _table_has_headers(a) or _table_has_headers(b):
            continue
        height = float(builder.pdf.pages[page_index].MediaBox[3])
        if last["bbox"]["b"] > 108 or height - first["bbox"]["t"] > 108:
            continue
        xs, ys = _column_starts(last), _column_starts(first)
        if (
            len(xs) < 2
            or len(xs) != len(ys)
            or any(abs(x - y) > 12 for x, y in zip(xs, ys, strict=True))
            or any(abs(last["bbox"][s] - first["bbox"][s]) > 8 for s in ("l", "r"))
        ):
            continue
        for row in b.K:
            row.P = a
            a.K.append(row)
        b.K = pikepdf.Array([])
        builder.doc_elem.K = pikepdf.Array([k for k in builder.doc_elem.K if k != b])
        nodes[id(first)] = a
        changed[a.objgen] = a
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
