from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from app.services.llm_client import make_llm_client
from app.services.remediation_intelligence import generate_remediation_intelligence
from app.services.structure_intelligence_apply import (
    apply_reading_order_change,
    apply_table_change,
    can_accept_reading_order_change,
    can_accept_table_change,
)

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


async def auto_apply_structure_tasks(
    *, job, settings, review_tasks: list[dict[str, Any]], structure_json: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Generate and apply safe structure changes before persistence.

    Returns:
      remaining_tasks: tasks with attached model intelligence (noop confirmations removed)
      updated_structure: structure after safe applies
      applied_specs: change specs for tasks whose structure changes should be adopted if retry succeeds
    """
    remaining_tasks: list[dict[str, Any]] = []
    applied_specs: list[dict[str, Any]] = []
    current_structure = copy.deepcopy(structure_json)

    llm_client = make_llm_client(settings)
    try:
        for index, task in enumerate(review_tasks):
            if not isinstance(task, dict):
                continue
            if not bool(task.get("blocking")):
                remaining_tasks.append(task)
                continue
            task_type = str(task.get("task_type") or "")
            if task_type not in {"reading_order", "table_semantics"}:
                remaining_tasks.append(task)
                continue

            pseudo_task = _pseudo_task(task)
            try:
                intelligence = await generate_remediation_intelligence(
                    job=job, task=pseudo_task, llm_client=llm_client
                )
            except Exception:
                remaining_tasks.append(task)
                continue

            task_metadata = _metadata(task)
            task_metadata["remediation_intelligence"] = intelligence
            enriched_task = {**task, "metadata": task_metadata}
            suggested_action = str(intelligence.get("suggested_action") or "").strip()

            if task_type == "reading_order":
                if suggested_action in READING_ORDER_NOOP_ACTIONS:
                    # model confirmed current structure; no review needed
                    continue
                if can_accept_reading_order_change(current_structure, intelligence):
                    next_structure = apply_reading_order_change(current_structure, intelligence)
                    if next_structure:
                        applied_specs.append(
                            {
                                "task_index": index,
                                "task_type": task_type,
                                "title": "Updated reading order",
                                "detail": str(
                                    intelligence.get("summary")
                                    or "The model reordered content to improve reading order."
                                ),
                                "importance": "high",
                                "metadata": {
                                    "suggested_action": suggested_action,
                                    "remediation_intelligence": intelligence,
                                },
                                "before": {"structure_json": current_structure},
                                "after": {"structure_json": next_structure},
                                "undo_payload": {
                                    "kind": "structure_json",
                                    "structure_json": current_structure,
                                },
                            }
                        )
                        current_structure = next_structure
                        continue

            if task_type == "table_semantics":
                if suggested_action in TABLE_NOOP_ACTIONS:
                    continue
                if can_accept_table_change(current_structure, intelligence):
                    next_structure = apply_table_change(current_structure, intelligence)
                    if next_structure:
                        applied_specs.append(
                            {
                                "task_index": index,
                                "task_type": task_type,
                                "title": "Updated table interpretation",
                                "detail": str(
                                    intelligence.get("summary")
                                    or "The model updated table semantics."
                                ),
                                "importance": "high",
                                "metadata": {
                                    "suggested_action": suggested_action,
                                    "remediation_intelligence": intelligence,
                                },
                                "before": {"structure_json": current_structure},
                                "after": {"structure_json": next_structure},
                                "undo_payload": {
                                    "kind": "structure_json",
                                    "structure_json": current_structure,
                                },
                            }
                        )
                        current_structure = next_structure
                        continue

            remaining_tasks.append(enriched_task)
    finally:
        await llm_client.close()

    return remaining_tasks, current_structure, applied_specs
