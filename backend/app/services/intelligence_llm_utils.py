from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable

from app.models import Job
from app.services.llm_client import LlmClient
from app.services.pdf_preview import render_page_jpeg_data_url


def job_pdf_path(job: Job) -> Path:
    candidates = []
    if getattr(job, "output_path", None):
        candidates.append(Path(str(job.output_path)))
    if getattr(job, "input_path", None):
        candidates.append(Path(str(job.input_path)))
    for pdf_path in candidates:
        if pdf_path.exists():
            return pdf_path
    preferred = candidates[0] if candidates else None
    raise RuntimeError(f"PDF file not found for page intelligence: {preferred}")


def context_json_part(payload: Any, *, prefix: str = "Context JSON:\n") -> dict[str, str]:
    return {
        "type": "text",
        "text": prefix + json.dumps(payload, indent=2, ensure_ascii=True),
    }


def page_preview_parts(job: Job | Any | None, page_numbers: Iterable[int]) -> list[dict[str, Any]]:
    if job is None:
        return []
    try:
        pdf_path = job_pdf_path(job)
    except Exception:
        return []

    parts: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page_number in page_numbers:
        if not isinstance(page_number, int) or page_number <= 0 or page_number in seen:
            continue
        seen.add(page_number)
        try:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": render_page_jpeg_data_url(pdf_path, page_number)},
                }
            )
        except Exception:
            continue
    return parts


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty LLM response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            text = "\n".join(lines[1:-1]).strip()

    start = text.find("{")
    if start < 0:
        raise ValueError("LLM response did not contain a JSON object")

    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response JSON could not be decoded: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON was not an object")
    return parsed


def apply_cache_breakpoint(
    content: list[dict[str, Any]],
    breakpoint_index: int | None,
) -> list[dict[str, Any]]:
    prepared = [copy.deepcopy(item) for item in content]
    for item in prepared:
        if isinstance(item, dict):
            item.pop("cache_control", None)
    if breakpoint_index is None:
        return prepared
    if breakpoint_index < 0 or breakpoint_index >= len(prepared):
        return prepared
    if isinstance(prepared[breakpoint_index], dict):
        prepared[breakpoint_index]["cache_control"] = {"type": "ephemeral"}
    return prepared


async def request_llm_json(
    *,
    llm_client: LlmClient,
    content: list[dict[str, Any]],
    schema_name: str | None = None,
    response_schema: dict[str, Any] | None = None,
    cache_breakpoint_index: int | None = None,
) -> dict[str, Any]:
    response = None
    provider_preferences = {"require_parameters": True}
    prepared_content = apply_cache_breakpoint(content, cache_breakpoint_index)
    if response_schema and schema_name:
        try:
            response = await llm_client.chat_completion(
                messages=[{"role": "user", "content": prepared_content}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": response_schema,
                    },
                },
                provider=provider_preferences,
                temperature=0,
            )
        except Exception:
            response = None
    if response is None:
        try:
            response = await llm_client.chat_completion(
                messages=[{"role": "user", "content": prepared_content}],
                response_format={"type": "json_object"},
                provider=provider_preferences,
                temperature=0,
            )
        except Exception:
            response = await llm_client.chat_completion(
                messages=[{"role": "user", "content": prepared_content}],
                temperature=0,
            )

    try:
        message_content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected LLM response format: {exc}") from exc
    return extract_json_object(str(message_content))
