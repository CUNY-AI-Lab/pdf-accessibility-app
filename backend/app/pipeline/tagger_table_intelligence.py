"""Apply queued model decisions without altering marked-content ownership."""

from collections import defaultdict

import pikepdf

from app.pipeline.tagger_table_structure import _associate_columns, _table_has_headers
from app.services.table_decisions import LAYOUT_KINDS, has_headers, header_selection, table_action


def _model_table_layout(table: dict) -> dict | None:
    if table.get("_table_readability") or has_headers(table):
        return None
    for item in table.get("table_llm_decisions", []):
        if (
            table_action(item) != "reclassify_region"
            or item.get("resolved_kind") not in LAYOUT_KINDS
        ):
            continue
        rows = defaultdict(list)
        for c in table.get("cells", []):
            if isinstance(c, dict):
                rows[c.get("row", 0)].append(c)
        groups = [
            [
                (c.get("row", 0), c.get("col", 0))
                for c in sorted(rows[r], key=lambda c: c.get("col", 0))
            ]
            for r in sorted(rows)
        ]
        if groups:
            kind = item["resolved_kind"]
            return {
                "reason": f"model reclassify_region: {kind}",
                "order": "row",
                "model_kind": kind,
                "blocks": [{"role": "L" if kind == "toc_group" else "P", "groups": groups}],
            }
    return None


def _apply_model_table_headers(builder, pages: dict[int, list[dict]]) -> list[dict]:
    audit = []
    applied_layouts = set()
    # The parts of a table on later pages, joined to it (_finish_table_structure).
    parts = defaultdict(list)
    for elems in pages.values():
        for table in elems:
            if table.get("_joined_table"):
                parts[id(table["_continues_table"])].append(table)
    for elems in pages.values():
        for table in elems:
            if table.get("type") != "table":
                continue
            cells = builder._fragmented_table_cache.get(id(table), {})
            layout = table.get("_layout_table") or {}
            for item in table.get("table_llm_decisions", []):
                action, kind = table_action(item), item.get("resolved_kind")
                status = "unsupported_decision"
                if table.get("_joined_table"):
                    # Its rows are the table's it continues, whose decision covers them.
                    status = "joined_previous_table"
                elif table.get("_table_readability"):
                    status = "blocked_unreadable_table"
                elif layout:
                    status = "superseded_by_layout"
                    if action == "reclassify_region" and layout.get("model_kind") == kind:
                        if id(table) in applied_layouts:
                            status = "duplicate_layout_decision"
                        else:
                            status = "applied_layout"
                            applied_layouts.add(id(table))
                elif action == "reclassify_region" and kind == "form_region":
                    status = "kept_table"
                elif action == "set_table_headers" and cells:
                    node = next(iter(cells.values())).P.P
                    if _table_has_headers(node):
                        status = "preserved_existing_headers"
                    else:
                        selected = header_selection(table, item)
                        selected = [
                            (c, scope)
                            for c, scope in selected
                            if (c.get("row", 0), c.get("col", 0)) in cells
                        ]
                        status = "invalid_or_empty_headers"
                        if selected:
                            for c, scope in selected:
                                cell = cells[c.get("row", 0), c.get("col", 0)]
                                cell.S = pikepdf.Name.TH
                                attrs = cell.get("/A") or pikepdf.Dictionary(O=pikepdf.Name.Table)
                                attrs.Scope = pikepdf.Name(f"/{scope}")
                                cell.A = attrs
                                cell.ID = pikepdf.String(
                                    f"t{node.objgen[0]}-model-r{c.get('row', 0)}-c{c.get('col', 0)}"
                                )
                            # Its row headers label the rows it continues onto too.
                            columns = set(item.get("row_header_columns") or [])
                            for part in parts.get(id(table), []):
                                offset = part["_joined_row_offset"]
                                part_cells = builder._fragmented_table_cache.get(id(part), {})
                                for (row, col), cell in part_cells.items():
                                    if col in columns and cell.S == "/TD":
                                        cell.S = pikepdf.Name.TH
                                        attrs = cell.get("/A") or pikepdf.Dictionary(
                                            O=pikepdf.Name.Table
                                        )
                                        attrs.Scope = pikepdf.Name.Row
                                        cell.A = attrs
                                        cell.ID = pikepdf.String(
                                            f"t{node.objgen[0]}-model-r{offset + row}-c{col}"
                                        )
                            _associate_columns(node)
                            for row in node.K:
                                row_ids = [
                                    c.ID for c in row.K if c.S == "/TH" and c.A.Scope == "/Row"
                                ]
                                for cell in row.K:
                                    if cell.S == "/TD" and row_ids:
                                        attrs = cell.get("/A") or pikepdf.Dictionary(
                                            O=pikepdf.Name.Table
                                        )
                                        existing = list(attrs.get("/Headers") or [])
                                        attrs.Headers = pikepdf.Array(
                                            existing + [i for i in row_ids if i not in existing]
                                        )
                                        cell.A = attrs
                            status = "applied_headers"
                audit.append(
                    {
                        "table_review_id": table.get("review_id"),
                        "page": table.get("page", 0) + 1,
                        "suggested_action": action,
                        "resolved_kind": kind,
                        "status": status,
                        "header_rows": item.get("header_rows", []),
                        "row_header_columns": item.get("row_header_columns", []),
                        "confidence": item.get("confidence"),
                        "reason": item.get("reason", ""),
                    }
                )
    return audit
