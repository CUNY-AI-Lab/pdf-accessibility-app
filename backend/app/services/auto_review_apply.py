from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from app.services.llm_client import make_llm_client
from app.services.recommendation_apply import (
    apply_reading_order_recommendation,
    apply_table_recommendation,
    can_accept_reading_order_recommendation,
    can_accept_table_recommendation,
)
from app.services.review_suggestions import generate_review_suggestion

READING_ORDER_NOOP_ACTIONS = {"confirm_current_order"}
TABLE_NOOP_ACTIONS = {"confirm_current_headers"}


def _metadata(task: dict[str, Any]) -> dict[str, Any]:
    value = task.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _pseudo_task(task: dict[str, Any]) -> Any:
    return SimpleNamespace(
        task_type=str(task.get("task_type") or "review_task"),
        title=str(task.get("title") or ""),
        detail=str(task.get("detail") or ""),
        severity=str(task.get("severity") or "medium"),
        source=str(task.get("source") or "fidelity"),
        metadata_json=json.dumps(_metadata(task)),
    )


async def auto_apply_structure_review_tasks(*, job, settings, review_tasks: list[dict[str, Any]], structure_json: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Generate/apply safe structure recommendations before review UI.

    Returns:
      suggested_tasks: tasks with llm_suggestion metadata (noop confirmations removed)
      updated_structure: structure after safe applies
      applied_specs: change specs for tasks whose structure changes should be adopted if retry succeeds
    """
    suggested_tasks: list[dict[str, Any]] = []
    applied_specs: list[dict[str, Any]] = []
    current_structure = copy.deepcopy(structure_json)

    llm_client = make_llm_client(settings)
    try:
        for index, task in enumerate(review_tasks):
            if not isinstance(task, dict):
                continue
            if not bool(task.get("blocking")):
                suggested_tasks.append(task)
                continue
            task_type = str(task.get("task_type") or "")
            if task_type not in {"reading_order", "table_semantics"}:
                suggested_tasks.append(task)
                continue

            pseudo_task = _pseudo_task(task)
            try:
                suggestion = await generate_review_suggestion(job=job, task=pseudo_task, llm_client=llm_client)
            except Exception:
                suggested_tasks.append(task)
                continue

            task_metadata = _metadata(task)
            task_metadata["llm_suggestion"] = suggestion
            enriched_task = {**task, "metadata": task_metadata}
            suggested_action = str(suggestion.get("suggested_action") or "").strip()

            if task_type == "reading_order":
                if suggested_action in READING_ORDER_NOOP_ACTIONS:
                    # model confirmed current structure; no review needed
                    continue
                if can_accept_reading_order_recommendation(current_structure, suggestion):
                    next_structure = apply_reading_order_recommendation(current_structure, suggestion)
                    if next_structure:
                        applied_specs.append(
                            {
                                "task_index": index,
                                "task_type": task_type,
                                "title": "Updated reading order",
                                "detail": str(suggestion.get("summary") or "The model reordered content to improve reading order."),
                                "importance": "high",
                                "metadata": {
                                    "suggested_action": suggested_action,
                                    "llm_suggestion": suggestion,
                                    "reopen_task": {
                                        "task_type": task_type,
                                        "title": str(task.get("title") or ""),
                                        "detail": str(task.get("detail") or ""),
                                        "severity": str(task.get("severity") or "high"),
                                        "blocking": bool(task.get("blocking", True)),
                                        "source": str(task.get("source") or "fidelity"),
                                        "metadata": task_metadata,
                                    },
                                },
                                "before": {"structure_json": current_structure},
                                "after": {"structure_json": next_structure},
                                "undo_payload": {"kind": "structure_json", "structure_json": current_structure},
                            }
                        )
                        current_structure = next_structure
                        continue

            if task_type == "table_semantics":
                if suggested_action in TABLE_NOOP_ACTIONS:
                    continue
                if can_accept_table_recommendation(current_structure, suggestion):
                    next_structure = apply_table_recommendation(current_structure, suggestion)
                    if next_structure:
                        applied_specs.append(
                            {
                                "task_index": index,
                                "task_type": task_type,
                                "title": "Updated table interpretation",
                                "detail": str(suggestion.get("summary") or "The model updated table semantics."),
                                "importance": "high",
                                "metadata": {
                                    "suggested_action": suggested_action,
                                    "llm_suggestion": suggestion,
                                    "reopen_task": {
                                        "task_type": task_type,
                                        "title": str(task.get("title") or ""),
                                        "detail": str(task.get("detail") or ""),
                                        "severity": str(task.get("severity") or "high"),
                                        "blocking": bool(task.get("blocking", True)),
                                        "source": str(task.get("source") or "fidelity"),
                                        "metadata": task_metadata,
                                    },
                                },
                                "before": {"structure_json": current_structure},
                                "after": {"structure_json": next_structure},
                                "undo_payload": {"kind": "structure_json", "structure_json": current_structure},
                            }
                        )
                        current_structure = next_structure
                        continue

            suggested_tasks.append(enriched_task)
    finally:
        await llm_client.close()

    return suggested_tasks, current_structure, applied_specs
