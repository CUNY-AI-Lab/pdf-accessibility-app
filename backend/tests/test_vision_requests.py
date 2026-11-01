"""Measure real serialized request bytes, including base64 and prompt overhead."""

import base64
import copy
import json
import random
import time
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.config import DEFAULT_LLM_MAX_REQUEST_BYTES, Settings
from app.pipeline import ocr_vlm_plugin
from app.pipeline.table_model import HttpReader
from app.services.alt_text_cache import AltTextCache, request_input_hash
from app.services.intelligence_figures import _CachedFigureRequest
from app.services.llm_client import LlmClient
from app.services.vision_requests import VisionRequestTooLarge, bound_vision_request

LIMIT = 64 * 1024


@pytest.fixture(scope="module")
def oversized_crop():
    image = Image.frombytes("RGB", (1800, 1200), random.Random(7).randbytes(1800 * 1200 * 3))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def image_part(png):
    return {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "detail": "high",
        },
        "cache_control": {"type": "ephemeral"},
    }


def decoded(part):
    return Image.open(BytesIO(base64.b64decode(part["image_url"]["url"].split(",", 1)[1])))


@pytest.mark.asyncio
async def test_oversized_crop_is_downscaled_below_configured_wire_limit(oversized_crop):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe the visible crop. Énoncé."},
                image_part(oversized_crop),
            ],
        }
    ]
    original = copy.deepcopy(messages)
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = LlmClient("https://vision.test/v1", "check-only", "vision", max_request_bytes=LIMIT)
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://vision.test/v1", transport=httpx.MockTransport(respond)
    )
    try:
        await client.chat_completion(
            messages, response_format={"type": "json_object"}, temperature=0
        )
    finally:
        await client.close()

    [request] = requests
    assert len(request.content) < LIMIT
    sent = json.loads(request.content)
    parts = sent["messages"][0]["content"]
    assert parts[0] == original[0]["content"][0]
    assert parts[1]["image_url"]["detail"] == "high"
    assert parts[1]["cache_control"] == {"type": "ephemeral"}
    with decoded(parts[1]) as image:
        assert 64 <= image.width < 1800
        assert abs(image.width / image.height - 1.5) < 0.02
    assert messages == original


def test_combined_images_history_schema_and_unicode_share_one_budget(oversized_crop):
    body = {
        "model": "vision",
        "messages": [
            {"role": "user", "content": [image_part(oversized_crop)]},
            {"role": "assistant", "content": "Please inspect this next crop."},
            {
                "role": "user",
                "content": [image_part(oversized_crop), {"type": "text", "text": "界" * 2000}],
            },
        ],
        "response_format": {"type": "json_schema", "json_schema": {"description": "schema" * 1000}},
    }
    original = copy.deepcopy(body)
    result = bound_vision_request(body, LIMIT)
    wire = httpx.Request("POST", "https://vision.test", json=result)
    assert len(wire.content) < LIMIT
    assert len(result["messages"]) == 3
    assert result["messages"][1] == original["messages"][1]
    assert result["response_format"] == original["response_format"]
    assert result["messages"][2]["content"][1] == original["messages"][2]["content"][1]
    for message in (result["messages"][0], result["messages"][2]):
        with decoded(message["content"][0]) as image:
            assert image.width < 1800
    assert body == original


def test_under_budget_requests_keep_their_exact_images_and_payload():
    body = {
        "model": "vision",
        "messages": [{"role": "user", "content": [image_part(b"unchanged")]}],
    }
    assert bound_vision_request(body, LIMIT) is body


@pytest.mark.asyncio
async def test_oversized_nonimage_content_is_never_truncated_or_sent(monkeypatch):
    client = LlmClient("https://vision.test/v1", "", "vision", max_request_bytes=LIMIT)

    async def forbidden(*args, **kwargs):
        pytest.fail("An oversized request reached transport")

    monkeypatch.setattr(client.client, "post", forbidden)
    try:
        with pytest.raises(VisionRequestTooLarge):
            await client.chat_completion([{"role": "user", "content": "x" * LIMIT}])
    finally:
        await client.close()


@pytest.mark.parametrize("transport", ["table", "ocr"])
@pytest.mark.asyncio
async def test_other_vision_transports_use_the_same_total_body_limit(
    monkeypatch, oversized_crop, transport
):
    requests = []

    def response(url, body):
        request = httpx.Request("POST", url, json=body)
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {"content": "[]"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    if transport == "table":

        async def post(self, url, *, json):
            return response(url, json)

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        await HttpReader("https://vision.test/v1", "table", max_request_bytes=LIMIT).read(
            oversized_crop
        )
    else:

        def post(url, *, json, **kwargs):
            return response(url, json)

        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setenv("CAIL_OCR_MAX_REQUEST_BYTES", str(LIMIT))
        ocr_vlm_plugin._complete(
            "https://vision.test/v1",
            {},
            {
                "model": "ocr",
                "messages": [{"role": "user", "content": [image_part(oversized_crop)]}],
            },
            time.monotonic() + 30,
        )
    [request] = requests
    assert len(request.content) < LIMIT
    with decoded(json.loads(request.content)["messages"][0]["content"][0]) as image:
        assert image.width < 1800


@pytest.mark.asyncio
async def test_description_cache_matches_the_resized_wire_input(
    tmp_path, monkeypatch, oversized_crop
):
    from app.pipeline.structure import FigureInfo

    path = tmp_path / "figure.png"
    path.write_bytes(oversized_crop)
    client = LlmClient("https://vision.test/v1", "", "vision", max_request_bytes=LIMIT)
    adapter = _CachedFigureRequest(
        client, AltTextCache(tmp_path / "cache.sqlite3"), FigureInfo(index=0, path=path), "test"
    )
    requests = []

    async def post(url, *, json):
        requests.append(json)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
            request=httpx.Request("POST", url, json=json),
        )

    monkeypatch.setattr(client.client, "post", post)
    try:
        await adapter.chat_completion(
            [{"role": "user", "content": [image_part(oversized_crop)]}], temperature=0
        )
    finally:
        await client.close()
    [body] = requests
    assert adapter.input_hash == request_input_hash(
        {key: value for key, value in body.items() if key != "model"}
    )


def test_request_limit_setting_defaults_and_validation():
    settings = Settings(_env_file=None, llm_api_key="check-only")
    assert settings.llm_max_request_bytes == DEFAULT_LLM_MAX_REQUEST_BYTES == 2 * 1024 * 1024
    with pytest.raises(ValueError):
        Settings(_env_file=None, llm_api_key="check-only", llm_max_request_bytes=1024)


@pytest.mark.asyncio
async def test_local_size_failure_does_not_change_model_format_capabilities(monkeypatch):
    from app.services import intelligence_llm_utils

    monkeypatch.setattr(intelligence_llm_utils, "FORMAT_CAPABILITIES", intelligence_llm_utils.FormatCapabilityCache())
    client = LlmClient("https://vision.test/v1", "", "vision", max_request_bytes=LIMIT)

    async def forbidden(*args, **kwargs):
        pytest.fail("An oversized request reached transport")

    monkeypatch.setattr(client.client, "post", forbidden)
    try:
        with pytest.raises(VisionRequestTooLarge):
            await intelligence_llm_utils.request_llm_json(
                llm_client=client,
                content=[{"type": "text", "text": "x" * LIMIT}],
                schema_name="vision",
                response_schema={"type": "object"},
            )
        assert not intelligence_llm_utils.FORMAT_CAPABILITIES.entries
    finally:
        await client.close()
