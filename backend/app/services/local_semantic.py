from __future__ import annotations

import json
from typing import Any

from app.config import Settings, get_settings
from app.services.llm_client import LlmClient, is_retryable_llm_exception


def local_semantic_enabled(settings: Settings | None = None) -> bool:
    resolved_settings = settings or get_settings()
    return bool(
        resolved_settings.semantic_media_backend.strip().lower() == "local"
        and resolved_settings.local_semantic_base_url.strip()
        and resolved_settings.local_semantic_model.strip()
    )


def make_local_semantic_client(settings: Settings | None = None) -> LlmClient:
    resolved_settings = settings or get_settings()
    api_key = (resolved_settings.local_semantic_api_key or "").strip() or "local"
    return LlmClient(
        base_url=resolved_settings.local_semantic_base_url,
        api_key=api_key,
        model=resolved_settings.local_semantic_model,
        timeout=resolved_settings.local_semantic_timeout,
        max_retries=resolved_settings.local_semantic_max_retries,
        retry_backoff_base=resolved_settings.llm_retry_backoff_base,
        max_backoff_seconds=resolved_settings.llm_retry_max_backoff_seconds,
        max_concurrency=resolved_settings.local_semantic_max_concurrency,
    )


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty local semantic response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            text = "\n".join(lines[1:-1]).strip()

    start = text.find("{")
    if start < 0:
        raise ValueError("Local semantic response did not contain a JSON object")

    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(text[start:])
    if not isinstance(parsed, dict):
        raise ValueError("Local semantic response JSON was not an object")
    return parsed


def _extract_json_from_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ValueError("Unexpected local semantic message format")

    content = str(message.get("content") or "").strip()
    if content:
        try:
            return _extract_json_object(content)
        except ValueError:
            pass

    reasoning_content = str(message.get("reasoning_content") or "").strip()
    if reasoning_content:
        return _extract_json_object(reasoning_content)

    raise ValueError("Local semantic response did not contain JSON in content or reasoning_content")


def _should_try_alternate_response_format(exc: BaseException) -> bool:
    return not is_retryable_llm_exception(exc)


async def request_local_semantic_content_json_with_response(
    *,
    content: list[dict[str, Any]],
    response_schema: dict[str, Any] | None = None,
    system_instruction: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = make_local_semantic_client()
    try:
        messages: list[dict[str, Any]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": content})

        request_kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": 0,
        }

        response = None
        if response_schema:
            try:
                response = await client.chat_completion(
                    **request_kwargs,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "semantic_response",
                            "strict": True,
                            "schema": response_schema,
                        },
                    },
                )
            except Exception as exc:
                if not _should_try_alternate_response_format(exc):
                    raise
                response = None
        if response is None:
            try:
                response = await client.chat_completion(
                    **request_kwargs,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                if not _should_try_alternate_response_format(exc):
                    raise
                response = await client.chat_completion(**request_kwargs)

        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected local semantic response format: {exc}") from exc
        return _extract_json_from_message(message), response
    finally:
        await client.close()
