import base64
import copy
import json
import logging
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import Any

import pikepdf

from app.models import Job
from app.services.gemini_direct import (
    direct_gemini_pdf_enabled,
    request_direct_gemini_content_json_with_response,
)
from app.services.llm_client import LlmClient, is_retryable_llm_exception
from app.services.local_semantic import (
    local_semantic_enabled,
    request_local_semantic_content_json_with_response,
)
from app.services.path_safety import validate_path_within_allowed_roots
from app.services.pdf_preview import render_page_jpeg_data_url

logger = logging.getLogger(__name__)


def _should_try_alternate_response_format(exc: BaseException) -> bool:
    """Fallback formats help with capability/validation issues, not flaky endpoints."""
    return not is_retryable_llm_exception(exc)


def job_pdf_path(job: Job) -> Path:
    from fastapi import HTTPException

    candidates = []
    if getattr(job, "output_path", None):
        candidates.append(Path(str(job.output_path)))
    if getattr(job, "input_path", None):
        candidates.append(Path(str(job.input_path)))
    for pdf_path in candidates:
        resolved = pdf_path.resolve()
        try:
            validated = validate_path_within_allowed_roots(resolved)
        except HTTPException:
            # This helper is used by internal remediation/intelligence flows, not
            # user-facing download APIs. Test fixtures and local worker temps can
            # live outside the configured data roots, so prefer any existing file.
            if resolved.exists():
                logger.debug("Using existing internal PDF path outside allowed roots: %s", resolved)
                return resolved
            logger.warning("PDF path outside allowed roots: %s", pdf_path)
            continue
        if validated.exists():
            return validated
    preferred = candidates[0] if candidates else None
    raise RuntimeError(f"PDF file not found for page intelligence: {preferred}")


def context_json_part(payload: Any, *, prefix: str = "Context JSON:\n") -> dict[str, str]:
    return {
        "type": "text",
        "text": prefix + json.dumps(payload, indent=2, ensure_ascii=True),
    }


def _normalize_pdf_page_numbers(page_numbers: Iterable[int] | None, *, total_pages: int) -> list[int]:
    if page_numbers is None:
        return list(range(1, total_pages + 1))

    normalized: list[int] = []
    seen: set[int] = set()
    for raw_page_number in page_numbers:
        if not isinstance(raw_page_number, int):
            continue
        page_number = int(raw_page_number)
        if page_number <= 0 or page_number > total_pages or page_number in seen:
            continue
        seen.add(page_number)
        normalized.append(page_number)
    return normalized


def pdf_file_bytes(pdf_path: Path, page_numbers: Iterable[int] | None = None) -> bytes:
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    with pikepdf.Pdf.open(pdf_path) as source_pdf:
        total_pages = len(source_pdf.pages)
        normalized_pages = _normalize_pdf_page_numbers(page_numbers, total_pages=total_pages)
        if not normalized_pages:
            raise ValueError("No valid PDF pages were selected for LLM document input")
        if normalized_pages == list(range(1, total_pages + 1)):
            return pdf_path.read_bytes()

        subset_pdf = pikepdf.Pdf.new()
        for page_number in normalized_pages:
            subset_pdf.pages.append(source_pdf.pages[page_number - 1])
        output = BytesIO()
        subset_pdf.save(output)
        return output.getvalue()


def pdf_file_data_url(pdf_path: Path, page_numbers: Iterable[int] | None = None) -> str:
    encoded = base64.b64encode(pdf_file_bytes(pdf_path, page_numbers)).decode("ascii")
    return f"data:application/pdf;base64,{encoded}"


def pdf_file_parts(
    job: Job | Any | None,
    page_numbers: Iterable[int] | None = None,
    *,
    filename: str | None = None,
) -> list[dict[str, Any]]:
    if job is None:
        return []
    try:
        pdf_path = job_pdf_path(job)
    except Exception:
        logger.warning("Could not resolve PDF path for PDF document input (job %s)", getattr(job, "id", "?"))
        return []

    resolved_filename = (filename or getattr(job, "original_filename", None) or pdf_path.name).strip()
    if not resolved_filename.lower().endswith(".pdf"):
        resolved_filename = f"{resolved_filename}.pdf"

    try:
        return [
            {
                "type": "file",
                "file": {
                    "filename": resolved_filename,
                    "file_data": pdf_file_data_url(pdf_path, page_numbers),
                },
            }
        ]
    except Exception:
        logger.debug("Failed to prepare PDF document input for intelligence", exc_info=True)
        return []


def page_preview_parts(job: Job | Any | None, page_numbers: Iterable[int]) -> list[dict[str, Any]]:
    if job is None:
        return []
    try:
        pdf_path = job_pdf_path(job)
    except Exception:
        logger.warning("Could not resolve PDF path for page previews (job %s)", getattr(job, "id", "?"))
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
            logger.debug("Failed to render page %d preview for intelligence", page_number)
            continue
    return parts


def semantic_page_parts(
    job: Job | Any | None,
    page_numbers: Iterable[int],
    *,
    filename: str | None = None,
) -> list[dict[str, Any]]:
    if local_semantic_enabled():
        return page_preview_parts(job, page_numbers)
    return pdf_file_parts(job, page_numbers, filename=filename)


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


def preferred_cache_breakpoint_index(content: list[dict[str, Any]]) -> int | None:
    if not content:
        return None

    last_media_index: int | None = None
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"image_url", "file"}:
            last_media_index = index

    if last_media_index is not None:
        return last_media_index
    return len(content) - 1


async def request_llm_json(
    *,
    llm_client: LlmClient,
    content: list[dict[str, Any]],
    schema_name: str | None = None,
    response_schema: dict[str, Any] | None = None,
    cache_breakpoint_index: int | None = None,
    conversation_prefix: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parsed, _ = await request_llm_json_with_response(
        llm_client=llm_client,
        content=content,
        schema_name=schema_name,
        response_schema=response_schema,
        cache_breakpoint_index=cache_breakpoint_index,
        conversation_prefix=conversation_prefix,
    )
    return parsed


async def request_llm_json_with_response(
    *,
    llm_client: LlmClient,
    content: list[dict[str, Any]],
    schema_name: str | None = None,
    response_schema: dict[str, Any] | None = None,
    cache_breakpoint_index: int | None = None,
    conversation_prefix: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        conversation_prefix is None
        and local_semantic_enabled()
        and any(
            isinstance(item, dict) and item.get("type") == "image_url"
            for item in content
        )
        and not any(
            isinstance(item, dict) and item.get("type") == "file"
            for item in content
        )
    ):
        response_schema_payload = response_schema if (response_schema and schema_name) else response_schema
        return await request_local_semantic_content_json_with_response(
            content=content,
            response_schema=response_schema_payload,
            system_instruction=(
                "You are evaluating PDF accessibility and document semantics. "
                "Stay grounded in the provided page evidence and return JSON only."
            ),
        )
    if direct_gemini_pdf_enabled() and conversation_prefix is None:
        response_schema_payload = response_schema if (response_schema and schema_name) else response_schema
        return await request_direct_gemini_content_json_with_response(
            content=content,
            response_schema=response_schema_payload,
            system_instruction=(
                "You are evaluating PDF accessibility and document semantics. "
                "Stay grounded in the provided document and return JSON only."
            ),
        )
    repair_note: str | None = None

    async def _request_once(request_content: list[dict[str, Any]]) -> Any:
        messages: list[dict[str, Any]] = []
        if conversation_prefix:
            messages.extend(copy.deepcopy(conversation_prefix))
        messages.append({"role": "user", "content": request_content})
        request_kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": 0,
        }
        response = None
        if response_schema and schema_name:
            try:
                response = await llm_client.chat_completion(
                    **request_kwargs,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": response_schema,
                        },
                    },
                )
            except Exception as exc:
                if not _should_try_alternate_response_format(exc):
                    raise
                logger.debug("Structured json_schema LLM call failed, falling back to json_object")
                response = None
        if response is None:
            try:
                response = await llm_client.chat_completion(
                    **request_kwargs,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                if not _should_try_alternate_response_format(exc):
                    raise
                response = await llm_client.chat_completion(
                    **request_kwargs,
                )
        return response

    for attempt in range(2):
        request_content = list(content)
        if repair_note:
            request_content = [
                *request_content,
                {
                    "type": "text",
                    "text": (
                        "Your previous response could not be parsed as valid JSON. "
                        "Re-evaluate the same evidence and return only one valid JSON object "
                        "that matches the requested schema. Do not include markdown or extra prose.\n\n"
                        f"Previous parse issue: {repair_note}"
                    ),
                },
            ]
        prepared_content = apply_cache_breakpoint(request_content, cache_breakpoint_index)
        response = await _request_once(prepared_content)

        try:
            message_content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected LLM response format: {exc}") from exc
        try:
            return extract_json_object(str(message_content)), response
        except ValueError as exc:
            if attempt == 1:
                raise
            repair_note = str(exc)
            logger.info("Retrying malformed JSON LLM response: %s", repair_note)

    raise RuntimeError("Unreachable")
