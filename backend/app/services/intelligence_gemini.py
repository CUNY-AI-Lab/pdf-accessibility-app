from __future__ import annotations

from typing import Any


def confidence_label(value: Any, *, default: str = "low") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"high", "medium", "low"} else default


def confidence_score(value: Any) -> float:
    normalized = confidence_label(value, default="")
    if normalized == "high":
        return 0.9
    if normalized == "medium":
        return 0.7
    if normalized == "low":
        return 0.4
    return 0.5


def normalize_reading_order_suggestion(suggestion: dict[str, Any]) -> dict[str, Any]:
    page_orders: list[dict[str, Any]] = []
    raw_page_orders = suggestion.get("proposed_page_orders")
    if isinstance(raw_page_orders, list):
        for raw_order in raw_page_orders:
            if not isinstance(raw_order, dict):
                continue
            page = raw_order.get("page")
            ordered_review_ids = raw_order.get("ordered_review_ids")
            if not isinstance(page, int) or page < 1 or not isinstance(ordered_review_ids, list):
                continue
            normalized_ids = [
                str(review_id).strip()
                for review_id in ordered_review_ids
                if str(review_id).strip()
            ]
            if not normalized_ids:
                continue
            page_orders.append(
                {
                    "page": page,
                    "ordered_review_ids": normalized_ids,
                    "reason": str(raw_order.get("reason") or "").strip(),
                }
            )

    element_updates: list[dict[str, Any]] = []
    raw_element_updates = suggestion.get("proposed_element_updates")
    if isinstance(raw_element_updates, list):
        for raw_update in raw_element_updates:
            if not isinstance(raw_update, dict):
                continue
            page = raw_update.get("page")
            review_id = str(raw_update.get("review_id") or "").strip()
            new_type = str(raw_update.get("new_type") or "").strip()
            if not isinstance(page, int) or page < 1 or not review_id or not new_type:
                continue
            normalized_update = {
                "page": page,
                "review_id": review_id,
                "new_type": new_type,
                "reason": str(raw_update.get("reason") or "").strip(),
            }
            if isinstance(raw_update.get("new_level"), int):
                normalized_update["new_level"] = int(raw_update["new_level"])
            element_updates.append(normalized_update)

    readable_text_hints: list[dict[str, Any]] = []
    raw_hints = suggestion.get("readable_text_hints")
    if isinstance(raw_hints, list):
        for raw_hint in raw_hints:
            if not isinstance(raw_hint, dict):
                continue
            page = raw_hint.get("page")
            review_id = str(raw_hint.get("review_id") or "").strip()
            if not isinstance(page, int) or page < 1 or not review_id:
                continue
            readable_text_hints.append(
                {
                    "page": page,
                    "review_id": review_id,
                    "extracted_text": str(raw_hint.get("extracted_text") or "").strip(),
                    "native_text_candidate": str(raw_hint.get("native_text_candidate") or "").strip(),
                    "ocr_text_candidate": str(raw_hint.get("ocr_text_candidate") or "").strip(),
                    "readable_text_hint": str(raw_hint.get("readable_text_hint") or "").strip(),
                    "chosen_source": str(raw_hint.get("chosen_source") or "").strip(),
                    "issue_type": str(raw_hint.get("issue_type") or "").strip(),
                    "confidence": str(raw_hint.get("confidence") or "").strip(),
                    "should_block_accessibility": bool(raw_hint.get("should_block_accessibility", False)),
                    "reason": str(raw_hint.get("reason") or "").strip(),
                }
            )

    return {
        "task_type": "reading_order",
        "confidence": confidence_score(suggestion.get("confidence")),
        "proposed_page_orders": page_orders,
        "proposed_element_updates": element_updates,
        "readable_text_hints": readable_text_hints,
    }


def normalize_table_suggestion(suggestion: dict[str, Any]) -> dict[str, Any]:
    table_updates: list[dict[str, Any]] = []
    raw_updates = suggestion.get("proposed_table_updates")
    if isinstance(raw_updates, list):
        for raw_update in raw_updates:
            if not isinstance(raw_update, dict):
                continue
            table_review_id = str(raw_update.get("table_review_id") or "").strip()
            page = raw_update.get("page")
            if not table_review_id or not isinstance(page, int) or page < 1:
                continue
            header_rows = [
                int(value)
                for value in raw_update.get("header_rows", [])
                if isinstance(value, int) and value >= 0
            ]
            row_header_columns = [
                int(value)
                for value in raw_update.get("row_header_columns", [])
                if isinstance(value, int) and value >= 0
            ]
            table_updates.append(
                {
                    "page": page,
                    "table_review_id": table_review_id,
                    "header_rows": sorted(set(header_rows)),
                    "row_header_columns": sorted(set(row_header_columns)),
                    "reason": str(raw_update.get("reason") or "").strip(),
                }
            )

    return {
        "task_type": "table_semantics",
        "confidence": confidence_score(suggestion.get("confidence")),
        "proposed_table_updates": table_updates,
    }
