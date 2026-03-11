from __future__ import annotations

from typing import Any

ALLOWED_REVIEW_STRUCTURE_TYPES = {
    "paragraph",
    "heading",
    "list_item",
    "code",
    "formula",
    "artifact",
}
READING_ORDER_NOOP_ACTIONS = {"confirm_current_order"}
TABLE_NOOP_ACTIONS = {"confirm_current_headers"}


def ensure_editable_structure(structure: dict[str, Any] | None) -> dict[str, Any] | None:
    if not structure or not isinstance(structure, dict):
        return None

    raw_elements = structure.get("elements", [])
    if not isinstance(raw_elements, list):
        return None

    elements: list[dict[str, Any]] = []
    for index, raw_element in enumerate(raw_elements):
        if not isinstance(raw_element, dict):
            elements.append(raw_element)
            continue
        review_id = str(raw_element.get("review_id") or "").strip() or f"review-{index}"
        elements.append({**raw_element, "review_id": review_id})

    return {
        **structure,
        "elements": elements,
    }


def sanitize_applied_structure(structure: dict[str, Any]) -> dict[str, Any]:
    raw_elements = structure.get("elements", [])
    if not isinstance(raw_elements, list):
        return structure

    cleaned_elements: list[Any] = []
    for raw_element in raw_elements:
        if not isinstance(raw_element, dict):
            cleaned_elements.append(raw_element)
            continue
        cleaned_elements.append(
            {
                key: value
                for key, value in raw_element.items()
                if key not in {"review_id", "_manual_original_type"}
            }
        )

    return {
        **structure,
        "elements": cleaned_elements,
    }


def font_review_targets(task_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    value = task_metadata.get("font_review_targets")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _reading_order_page_orders(remediation_intelligence: dict[str, Any]) -> list[dict[str, Any]]:
    value = remediation_intelligence.get("proposed_page_orders")
    if not isinstance(value, list):
        return []
    orders: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        raw_ids = item.get("ordered_review_ids")
        if not isinstance(page, int) or not isinstance(raw_ids, list):
            continue
        ordered_review_ids = [str(entry).strip() for entry in raw_ids if str(entry).strip()]
        if ordered_review_ids:
            orders.append(
                {
                    "page": page,
                    "ordered_review_ids": ordered_review_ids,
                }
            )
    return orders


def _reading_order_element_updates(remediation_intelligence: dict[str, Any]) -> list[dict[str, Any]]:
    value = remediation_intelligence.get("proposed_element_updates")
    if not isinstance(value, list):
        return []
    updates: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        review_id = str(item.get("review_id") or "").strip()
        new_type = str(item.get("new_type") or "").strip()
        new_level = item.get("new_level")
        if review_id and new_type:
            updates.append(
                {
                    "review_id": review_id,
                    "new_type": new_type,
                    "new_level": new_level if isinstance(new_level, int) else None,
                }
            )
    return updates


def _table_updates(remediation_intelligence: dict[str, Any]) -> list[dict[str, Any]]:
    value = remediation_intelligence.get("proposed_table_updates")
    if not isinstance(value, list):
        return []
    updates: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        table_review_id = str(item.get("table_review_id") or "").strip()
        suggested_action = str(item.get("suggested_action") or "").strip()
        if not table_review_id:
            continue
        header_rows = (
            [
                entry
                for entry in item.get("header_rows", [])
                if isinstance(entry, int) and entry >= 0
            ]
            if isinstance(item.get("header_rows"), list)
            else []
        )
        row_header_columns = (
            [
                entry
                for entry in item.get("row_header_columns", [])
                if isinstance(entry, int) and entry >= 0
            ]
            if isinstance(item.get("row_header_columns"), list)
            else []
        )
        updates.append(
            {
                "table_review_id": table_review_id,
                "header_rows": header_rows,
                "row_header_columns": row_header_columns,
                "suggested_action": suggested_action,
            }
        )
    return updates


def _structure_elements_for_page(
    structure: dict[str, Any],
    page_number: int,
) -> list[tuple[int, dict[str, Any]]]:
    raw_elements = structure.get("elements", [])
    if not isinstance(raw_elements, list):
        return []
    entries: list[tuple[int, dict[str, Any]]] = []
    for index, raw_element in enumerate(raw_elements):
        if not isinstance(raw_element, dict):
            continue
        page = raw_element.get("page")
        if (
            isinstance(page, int)
            and page + 1 == page_number
            and isinstance(raw_element.get("review_id"), str)
        ):
            entries.append((index, raw_element))
    return entries


def can_apply_reading_order_change(
    structure: dict[str, Any] | None,
    remediation_intelligence: dict[str, Any] | None,
) -> bool:
    if (
        not structure
        or not isinstance(structure.get("elements"), list)
        or not isinstance(remediation_intelligence, dict)
    ):
        return False

    page_orders = _reading_order_page_orders(remediation_intelligence)
    element_updates = _reading_order_element_updates(remediation_intelligence)
    if not page_orders and not element_updates:
        return False

    for page_order in page_orders:
        entries = _structure_elements_for_page(structure, int(page_order["page"]))
        current_ids = [str(element["review_id"]) for _, element in entries]
        ordered_review_ids = list(page_order["ordered_review_ids"])
        if not current_ids or len(current_ids) != len(ordered_review_ids):
            return False
        if len(set(ordered_review_ids)) != len(ordered_review_ids):
            return False
        if any(review_id not in current_ids for review_id in ordered_review_ids):
            return False

    elements = structure.get("elements", [])
    for update in element_updates:
        if update["new_type"] not in ALLOWED_REVIEW_STRUCTURE_TYPES:
            return False
        if not any(
            isinstance(raw_element, dict)
            and str(raw_element.get("review_id") or "").strip() == update["review_id"]
            for raw_element in elements
        ):
            return False
    return True


def can_accept_reading_order_change(
    structure: dict[str, Any] | None,
    remediation_intelligence: dict[str, Any] | None,
) -> bool:
    if not isinstance(remediation_intelligence, dict):
        return False
    action = str(remediation_intelligence.get("suggested_action") or "").strip()
    if action in READING_ORDER_NOOP_ACTIONS:
        return True
    return can_apply_reading_order_change(structure, remediation_intelligence)


def apply_reading_order_change(
    structure: dict[str, Any] | None,
    remediation_intelligence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    editable = ensure_editable_structure(structure)
    if not can_apply_reading_order_change(editable, remediation_intelligence) or editable is None:
        return None

    next_elements = list(editable["elements"])
    page_orders = _reading_order_page_orders(remediation_intelligence or {})
    element_updates = _reading_order_element_updates(remediation_intelligence or {})

    for page_order in page_orders:
        entries = _structure_elements_for_page(
            {**editable, "elements": next_elements}, int(page_order["page"])
        )
        replacement_by_id = {str(element["review_id"]): element for _, element in entries}
        replacements = [
            replacement_by_id.get(review_id) for review_id in page_order["ordered_review_ids"]
        ]
        if any(element is None for element in replacements):
            return None
        for (index, _), replacement in zip(entries, replacements, strict=True):
            next_elements[index] = replacement

    if element_updates:
        updates_by_id = {str(update["review_id"]): update for update in element_updates}
        updated_elements: list[Any] = []
        for raw_element in next_elements:
            if not isinstance(raw_element, dict):
                updated_elements.append(raw_element)
                continue
            review_id = str(raw_element.get("review_id") or "").strip()
            update = updates_by_id.get(review_id)
            if not update:
                updated_elements.append(raw_element)
                continue
            next_type = str(update["new_type"])
            next_element = {**raw_element, "type": next_type}
            if next_type == "artifact":
                next_element["_manual_original_type"] = (
                    str(raw_element.get("type") or "").strip() or "paragraph"
                )
            else:
                next_element.pop("_manual_original_type", None)

            if next_type == "heading":
                new_level = update.get("new_level")
                existing_level = raw_element.get("level")
                if isinstance(new_level, int) and 1 <= new_level <= 6:
                    next_element["level"] = new_level
                elif isinstance(existing_level, int) and 1 <= existing_level <= 6:
                    next_element["level"] = existing_level
                else:
                    next_element["level"] = 1
            else:
                next_element.pop("level", None)
            updated_elements.append(next_element)
        next_elements = updated_elements

    return sanitize_applied_structure(
        {
            **editable,
            "elements": next_elements,
        }
    )


def can_apply_table_change(
    structure: dict[str, Any] | None,
    remediation_intelligence: dict[str, Any] | None,
) -> bool:
    editable = ensure_editable_structure(structure)
    if not editable or not isinstance(remediation_intelligence, dict):
        return False
    updates = _table_updates(remediation_intelligence)
    if not updates:
        return False

    elements = editable.get("elements", [])
    for update in updates:
        if update["suggested_action"] and update["suggested_action"] != "set_table_headers":
            return False
        match = next(
            (
                raw_element
                for raw_element in elements
                if isinstance(raw_element, dict)
                and str(raw_element.get("review_id") or "").strip() == update["table_review_id"]
                and raw_element.get("type") == "table"
            ),
            None,
        )
        if not isinstance(match, dict) or not isinstance(match.get("cells"), list):
            return False
    return True


def can_accept_table_change(
    structure: dict[str, Any] | None,
    remediation_intelligence: dict[str, Any] | None,
) -> bool:
    if not isinstance(remediation_intelligence, dict):
        return False
    action = str(remediation_intelligence.get("suggested_action") or "").strip()
    if action in TABLE_NOOP_ACTIONS:
        return True
    return can_apply_table_change(structure, remediation_intelligence)


def apply_table_change(
    structure: dict[str, Any] | None,
    remediation_intelligence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    editable = ensure_editable_structure(structure)
    if not can_apply_table_change(editable, remediation_intelligence) or editable is None:
        return None

    updates_by_id = {
        update["table_review_id"]: update for update in _table_updates(remediation_intelligence or {})
    }
    next_elements: list[Any] = []
    for raw_element in editable.get("elements", []):
        if not isinstance(raw_element, dict):
            next_elements.append(raw_element)
            continue
        update = updates_by_id.get(str(raw_element.get("review_id") or "").strip())
        if (
            not update
            or raw_element.get("type") != "table"
            or not isinstance(raw_element.get("cells"), list)
        ):
            next_elements.append(raw_element)
            continue

        header_rows = set(update["header_rows"])
        row_header_columns = set(update["row_header_columns"])
        next_cells: list[Any] = []
        for raw_cell in raw_element["cells"]:
            if not isinstance(raw_cell, dict):
                next_cells.append(raw_cell)
                continue
            row = raw_cell.get("row")
            col = raw_cell.get("col")
            column_header = isinstance(row, int) and row in header_rows
            row_header = isinstance(col, int) and col in row_header_columns
            next_cells.append(
                {
                    **raw_cell,
                    "column_header": column_header,
                    "row_header": row_header,
                    "is_header": column_header or row_header,
                }
            )

        next_elements.append({**raw_element, "cells": next_cells})

    return sanitize_applied_structure(
        {
            **editable,
            "elements": next_elements,
        }
    )


def applicable_actualtext_candidates(
    remediation_intelligence: dict[str, Any] | None,
    task_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(remediation_intelligence, dict):
        return []
    candidates = remediation_intelligence.get("actualtext_candidates")
    if not isinstance(candidates, list):
        return []
    targets = font_review_targets(task_metadata)
    matched: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        page = candidate.get("page")
        operator_index = candidate.get("operator_index")
        proposed_actualtext = str(candidate.get("proposed_actualtext") or "").strip()
        if (
            not isinstance(page, int)
            or not isinstance(operator_index, int)
            or not proposed_actualtext
        ):
            continue
        font = str(candidate.get("font") or "").strip() or None
        is_target = any(
            isinstance(target.get("page"), int)
            and isinstance(target.get("operator_index"), int)
            and int(target["page"]) == page
            and int(target["operator_index"]) == operator_index
            and (
                not font
                or not str(target.get("font") or "").strip()
                or str(target.get("font") or "").strip() == font
            )
            for target in targets
        )
        if not is_target:
            continue
        matched.append(
            {
                "page": page,
                "operator_index": operator_index,
                "font": font,
                "proposed_actualtext": proposed_actualtext,
                "confidence": str(candidate.get("confidence") or "").strip() or None,
                "reason": str(candidate.get("reason") or "").strip() or None,
            }
        )
    return matched
