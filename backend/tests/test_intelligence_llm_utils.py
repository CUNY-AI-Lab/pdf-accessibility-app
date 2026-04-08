import asyncio
import base64
from io import BytesIO
from pathlib import Path

import httpx
import pikepdf
import pytest

from app.services.intelligence_llm_utils import (
    apply_cache_breakpoint,
    extract_json_object,
    pdf_file_parts,
    preferred_cache_breakpoint_index,
    request_llm_json,
    request_llm_json_with_response,
)


@pytest.fixture(autouse=True)
def _disable_direct_gemini_by_default(monkeypatch):
    monkeypatch.setattr(
        "app.services.intelligence_llm_utils.direct_gemini_pdf_enabled",
        lambda: False,
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
    assert llm.calls[1]["response_format"]["type"] == "json_object"


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


def test_request_llm_json_with_response_uses_direct_gemini_for_media(monkeypatch):
    captured = {}

    async def _fake_direct_request(**kwargs):
        captured.update(kwargs)
        return (
            {"summary": "ok"},
            {
                "choices": [{"message": {"content": '{"summary":"ok"}', "annotations": []}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.0},
            },
        )

    monkeypatch.setattr(
        "app.services.intelligence_llm_utils.direct_gemini_pdf_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.intelligence_llm_utils.request_direct_gemini_content_json_with_response",
        _fake_direct_request,
    )

    parsed, response = asyncio.run(
        request_llm_json_with_response(
            llm_client=object(),
            content=[
                {"type": "text", "text": "prompt"},
                {"type": "file", "file": {"filename": "doc.pdf", "file_data": "data:application/pdf;base64,AA=="}},
            ],
            schema_name="demo",
            response_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        )
    )

    assert parsed == {"summary": "ok"}
    assert response["usage"]["total_tokens"] == 12
    assert captured["content"][1]["type"] == "file"


def test_preferred_cache_breakpoint_index_prefers_last_image_block():
    content = [
        {"type": "text", "text": "prompt"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,def"}},
        {"type": "text", "text": "dynamic"},
    ]

    assert preferred_cache_breakpoint_index(content) == 2


def test_preferred_cache_breakpoint_index_prefers_last_file_block():
    content = [
        {"type": "text", "text": "prompt"},
        {"type": "file", "file": {"filename": "document.pdf", "file_data": "data:application/pdf;base64,abc"}},
        {"type": "text", "text": "dynamic"},
    ]

    assert preferred_cache_breakpoint_index(content) == 1


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


class _ConversationPrefixLlm:
    def __init__(self):
        self.messages = []
        self.kwargs = []

    async def chat_completion(self, messages, **kwargs):
        self.messages.append(messages)
        self.kwargs.append(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"summary":"ok"}',
                        "annotations": [{"type": "file", "file": {"hash": "abc"}}],
                    }
                }
            ]
        }


def test_request_llm_json_with_response_includes_conversation_prefix():
    llm = _ConversationPrefixLlm()

    parsed, response = asyncio.run(
        request_llm_json_with_response(
            llm_client=llm,
            content=[{"type": "text", "text": "hello"}],
            conversation_prefix=[
                {
                    "role": "assistant",
                    "content": "Previous context is available.",
                }
            ],
        )
    )

    assert parsed == {"summary": "ok"}
    assert llm.messages[0][0]["role"] == "assistant"
    assert llm.messages[0][1]["role"] == "user"


def test_request_llm_json_with_response_omits_plugins_when_not_provided():
    llm = _ConversationPrefixLlm()

    parsed, _ = asyncio.run(
        request_llm_json_with_response(
            llm_client=llm,
            content=[{"type": "text", "text": "hello"}],
        )
    )

    assert parsed == {"summary": "ok"}
    assert "plugins" not in llm.kwargs[0]


def _make_pdf(pdf_path: Path, page_count: int) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(page_count):
        pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))


def test_pdf_file_parts_emits_pdf_subset_as_file_content(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, 3)

    job = type(
        "JobLike",
        (),
        {
            "input_path": str(pdf_path),
            "output_path": str(pdf_path),
            "original_filename": "sample.pdf",
        },
    )()

    parts = pdf_file_parts(job, [1, 3])

    assert len(parts) == 1
    assert parts[0]["type"] == "file"
    assert parts[0]["file"]["filename"] == "sample.pdf"
    data_url = parts[0]["file"]["file_data"]
    assert data_url.startswith("data:application/pdf;base64,")
    decoded = base64.b64decode(data_url.split(",", 1)[1])
    with pikepdf.Pdf.open(BytesIO(decoded)) as pdf:
        assert len(pdf.pages) == 2
