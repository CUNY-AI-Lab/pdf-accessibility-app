"""Step 4: Generate alt text for figures using a vision LLM."""

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.structure import FigureInfo
from app.services.llm_client import LlmClient

logger = logging.getLogger(__name__)

ALT_TEXT_PROMPT = """You are generating alt text for a PDF accessibility remediation workflow.

Generate concise but complete alt text for this image following WCAG guidelines:
- For charts/graphs: describe the data being shown, key trends, and units
- For diagrams: describe the relationships and key components
- For decorative images: respond with exactly "decorative"
- For photos: describe the essential content relevant to the document
- Maximum 125 words; prioritize information over visual description
- Do not begin with "Image of" or "Picture of"

Respond with ONLY the alt text, nothing else."""


def _caption_fallback(caption: str | None) -> str:
    if not isinstance(caption, str):
        return ""
    return caption.strip()


@dataclass
class AltTextResult:
    figure_index: int
    generated_text: str
    status: str = "pending_review"


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

            suffix = fig.path.suffix.lower()
            if suffix in {".jpg", ".jpeg"}:
                mime_type = "image/jpeg"
            elif suffix == ".webp":
                mime_type = "image/webp"
            else:
                mime_type = "image/png"
            image_b64 = base64.b64encode(fig.path.read_bytes()).decode("ascii")
            prompt = ALT_TEXT_PROMPT
            caption = _caption_fallback(fig.caption)
            if caption:
                prompt += (
                    "\n\nDocument caption/context:\n"
                    f"{caption}\n\n"
                    "Use the caption as supporting context when it matches the image."
                )
            response = await llm_client.chat_completion(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_b64}",
                                },
                            },
                        ],
                    }
                ],
            )

            try:
                text = response["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError) as parse_err:
                logger.error(f"Unexpected LLM response format for figure {fig.index}: {parse_err}")
                text = "[Could not parse LLM response]"
            logger.info(f"Generated alt text for figure {fig.index}: {text[:80]}...")

            results.append(AltTextResult(
                figure_index=fig.index,
                generated_text=text,
            ))

        except Exception as e:
            logger.error(f"Alt text generation failed for figure {fig.index}: {e}")
            fallback = _caption_fallback(fig.caption)
            results.append(AltTextResult(
                figure_index=fig.index,
                generated_text=fallback or f"[Generation failed: {e}]",
                status="pending_review",
            ))

    return results
