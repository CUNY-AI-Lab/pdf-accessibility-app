"""Step 4: Generate alt text for figures using a vision LLM."""

import logging
from dataclasses import dataclass

from app.pipeline.structure import FigureInfo
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
    status: str = "pending_review"
    resolved_kind: str | None = None


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
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=fallback or "[Image file not found]",
                status="pending_review",
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
            resolved_kind = str(adjudication.get("resolved_kind") or "").strip() or None
            if suggested_action == "reclassify_region" and resolved_kind:
                existing[fig.index] = AltTextResult(
                    figure_index=fig.index,
                    generated_text="",
                    status="reclassified",
                    resolved_kind=resolved_kind,
                )
                continue
            if suggested_action == "mark_decorative" or adjudication.get("is_decorative"):
                text = "decorative"
            else:
                text = str(adjudication.get("alt_text") or "").strip()
            if not text:
                text = _caption_fallback(fig.caption) or "[Could not determine figure semantics]"
            logger.info(f"Generated alt text for figure {fig.index}: {text[:80]}...")
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=text,
                resolved_kind=resolved_kind,
            )
        except Exception as e:
            logger.error(f"Alt text generation failed for figure {fig.index}: {e}")
            fallback = _caption_fallback(fig.caption)
            existing[fig.index] = AltTextResult(
                figure_index=fig.index,
                generated_text=fallback or f"[Generation failed: {e}]",
                status="pending_review",
                resolved_kind=None,
            )

    for fig in figures:
        result = existing.get(fig.index)
        if result is None:
            fallback = _caption_fallback(fig.caption)
            result = AltTextResult(
                figure_index=fig.index,
                generated_text=fallback or "[Could not determine figure semantics]",
                status="pending_review",
                resolved_kind=None,
            )
        results.append(result)

    return results
