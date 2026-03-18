"""Step 4: Generate figure semantics using a vision LLM."""

import logging
from dataclasses import dataclass
from typing import Any

from app.pipeline.structure import FigureInfo
from app.services.intelligence_gemini import confidence_label
from app.services.intelligence_gemini_figures import generate_figures_intelligence
from app.services.llm_client import LlmClient

logger = logging.getLogger(__name__)


def _caption_fallback(caption: str | None) -> str:
    if not isinstance(caption, str):
        return ""
    return caption.strip()


@dataclass
class AltTextResult:
    figure_index: int
    generated_text: str
    status: str = "approved"
    resolved_kind: str | None = None
    suggested_action: str = "set_alt_text"
    confidence: str = "medium"
    summary: str = ""
    reason: str = ""
    used_caption_fallback: bool = False
    used_placeholder_fallback: bool = False
    reviewable: bool = False
    importance: str = "medium"
    remediation_intelligence: dict[str, Any] | None = None


def _figure_reviewability(
    *,
    suggested_action: str,
    confidence: str,
    used_caption_fallback: bool,
    used_placeholder_fallback: bool,
    is_decorative: bool,
) -> tuple[bool, str]:
    if suggested_action == "reclassify_region":
        return False, "high"
    if used_placeholder_fallback:
        return True, "high"
    if suggested_action == "manual_only":
        return True, "high"
    if is_decorative:
        return (confidence != "high"), "medium"
    if used_caption_fallback:
        return True, "medium"
    if confidence != "high":
        return True, "medium"
    return False, "low"


def figure_applied_change_specs(
    *,
    figures: list[FigureInfo],
    alt_texts: list[AltTextResult],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    figure_by_index = {figure.index: figure for figure in figures}
    for result in alt_texts:
        if result.status == "reclassified":
            continue
        figure = figure_by_index.get(result.figure_index)
        if figure is None:
            continue

        if result.status == "rejected":
            title = f"Hid figure {result.figure_index + 1}"
            detail = result.summary or "The model hid this figure from assistive technology."
        else:
            title = f"Updated figure {result.figure_index + 1}"
            detail = result.summary or "The model updated this figure description."

        metadata: dict[str, Any] = {
            "figure_index": result.figure_index,
            "summary": result.summary,
            "reason": result.reason,
            "confidence": result.confidence,
            "suggested_action": result.suggested_action,
            "remediation_intelligence": result.remediation_intelligence or {},
        }
        if figure.page is not None:
            metadata["page"] = int(figure.page) + 1
        if isinstance(figure.caption, str) and figure.caption.strip():
            metadata["caption"] = figure.caption.strip()
        if isinstance(figure.bbox, dict):
            metadata["bbox"] = figure.bbox

        specs.append(
            {
                "task_type": "figure_semantics",
                "title": title,
                "detail": detail,
                "importance": result.importance,
                "reviewable": result.reviewable,
                "metadata": metadata,
                "before": {
                    "generated_text": None,
                    "edited_text": None,
                    "status": None,
                },
                "after": {
                    "generated_text": result.generated_text,
                    "edited_text": None,
                    "status": result.status,
                },
                "undo_payload": {
                    "kind": "alt_text_entry",
                    "figure_index": result.figure_index,
                    "generated_text": None,
                    "edited_text": None,
                    "status": None,
                    "delete_if_absent": True,
                },
            }
        )
    return specs


async def generate_alt_text(
    figures: list[FigureInfo],
    llm_client: LlmClient,
    *,
    job=None,
    original_filename: str = "",
) -> list[AltTextResult]:
    """Generate alt text for each figure using a vision LLM."""
    results = []
    existing: dict[int, AltTextResult] = {}
    figures_for_intelligence: list[FigureInfo] = []

    for fig in figures:
        if not fig.path.exists():
            logger.warning(f"Figure {fig.index} image not found: {fig.path}")
            fallback = _caption_fallback(fig.caption)
            generated_text = fallback or "[Image file not found]"
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=generated_text,
                status="approved",
                suggested_action="manual_only",
                confidence="low",
                summary="The image file was missing, so the app used the best available fallback.",
                reason="The extracted figure image was not available during semantic analysis.",
                used_caption_fallback=bool(fallback),
                used_placeholder_fallback=not bool(fallback),
                reviewable=True,
                importance="high",
                remediation_intelligence={
                    "suggested_action": "manual_only",
                    "confidence": "low",
                    "summary": "The image file was missing, so the app used the best available fallback.",
                    "reason": "The extracted figure image was not available during semantic analysis.",
                },
            )
            continue
        figures_for_intelligence.append(fig)

    adjudications = await generate_figures_intelligence(
        figures=figures_for_intelligence,
        llm_client=llm_client,
        job=job,
        original_filename=original_filename,
    )

    for fig, adjudication in zip(figures_for_intelligence, adjudications, strict=False):
        try:
            suggested_action = str(adjudication.get("suggested_action") or "").strip()
            confidence = confidence_label(adjudication.get("confidence"))
            resolved_kind = str(adjudication.get("resolved_kind") or "").strip() or None
            if suggested_action == "reclassify_region" and resolved_kind:
                existing[fig.index] = AltTextResult(
                    figure_index=fig.index,
                    generated_text="",
                    status="reclassified",
                    resolved_kind=resolved_kind,
                    suggested_action=suggested_action,
                    confidence=confidence,
                    summary=str(adjudication.get("summary") or "").strip(),
                    reason=str(adjudication.get("reason") or "").strip(),
                    remediation_intelligence=dict(adjudication),
                )
                continue
            is_decorative = bool(suggested_action == "mark_decorative" or adjudication.get("is_decorative"))
            if suggested_action == "mark_decorative" or adjudication.get("is_decorative"):
                text = "decorative"
            else:
                text = str(adjudication.get("alt_text") or "").strip()
            used_caption_fallback = False
            used_placeholder_fallback = False
            if not text:
                fallback = _caption_fallback(fig.caption)
                if fallback:
                    text = fallback
                    used_caption_fallback = True
                else:
                    text = "[Could not determine figure semantics]"
                    used_placeholder_fallback = True
            reviewable, importance = _figure_reviewability(
                suggested_action=suggested_action or "manual_only",
                confidence=confidence,
                used_caption_fallback=used_caption_fallback,
                used_placeholder_fallback=used_placeholder_fallback,
                is_decorative=is_decorative,
            )
            logger.info(f"Generated alt text for figure {fig.index}: {text[:80]}...")
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=text,
                status="rejected" if is_decorative else "approved",
                resolved_kind=resolved_kind,
                suggested_action=suggested_action or ("mark_decorative" if is_decorative else "set_alt_text"),
                confidence=confidence,
                summary=str(adjudication.get("summary") or "").strip(),
                reason=str(adjudication.get("reason") or "").strip(),
                used_caption_fallback=used_caption_fallback,
                used_placeholder_fallback=used_placeholder_fallback,
                reviewable=reviewable,
                importance=importance,
                remediation_intelligence=dict(adjudication),
            )
        except Exception as e:
            logger.error(f"Alt text generation failed for figure {fig.index}: {e}")
            fallback = _caption_fallback(fig.caption)
            generated_text = fallback or f"[Generation failed: {e}]"
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=generated_text,
                status="approved",
                resolved_kind=None,
                suggested_action="manual_only",
                confidence="low",
                summary="The app used a fallback because the semantic analysis failed.",
                reason=f"Figure semantic analysis failed: {e}",
                used_caption_fallback=bool(fallback),
                used_placeholder_fallback=not bool(fallback),
                reviewable=True,
                importance="high",
                remediation_intelligence={
                    "suggested_action": "manual_only",
                    "confidence": "low",
                    "summary": "The app used a fallback because the semantic analysis failed.",
                    "reason": f"Figure semantic analysis failed: {e}",
                },
            )

    for fig in figures:
        result = existing.get(fig.index)
        if result is None:
            fallback = _caption_fallback(fig.caption)
            generated_text = fallback or "[Could not determine figure semantics]"
            result = AltTextResult(
                figure_index=fig.index,
                generated_text=generated_text,
                status="approved",
                resolved_kind=None,
                suggested_action="manual_only",
                confidence="low",
                summary="The app used a fallback because no semantic decision was available.",
                reason="No figure semantic decision was returned.",
                used_caption_fallback=bool(fallback),
                used_placeholder_fallback=not bool(fallback),
                reviewable=True,
                importance="high",
                remediation_intelligence={
                    "suggested_action": "manual_only",
                    "confidence": "low",
                    "summary": "The app used a fallback because no semantic decision was available.",
                    "reason": "No figure semantic decision was returned.",
                },
            )
        results.append(result)

    return results
