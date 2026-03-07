import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import Job, ReviewTask
from app.services.llm_client import LlmClient
from app.services.font_unicode_override import inspect_context_font_target
from app.services.pdf_preview import (
    render_page_png_data_url,
    render_target_preview_png_data_url,
)

SUPPORTED_SUGGESTION_TASK_TYPES = {"font_text_fidelity", "reading_order"}
MAX_REVIEW_PAGES = 2
MAX_STRUCTURE_FRAGMENTS = 10
MAX_FONT_TARGET_PREVIEWS = 3
MAX_AUTO_FONT_MAP_TARGETS = 8
AUTO_FONT_MAP_ACTIONS = {"font_map_candidate", "actualtext_candidate"}
AUTO_FONT_MAP_CONFIDENCE = {"high"}

FONT_REVIEW_PROMPT = """You are assisting manual PDF accessibility remediation for a PDF/UA workflow.

You will receive:
- one or more full-page images from the PDF
- structured metadata about remaining font/Unicode issues

Your job is NOT to invent a remediation. You must help a human reviewer decide what to do next.

Respond with strict JSON only using this schema:
{
  "task_type": "font_text_fidelity",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "suggested_action": "manual_only" | "artifact_if_decorative" | "actualtext_candidate" | "font_map_candidate" | "re-run-deterministic-remediation",
  "reason": "short explanation",
  "review_focus": [
    {
      "page": 1,
      "font": "ExampleSymbolFont",
      "operator_index": 17,
      "rule_id": "ISO 14289-1:2014-7.21.7-1",
      "visible_text_hypothesis": "single visible symbol or marker",
      "is_likely_decorative": false,
      "recommended_reviewer_action": "compare visible symbol against spoken/copied text"
    }
  ],
  "actualtext_candidates": [
    {
      "page": 1,
      "operator_index": 17,
      "font": "ExampleSymbolFont",
      "proposed_actualtext": "*",
      "confidence": "medium",
      "reason": "The visible symbol appears to be a single marker glyph."
    }
  ],
  "reviewer_checklist": [
    "bullet one",
    "bullet two"
  ]
}

Rules:
- If the visible meaning is uncertain, set suggested_action to "manual_only".
- Only use "artifact_if_decorative" when the problematic text appears to be purely decorative or a visual ornament.
- Use "font_map_candidate" only when every flagged occurrence appears to be the same localized symbol and a single Unicode character would correctly represent it everywhere that font/code is used.
- Only use "actualtext_candidate" when the visible symbol/text looks semantically important and localized.
- Only include an item in actualtext_candidates when you can tie it to a specific page and operator_index from the provided font_review_targets.
- Leave actualtext_candidates empty if the visible text is uncertain.
- Base any glyph hypothesis on the supplied page images, target crop previews, nearby_text/decoded_text, repeated same-font same-code occurrences, and page_structure_fragments.
- Do not infer a glyph from the font name alone.
- If the repeated occurrence looks like a list marker or decorative pointer and the surrounding structure already conveys that meaning, prefer "artifact_if_decorative" or "manual_only" over "font_map_candidate".
- Keep summaries concise and factual.
- Do not include markdown fences or commentary outside the JSON object.
"""

READING_ORDER_PROMPT = """You are assisting manual PDF accessibility remediation for a PDF/UA workflow.

You will receive:
- one or more full-page images from the PDF
- sampled structural elements in the order our pipeline extracted them
- reading-order metrics from the fidelity gate

Your job is NOT to rewrite the PDF. You must help a human reviewer decide whether the order looks acceptable or needs manual correction.

Respond with strict JSON only using this schema:
{
  "task_type": "reading_order",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "suggested_action": "confirm_current_order" | "reorder_review" | "artifact_headers_footers" | "manual_only",
  "reason": "short explanation",
  "review_focus": [
    {
      "page": 1,
      "font": "",
      "rule_id": "",
      "visible_text_hypothesis": "sidebar appears before main paragraph",
      "is_likely_decorative": false,
      "recommended_reviewer_action": "check whether the sidebar should be artifacted or moved after the body text"
    }
  ],
  "reviewer_checklist": [
    "bullet one",
    "bullet two"
  ]
}

Rules:
- Use "confirm_current_order" only when the current order looks acceptable from the page images and sampled structure.
- Use "artifact_headers_footers" only for repeated running heads, page numbers, or purely decorative side material.
- Use "reorder_review" when the issue appears to be structural ordering, not missing text.
- Use "manual_only" when the visual evidence is ambiguous.
- Keep summaries concise and factual.
- Do not include markdown fences or commentary outside the JSON object.
"""


def _parse_metadata(task: ReviewTask) -> dict[str, Any]:
    if not task.metadata_json:
        return {}
    try:
        data = json.loads(task.metadata_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty LLM response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            text = "\n".join(lines[1:-1]).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")

    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON was not an object")
    return parsed


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _single_unicode_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) != 1 or text.isspace():
        return None
    return text


def _parse_job_structure(job: Job) -> dict[str, Any]:
    if not job.structure_json:
        return {}
    try:
        parsed = json.loads(job.structure_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _render_page_image(pdf_path: Path, page_number: int) -> str:
    return render_page_png_data_url(pdf_path, page_number)


def _job_pdf_path(job: Job) -> Path:
    candidates = []
    if getattr(job, "output_path", None):
        candidates.append(Path(str(job.output_path)))
    if getattr(job, "input_path", None):
        candidates.append(Path(str(job.input_path)))

    for pdf_path in candidates:
        if pdf_path.exists():
            return pdf_path

    preferred = candidates[0] if candidates else None
    raise RuntimeError(f"PDF file not found for review suggestion: {preferred}")


def _collect_structure_fragments(job: Job) -> list[dict[str, Any]]:
    structure_json = _parse_job_structure(job)
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []

    fragments: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        text = _normalize_text(element.get("text"))
        if len(text) < 16:
            continue
        page_raw = element.get("page")
        page = int(page_raw) + 1 if isinstance(page_raw, int) and page_raw >= 0 else None
        element_type = str(element.get("type") or "").strip()
        key = (page or 0, element_type, text[:120])
        if key in seen:
            continue
        seen.add(key)
        fragments.append({
            "page": page,
            "type": element_type,
            "text": text[:240],
            "bbox": element.get("bbox") if isinstance(element.get("bbox"), dict) else None,
        })
        if len(fragments) >= MAX_STRUCTURE_FRAGMENTS:
            break
    return fragments


def _page_structure_fragments(job: Job, page_numbers: list[int]) -> list[dict[str, Any]]:
    allowed_pages = {page for page in page_numbers if isinstance(page, int) and page > 0}
    if not allowed_pages:
        return []

    fragments: list[dict[str, Any]] = []
    for fragment in _collect_structure_fragments(job):
        page = fragment.get("page")
        if isinstance(page, int) and page in allowed_pages:
            fragments.append(fragment)
    return fragments[:MAX_STRUCTURE_FRAGMENTS]


def _enrich_font_review_targets(pdf_path: Path, raw_targets: list[Any]) -> list[dict[str, Any]]:
    enriched_targets: list[dict[str, Any]] = []
    for target in raw_targets:
        if not isinstance(target, dict):
            continue
        enriched = dict(target)
        context_path = str(target.get("context_path") or "").strip()
        if context_path:
            try:
                font_target = inspect_context_font_target(
                    pdf_path=pdf_path,
                    context_path=context_path,
                )
            except Exception:
                font_target = None
            if isinstance(font_target, dict):
                enriched.update(font_target)
        enriched_targets.append(enriched)
    return enriched_targets


def _group_font_review_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for target in targets:
        font = _normalize_text(target.get("font") or target.get("font_base_name"))
        font_base = _normalize_text(target.get("font_base_name"))
        font_code_hex = _normalize_text(target.get("font_code_hex"))
        key = (font, font_base, font_code_hex)
        entry = grouped.setdefault(
            key,
            {
                "font": font,
                "font_base_name": font_base,
                "font_code_hex": font_code_hex,
                "occurrences": 0,
                "pages": [],
                "sample_decoded_texts": [],
            },
        )
        entry["occurrences"] += 1
        page = target.get("page")
        if isinstance(page, int) and page > 0 and page not in entry["pages"]:
            entry["pages"].append(page)
        decoded = _normalize_text(target.get("decoded_text"))
        if decoded and decoded not in entry["sample_decoded_texts"]:
            entry["sample_decoded_texts"].append(decoded)

    groups = list(grouped.values())
    groups.sort(
        key=lambda item: (
            -int(item.get("occurrences", 0) or 0),
            str(item.get("font") or "").lower(),
            str(item.get("font_code_hex") or ""),
        ),
    )
    return groups


def _font_task_payload(job: Job, task: ReviewTask) -> tuple[str, list[dict[str, Any]]]:
    metadata = _parse_metadata(task)
    pages = metadata.get("pages_to_check")
    page_numbers = [
        int(page)
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, int) and page > 0
    ][:MAX_REVIEW_PAGES]
    if not page_numbers:
        page_numbers = [1]

    pdf_path = _job_pdf_path(job)
    raw_targets = metadata.get("font_review_targets")
    enriched_targets = _enrich_font_review_targets(
        pdf_path,
        raw_targets if isinstance(raw_targets, list) else [],
    )
    images = []
    for page_number in page_numbers:
        images.append({
            "type": "image_url",
            "image_url": {"url": _render_page_image(pdf_path, page_number)},
        })

    target_previews = []
    if enriched_targets:
        for target in enriched_targets[:MAX_FONT_TARGET_PREVIEWS]:
            if not isinstance(target, dict):
                continue
            context_path = str(target.get("context_path") or "").strip()
            page = target.get("page")
            operator_index = target.get("operator_index")
            if not context_path:
                continue
            try:
                preview_url = render_target_preview_png_data_url(pdf_path, context_path)
            except Exception:
                continue
            target_previews.append({
                "page": page,
                "operator_index": operator_index,
                "font": target.get("font"),
            })
            images.append({
                "type": "image_url",
                "image_url": {"url": preview_url},
            })

    payload = {
        "job_filename": job.original_filename,
        "review_task": {
            "task_type": task.task_type,
            "title": task.title,
            "detail": task.detail,
            "severity": task.severity,
            "source": task.source,
        },
        "font_rule_ids": metadata.get("font_rule_ids", []),
        "pages_to_check": page_numbers,
        "fonts_to_check": metadata.get("fonts_to_check", []),
        "font_review_targets": enriched_targets,
        "font_review_groups": _group_font_review_targets(enriched_targets),
        "target_previews": target_previews,
        "page_structure_fragments": _page_structure_fragments(job, page_numbers),
        "unicode_gate": metadata.get("unicode_gate", {}),
        "font_diagnostics_summary": metadata.get("font_diagnostics_summary", {}),
        "top_font_profiles": metadata.get("top_font_profiles", []),
    }
    prompt_text = (
        f"{FONT_REVIEW_PROMPT}\n\n"
        "Image order: full-page previews first, then target crop previews in the same order as target_previews.\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
    )
    content = [{"type": "text", "text": prompt_text}, *images]
    return prompt_text, content


def _reading_order_task_payload(job: Job, task: ReviewTask) -> tuple[str, list[dict[str, Any]]]:
    metadata = _parse_metadata(task)
    structure_fragments = _collect_structure_fragments(job)
    page_numbers: list[int] = []
    for fragment in structure_fragments:
        page = fragment.get("page")
        if isinstance(page, int) and page > 0 and page not in page_numbers:
            page_numbers.append(page)
        if len(page_numbers) >= MAX_REVIEW_PAGES:
            break
    if not page_numbers:
        page_numbers = [1]

    pdf_path = _job_pdf_path(job)
    images = []
    for page_number in page_numbers:
        images.append({
            "type": "image_url",
            "image_url": {"url": _render_page_image(pdf_path, page_number)},
        })

    payload = {
        "job_filename": job.original_filename,
        "review_task": {
            "task_type": task.task_type,
            "title": task.title,
            "detail": task.detail,
            "severity": task.severity,
            "source": task.source,
        },
        "reading_order_metrics": metadata,
        "pages_to_check": page_numbers,
        "structure_fragments": structure_fragments,
    }
    prompt_text = (
        f"{READING_ORDER_PROMPT}\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
    )
    content = [{"type": "text", "text": prompt_text}, *images]
    return prompt_text, content


async def generate_review_suggestion(
    *,
    job: Job,
    task: ReviewTask,
    llm_client: LlmClient,
) -> dict[str, Any]:
    if task.task_type not in SUPPORTED_SUGGESTION_TASK_TYPES:
        raise ValueError(f"Suggestions are not supported for task type '{task.task_type}'")

    if task.task_type == "font_text_fidelity":
        _prompt_text, content = _font_task_payload(job, task)
    elif task.task_type == "reading_order":
        _prompt_text, content = _reading_order_task_payload(job, task)
    else:
        raise ValueError(f"Suggestions are not supported for task type '{task.task_type}'")

    try:
        response = await llm_client.chat_completion(
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception:
        response = await llm_client.chat_completion(
            messages=[{"role": "user", "content": content}],
            temperature=0,
        )

    try:
        message_content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected LLM response format: {exc}") from exc

    suggestion = _extract_json_object(str(message_content))
    suggestion.setdefault("task_type", task.task_type)
    suggestion["generated_at"] = datetime.now(UTC).isoformat()
    suggestion["model"] = llm_client.model
    return suggestion


def select_auto_font_map_override(
    *,
    job: Job,
    task: ReviewTask,
    suggestion: dict[str, Any],
) -> dict[str, Any] | None:
    if task.task_type != "font_text_fidelity":
        return None

    action = str(suggestion.get("suggested_action") or "").strip()
    confidence = str(suggestion.get("confidence") or "").strip().lower()
    if action not in AUTO_FONT_MAP_ACTIONS or confidence not in AUTO_FONT_MAP_CONFIDENCE:
        return None

    review_focus = suggestion.get("review_focus")
    if isinstance(review_focus, list):
        for item in review_focus:
            if not isinstance(item, dict):
                continue
            if bool(item.get("is_likely_decorative")):
                return None

    metadata = _parse_metadata(task)
    font_rule_ids = metadata.get("font_rule_ids")
    if isinstance(font_rule_ids, list):
        normalized_rules = {
            str(rule_id).strip()
            for rule_id in font_rule_ids
            if str(rule_id).strip()
        }
        if not normalized_rules or normalized_rules != {"ISO 14289-1:2014-7.21.7-1"}:
            return None

    raw_targets = metadata.get("font_review_targets")
    if not isinstance(raw_targets, list) or not raw_targets or len(raw_targets) > MAX_AUTO_FONT_MAP_TARGETS:
        return None

    candidates = suggestion.get("actualtext_candidates")
    if not isinstance(candidates, list) or len(candidates) != len(raw_targets):
        return None

    pdf_path = _job_pdf_path(job)
    enriched_targets = _enrich_font_review_targets(pdf_path, raw_targets)
    if len(enriched_targets) != len(raw_targets):
        return None

    target_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
    for target in enriched_targets:
        page = target.get("page")
        operator_index = target.get("operator_index")
        if not isinstance(page, int) or not isinstance(operator_index, int):
            return None
        if not str(target.get("font_code_hex") or "").strip():
            return None
        target_by_pair[(page, operator_index)] = target

    normalized_candidates: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()
    suggested_texts: set[str] = set()
    font_names: set[str] = set()
    font_base_names: set[str] = set()
    font_codes: set[str] = set()

    for candidate in candidates:
        if not isinstance(candidate, dict):
            return None
        page = candidate.get("page")
        operator_index = candidate.get("operator_index")
        if not isinstance(page, int) or not isinstance(operator_index, int):
            return None
        pair = (page, operator_index)
        if pair in seen_pairs:
            return None
        seen_pairs.add(pair)

        target = target_by_pair.get(pair)
        if target is None:
            return None

        candidate_confidence = str(candidate.get("confidence") or "").strip().lower()
        if candidate_confidence and candidate_confidence not in AUTO_FONT_MAP_CONFIDENCE:
            return None

        proposed_text = _single_unicode_text(candidate.get("proposed_actualtext"))
        if proposed_text is None:
            return None

        candidate_font = _normalize_text(candidate.get("font"))
        target_font = _normalize_text(target.get("font"))
        if candidate_font and target_font and candidate_font != target_font:
            return None

        suggested_texts.add(proposed_text)
        if target_font:
            font_names.add(target_font)
        target_font_base = _normalize_text(target.get("font_base_name"))
        if target_font_base:
            font_base_names.add(target_font_base)
        target_code = _normalize_text(target.get("font_code_hex"))
        if not target_code:
            return None
        font_codes.add(target_code)

        normalized_candidates.append({
            "page": page,
            "operator_index": operator_index,
            "font": target_font or candidate_font,
            "font_base_name": target_font_base,
            "font_code_hex": target_code,
            "unicode_text": proposed_text,
        })

    if seen_pairs != set(target_by_pair):
        return None
    if len(suggested_texts) != 1 or len(font_codes) != 1:
        return None
    if len(font_names) > 1 or len(font_base_names) > 1:
        return None

    selected = normalized_candidates[0]
    return {
        "page_number": int(selected["page"]),
        "operator_index": int(selected["operator_index"]),
        "unicode_text": str(selected["unicode_text"]),
        "font": str(selected.get("font") or ""),
        "font_base_name": str(selected.get("font_base_name") or ""),
        "font_code_hex": str(selected.get("font_code_hex") or ""),
        "target_count": len(normalized_candidates),
    }
