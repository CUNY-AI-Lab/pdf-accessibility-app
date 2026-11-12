import asyncio
import json
from pathlib import Path

import httpx
import pikepdf
import pytest

from app.config import Settings
from app.runner.client import Task, WorkerClient
from app.services.intelligence_llm_utils import (
    FORMAT_CAPABILITIES,
    apply_cache_breakpoint,
    extract_json_object,
    preferred_cache_breakpoint_index,
    request_llm_json,
    request_llm_json_with_response,
    request_pdf_pages_json,
)
from app.services.llm_client import job_model_access, make_llm_client


@pytest.fixture(autouse=True)
def _forget_json_schema_refusals():
    FORMAT_CAPABILITIES.entries.clear()
    yield
    FORMAT_CAPABILITIES.entries.clear()


def test_extract_json_object_accepts_trailing_text_after_first_json_object():
    parsed = extract_json_object('{"summary":"ok"}\n{"ignored":true}')

    assert parsed == {"summary": "ok"}


class _SchemaFallbackLlm:
    model = "test-model"
    base_url = "https://gateway.test/v1"

    def __init__(self):
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(kwargs)
        response_format = kwargs.get("response_format") or {}
        if response_format.get("type") == "json_schema":
            request = httpx.Request("POST", self.base_url + "/chat/completions")
            response = httpx.Response(400, request=request, json={"error": {
                "code": "capability_unsupported", "message": "model does not support response_format json_schema",
            }})
            response.raise_for_status()
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
    model = "test-model"

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
    model = "test-model"

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
    model = "test-model"

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
    model = "test-model"

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


class _RecordingLlm:
    model = "test-model"

    def __init__(self, message):
        self.message = message
        self.messages = []
        self.closed = False

    async def chat_completion(self, messages, **kwargs):
        self.messages.append(messages)
        return {"choices": [{"message": self.message}]}

    async def close(self):
        self.closed = True


def test_image_content_goes_to_the_chat_completions_lane():
    llm = _RecordingLlm({"content": '{"summary":"ok"}'})
    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}

    parsed = asyncio.run(
        request_llm_json(llm_client=llm, content=[{"type": "text", "text": "prompt"}, image])
    )

    assert parsed == {"summary": "ok"}
    assert llm.messages[0][-1]["content"][1] == image


def test_a_reasoning_models_json_is_read_from_reasoning_content():
    llm = _RecordingLlm({"content": "", "reasoning_content": '{"summary":"ok"}'})

    parsed = asyncio.run(request_llm_json(llm_client=llm, content=[{"type": "text", "text": "hi"}]))

    assert parsed == {"summary": "ok"}


def test_pdf_pages_request_sends_page_images_and_the_system_instruction(monkeypatch, tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    document = pikepdf.new()
    document.add_blank_page()
    document.add_blank_page()
    document.save(pdf_path)
    llm = _RecordingLlm({"content": '{"title":"Annual Report"}'})
    monkeypatch.setattr("app.services.intelligence_llm_utils.make_llm_client", lambda settings: llm)

    parsed = asyncio.run(
        request_pdf_pages_json(
            pdf_path=pdf_path,
            page_numbers=[1, 2],
            prompt="Find the title.",
            context_payload={"candidates": ["Annual Report"]},
            system_instruction="You read documents.",
        )
    )

    assert parsed == {"title": "Annual Report"}
    system, user = llm.messages[0]
    assert system == {"role": "system", "content": "You read documents."}
    assert [part["type"] for part in user["content"]] == ["text", "image_url", "image_url", "text"]
    assert llm.closed


def test_a_model_that_refused_json_schema_is_asked_for_json_object_next_time():
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    first, second = _SchemaFallbackLlm(), _SchemaFallbackLlm()

    for llm in (first, second):
        asyncio.run(
            request_llm_json(
                llm_client=llm,
                content=[{"type": "text", "text": "hello"}],
                schema_name="demo",
                response_schema=schema,
            )
        )

    assert [call["response_format"]["type"] for call in first.calls] == [
        "json_schema",
        "json_object",
    ]
    assert [call["response_format"]["type"] for call in second.calls] == ["json_object"]


def test_a_format_refusal_learned_in_one_job_holds_for_the_next(tmp_path):
    # Each runner task has its own relay URL; all of them reach one Gateway.
    asked = []

    def gateway(request):
        requested = json.loads(request.content)["response_format"]["type"]
        asked.append((request.url.path.split("/tasks/")[1].split("/")[0], requested))
        if requested == "json_schema":
            return httpx.Response(400, json={"error": {
                "code": "capability_unsupported",
                "message": 'The model "check-model" does not support response_format json_schema.',
            }})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"summary":"ok"}'}}]})

    settings = Settings(upload_dir=tmp_path / "u", processing_dir=tmp_path / "p", output_dir=tmp_path / "o")
    worker = WorkerClient("https://tools.test/pdf-accessibility/runner", "host-token")

    async def run(job_id):
        task = Task.from_json({"job_id": job_id, "kind": "process", "lease": "lease", "attempt": 1,
                               "deadline": 4_102_444_800_000, "original_filename": "a.pdf"})
        with job_model_access(worker.model_endpoint(task)):
            llm = make_llm_client(settings)
            await llm.client.aclose()
            llm.client = httpx.AsyncClient(base_url=llm.base_url, transport=httpx.MockTransport(gateway))
            try:
                await request_llm_json(llm_client=llm, content=[{"type": "text", "text": "hello"}],
                                       schema_name="demo", response_schema={
                                           "type": "object", "properties": {"summary": {"type": "string"}},
                                           "required": ["summary"]})
            finally:
                await llm.close()

    async def both():
        await run("job-a")
        await run("job-b")
        await worker.close()

    asyncio.run(both())
    assert asked == [("job-a", "json_schema"), ("job-a", "json_object"), ("job-b", "json_object")]


class _RecordingSchemaFallbackLlm(_SchemaFallbackLlm):
    def __init__(self, *, reject_schema=True):
        super().__init__()
        self.messages = []
        self.reject_schema = reject_schema

    async def chat_completion(self, messages, **kwargs):
        self.messages.append(messages)
        if not self.reject_schema:
            self.calls.append(kwargs)
            return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}
        return await super().chat_completion(messages, **kwargs)


def _stated_schema(message):
    """The JSON object stated in the last text part of a user message, if any."""
    last = message["content"][-1]
    if last.get("type") != "text" or "{" not in last["text"]:
        return None
    return extract_json_object(last["text"][last["text"].index("{") :])


_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"figure_index": {"type": "integer"}},
                "required": ["figure_index"],
            },
        }
    },
    "required": ["decisions"],
}


@pytest.mark.parametrize("prefix", [None, [{"role": "system", "content": "You read documents."}]])
def test_json_object_fallback_states_the_schema_in_the_user_turn(prefix):
    llm = _RecordingSchemaFallbackLlm()
    content = [
        {"type": "text", "text": "Decide each figure."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]

    asyncio.run(
        request_llm_json(
            llm_client=llm,
            content=content,
            schema_name="figure_batch",
            response_schema=_DECISION_SCHEMA,
            conversation_prefix=prefix,
        )
    )

    schema_call, object_call = llm.messages
    assert [call["response_format"]["type"] for call in llm.calls] == ["json_schema", "json_object"]
    # The provider enforces json_schema itself; that request is unchanged.
    assert schema_call[-1]["content"] == content
    assert object_call[:-1] == schema_call[:-1]
    assert object_call[-1]["role"] == "user"
    assert object_call[-1]["content"][: len(content)] == content
    assert _stated_schema(object_call[-1]) == _DECISION_SCHEMA


def test_a_remembered_refusal_still_states_the_schema():
    first, second = _RecordingSchemaFallbackLlm(), _RecordingSchemaFallbackLlm()
    for llm in (first, second):
        asyncio.run(
            request_llm_json(
                llm_client=llm,
                content=[{"type": "text", "text": "hello"}],
                schema_name="demo",
                response_schema=_DECISION_SCHEMA,
            )
        )
    [only] = second.messages
    assert _stated_schema(only[-1]) == _DECISION_SCHEMA


@pytest.mark.parametrize("schema_name", [None, "demo"])
def test_schema_is_stated_only_when_the_provider_cannot_enforce_it(schema_name):
    llm = _RecordingSchemaFallbackLlm(reject_schema=False)
    asyncio.run(
        request_llm_json(
            llm_client=llm,
            content=[{"type": "text", "text": "hello"}],
            schema_name=schema_name,
            response_schema=_DECISION_SCHEMA,
        )
    )
    [only] = llm.messages
    if schema_name:
        assert llm.calls[0]["response_format"]["type"] == "json_schema"
        assert _stated_schema(only[-1]) is None
    else:
        # A schema without a name is never sent as json_schema.
        assert llm.calls[0]["response_format"]["type"] == "json_object"
        assert _stated_schema(only[-1]) == _DECISION_SCHEMA


def test_a_request_without_a_schema_is_unchanged():
    llm = _RecordingSchemaFallbackLlm(reject_schema=False)
    content = [{"type": "text", "text": 'Return {"summary": "..."}'}]
    asyncio.run(request_llm_json(llm_client=llm, content=content))
    [only] = llm.messages
    assert only[-1]["content"] == content


class _AnsweringLlm:
    model = "test-model"
    base_url = "https://gateway.test/v1"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(kwargs)
        content, finish = self.answers.pop(0)
        return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


@pytest.mark.parametrize("cap", [None, 900])
def test_every_structured_request_caps_its_output(cap):
    from app.services.intelligence_llm_utils import DEFAULT_MAX_OUTPUT_TOKENS

    llm = _AnsweringLlm(('{"summary":"ok"}', "stop"))
    options = {} if cap is None else {"max_tokens": cap}
    parsed = asyncio.run(
        request_llm_json(llm_client=llm, content=[{"type": "text", "text": "hi"}], **options)
    )
    assert parsed == {"summary": "ok"}
    assert llm.calls[0]["max_tokens"] == (cap or DEFAULT_MAX_OUTPUT_TOKENS)


@pytest.mark.parametrize(
    "content",
    ['{"decisions": [{"figure_index": 3}]}', '{"decisions": [{"figure_index": 3, "alt', ""],
)
def test_an_answer_cut_off_at_the_cap_is_a_failed_call(content):
    from app.services.intelligence_llm_utils import ModelOutputCutOff

    llm = _AnsweringLlm((content, "length"), ('{"decisions": []}', "stop"))
    with pytest.raises(ModelOutputCutOff):
        asyncio.run(request_llm_json(llm_client=llm, content=[{"type": "text", "text": "hi"}]))
    # Asking again under the same cap would run on the same way.
    assert len(llm.calls) == 1


def test_the_cap_reaches_the_pages_request(monkeypatch, tmp_path):
    from app.services import intelligence_llm_utils

    pdf_path = tmp_path / "one.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(pdf_path)
    llm = _AnsweringLlm(('{"title":"T"}', "stop"))
    llm.close = lambda: asyncio.sleep(0)
    monkeypatch.setattr(intelligence_llm_utils, "make_llm_client", lambda settings: llm)
    monkeypatch.setattr(intelligence_llm_utils, "page_preview_parts", lambda job, pages: [])
    asyncio.run(
        request_pdf_pages_json(
            pdf_path=pdf_path,
            page_numbers=[1],
            prompt="Title?",
            system_instruction="Read.",
            max_tokens=1234,
        )
    )
    assert llm.calls[0]["max_tokens"] == 1234
