from __future__ import annotations

import asyncio
import base64
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pikepdf

from app.config import Settings, get_settings
from app.services.llm_client import record_external_llm_usage

DIRECT_GEMINI_JSON_PROMPT_SUFFIX = """

Return exactly one JSON object and nothing else.
Do not wrap the JSON in markdown fences.
"""
_DIRECT_GEMINI_TIMEOUT_OVERRIDE_SECONDS: ContextVar[float | None] = ContextVar(
    "direct_gemini_timeout_override_seconds",
    default=None,
)


@dataclass(slots=True)
class DirectGeminiPdfCacheHandle:
    cache_name: str
    uploaded_file_name: str
    model_name: str


def direct_gemini_pdf_enabled(settings: Settings | None = None) -> bool:
    resolved_settings = settings or get_settings()
    return bool(
        resolved_settings.use_direct_gemini_pdf
        and ((resolved_settings.gemini_api_key or "").strip() or (resolved_settings.llm_api_key or "").strip())
        and resolved_settings.gemini_model.strip()
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:
            pass
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        result[name] = _json_safe(attr)
    return result


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty Gemini response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("Gemini response did not contain a JSON object")
    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(text[start:])
    if not isinstance(parsed, dict):
        raise ValueError("Gemini response JSON was not an object")
    return parsed


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            value = getattr(part, "text", None)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _usage_to_dict(response: Any) -> dict[str, Any]:
    return _json_safe(getattr(response, "usage_metadata", None)) or {}


def _gemini_price_per_million(model_name: str) -> tuple[float, float, float]:
    normalized = (model_name or "").strip().lower()
    if "gemini-3-flash-preview" in normalized:
        return 0.50, 3.00, 0.05
    if "gemini-2.5-flash-preview" in normalized or "gemini-2.5-flash" in normalized:
        return 0.30, 2.50, 0.03
    if "gemini-2.5-flash-lite-preview" in normalized or "gemini-2.5-flash-lite" in normalized:
        return 0.10, 0.40, 0.01
    return 0.0, 0.0, 0.0


def _estimate_gemini_cost_usd(*, model_name: str, usage: dict[str, Any]) -> float:
    input_price, output_price, cache_price = _gemini_price_per_million(model_name)
    if input_price <= 0.0 and output_price <= 0.0 and cache_price <= 0.0:
        return 0.0
    prompt_tokens = int(usage.get("prompt_token_count") or 0)
    total_tokens = int(usage.get("total_token_count") or 0)
    cached_tokens = int(usage.get("cached_content_token_count") or 0)
    completion_tokens = max(0, total_tokens - prompt_tokens)
    fresh_prompt_tokens = max(0, prompt_tokens - cached_tokens)
    return (
        (fresh_prompt_tokens * input_price)
        + (cached_tokens * cache_price)
        + (completion_tokens * output_price)
    ) / 1_000_000.0


def _record_usage(*, model_name: str, usage: dict[str, Any]) -> None:
    prompt_tokens = int(usage.get("prompt_token_count") or 0)
    total_tokens = int(usage.get("total_token_count") or 0)
    completion_tokens = max(0, total_tokens - prompt_tokens)
    record_external_llm_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=_estimate_gemini_cost_usd(model_name=model_name, usage=usage),
    )


def _build_prompt_text(
    *,
    prompt: str,
    context_payload: Any | None,
    response_schema: dict[str, Any] | None,
) -> str:
    chunks = [prompt.strip()]
    if response_schema is None:
        chunks.append(DIRECT_GEMINI_JSON_PROMPT_SUFFIX.strip())
    if context_payload is not None:
        chunks.append("Context JSON:\n" + json.dumps(context_payload, indent=2, ensure_ascii=True))
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _positive_int(value: Any, *, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < minimum:
        return minimum
    return parsed


@contextmanager
def direct_gemini_timeout_override(timeout_seconds: float | None):
    if timeout_seconds is None:
        yield
        return
    try:
        parsed = float(timeout_seconds)
    except (TypeError, ValueError):
        yield
        return
    token = _DIRECT_GEMINI_TIMEOUT_OVERRIDE_SECONDS.set(max(1.0, parsed))
    try:
        yield
    finally:
        _DIRECT_GEMINI_TIMEOUT_OVERRIDE_SECONDS.reset(token)


def _gemini_timeout_milliseconds(settings: Settings) -> int:
    override = _DIRECT_GEMINI_TIMEOUT_OVERRIDE_SECONDS.get()
    raw_timeout = override if override is not None else getattr(settings, "gemini_direct_timeout", 45)
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        timeout_seconds = 45.0
    return max(1000, int(max(1.0, timeout_seconds) * 1000))


def _gemini_http_options(types_module: Any, *, settings: Settings) -> Any:
    return types_module.HttpOptions(timeout=_gemini_timeout_milliseconds(settings))


def _gemini_thinking_config(types_module: Any, *, settings: Settings) -> Any | None:
    model_name = str(getattr(settings, "gemini_model", "") or "").lower()
    if "gemini-3" in model_name:
        raw_level = str(getattr(settings, "gemini_direct_thinking_level", "minimal") or "minimal")
        level_name = raw_level.strip().upper().replace("-", "_")
        thinking_level = getattr(types_module.ThinkingLevel, level_name, types_module.ThinkingLevel.MINIMAL)
        return types_module.ThinkingConfig(include_thoughts=False, thinking_level=thinking_level)
    if "gemini-2.5" in model_name:
        budget = _positive_int(
            getattr(settings, "gemini_direct_thinking_budget", 0),
            default=0,
            minimum=0,
        )
        return types_module.ThinkingConfig(include_thoughts=False, thinking_budget=budget)
    return None


def _gemini_client(genai_module: Any, types_module: Any, *, settings: Settings) -> Any:
    return genai_module.Client(
        api_key=_gemini_api_key(settings),
        http_options=_gemini_http_options(types_module, settings=settings),
    )


def _gemini_json_config(
    types_module: Any,
    *,
    settings: Settings,
    system_instruction: str | None,
    response_schema: dict[str, Any] | None,
    cached_content: str | None = None,
) -> Any:
    return types_module.GenerateContentConfig(
        http_options=_gemini_http_options(types_module, settings=settings),
        system_instruction=system_instruction,
        cached_content=cached_content,
        temperature=0,
        max_output_tokens=_positive_int(
            getattr(settings, "gemini_direct_max_output_tokens", 8192),
            default=8192,
            minimum=256,
        ),
        thinking_config=_gemini_thinking_config(types_module, settings=settings),
        response_mime_type="application/json",
        response_json_schema=response_schema if response_schema else None,
    )


def _gemini_api_key(settings: Settings) -> str:
    value = (settings.gemini_api_key or "").strip() or (settings.llm_api_key or "").strip()
    if not value:
        raise RuntimeError("Gemini API key is not configured")
    return value


def _normalize_pdf_page_numbers(page_numbers: list[int] | None, *, total_pages: int) -> list[int]:
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


def _make_pdf_subset_io(pdf_path: Path, *, page_numbers: list[int] | None) -> BytesIO:
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    with pikepdf.Pdf.open(pdf_path) as source_pdf:
        total_pages = len(source_pdf.pages)
        normalized_pages = _normalize_pdf_page_numbers(page_numbers, total_pages=total_pages)
        if not normalized_pages:
            raise ValueError("No valid PDF pages were selected for Gemini document input")
        if normalized_pages == list(range(1, total_pages + 1)):
            stream = BytesIO(pdf_path.read_bytes())
        else:
            subset_pdf = pikepdf.Pdf.new()
            for page_number in normalized_pages:
                subset_pdf.pages.append(source_pdf.pages[page_number - 1])
            stream = BytesIO()
            subset_pdf.save(stream)
        stream.name = pdf_path.name
        stream.seek(0)
        return stream


def _split_data_url(data_url: str) -> tuple[str, bytes]:
    if not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("Unsupported data URL")
    header, encoded = data_url.split(",", 1)
    mime_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    padding = (-len(encoded)) % 4
    if padding:
        encoded = encoded + ("=" * padding)
    return mime_type, base64.b64decode(encoded)


def _extract_text_parts(content: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text"]


def _has_media_parts(content: list[dict[str, Any]]) -> bool:
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"file", "image_url"}:
            return True
    return False


def _prepare_uploaded_parts(client: Any, content: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    uploaded: list[Any] = []
    contents: list[Any] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                contents.append(text)
            continue
        if item_type == "file":
            file_info = item.get("file") or {}
            file_data = str(file_info.get("file_data") or "")
            if not file_data:
                continue
            mime_type, payload = _split_data_url(file_data)
            stream = BytesIO(payload)
            stream.name = str(file_info.get("filename") or f"upload-{index}")
            uploaded_file = client.files.upload(file=stream, config={"mime_type": mime_type})
            uploaded.append(uploaded_file)
            contents.append(uploaded_file)
            continue
        if item_type == "image_url":
            image_info = item.get("image_url") or {}
            url = str(image_info.get("url") or "")
            if not url.startswith("data:"):
                continue
            mime_type, payload = _split_data_url(url)
            stream = BytesIO(payload)
            suffix = mime_type.split("/", 1)[-1] or "bin"
            stream.name = f"image-{index}.{suffix}"
            uploaded_file = client.files.upload(file=stream, config={"mime_type": mime_type})
            uploaded.append(uploaded_file)
            contents.append(uploaded_file)
    return contents, uploaded


def _request_direct_gemini_pdf_json_sync(
    *,
    settings: Settings,
    pdf_path: Path,
    page_numbers: list[int] | None,
    prompt: str,
    context_payload: Any | None,
    response_schema: dict[str, Any] | None,
    system_instruction: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from google import genai
    from google.genai import types

    client = _gemini_client(genai, types, settings=settings)
    uploaded_file = None
    try:
        subset_stream = _make_pdf_subset_io(pdf_path, page_numbers=page_numbers)
        uploaded_file = client.files.upload(
            file=subset_stream,
            config={"mime_type": "application/pdf"},
        )
        prompt_text = _build_prompt_text(
            prompt=prompt,
            context_payload=context_payload,
            response_schema=response_schema,
        )
        response = client.models.generate_content(
            model=settings.gemini_model.strip(),
            contents=[uploaded_file, prompt_text],
            config=_gemini_json_config(
                types,
                settings=settings,
                system_instruction=system_instruction,
                response_schema=response_schema,
            ),
        )
        usage = _usage_to_dict(response)
        parsed = _extract_json_object(_response_text(response))
        return parsed, usage
    finally:
        if uploaded_file is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass


def _create_direct_gemini_pdf_cache_sync(
    *,
    settings: Settings,
    pdf_path: Path,
    page_numbers: list[int] | None,
    system_instruction: str | None,
    ttl: str,
) -> DirectGeminiPdfCacheHandle:
    from google import genai
    from google.genai import types

    client = _gemini_client(genai, types, settings=settings)
    subset_stream = _make_pdf_subset_io(pdf_path, page_numbers=page_numbers)
    uploaded_file = client.files.upload(
        file=subset_stream,
        config={"mime_type": "application/pdf"},
    )
    try:
        cache = client.caches.create(
            model=settings.gemini_model.strip(),
            config=types.CreateCachedContentConfig(
                http_options=_gemini_http_options(types, settings=settings),
                contents=[uploaded_file],
                system_instruction=system_instruction,
                ttl=ttl,
            ),
        )
        return DirectGeminiPdfCacheHandle(
            cache_name=str(cache.name),
            uploaded_file_name=str(uploaded_file.name),
            model_name=settings.gemini_model.strip(),
        )
    except Exception:
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass
        raise


async def create_direct_gemini_pdf_cache(
    *,
    pdf_path: str | Path,
    page_numbers: list[int] | None = None,
    system_instruction: str | None = None,
    ttl: str = "3600s",
    settings: Settings | None = None,
) -> DirectGeminiPdfCacheHandle:
    resolved_settings = settings or get_settings()
    if not direct_gemini_pdf_enabled(resolved_settings):
        raise RuntimeError("Direct Gemini PDF path is not configured")
    resolved_pdf_path = Path(pdf_path).expanduser().resolve()
    return await asyncio.to_thread(
        _create_direct_gemini_pdf_cache_sync,
        settings=resolved_settings,
        pdf_path=resolved_pdf_path,
        page_numbers=page_numbers,
        system_instruction=system_instruction,
        ttl=ttl,
    )


def _delete_direct_gemini_pdf_cache_sync(
    *,
    settings: Settings,
    cache_handle: DirectGeminiPdfCacheHandle,
) -> None:
    from google import genai
    from google.genai import types

    client = _gemini_client(genai, types, settings=settings)
    try:
        client.caches.delete(name=cache_handle.cache_name)
    finally:
        try:
            client.files.delete(name=cache_handle.uploaded_file_name)
        except Exception:
            pass


async def delete_direct_gemini_pdf_cache(
    cache_handle: DirectGeminiPdfCacheHandle,
    *,
    settings: Settings | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    await asyncio.to_thread(
        _delete_direct_gemini_pdf_cache_sync,
        settings=resolved_settings,
        cache_handle=cache_handle,
    )


async def request_direct_gemini_pdf_json(
    *,
    pdf_path: str | Path,
    page_numbers: list[int] | None,
    prompt: str,
    context_payload: Any | None = None,
    response_schema: dict[str, Any] | None = None,
    system_instruction: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    if not direct_gemini_pdf_enabled(resolved_settings):
        raise RuntimeError("Direct Gemini PDF path is not configured")
    resolved_pdf_path = Path(pdf_path).expanduser().resolve()
    parsed, usage = await asyncio.to_thread(
        _request_direct_gemini_pdf_json_sync,
        settings=resolved_settings,
        pdf_path=resolved_pdf_path,
        page_numbers=page_numbers,
        prompt=prompt,
        context_payload=context_payload,
        response_schema=response_schema,
        system_instruction=system_instruction,
    )
    _record_usage(model_name=resolved_settings.gemini_model.strip(), usage=usage)
    return parsed


def _request_direct_gemini_content_json_sync(
    *,
    settings: Settings,
    content: list[dict[str, Any]],
    response_schema: dict[str, Any] | None,
    system_instruction: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from google import genai
    from google.genai import types

    client = _gemini_client(genai, types, settings=settings)
    uploaded_files: list[Any] = []
    try:
        contents, uploaded_files = _prepare_uploaded_parts(client, content)
        prompt_text = "\n\n".join(text.strip() for text in _extract_text_parts(content) if text.strip())
        if not response_schema and prompt_text:
            prompt_text = "\n\n".join(
                chunk
                for chunk in [
                    prompt_text,
                    DIRECT_GEMINI_JSON_PROMPT_SUFFIX.strip(),
                ]
                if chunk
            )
        if prompt_text:
            contents.append(prompt_text)
        response = client.models.generate_content(
            model=settings.gemini_model.strip(),
            contents=contents,
            config=_gemini_json_config(
                types,
                settings=settings,
                system_instruction=system_instruction,
                response_schema=response_schema,
            ),
        )
        usage = _usage_to_dict(response)
        parsed = _extract_json_object(_response_text(response))
        response_json = {
            "choices": [{"message": {"content": json.dumps(parsed, ensure_ascii=True), "annotations": []}}],
            "usage": {
                "prompt_tokens": int(usage.get("prompt_token_count") or 0),
                "completion_tokens": max(
                    0,
                    int(usage.get("total_token_count") or 0) - int(usage.get("prompt_token_count") or 0),
                ),
                "total_tokens": int(usage.get("total_token_count") or 0),
                "cost": 0.0,
            },
            "gemini_usage_metadata": usage,
        }
        return parsed, usage, response_json
    finally:
        for uploaded_file in uploaded_files:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass


def _request_direct_gemini_cached_json_sync(
    *,
    settings: Settings,
    cache_name: str,
    prompt: str,
    context_payload: Any | None,
    response_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from google import genai
    from google.genai import types

    client = _gemini_client(genai, types, settings=settings)
    prompt_text = _build_prompt_text(
        prompt=prompt,
        context_payload=context_payload,
        response_schema=response_schema,
    )
    response = client.models.generate_content(
        model=settings.gemini_model.strip(),
        contents=prompt_text,
        config=_gemini_json_config(
            types,
            settings=settings,
            system_instruction=None,
            response_schema=response_schema,
            cached_content=cache_name,
        ),
    )
    usage = _usage_to_dict(response)
    parsed = _extract_json_object(_response_text(response))
    response_json = {
        "choices": [{"message": {"content": json.dumps(parsed, ensure_ascii=True), "annotations": []}}],
        "usage": {
            "prompt_tokens": int(usage.get("prompt_token_count") or 0),
            "completion_tokens": max(
                0,
                int(usage.get("total_token_count") or 0) - int(usage.get("prompt_token_count") or 0),
            ),
            "total_tokens": int(usage.get("total_token_count") or 0),
            "cost": _estimate_gemini_cost_usd(
                model_name=settings.gemini_model.strip(),
                usage=usage,
            ),
        },
        "gemini_usage_metadata": usage,
    }
    return parsed, usage, response_json


async def request_direct_gemini_content_json_with_response(
    *,
    content: list[dict[str, Any]],
    response_schema: dict[str, Any] | None = None,
    system_instruction: str | None = None,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_settings = settings or get_settings()
    if not direct_gemini_pdf_enabled(resolved_settings):
        raise RuntimeError("Direct Gemini PDF path is not configured")
    parsed, usage, response_json = await asyncio.to_thread(
        _request_direct_gemini_content_json_sync,
        settings=resolved_settings,
        content=content,
        response_schema=response_schema,
        system_instruction=system_instruction,
    )
    _record_usage(model_name=resolved_settings.gemini_model.strip(), usage=usage)
    return parsed, response_json


async def request_direct_gemini_content_json(
    *,
    content: list[dict[str, Any]],
    response_schema: dict[str, Any] | None = None,
    system_instruction: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    parsed, _response = await request_direct_gemini_content_json_with_response(
        content=content,
        response_schema=response_schema,
        system_instruction=system_instruction,
        settings=settings,
    )
    return parsed


async def request_direct_gemini_cached_json(
    *,
    cache_handle: DirectGeminiPdfCacheHandle,
    prompt: str,
    context_payload: Any | None = None,
    response_schema: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    parsed, usage, _response = await asyncio.to_thread(
        _request_direct_gemini_cached_json_sync,
        settings=resolved_settings,
        cache_name=cache_handle.cache_name,
        prompt=prompt,
        context_payload=context_payload,
        response_schema=response_schema,
    )
    _record_usage(model_name=resolved_settings.gemini_model.strip(), usage=usage)
    return parsed
