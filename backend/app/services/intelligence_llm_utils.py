"""Structured-response state machines used by runner semantic analysis.

Transitions::
    format: schema -> object -> text (explicit format-capability rejection only)
    capability: unknown/supported/unsupported -> supported/unsupported (verified response)
    capability: supported/unsupported -> unknown (900s expiry or 256-entry eviction)
    JSON: initial -> repair (one malformed answer)
    JSON: initial/repair -> succeeded | failed; terminal states have no successors

Capability keys include endpoint, model and format; access, transport, invalid
schema and unrelated capability failures cannot downgrade or poison the cache.
"""

import copy
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from app.config import get_settings
from app.models import Job
from app.services.llm_client import LlmClient, make_llm_client
from app.services.path_safety import PathAccessError, validate_path_within_allowed_roots
from app.services.pdf_preview import render_page_jpeg_data_url

logger = logging.getLogger(__name__)

class ResponseFormatMode(Enum):
    SCHEMA = "json_schema"
    OBJECT = "json_object"
    TEXT = "text"


_FORMAT_DOWNGRADES = {
    ResponseFormatMode.SCHEMA: ResponseFormatMode.OBJECT,
    ResponseFormatMode.OBJECT: ResponseFormatMode.TEXT,
}


def downgrade_response_format(mode: ResponseFormatMode) -> ResponseFormatMode:
    if mode not in _FORMAT_DOWNGRADES:
        raise ValueError(f"Illegal response format downgrade: {mode.value}")
    return _FORMAT_DOWNGRADES[mode]


class FormatCapability(Enum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class FormatCapabilityCache:
    """Endpoint/model/format facts, never credentials or document content."""

    def __init__(self, ttl: float = 900, max_entries: int = 256):
        self.ttl = ttl
        self.max_entries = max(1, max_entries)
        self.entries: OrderedDict[tuple[str, str, ResponseFormatMode],
                                  tuple[float, FormatCapability]] = OrderedDict()

    def get(self, key: tuple[str, str, ResponseFormatMode]) -> FormatCapability:
        entry = self.entries.get(key)
        if entry is None:
            return FormatCapability.UNKNOWN
        stored, state = entry
        if time.monotonic() - stored >= self.ttl:
            del self.entries[key]
            return FormatCapability.UNKNOWN
        return state

    def record(self, key: tuple[str, str, ResponseFormatMode], state: FormatCapability) -> None:
        if state not in {FormatCapability.SUPPORTED, FormatCapability.UNSUPPORTED}:
            raise ValueError("Illegal capability write: require a verified response")
        self.entries[key] = (time.monotonic(), state)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)


FORMAT_CAPABILITIES = FormatCapabilityCache()


def _should_try_alternate_response_format(exc: BaseException, mode: ResponseFormatMode) -> bool:
    # Only an explicit capability rejection proves that another format helps.
    # Schema validation errors, access refusals, and uncertain transport failures
    # must propagate without poisoning the cache or issuing another model call.
    if mode is ResponseFormatMode.TEXT or not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code not in {400, 422}:
        return False
    try:
        error = exc.response.json().get("error")
    except (ValueError, AttributeError):
        return False
    if not isinstance(error, dict):
        return False
    message = str(error.get("message", "")).lower()
    param = str(error.get("param", "")).lower()
    code = str(error.get("code", "")).lower()
    if code == "invalid_json_schema" or "invalid schema" in message:
        return False
    if code == "capability_unsupported" and any(
        feature in message for feature in ("no single route", "image input", "tools", "reasoning controls")
    ):
        # A rejection of another feature or a feature combination is not a
        # model-wide fact about this response format.
        return False
    named = mode.value in message or param == f"response_format.{mode.value}"
    # Gateway's capability_unsupported also names the missing feature; a
    # rejection for vision or tools must not downgrade structured output.
    return named and (
        code in {"capability_unsupported", "unsupported_response_format", "unsupported_value"}
        or (param == "response_format" and ("not support" in message or "unsupported" in message))
    )


class JsonAttempt(Enum):
    INITIAL = "initial"
    REPAIR = "repair"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_JSON_TRANSITIONS = {
    JsonAttempt.INITIAL: {JsonAttempt.REPAIR, JsonAttempt.SUCCEEDED, JsonAttempt.FAILED},
    JsonAttempt.REPAIR: {JsonAttempt.SUCCEEDED, JsonAttempt.FAILED},
    JsonAttempt.SUCCEEDED: set(),
    JsonAttempt.FAILED: set(),
}


def transition_json_attempt(current: JsonAttempt, target: JsonAttempt) -> JsonAttempt:
    if target not in _JSON_TRANSITIONS[current]:
        raise ValueError(f"Illegal JSON attempt transition: {current.value} -> {target.value}")
    return target


def job_pdf_path(job: Job) -> Path:
    candidates = []
    if getattr(job, "output_path", None):
        candidates.append(Path(str(job.output_path)))
    if getattr(job, "input_path", None):
        candidates.append(Path(str(job.input_path)))
    for pdf_path in candidates:
        resolved = pdf_path.resolve()
        try:
            validated = validate_path_within_allowed_roots(resolved)
        except PathAccessError:
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


def extract_message_json(message: Any) -> dict[str, Any]:
    """The JSON object in a chat-completions message: from its content, or,
    for reasoning models that leave content empty, its reasoning_content."""
    if not isinstance(message, dict):
        raise ValueError("Unexpected LLM message format")
    content = str(message.get("content") or "").strip()
    reasoning = str(message.get("reasoning_content") or "").strip()
    if content:
        try:
            return extract_json_object(content)
        except ValueError:
            if not reasoning:
                raise
    return extract_json_object(reasoning)


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
        if item.get("type") == "image_url":
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
        mode = ResponseFormatMode.SCHEMA if response_schema and schema_name else ResponseFormatMode.OBJECT
        endpoint = str(getattr(llm_client, "base_url", ""))
        while True:
            key = (endpoint, str(llm_client.model), mode)
            if mode is not ResponseFormatMode.TEXT:
                if endpoint and FORMAT_CAPABILITIES.get(key) is FormatCapability.UNSUPPORTED:
                    mode = downgrade_response_format(mode)
                    continue
                request_kwargs["response_format"] = {"type": mode.value}
                if mode is ResponseFormatMode.SCHEMA:
                    request_kwargs["response_format"]["json_schema"] = {
                        "name": schema_name, "strict": True, "schema": response_schema,
                    }
            else:
                request_kwargs.pop("response_format", None)
            try:
                response = await llm_client.chat_completion(**request_kwargs)
            except Exception as exc:
                if not _should_try_alternate_response_format(exc, mode):
                    raise
                if endpoint:
                    FORMAT_CAPABILITIES.record(key, FormatCapability.UNSUPPORTED)
                mode = downgrade_response_format(mode)
                continue
            if endpoint and mode is not ResponseFormatMode.TEXT:
                FORMAT_CAPABILITIES.record(key, FormatCapability.SUPPORTED)
            return response

    attempt = JsonAttempt.INITIAL
    while True:
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
        try:
            response = await _request_once(prepared_content)
        except BaseException:
            attempt = transition_json_attempt(attempt, JsonAttempt.FAILED)
            raise

        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            attempt = transition_json_attempt(attempt, JsonAttempt.FAILED)
            raise ValueError(f"Unexpected LLM response format: {exc}") from exc
        try:
            parsed = extract_message_json(message)
        except ValueError as exc:
            if attempt is JsonAttempt.REPAIR:
                attempt = transition_json_attempt(attempt, JsonAttempt.FAILED)
                raise
            attempt = transition_json_attempt(attempt, JsonAttempt.REPAIR)
            repair_note = str(exc)
            logger.info("Retrying malformed JSON LLM response: %s", repair_note)
            continue
        attempt = transition_json_attempt(attempt, JsonAttempt.SUCCEEDED)
        return parsed, response


async def request_pdf_pages_json(
    *,
    pdf_path: str | Path,
    page_numbers: list[int],
    prompt: str,
    context_payload: Any | None = None,
    response_schema: dict[str, Any] | None = None,
    schema_name: str = "document_decision",
    system_instruction: str,
) -> dict[str, Any]:
    """Ask the model lane about some pages of a PDF, shown as rendered page
    images."""
    job = SimpleNamespace(id=None, output_path=None, input_path=str(pdf_path))
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        *page_preview_parts(job, page_numbers),
    ]
    if context_payload is not None:
        content.append(context_json_part(context_payload))
    client = make_llm_client(get_settings())
    try:
        return await request_llm_json(
            llm_client=client,
            content=content,
            schema_name=schema_name if response_schema else None,
            response_schema=response_schema,
            conversation_prefix=[{"role": "system", "content": system_instruction}],
        )
    finally:
        await client.close()
