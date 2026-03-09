from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models import Job
from app.services.llm_client import LlmClient


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


def extract_json_object(raw_text: str) -> dict[str, Any]:
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


async def request_llm_json(
    *,
    llm_client: LlmClient,
    content: list[dict[str, Any]],
) -> dict[str, Any]:
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
    return extract_json_object(str(message_content))
