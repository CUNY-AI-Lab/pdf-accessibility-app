"""Step 4: Generate alt text for figures using a vision LLM."""

import logging
from dataclasses import dataclass

from app.pipeline.structure import FigureInfo
from app.services.intelligence_gemini_figures import generate_figure_intelligence
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
) -> list[AltTextResult]:
    """Generate alt text for each figure using a vision LLM."""
    results = []

    for fig in figures:
        try:
            if not fig.path.exists():
                logger.warning(f"Figure {fig.index} image not found: {fig.path}")
                fallback = _caption_fallback(fig.caption)
                results.append(AltTextResult(
                    figure_index=fig.index,
                    generated_text=fallback or "[Image file not found]",
                    status="pending_review",
                ))
                continue

            adjudication = await generate_figure_intelligence(
                figure=fig,
                llm_client=llm_client,
            )
            suggested_action = str(adjudication.get("suggested_action") or "").strip()
            resolved_kind = str(adjudication.get("resolved_kind") or "").strip() or None
            if suggested_action == "reclassify_region" and resolved_kind:
                results.append(AltTextResult(
                    figure_index=fig.index,
                    generated_text="",
                    status="reclassified",
                    resolved_kind=resolved_kind,
                ))
                continue
            if suggested_action == "mark_decorative" or adjudication.get("is_decorative"):
                text = "decorative"
            else:
                text = str(adjudication.get("alt_text") or "").strip()
            if not text:
                text = _caption_fallback(fig.caption) or "[Could not determine figure semantics]"
            logger.info(f"Generated alt text for figure {fig.index}: {text[:80]}...")

            results.append(AltTextResult(
                figure_index=fig.index,
                generated_text=text,
                resolved_kind=resolved_kind,
            ))

        except Exception as e:
            logger.error(f"Alt text generation failed for figure {fig.index}: {e}")
            fallback = _caption_fallback(fig.caption)
            results.append(AltTextResult(
                figure_index=fig.index,
                generated_text=fallback or f"[Generation failed: {e}]",
                status="pending_review",
                resolved_kind=None,
            ))

    return results
