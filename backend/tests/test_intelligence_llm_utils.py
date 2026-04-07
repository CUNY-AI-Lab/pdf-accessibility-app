import asyncio

import httpx
import pytest

from app.services.intelligence_llm_utils import (
    apply_cache_breakpoint,
    extract_json_object,
    preferred_cache_breakpoint_index,
    request_llm_json,
)


def test_extract_json_object_accepts_trailing_text_after_first_json_object():
    parsed = extract_json_object('{"summary":"ok"}\n{"ignored":true}')

    assert parsed == {"summary": "ok"}


class _SchemaFallbackLlm:
    def __init__(self):
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(kwargs)
        response_format = kwargs.get("response_format") or {}
        if response_format.get("type") == "json_schema":
            raise RuntimeError("json_schema unsupported")
        return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}


def test_request_llm_json_tries_json_schema_then_falls_back_to_json_object():
    llm = _SchemaFallbackLlm()

    parsed = asyncio.run(
        request_llm_json(
            llm_client=llm,
            content=[{"type": "text", "text": "hello"}],
            schema_name="demo",
            response_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        )
    )

    assert parsed == {"summary": "ok"}
    assert llm.calls[0]["response_format"]["type"] == "json_schema"
    assert llm.calls[0]["provider"] == {"require_parameters": True}
    assert llm.calls[1]["response_format"]["type"] == "json_object"
    assert llm.calls[1]["provider"] == {"require_parameters": True}


class _RetryableFailureLlm:
    def __init__(self):
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(kwargs)
        request = httpx.Request("POST", "https://example.com/chat/completions")
        response = httpx.Response(502, request=request)
        raise httpx.HTTPStatusError("bad gateway", request=request, response=response)


def test_request_llm_json_does_not_fallback_formats_on_retryable_endpoint_failure():
    llm = _RetryableFailureLlm()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            request_llm_json(
                llm_client=llm,
                content=[{"type": "text", "text": "hello"}],
                schema_name="demo",
                response_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            )
        )

    assert len(llm.calls) == 1
    assert llm.calls[0]["response_format"]["type"] == "json_schema"


def test_apply_cache_breakpoint_marks_only_requested_content_item():
    content = [
        {"type": "text", "text": "instructions"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        {"type": "text", "text": "dynamic"},
    ]

    prepared = apply_cache_breakpoint(content, 1)

    assert content[1].get("cache_control") is None
    assert prepared[0].get("cache_control") is None
    assert prepared[1]["cache_control"] == {"type": "ephemeral"}
    assert prepared[2].get("cache_control") is None


class _CacheBreakpointLlm:
    def __init__(self):
        self.messages = []

    async def chat_completion(self, messages, **kwargs):
        self.messages.append(messages)
        return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}


def test_request_llm_json_applies_cache_breakpoint_to_message_content():
    llm = _CacheBreakpointLlm()

    parsed = asyncio.run(
        request_llm_json(
            llm_client=llm,
            content=[
                {"type": "text", "text": "prompt"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                {"type": "text", "text": "dynamic"},
            ],
            cache_breakpoint_index=1,
        )
    )

    assert parsed == {"summary": "ok"}
    sent_content = llm.messages[0][0]["content"]
    assert sent_content[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent_content[0]
    assert "cache_control" not in sent_content[2]


def test_preferred_cache_breakpoint_index_prefers_last_image_block():
    content = [
        {"type": "text", "text": "prompt"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,def"}},
        {"type": "text", "text": "dynamic"},
    ]

    assert preferred_cache_breakpoint_index(content) == 2


def test_preferred_cache_breakpoint_index_uses_last_block_when_no_images():
    content = [
        {"type": "text", "text": "prompt"},
        {"type": "text", "text": "dynamic"},
    ]

    assert preferred_cache_breakpoint_index(content) == 1


class _MalformedThenValidLlm:
    def __init__(self):
        self.messages = []

    async def chat_completion(self, messages, **kwargs):
        self.messages.append(messages)
        if len(self.messages) == 1:
            return {"choices": [{"message": {"content": '{"summary":"unterminated'}}]}
        return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}


def test_request_llm_json_retries_after_malformed_json_response():
    llm = _MalformedThenValidLlm()

    parsed = asyncio.run(
        request_llm_json(
            llm_client=llm,
            content=[{"type": "text", "text": "hello"}],
            schema_name="demo",
            response_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        )
    )

    assert parsed == {"summary": "ok"}
    assert len(llm.messages) == 2
    retry_content = llm.messages[1][0]["content"]
    assert retry_content[-1]["type"] == "text"
    assert "could not be parsed as valid JSON" in retry_content[-1]["text"]
