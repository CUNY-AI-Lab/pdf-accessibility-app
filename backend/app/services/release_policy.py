"""Release holds are named failures, independent of the review backlog."""

from __future__ import annotations

from typing import Any

from app.services.review_surface import is_user_visible_review_task_type

FIDELITY_HOLD_CHECKS = {
    "tagged_text_coverage": (
        "content_fidelity",
        "Text is missing from the screen-reader reading",
        "The screen-reader coverage check found catastrophic text loss.",
    ),
    "figure_descriptions": (
        "alt_text",
        "Figures have no usable description",
        "A Figure has no usable Alt or ActualText for a screen reader to announce.",
    ),
    "table_readability": (
        "table_semantics",
        "This table is rotated or its text could not be read in order",
        "Table headers cannot repair rotated or disordered cell text.",
    ),
    "table_headers": (
        "table_semantics",
        "Data tables have no headers",
        "A data table has no TH header cells to identify its rows or columns.",
    ),
}


def release_hold_reasons(*, compliant: bool, checks: list[dict[str, Any]]) -> list[str]:
    """Use saved check verdicts too: no PDF processing is needed to replay policy."""
    reasons = [] if compliant else ["compliance"]
    for check in checks:
        name = str(check.get("check") or "unknown")
        if check.get("status") == "error" or (
            name in FIDELITY_HOLD_CHECKS and check.get("status") == "fail"
        ):
            if name not in reasons:
                reasons.append(name)
    return reasons


def apply_release_policy(
    fidelity_report: dict[str, Any],
    review_tasks: list[dict[str, Any]],
    *,
    compliant: bool,
) -> None:
    """Normalize flags and ensure every hold has a visible explanation, in place.

    Run after checks and again after model adjudication. A task's old blocking
    flag, severity or type is not evidence of a release failure.
    """
    checks = fidelity_report.get("checks", [])
    reasons = release_hold_reasons(compliant=compliant, checks=checks)
    for check in checks:
        if check.get("status") == "fail" and check.get("check") not in reasons:
            check["status"] = "warning"

    descriptions = {
        **FIDELITY_HOLD_CHECKS,
        "compliance": (
            "content_fidelity",
            "PDF/UA validation did not pass",
            "veraPDF reports that this document is not compliant with PDF/UA.",
        ),
    }
    for check in checks:
        if check.get("status") == "error":
            name = str(check.get("check") or "unknown")
            descriptions[name] = (
                "content_fidelity",
                f"Accessibility check could not finish: {name.replace('_', ' ')}",
                "An internal check failed, so its accessibility result is unknown.",
            )

    explained = set()
    for task in review_tasks:
        metadata = task.get("metadata")
        reason = metadata.get("check") if isinstance(metadata, dict) else None
        if task.get("source") == "validation":
            reason = "compliance"
        task["blocking"] = reason in reasons
        if task["blocking"]:
            explained.add(reason)
            task_type, title, detail = descriptions[reason]
            if not is_user_visible_review_task_type(str(task.get("task_type") or "")):
                task["task_type"] = task_type
            task["title"] = task.get("title") or title
            task["detail"] = task.get("detail") or detail

    for reason in reasons:
        if reason in explained:
            continue
        task_type, title, detail = descriptions[reason]
        review_tasks.append({
            "task_type": task_type,
            "title": title,
            "detail": detail,
            "severity": "high",
            "blocking": True,
            "status": "pending_review",
            "source": "validation" if reason == "compliance" else "fidelity",
            "metadata": {"check": reason},
        })

    blocking = sum(bool(task.get("blocking")) for task in review_tasks)
    fidelity_report["passed"] = not reasons
    fidelity_report["summary"] = {
        "blocking_tasks": blocking,
        "advisory_tasks": len(review_tasks) - blocking,
        "total_tasks": len(review_tasks),
    }
