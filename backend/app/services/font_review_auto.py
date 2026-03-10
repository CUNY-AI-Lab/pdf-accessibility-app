"""Auto-apply helpers for LLM-assisted font review tasks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from app.pipeline.validator import validate_pdf
from app.services.file_storage import get_output_path
from app.services.font_artifact import apply_artifact_batch_to_contexts
from app.services.font_unicode_override import apply_unicode_override_to_context
from app.services.llm_client import make_llm_client
from app.services.review_suggestions import (
    generate_review_suggestion,
    select_auto_font_review_resolution,
)
from app.services.validation_compare import is_better_validation

if TYPE_CHECKING:
    from app.config import Settings
    from app.models import Job


def summarize_llm_font_map_suggestion(suggestion: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in (
        "task_type",
        "summary",
        "confidence",
        "suggested_action",
        "reason",
        "generated_at",
        "model",
        "review_focus",
        "actualtext_candidates",
        "reviewer_checklist",
    ):
        if key in suggestion:
            summary[key] = suggestion[key]
    return summary


def fidelity_not_worse(candidate: dict[str, object], current: dict[str, object]) -> bool:
    candidate_summary = candidate.get("summary") if isinstance(candidate, dict) else None
    current_summary = current.get("summary") if isinstance(current, dict) else None
    candidate_blocking = int(candidate_summary.get("blocking_tasks", 0) or 0) if isinstance(candidate_summary, dict) else 0
    current_blocking = int(current_summary.get("blocking_tasks", 0) or 0) if isinstance(current_summary, dict) else 0
    return candidate_blocking <= current_blocking


async def attempt_auto_llm_font_map(
    *,
    job: Job,
    settings: Settings,
    output_pdf: Path,
    current_validation,
    review_tasks: list[dict[str, object]],
) -> tuple[dict[str, object], object | None, Path | None, dict[tuple[str, str], dict[str, object]]]:
    audit: dict[str, object] = {
        "enabled": bool(settings.auto_apply_llm_font_map),
        "attempted": False,
        "applied": False,
        "reason": "",
    }
    metadata_overrides: dict[tuple[str, str], dict[str, object]] = {}

    if not settings.auto_apply_llm_font_map:
        audit["reason"] = "disabled"
        return audit, None, None, metadata_overrides

    task = next(
        (
            task
            for task in review_tasks
            if bool(task.get("blocking"))
            and str(task.get("task_type") or "") == "font_text_fidelity"
        ),
        None,
    )
    if not isinstance(task, dict):
        audit["reason"] = "no_eligible_font_review_task"
        return audit, None, None, metadata_overrides

    task_type = str(task.get("task_type") or "font_text_fidelity")
    task_source = str(task.get("source") or "validation")
    pseudo_task = SimpleNamespace(
        task_type=task_type,
        title=str(task.get("title") or ""),
        detail=str(task.get("detail") or ""),
        severity=str(task.get("severity") or "high"),
        source=task_source,
        metadata_json=json.dumps(task.get("metadata", {})),
    )

    llm_client = make_llm_client(settings)
    audit["attempted"] = True
    try:
        suggestion = await generate_review_suggestion(
            job=job,
            task=pseudo_task,
            llm_client=llm_client,
        )
    except Exception as exc:
        audit["reason"] = f"suggestion_failed: {exc}"
        return audit, None, None, metadata_overrides
    finally:
        await llm_client.close()

    suggestion_summary = summarize_llm_font_map_suggestion(suggestion)
    metadata_overrides[(task_type, task_source)] = {
        "llm_suggestion": suggestion_summary,
    }
    audit["suggestion"] = suggestion_summary

    selected = select_auto_font_review_resolution(
        job=job,
        task=pseudo_task,
        suggestion=suggestion,
    )
    if not isinstance(selected, dict):
        audit["reason"] = "suggestion_not_eligible"
        metadata_overrides[(task_type, task_source)]["llm_auto_font_map"] = {
            "attempted": True,
            "applied": False,
            "reason": audit["reason"],
        }
        return audit, None, None, metadata_overrides

    patched_output = get_output_path(
        job.id,
        f"accessible_auto_llm_fontmap_{job.original_filename}",
    )
    resolution_type = str(selected.get("resolution_type") or "font_map")

    try:
        if resolution_type == "artifact":
            targets = selected.get("targets")
            if not isinstance(targets, list) or not targets:
                raise ValueError("artifact resolution missing targets")
            context_paths = [
                str(target.get("context_path") or "").strip()
                for target in targets
                if isinstance(target, dict)
            ]
            if any(not context_path for context_path in context_paths):
                raise ValueError("artifact resolution missing context path")
            apply_artifact_batch_to_contexts(
                input_pdf=output_pdf,
                output_pdf=patched_output,
                context_paths=context_paths,
            )
        else:
            task_metadata = task.get("metadata", {})
            raw_targets = task_metadata.get("font_review_targets") if isinstance(task_metadata, dict) else []
            context_path = ""
            if isinstance(raw_targets, list):
                for target in raw_targets:
                    if not isinstance(target, dict):
                        continue
                    page = target.get("page")
                    operator_index = target.get("operator_index")
                    if not isinstance(page, int) or not isinstance(operator_index, int):
                        continue
                    if page == int(selected["page_number"]) and operator_index == int(selected["operator_index"]):
                        context_path = str(target.get("context_path") or "").strip()
                        break
            if not context_path:
                raise ValueError("missing_context_path")
            apply_unicode_override_to_context(
                input_pdf=output_pdf,
                output_pdf=patched_output,
                context_path=context_path,
                unicode_text=str(selected["unicode_text"]),
            )
    except Exception as exc:
        reason = str(exc)
        if reason == "missing_context_path":
            audit["reason"] = "missing_context_path"
        else:
            audit["reason"] = f"apply_failed: {exc}"
        metadata_overrides[(task_type, task_source)]["llm_auto_font_map"] = {
            "attempted": True,
            "applied": False,
            "reason": audit["reason"],
        }
        return audit, None, None, metadata_overrides

    candidate_validation = await validate_pdf(
        pdf_path=patched_output,
        verapdf_path=settings.verapdf_path,
        flavour=settings.verapdf_flavour,
        timeout_seconds=settings.subprocess_timeout_validation,
    )

    if not is_better_validation(candidate_validation, current_validation):
        fallback_unicode_text = str(selected.get("unicode_text") or "").strip()
        fallback_targets = selected.get("targets") if isinstance(selected.get("targets"), list) else []
        fallback_context_path = ""
        if fallback_targets:
            first_target = fallback_targets[0]
            if isinstance(first_target, dict):
                fallback_context_path = str(first_target.get("context_path") or "").strip()

        if (
            resolution_type == "artifact"
            and fallback_unicode_text
            and fallback_context_path
        ):
            fallback_output = get_output_path(
                job.id,
                f"accessible_auto_llm_fontmap_fallback_{job.original_filename}",
            )
            try:
                apply_unicode_override_to_context(
                    input_pdf=output_pdf,
                    output_pdf=fallback_output,
                    context_path=fallback_context_path,
                    unicode_text=fallback_unicode_text,
                )
                fallback_validation = await validate_pdf(
                    pdf_path=fallback_output,
                    verapdf_path=settings.verapdf_path,
                    flavour=settings.verapdf_flavour,
                    timeout_seconds=settings.subprocess_timeout_validation,
                )
            except Exception as exc:
                audit["reason"] = f"fallback_apply_failed: {exc}"
                metadata_overrides[(task_type, task_source)]["llm_auto_font_map"] = {
                    "attempted": True,
                    "applied": False,
                    "reason": audit["reason"],
                }
                return audit, None, None, metadata_overrides

            if is_better_validation(fallback_validation, current_validation):
                candidate_validation = fallback_validation
                patched_output = fallback_output
                resolution_type = "font_map_fallback"
            else:
                audit["reason"] = "no_validation_improvement"
                metadata_overrides[(task_type, task_source)]["llm_auto_font_map"] = {
                    "attempted": True,
                    "applied": False,
                    "reason": audit["reason"],
                }
                return audit, None, None, metadata_overrides
        else:
            audit["reason"] = "no_validation_improvement"
            metadata_overrides[(task_type, task_source)]["llm_auto_font_map"] = {
                "attempted": True,
                "applied": False,
                "reason": audit["reason"],
            }
            return audit, None, None, metadata_overrides

    audit.update({
        "applied": True,
        "reason": "applied",
        "resolution_type": resolution_type,
        "font": str(selected.get("font") or ""),
        "font_base_name": str(selected.get("font_base_name") or ""),
        "font_code_hex": str(selected.get("font_code_hex") or ""),
        "unicode_text": str(selected.get("unicode_text") or ""),
        "target_count": int(selected.get("target_count", 1) or 1),
        "suggested_action": str(suggestion.get("suggested_action") or ""),
        "model": str(suggestion.get("model") or settings.llm_model),
    })
    metadata_overrides[(task_type, task_source)]["llm_auto_font_map"] = {
        "attempted": True,
        "applied": True,
        "reason": "applied",
        "resolution_type": resolution_type,
        "font_code_hex": audit["font_code_hex"],
        "unicode_text": audit["unicode_text"],
        "target_count": audit["target_count"],
    }
    return audit, candidate_validation, patched_output, metadata_overrides
