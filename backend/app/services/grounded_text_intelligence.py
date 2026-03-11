"""Grounded-text review task and fidelity bookkeeping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.intelligence_gemini_pages import generate_suspicious_text_intelligence
from app.services.llm_client import make_llm_client

if TYPE_CHECKING:
    from app.config import Settings
    from app.models import Job


def recalculate_fidelity_summary(
    fidelity_report: dict[str, object],
    review_tasks: list[dict[str, object]],
) -> dict[str, object]:
    blocking_tasks = sum(1 for task in review_tasks if isinstance(task, dict) and bool(task.get("blocking")))
    advisory_tasks = sum(1 for task in review_tasks if isinstance(task, dict) and not bool(task.get("blocking")))
    fidelity_report["passed"] = blocking_tasks == 0
    fidelity_report["summary"] = {
        "blocking_tasks": blocking_tasks,
        "advisory_tasks": advisory_tasks,
        "total_tasks": len(review_tasks),
    }
    return fidelity_report


def update_grounded_text_check(
    fidelity_report: dict[str, object],
    *,
    status: str,
    message: str,
    candidate_count: int,
    confirmed_count: int,
) -> dict[str, object]:
    checks = fidelity_report.get("checks")
    if not isinstance(checks, list):
        checks = []
        fidelity_report["checks"] = checks
    for check in checks:
        if isinstance(check, dict) and str(check.get("check") or "") == "grounded_text_fidelity":
            check["status"] = status
            check["message"] = message
            check["metrics"] = {
                "candidate_blocks": candidate_count,
                "confirmed_blocks": confirmed_count,
            }
            return fidelity_report
    checks.append(
        {
            "check": "grounded_text_fidelity",
            "status": status,
            "message": message,
            "metrics": {
                "candidate_blocks": candidate_count,
                "confirmed_blocks": confirmed_count,
            },
        }
    )
    return fidelity_report


def apply_grounded_text_adjudication(
    review_tasks: list[dict[str, object]],
    fidelity_report: dict[str, object],
    adjudication: dict[str, object] | None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    task_index = next(
        (
            index
            for index, task in enumerate(review_tasks)
            if str(task.get("task_type") or "") == "content_fidelity"
            and str(task.get("source") or "fidelity") == "fidelity"
            and isinstance(task.get("metadata"), dict)
            and bool(task["metadata"].get("grounded_text_candidate"))
        ),
        None,
    )
    if task_index is None:
        return review_tasks, fidelity_report

    task = review_tasks[task_index]
    metadata = task.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    candidate_blocks = metadata.get("flagged_blocks")
    if not isinstance(candidate_blocks, list):
        candidate_blocks = []

    confirmed_blocks: list[dict[str, object]] = []
    if isinstance(adjudication, dict):
        for block in adjudication.get("blocks", []) if isinstance(adjudication.get("blocks"), list) else []:
            if isinstance(block, dict) and bool(block.get("should_block_accessibility", False)):
                confirmed_blocks.append(block)

    if not confirmed_blocks:
        review_tasks = [
            existing
            for index, existing in enumerate(review_tasks)
            if index != task_index
        ]
        fidelity_report = update_grounded_text_check(
            fidelity_report,
            status="pass",
            message="Grounded semantic review did not confirm any accessibility-significant text mismatch.",
            candidate_count=len(candidate_blocks),
            confirmed_count=0,
        )
        return review_tasks, recalculate_fidelity_summary(fidelity_report, review_tasks)

    issue_types = sorted(
        {
            str(block.get("issue_type") or "").strip()
            for block in confirmed_blocks
            if str(block.get("issue_type") or "").strip()
        }
    )
    pages_to_check = sorted(
        {
            int(block["page"])
            for block in confirmed_blocks
            if isinstance(block.get("page"), int)
        }
    )
    task["title"] = "Verify readable text on flagged blocks"
    task["detail"] = (
        "Grounded Gemini review confirmed text blocks where the extracted accessible text "
        "likely does not match what appears on the page."
    )
    task["severity"] = "high"
    task["blocking"] = True
    task["metadata"] = {
        **metadata,
        "grounded_text_candidate": True,
        "grounded_text_llm_adjudicated": True,
        "pages_to_check": pages_to_check,
        "flagged_blocks": confirmed_blocks,
        "issue_types": issue_types,
        "grounded_target_count": len(confirmed_blocks),
        "encoding_problem_count": len(
            [block for block in confirmed_blocks if str(block.get("issue_type") or "") == "encoding_problem"]
        ),
        "llm_summary": str(adjudication.get("summary") or "").strip() if isinstance(adjudication, dict) else "",
        "llm_confidence": str(adjudication.get("confidence") or "").strip() if isinstance(adjudication, dict) else "",
    }
    fidelity_report = update_grounded_text_check(
        fidelity_report,
        status="fail",
        message="Grounded semantic review confirmed accessibility-significant extracted-text mismatches.",
        candidate_count=len(candidate_blocks),
        confirmed_count=len(confirmed_blocks),
    )
    return review_tasks, recalculate_fidelity_summary(fidelity_report, review_tasks)


def blocking_task_count(review_tasks: list[dict[str, object]]) -> int:
    return sum(1 for task in review_tasks if isinstance(task, dict) and bool(task.get("blocking")))


async def adjudicate_grounded_text_candidates(
    *,
    job: Job,
    settings: Settings,
    review_tasks: list[dict[str, object]],
    fidelity_report: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object] | None]:
    candidate_task = next(
        (
            task
            for task in review_tasks
            if str(task.get("task_type") or "") == "content_fidelity"
            and str(task.get("source") or "fidelity") == "fidelity"
            and isinstance(task.get("metadata"), dict)
            and bool(task["metadata"].get("grounded_text_candidate"))
        ),
        None,
    )
    if not isinstance(candidate_task, dict):
        return review_tasks, fidelity_report, None

    metadata = candidate_task.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    pages_to_check = metadata.get("pages_to_check")
    suspicious_blocks = metadata.get("flagged_blocks")
    if not isinstance(pages_to_check, list) or not isinstance(suspicious_blocks, list) or not suspicious_blocks:
        return review_tasks, fidelity_report, None

    llm_client = make_llm_client(settings)
    try:
        adjudication = await generate_suspicious_text_intelligence(
            job=job,
            page_numbers=[int(page) for page in pages_to_check if isinstance(page, int) and page >= 1],
            suspicious_blocks=suspicious_blocks,
            llm_client=llm_client,
        )
    except Exception as exc:
        candidate_task["metadata"] = {
            **metadata,
            "grounded_text_candidate": True,
            "grounded_text_llm_adjudicated": False,
            "grounded_text_llm_error": str(exc),
        }
        fidelity_report = update_grounded_text_check(
            fidelity_report,
            status="warning",
            message="Grounded semantic adjudication failed; suspicious text remains advisory only.",
            candidate_count=len(suspicious_blocks),
            confirmed_count=0,
        )
        return review_tasks, recalculate_fidelity_summary(fidelity_report, review_tasks), None
    finally:
        await llm_client.close()

    updated_tasks, updated_report = apply_grounded_text_adjudication(review_tasks, fidelity_report, adjudication)
    return updated_tasks, updated_report, adjudication
