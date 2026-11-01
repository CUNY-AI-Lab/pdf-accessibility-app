"""Carry model table decisions to the tagger, where deterministic headers win."""

from copy import deepcopy
from typing import Any

LAYOUT_KINDS = frozenset({"layout_grid", "toc_group", "text_block"})
HEADER_FLAGS = ("is_header", "column_header", "row_header")


def table_action(item: dict) -> str:
    return str(
        item.get("suggested_action") or item.get("action") or item.get("decision") or ""
    ).strip()


def has_headers(table: dict) -> bool:
    return any(
        c.get(f) for c in table.get("cells", []) if isinstance(c, dict) for f in HEADER_FLAGS
    )


def header_selection(table: dict, item: dict) -> list[tuple[dict, str]]:
    """Reject the whole decision if any index is invalid; never fabricate cells."""
    rows, cols = item.get("header_rows", []), item.get("row_header_columns", [])
    nr, nc = table.get("num_rows", 0), table.get("num_cols", 0)
    if type(nr) is not int or type(nc) is not int or nr <= 0 or nc <= 0:
        return []
    if not isinstance(rows, list) or not isinstance(cols, list):
        return []
    if any(type(i) is not int or not 0 <= i < nr for i in rows):
        return []
    if any(type(i) is not int or not 0 <= i < nc for i in cols):
        return []
    return [
        (c, "Column" if c.get("row", 0) in rows else "Row")
        for c in table.get("cells", [])
        if isinstance(c, dict) and (c.get("row", 0) in rows or c.get("col", 0) in cols)
    ]


def queue_table_decision(table: dict, item: dict) -> bool:
    """Keep the original cell ownership and extractor flags until tagging."""
    action = table_action(item)
    if action == "set_table_headers":
        if has_headers(table) or not header_selection(table, item):
            return False
    elif action == "reclassify_region":
        if has_headers(table) or item.get("resolved_kind") not in LAYOUT_KINDS | {"form_region"}:
            return False
    else:
        return False
    decision = {
        k: deepcopy(item[k])
        for k in (
            "header_rows",
            "row_header_columns",
            "resolved_kind",
            "confidence",
            "reason",
            "summary",
        )
        if k in item
    }
    decision["suggested_action"] = action
    pending = table.setdefault("table_llm_decisions", [])
    if decision in pending:
        return False
    pending.append(decision)
    return True


def queue_table_decisions(structure: dict, items: list[dict]) -> tuple[dict, list[dict[str, Any]]]:
    result, queued = deepcopy(structure), []
    for index, table in enumerate(result.get("elements", [])):
        if not isinstance(table, dict) or table.get("type") != "table":
            continue
        review_id = str(table.get("review_id") or f"review-{index}")
        for item in items:
            if (
                not isinstance(item, dict)
                or str(item.get("table_review_id") or item.get("unit_id") or "") != review_id
            ):
                continue
            if item.get("page") is not None and item["page"] != table.get("page", 0) + 1:
                continue
            if queue_table_decision(table, item):
                table["review_id"] = review_id
                queued.append(
                    {
                        "table_review_id": review_id,
                        "page": table.get("page", 0) + 1,
                        "suggested_action": table_action(item),
                        "status": "queued",
                    }
                )
    return result, queued
