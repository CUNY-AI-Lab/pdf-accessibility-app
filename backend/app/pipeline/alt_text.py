"""Step 4: Generate figure semantics using a vision LLM."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.pipeline.figure_rules import decorative_reasons, model_decoration_allowed
from app.pipeline.structure import FigureInfo
from app.pipeline.tagger_semantics import is_placeholder_alt_text
from app.services.alt_text_cache import AltTextCache, usable_description
from app.services.intelligence_figures import (
    generate_figure_intelligence,
    generate_figures_intelligence,
)
from app.services.intelligence_normalize import confidence_label
from app.services.llm_client import LlmClient
from app.services.thread_work import to_thread

logger = logging.getLogger(__name__)

# A figure the app could not describe: it has no alt text and waits for a
# person. Nothing is approved or written in its place.
NEEDS_DESCRIPTION = "pending_review"

# Bump whenever the crop-description instructions or type templates change.
PROMPT_VERSION = "3-exact-request-context"


def _caption_fallback(caption: str | None) -> str:
    if not isinstance(caption, str):
        return ""
    caption = caption.strip()
    return caption if usable_description(caption) else ""


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

        if (result.remediation_intelligence or {}).get("source") == "author":
            title = f"Kept author description for figure {result.figure_index + 1}"
            detail = "Kept the author's description for the matching figure."
        elif result.status == "rejected":
            title = f"Hid figure {result.figure_index + 1}"
            detail = result.summary or "The model hid this figure from assistive technology."
        elif result.status == NEEDS_DESCRIPTION:
            title = f"Figure {result.figure_index + 1} needs a description"
            if result.resolved_kind in ("table", "form_region"):
                kind = "a table" if result.resolved_kind == "table" else "part of a form"
                detail = (
                    f"The model judged this figure to be {kind}, but nothing tagged in the "
                    "PDF carries its content, so it stays a figure with no alt text. "
                    "Add a description before distribution."
                )
            else:
                detail = (
                    "The app could not describe this figure, so it has no alt text. "
                    "Add a description before distribution."
                )
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
    settings: Settings | None = None,
    structure: dict[str, Any] | None = None,
    pdf_path: Path | None = None,
) -> list[AltTextResult]:
    """Generate alt text for each figure using a vision LLM."""
    results = []
    existing: dict[int, AltTextResult] = {}
    figures_for_intelligence: list[FigureInfo] = []
    structure = structure or {}
    rules = await to_thread(
        decorative_reasons, figures, structure=structure, pdf_path=pdf_path,
    )
    model = getattr(llm_client, "model", None)
    cache = None
    if isinstance(model, str) and model.strip():
        settings = settings or get_settings()
        # Both v1 and the runner keep processing/ beside uploads/ and output/.
        # This file is outside per-job directories and the runner's state tar.
        if settings.alt_text_cache_enabled:
            cache = AltTextCache((settings.alt_text_cache_dir or settings.processing_dir.parent) / "alt_text_cache.sqlite3")

    author_descriptions = {
        e.get("figure_index"): e.get("author_alt") or e.get("author_actual_text")
        for e in structure.get("elements", []) if e.get("type") == "figure"
        and not is_placeholder_alt_text(e.get("author_alt") or e.get("author_actual_text"))
    }
    for fig in figures:
        if fig.index in author_descriptions:
            existing[fig.index] = AltTextResult(
                figure_index=fig.index, generated_text=author_descriptions[fig.index],
                status="approved", confidence="high", reviewable=False,
                summary="Kept the author description for the matching figure.",
                reason="Matched author description", remediation_intelligence={"source": "author"},
            )
            continue
        if fig.index in rules:
            reason = rules[fig.index]
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text="decorative",
                status="rejected",
                suggested_action="mark_decorative",
                confidence="high",
                summary="Local evidence identified a decorative image.",
                reason=reason,
                reviewable=True,
                remediation_intelligence={"source": "rule", "rule": reason.split(":", 1)[0],
                                         "reason": reason},
            )
            continue
        if not fig.path.exists():
            logger.warning(f"Figure {fig.index} image not found: {fig.path}")
            fallback = _caption_fallback(fig.caption)
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=fallback,
                status="approved" if fallback else NEEDS_DESCRIPTION,
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
        **({"description_cache": cache, "prompt_version": PROMPT_VERSION} if cache else {}),
    ) if figures_for_intelligence else []
    decisions = {
        figure.index: decision
        for figure, decision in zip(
            sorted(figures_for_intelligence, key=lambda figure: figure.index), adjudications, strict=False,
        )
    }

    for fig in figures:
        if fig.index not in decisions:
            continue
        adjudication = decisions[fig.index]
        try:
            suggested_action = str(adjudication.get("suggested_action") or "").strip()
            confidence = confidence_label(adjudication.get("confidence"))
            resolved_kind = str(adjudication.get("resolved_kind") or "").strip() or None
            if not model_decoration_allowed(fig, structure, adjudication) and (
                suggested_action == "mark_decorative" or adjudication.get("is_decorative")
                or suggested_action == "reclassify_region" and resolved_kind == "artifact"
            ):
                adjudication = {
                    **adjudication, "suggested_action": "manual_only", "is_decorative": False,
                    "alt_text": "", "confidence": "low",
                    "reason": "Captioned or referenced content cannot be marked decorative; overriding a classification needs an explanation.",
                }
                suggested_action = "manual_only"
                confidence = "low"
                resolved_kind = None
            if suggested_action == "reclassify_region" and resolved_kind:
                existing[fig.index] = AltTextResult(
                    figure_index=fig.index,
                    generated_text=str(adjudication.get("alt_text") or "").strip(),
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
                if not usable_description(text):
                    text = ""
            used_caption_fallback = False
            used_placeholder_fallback = False
            if not text:
                fallback = _caption_fallback(fig.caption)
                if fallback:
                    text = fallback
                    used_caption_fallback = True
                else:
                    used_placeholder_fallback = True
            reviewable, importance = _figure_reviewability(
                suggested_action=suggested_action or "manual_only",
                confidence=confidence,
                used_caption_fallback=used_caption_fallback,
                used_placeholder_fallback=used_placeholder_fallback,
                is_decorative=is_decorative,
            )
            logger.info(
                "Figure %s: %s (%s confidence): %s; %s",
                fig.index,
                suggested_action or "no action",
                confidence,
                text[:80],
                str(adjudication.get("reason") or "")[:160],
            )
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=text,
                status=(
                    "rejected"
                    if is_decorative
                    else NEEDS_DESCRIPTION
                    if used_placeholder_fallback
                    else "approved"
                ),
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
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=fallback,
                status="approved" if fallback else NEEDS_DESCRIPTION,
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
            result = AltTextResult(
                figure_index=fig.index,
                generated_text=fallback,
                status="approved" if fallback else NEEDS_DESCRIPTION,
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


FIGURE_RECOVERY_FEEDBACK = """This region remains a figure in the accessible PDF.
No tagged table or form carries its content. Supply alt_text describing the visible
crop even if it looks like a table, form, screenshot, or part of a page layout.
For a table, describe its subject, headers and legible key information briefly.
For a form or screenshot, describe visible controls and state; for a photo,
describe visible subjects without inferring identities. Return set_alt_text with
a nonempty description, not reclassify_region. If the crop is truly empty or
pure decoration with no information, return mark_decorative. Never use a
placeholder or invent unreadable content; use manual_only if no description
can be grounded in the crop. A captioned or referenced figure needs a description.
"""


async def describe_unresolved_figures(
    figures: list[FigureInfo],
    alt_texts: list[AltTextResult],
    llm_client: LlmClient,
    *,
    structure: dict[str, Any],
    job=None,
    original_filename: str = "",
) -> None:
    """One targeted recovery for each unresolved figure after coverage checks.

    This prompt differs from the initial semantic decision, so it deliberately
    bypasses the persistent description cache. Failure leaves the release hold.
    """
    by_index = {figure.index: figure for figure in figures}
    live_indexes = (
        {e.get("figure_index") for e in structure["elements"] if e.get("type") == "figure"}
        if "elements" in structure else set(by_index)
    )
    present = {result.figure_index for result in alt_texts}
    for index in sorted(set(by_index) & live_indexes - present):
        alt_texts.append(AltTextResult(
            figure_index=index, generated_text="", status=NEEDS_DESCRIPTION,
        ))

    async def recover(result: AltTextResult) -> None:
        if result.figure_index not in live_indexes or result.status != NEEDS_DESCRIPTION:
            return
        figure = by_index.get(result.figure_index)
        if figure is None or not figure.path.is_file():
            return
        decision = await generate_figure_intelligence(
            figure=figure, llm_client=llm_client, job=job,
            original_filename=original_filename,
            reviewer_feedback=FIGURE_RECOVERY_FEEDBACK,
            previous_intelligence=result.remediation_intelligence,
            description_only=True,
        )
        action = decision.get("suggested_action")
        text = str(decision.get("alt_text") or "").strip()
        if action == "set_alt_text" and usable_description(text):
            result.generated_text = text
            result.status = "approved"
            result.suggested_action = "set_alt_text"
            result.used_placeholder_fallback = False
            result.used_caption_fallback = False
        elif action == "mark_decorative" and model_decoration_allowed(figure, structure, decision):
            result.generated_text = "decorative"
            result.status = "rejected"
            result.suggested_action = "mark_decorative"
        else:
            return
        result.confidence = confidence_label(decision.get("confidence"))
        result.reason = str(decision.get("reason") or "")
        result.summary = str(decision.get("summary") or "")
        result.remediation_intelligence = dict(decision)
        result.reviewable = True

    # Bound queued work as well as model requests on large documents.
    concurrency = max(1, get_settings().alt_text_max_concurrency)
    for start in range(0, len(alt_texts), concurrency):
        await asyncio.gather(*(recover(result) for result in alt_texts[start:start + concurrency]))
