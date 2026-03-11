from __future__ import annotations

from typing import Any

USER_VISIBLE_REVIEW_TASK_TYPES = frozenset(
    {
        "alt_text",
        "annotation_description",
    }
)

USER_VISIBLE_APPLIED_CHANGE_TYPES = frozenset(
    {
        "figure_semantics",
    }
)


def is_user_visible_review_task_type(task_type: str) -> bool:
    return str(task_type or "").strip() in USER_VISIBLE_REVIEW_TASK_TYPES


def is_user_visible_applied_change_type(change_type: str) -> bool:
    return str(change_type or "").strip() in USER_VISIBLE_APPLIED_CHANGE_TYPES


def filter_user_visible_review_tasks(
    review_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        task
        for task in review_tasks
        if isinstance(task, dict)
        and is_user_visible_review_task_type(str(task.get("task_type") or ""))
    ]
