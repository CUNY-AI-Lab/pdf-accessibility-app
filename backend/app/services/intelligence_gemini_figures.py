from __future__ import annotations

import base64
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.models import Job
from app.pipeline.structure import FigureInfo
from app.services.gemini_direct import (
    create_direct_gemini_pdf_cache,
    delete_direct_gemini_pdf_cache,
    direct_gemini_pdf_enabled,
    request_direct_gemini_cached_json,
)
from app.services.intelligence_gemini import confidence_label, confidence_score
from app.services.intelligence_gemini_semantics import adjudicate_semantic_unit
from app.services.intelligence_llm_utils import (
    context_json_part,
    job_pdf_path,
    page_preview_parts,
    request_llm_json,
)
from app.services.llm_client import LlmClient
from app.services.semantic_units import SemanticUnit

MAX_FIGURES_PER_BATCH = 4

FIGURE_BATCH_PROMPT = """You are reviewing multiple figure candidates from the same PDF page for accessibility.

For each candidate, decide whether it should:
- keep figure semantics with short alt text
- be marked decorative
- be reclassified because it is actually a table, form region, or artifact
- fall back to manual follow-up

Rules:
- Decide each figure candidate independently.
- Use the PDF page as the primary evidence.
- Use each candidate's bbox, caption, and page-local context to locate it on the page.
- Preserve visible meaning.
- Prefer reclassification when the crop is clearly not a standalone figure.
- When the same page also contains a much larger screenshot or interface image, tiny icon or button crops are usually child UI details, not standalone figures.
- In those cases, prefer `mark_decorative` unless the small crop has clear standalone instructional meaning that would be lost if it were hidden.
- Do not create separate alt text like "magnifying glass icon" or "printer icon" for child UI details when the larger screenshot already conveys the workflow.
- Use `resolved_kind` only when `suggested_action` is `reclassify_region`.
- `resolved_kind` must be one of: `table`, `form_region`, `artifact`.
- Use `set_alt_text` only when one concise factual description is clearly supported.
- Use `mark_decorative` only when the crop is decorative or redundant.
- Use `manual_only` when the image purpose is unclear.
- Do not start alt text with "Image of" or "Picture of".
"""

FIGURE_DIRECT_GEMINI_SYSTEM_INSTRUCTION = (
    "You are evaluating PDF accessibility and figure semantics. "
    "Stay grounded in the provided PDF page and return JSON only."
)

FIGURE_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["figure_batch_intelligence"]},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "figure_index": {"type": "integer", "minimum": 0},
                    "summary": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "suggested_action": {
                        "type": "string",
                        "enum": [
                            "set_alt_text",
                            "mark_decorative",
                            "reclassify_region",
                            "manual_only",
                        ],
                    },
                    "reason": {"type": "string"},
                    "alt_text": {"type": "string"},
                    "resolved_kind": {
                        "type": "string",
                        "enum": ["table", "form_region", "artifact"],
                    },
                    "is_decorative": {"type": "boolean"},
                },
                "required": [
                    "figure_index",
                    "summary",
                    "confidence",
                    "suggested_action",
                    "reason",
                ],
            },
        },
    },
    "required": ["task_type", "decisions"],
}


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


def _normalize_figure_result(*, figure_index: int, raw: dict[str, Any]) -> dict[str, object]:
    confidence = confidence_label(raw.get("confidence"))
    suggested_action = str(raw.get("suggested_action") or "manual_only").strip() or "manual_only"
    resolved_kind = str(raw.get("resolved_kind") or "").strip() or None
    if suggested_action == "reclassify_region" and resolved_kind not in {
        "table",
        "form_region",
        "artifact",
    }:
        suggested_action = "manual_only"
        resolved_kind = None
    return {
        "task_type": "figure_intelligence",
        "summary": str(raw.get("summary") or "Figure review required.").strip()
        or "Figure review required.",
        "confidence": confidence,
        "confidence_score": confidence_score(confidence),
        "suggested_action": suggested_action,
        "reason": str(raw.get("reason") or "").strip(),
        "figure_index": figure_index,
        "alt_text": str(raw.get("alt_text") or "").strip(),
        "resolved_kind": resolved_kind,
        "is_decorative": bool(raw.get("is_decorative", False)),
    }


def _manual_only_figure_result(*, figure_index: int, reason: str) -> dict[str, object]:
    return {
        "task_type": "figure_intelligence",
        "summary": "Figure review required.",
        "confidence": "low",
        "confidence_score": confidence_score("low"),
        "suggested_action": "manual_only",
        "reason": reason,
        "figure_index": figure_index,
        "alt_text": "",
        "resolved_kind": None,
        "is_decorative": False,
    }


def _bbox_area(bbox: dict[str, Any] | None) -> float:
    if not isinstance(bbox, dict):
        return 0.0
    try:
        width = max(0.0, float(bbox.get("r", 0.0)) - float(bbox.get("l", 0.0)))
        height = max(0.0, float(bbox.get("t", 0.0)) - float(bbox.get("b", 0.0)))
    except Exception:
        return 0.0
    return width * height


def _figure_page_context(page_figures: list[FigureInfo]) -> dict[int, dict[str, Any]]:
    areas = {figure.index: _bbox_area(figure.bbox) for figure in page_figures}
    page_max_area = max(areas.values(), default=0.0)
    context: dict[int, dict[str, Any]] = {}
    for figure in page_figures:
        area = areas.get(figure.index, 0.0)
        larger_siblings = [
            sibling
            for sibling in page_figures
            if sibling.index != figure.index and areas.get(sibling.index, 0.0) > area * 8.0
        ]
        likely_child_ui = bool(
            figure.caption in (None, "")
            and area > 0.0
            and page_max_area > 0.0
            and area / page_max_area <= 0.02
            and larger_siblings
        )
        context[figure.index] = {
            "bbox_area": round(area, 2),
            "relative_area": round(area / page_max_area, 4) if page_max_area > 0.0 else 0.0,
            "likely_child_ui_figure": likely_child_ui,
            "larger_sibling_indexes": [sibling.index for sibling in larger_siblings[:4]],
        }
    return context


_GENERIC_CHILD_UI_ALT_RE = re.compile(
    r"\b(icon|button|symbol|triangle|arrow|magnifying glass|printer|paper icon|pencil icon|star)\b",
    re.IGNORECASE,
)


def _should_suppress_child_ui_alt(
    *, raw: dict[str, Any], figure_context: dict[str, Any] | None
) -> bool:
    if not isinstance(figure_context, dict) or not figure_context.get("likely_child_ui_figure"):
        return False
    if str(raw.get("suggested_action") or "").strip() != "set_alt_text":
        return False
    alt_text = str(raw.get("alt_text") or "").strip()
    if not alt_text:
        return False
    normalized = " ".join(alt_text.split())
    if len(normalized) > 80:
        return False
    if normalized.lower().startswith("screenshot of"):
        return False
    if normalized.lower().startswith("dialog showing"):
        return False
    if _GENERIC_CHILD_UI_ALT_RE.search(normalized) is None:
        return False
    return True


def _finalize_figure_result(
    *,
    figure_index: int,
    raw: dict[str, Any],
    figure_context: dict[str, Any] | None = None,
) -> dict[str, object]:
    if _should_suppress_child_ui_alt(raw=raw, figure_context=figure_context):
        raw = {
            **raw,
            "summary": str(raw.get("summary") or "").strip()
            or "Redundant child UI detail inside a larger screenshot.",
            "suggested_action": "mark_decorative",
            "reason": str(raw.get("reason") or "").strip()
            or "Tiny child UI figure is redundant with the larger screenshot on the page.",
            "alt_text": "",
            "is_decorative": True,
        }
    return _normalize_figure_result(figure_index=figure_index, raw=raw)


def _requires_single_figure_followup(result: dict[str, object]) -> bool:
    suggested_action = str(result.get("suggested_action") or "").strip() or "manual_only"
    confidence = confidence_label(result.get("confidence"))
    alt_text = str(result.get("alt_text") or "").strip()
    if confidence == "low":
        return True
    if suggested_action == "manual_only":
        return True
    if suggested_action == "set_alt_text" and not alt_text:
        return True
    return False


async def generate_figure_intelligence(
    *,
    figure: FigureInfo,
    llm_client: LlmClient,
    job: Job | Any | None = None,
    original_filename: str = "",
    reviewer_feedback: str | None = None,
    previous_intelligence: dict[str, Any] | None = None,
    figure_context: dict[str, Any] | None = None,
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
        ]
        if figure.caption
        else [],
        current_semantics={"caption": str(figure.caption or "").strip()},
        metadata={
            "extra_image_data_urls": [_image_data_url(figure.path)] if figure.path.exists() else [],
            "figure_index": figure.index,
            "reviewer_feedback": reviewer_feedback or "",
            "previous_intelligence": previous_intelligence or {},
        },
    )
    if job is None and original_filename:
        from types import SimpleNamespace

        job = SimpleNamespace(original_filename=original_filename)
    try:
        decision = await adjudicate_semantic_unit(job=job, unit=unit, llm_client=llm_client)
    except Exception as exc:
        return _manual_only_figure_result(
            figure_index=figure.index,
            reason=f"Figure semantics fallback: {exc}",
        )
    return _finalize_figure_result(
        figure_index=figure.index,
        figure_context=figure_context,
        raw={
            "summary": decision.summary,
            "confidence": decision.confidence,
            "suggested_action": decision.suggested_action,
            "reason": decision.reason,
            "alt_text": decision.alt_text or decision.resolved_text or "",
            "resolved_kind": decision.resolved_kind,
            "is_decorative": decision.is_decorative,
        },
    )


async def generate_figures_intelligence(
    *,
    figures: list[FigureInfo],
    llm_client: LlmClient,
    job: Job | Any | None = None,
    original_filename: str = "",
) -> list[dict[str, object]]:
    if not figures:
        return []

    grouped: dict[int, list[FigureInfo]] = defaultdict(list)
    for figure in figures:
        page = int(figure.page) + 1 if isinstance(figure.page, int) and figure.page >= 0 else 1
        grouped[page].append(figure)

    results: dict[int, dict[str, object]] = {}
    for page in sorted(grouped):
        page_figures = sorted(grouped[page], key=lambda fig: fig.index)
        page_context = _figure_page_context(page_figures)
        pdf_page_cache = None
        if direct_gemini_pdf_enabled() and job is not None:
            try:
                pdf_path = job_pdf_path(job)
                pdf_page_cache = await create_direct_gemini_pdf_cache(
                    pdf_path=pdf_path,
                    page_numbers=[page],
                    system_instruction=FIGURE_DIRECT_GEMINI_SYSTEM_INSTRUCTION,
                    ttl="900s",
                )
            except Exception:
                pdf_page_cache = None

        try:
            for start in range(0, len(page_figures), MAX_FIGURES_PER_BATCH):
                chunk = page_figures[start : start + MAX_FIGURES_PER_BATCH]
                payload_candidates = [
                    {
                        "figure_index": figure.index,
                        "caption": str(figure.caption or "").strip(),
                        "page": page,
                        "bbox": figure.bbox,
                        **page_context.get(figure.index, {}),
                    }
                    for figure in chunk
                ]

                parsed: dict[str, Any] | None = None
                if pdf_page_cache is not None:
                    try:
                        parsed = await request_direct_gemini_cached_json(
                            cache_handle=pdf_page_cache,
                            prompt=FIGURE_BATCH_PROMPT,
                            context_payload={
                                "job_filename": getattr(job, "original_filename", original_filename)
                                if job is not None
                                else original_filename,
                                "page": page,
                                "candidates": payload_candidates,
                            },
                            response_schema=FIGURE_BATCH_SCHEMA,
                        )
                    except Exception:
                        parsed = None
                if parsed is None:
                    page_images: list[dict[str, Any]] = page_preview_parts(job, [page])
                    content: list[dict[str, Any]] = [
                        {"type": "text", "text": FIGURE_BATCH_PROMPT},
                        *page_images,
                        context_json_part(
                            {
                                "job_filename": getattr(job, "original_filename", original_filename)
                                if job is not None
                                else original_filename,
                                "page": page,
                                "candidates": payload_candidates,
                            }
                        ),
                    ]
                    for figure in chunk:
                        if figure.path.exists():
                            content.extend(
                                [
                                    {"type": "text", "text": f"Figure candidate {figure.index} crop:"},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": _image_data_url(figure.path)},
                                    },
                                ]
                            )
                    try:
                        parsed = await request_llm_json(
                            llm_client=llm_client,
                            content=content,
                            schema_name="figure_batch_intelligence",
                            response_schema=FIGURE_BATCH_SCHEMA,
                            cache_breakpoint_index=len(page_images) if page_images else 0,
                        )
                    except Exception:
                        parsed = None

                chunk_index_set = {figure.index for figure in chunk}
                seen: set[int] = set()
                if parsed is not None:
                    for item in parsed.get("decisions") or []:
                        if not isinstance(item, dict):
                            continue
                        figure_index = item.get("figure_index")
                        if not isinstance(figure_index, int) or figure_index not in chunk_index_set:
                            continue
                        finalized = _finalize_figure_result(
                            figure_index=figure_index,
                            figure_context=page_context.get(figure_index),
                            raw=item,
                        )
                        if _requires_single_figure_followup(finalized):
                            continue
                        results[figure_index] = finalized
                        seen.add(figure_index)

                for figure in chunk:
                    if figure.index in seen:
                        continue
                    results[figure.index] = await generate_figure_intelligence(
                        figure=figure,
                        llm_client=llm_client,
                        job=job,
                        original_filename=original_filename,
                        figure_context=page_context.get(figure.index),
                    )
        finally:
            if pdf_page_cache is not None:
                await delete_direct_gemini_pdf_cache(pdf_page_cache)

    return [results[figure.index] for figure in sorted(figures, key=lambda fig: fig.index)]
