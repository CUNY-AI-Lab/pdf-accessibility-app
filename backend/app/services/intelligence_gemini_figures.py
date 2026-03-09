from __future__ import annotations

import base64
from pathlib import Path

from app.pipeline.structure import FigureInfo
from app.services.intelligence_gemini_semantics import adjudicate_semantic_unit
from app.services.llm_client import LlmClient
from app.services.semantic_units import SemanticUnit


def _image_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
    elif suffix == ".webp":
        mime_type = "image/webp"
    else:
        mime_type = "image/png"
    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{image_b64}"


async def generate_figure_intelligence(
    *,
    figure: FigureInfo,
    llm_client: LlmClient,
    original_filename: str = "",
) -> dict[str, object]:
    unit = SemanticUnit(
        unit_id=f"figure-{figure.index}",
        unit_type="figure",
        page=int(figure.page) + 1 if isinstance(figure.page, int) and figure.page >= 0 else 1,
        accessibility_goal=(
            "Decide whether this candidate region is a meaningful standalone figure, decorative content, "
            "or not really a figure at all for assistive technology."
        ),
        bbox=figure.bbox,
        nearby_context=[
            {"type": "caption", "text": str(figure.caption or "").strip()},
        ] if figure.caption else [],
        current_semantics={"caption": str(figure.caption or "").strip()},
        metadata={
            "extra_image_data_urls": [_image_data_url(figure.path)] if figure.path.exists() else [],
            "figure_index": figure.index,
        },
    )
    job = None
    if original_filename:
        from types import SimpleNamespace

        job = SimpleNamespace(original_filename=original_filename)
    try:
        decision = await adjudicate_semantic_unit(job=job, unit=unit, llm_client=llm_client)
    except Exception as exc:
        return {
            "task_type": "figure_intelligence",
            "summary": "Figure review required.",
            "confidence": "low",
            "confidence_score": 0.0,
            "suggested_action": "manual_only",
            "reason": f"Figure semantics fallback: {exc}",
            "figure_index": figure.index,
            "alt_text": "",
            "resolved_kind": None,
            "is_decorative": False,
        }
    return {
        "task_type": "figure_intelligence",
        "summary": decision.summary,
        "confidence": decision.confidence,
        "confidence_score": decision.confidence_score,
        "suggested_action": decision.suggested_action,
        "reason": decision.reason,
        "figure_index": figure.index,
        "alt_text": decision.alt_text or decision.resolved_text or "",
        "resolved_kind": decision.resolved_kind,
        "is_decorative": decision.is_decorative,
    }
