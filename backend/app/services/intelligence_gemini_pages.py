from __future__ import annotations

import json
from typing import Any

from app.models import Job
from app.services.intelligence_gemini import confidence_score
from app.services.intelligence_llm_utils import (
    job_pdf_path,
    request_llm_json,
)
from app.services.llm_client import LlmClient
from app.services.pdf_preview import (
    render_bbox_preview_png_data_url,
    render_page_png_data_url,
)

SUSPICIOUS_TEXT_PROMPT = """You are a PDF accessibility page-intelligence assistant.

You will receive:
- one or more full-page images from the PDF
- crop previews for suspicious text blocks
- structured metadata for those blocks

Your goal is to decide what a screen reader user should probably hear for each suspicious block.

Respond with strict JSON only using this schema:
{
  "task_type": "page_text_intelligence",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "blocks": [
    {
      "page": 1,
      "review_id": "review-5",
      "readable_text_hint": "Data Book",
      "chosen_source": "native" | "ocr" | "llm_inferred",
      "issue_type": "spacing_only" | "encoding_problem" | "uncertain",
      "confidence": "high" | "medium" | "low",
      "should_block_accessibility": true,
      "reason": "short explanation"
    }
  ]
}

Rules:
- Only return blocks that appear in the provided suspicious_text_blocks context.
- Use the full-page image, crop preview, native_text_candidate, ocr_text_candidate, and nearby context together.
- Prefer one of the provided text candidates when it clearly matches the visible text.
- Use `chosen_source="native"` when the extracted PDF text is already the best accessible reading.
- Use `chosen_source="ocr"` when the OCR candidate better matches the visible text than the native candidate.
- Use `chosen_source="llm_inferred"` only when neither candidate is good enough and you can still infer the visible text confidently.
- Prefer precision over recall. Omit a block if the visible text is too ambiguous.
- Use `should_block_accessibility=true` only when the extracted text would likely mislead assistive technology.
- Use `spacing_only` when the text meaning is clear but extraction spacing is broken.
- Use `encoding_problem` when characters appear materially wrong or garbled.
- Use `uncertain` when you cannot determine the visible text confidently.
- The readable_text_hint should be what assistive technology should probably announce.
- For code blocks, preserve the visible line breaks and indentation in `readable_text_hint`.
- For code blocks, prefer reconstructing the smallest faithful snippet visible in the crop rather than paraphrasing it.
- Do not include markdown fences or commentary outside the JSON object.
"""

def _normalize_block_hints(raw_blocks: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(raw_blocks, list):
        return normalized
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        page = raw_block.get("page")
        review_id = str(raw_block.get("review_id") or "").strip()
        if not isinstance(page, int) or page < 1 or not review_id:
            continue
        hint = str(raw_block.get("readable_text_hint") or "").strip()
        if not hint:
            continue
        normalized.append(
            {
                "page": page,
                "review_id": review_id,
                "readable_text_hint": hint,
                "chosen_source": str(raw_block.get("chosen_source") or "llm_inferred").strip() or "llm_inferred",
                "issue_type": str(raw_block.get("issue_type") or "uncertain").strip() or "uncertain",
                "confidence": str(raw_block.get("confidence") or "low").strip() or "low",
                "should_block_accessibility": bool(raw_block.get("should_block_accessibility", False)),
                "reason": str(raw_block.get("reason") or "").strip(),
            }
        )
    return normalized


def _attach_grounding_evidence(
    normalized_blocks: list[dict[str, Any]],
    suspicious_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for block in suspicious_blocks:
        if not isinstance(block, dict):
            continue
        page = block.get("page")
        review_id = str(block.get("review_id") or "").strip()
        if not isinstance(page, int) or page < 1 or not review_id:
            continue
        evidence_by_key[(page, review_id)] = block

    enriched: list[dict[str, Any]] = []
    for hint in normalized_blocks:
        page = int(hint["page"])
        review_id = str(hint["review_id"])
        evidence = evidence_by_key.get((page, review_id), {})
        enriched.append(
            {
                **hint,
                "role": str(evidence.get("role") or "").strip(),
                "extracted_text": str(evidence.get("extracted_text") or evidence.get("text") or "").strip(),
                "native_text_candidate": str(evidence.get("native_text_candidate") or "").strip(),
                "original_text_candidate": str(
                    evidence.get("original_text_candidate")
                    or evidence.get("native_text_candidate")
                    or ""
                ).strip(),
                "ocr_text_candidate": str(evidence.get("ocr_text_candidate") or "").strip(),
                "previous_text": str(evidence.get("previous_text") or "").strip(),
                "previous_role": str(evidence.get("previous_role") or "").strip(),
                "next_text": str(evidence.get("next_text") or "").strip(),
                "next_role": str(evidence.get("next_role") or "").strip(),
                "signals": list(evidence.get("signals") or []),
            }
        )
    return enriched


async def generate_suspicious_text_intelligence(
    *,
    job: Job,
    page_numbers: list[int],
    suspicious_blocks: list[dict[str, Any]],
    llm_client: LlmClient,
) -> dict[str, Any]:
    if not suspicious_blocks:
        return {
            "task_type": "page_text_intelligence",
            "summary": "",
            "confidence": "low",
            "blocks": [],
        }

    pdf_path = job_pdf_path(job)
    images: list[dict[str, Any]] = []
    for page_number in page_numbers:
        images.append(
            {
                "type": "image_url",
                "image_url": {"url": render_page_png_data_url(pdf_path, page_number)},
            }
        )

    block_previews: list[dict[str, Any]] = []
    for block in suspicious_blocks:
        page = block.get("page")
        bbox = block.get("bbox")
        if not isinstance(page, int) or not isinstance(bbox, dict):
            continue
        try:
            preview_url = render_bbox_preview_png_data_url(pdf_path, page, bbox)
        except Exception:
            continue
        block_previews.append({"page": page, "review_id": block.get("review_id")})
        images.append({"type": "image_url", "image_url": {"url": preview_url}})

    payload = {
        "job_filename": job.original_filename,
        "accessibility_goal": (
            "Infer what assistive technology should probably announce when extracted text "
            "looks suspicious or garbled."
        ),
        "pages_to_check": page_numbers,
        "suspicious_text_blocks": suspicious_blocks,
        "block_previews": block_previews,
    }
    content = [
        {
            "type": "text",
            "text": (
                f"{SUSPICIOUS_TEXT_PROMPT}\n\n"
                "Image order: full-page previews first, then suspicious block crop previews in the same order as block_previews.\n\n"
                "Context JSON:\n"
                f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
            ),
        },
        *images,
    ]

    parsed = await request_llm_json(llm_client=llm_client, content=content)
    normalized_blocks = _attach_grounding_evidence(
        _normalize_block_hints(parsed.get("blocks")),
        suspicious_blocks,
    )
    return {
        "task_type": "page_text_intelligence",
        "summary": str(parsed.get("summary") or "").strip(),
        "confidence": str(parsed.get("confidence") or "low").strip() or "low",
        "confidence_score": confidence_score(parsed.get("confidence")),
        "blocks": normalized_blocks,
    }
