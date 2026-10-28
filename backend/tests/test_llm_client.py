import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.services.llm_client import (
    LlmClient,
    ModelAccessRefused,
    ModelEndpoint,
    job_model_access,
    make_llm_client,
    track_llm_usage,
)


def _ok_response(request: httpx.Request, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json=payload or {"choices": [{"message": {"content": "{}"}}]}, request=request)


def test_llm_client_honors_retry_after_header(monkeypatch):
    client = LlmClient(
        base_url="https://tools.ailab.gc.cuny.edu/v1",
        api_key="test",
        model="qwen3-vl-235b-a22b-instruct",
        max_retries=1,
        max_concurrency=1,
    )
    attempts = {"count": 0}
    sleeps: list[float] = []

    async def fake_post(path, json):
        attempts["count"] += 1
        request = client.client.build_request("POST", path, json=json)
        if attempts["count"] == 1:
            response = httpx.Response(
                503,
                headers={"Retry-After": "7"},
                request=request,
            )
            raise httpx.HTTPStatusError("retry later", request=request, response=response)
        return _ok_response(request)

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(client.client, "post", fake_post)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
    asyncio.run(client.close())

    assert result["choices"][0]["message"]["content"] == "{}"
    assert attempts["count"] == 2
    assert sleeps == [7.0]


def test_llm_client_retries_transport_error_then_succeeds(monkeypatch):
    client = LlmClient(
        base_url="https://tools.ailab.gc.cuny.edu/v1",
        api_key="test",
        model="qwen3-vl-235b-a22b-instruct",
        max_retries=2,
        retry_backoff_base=2.0,
        max_concurrency=1,
    )
    attempts = {"count": 0}
    sleeps: list[float] = []

    async def fake_post(path, json):
        attempts["count"] += 1
        request = client.client.build_request("POST", path, json=json)
        if attempts["count"] < 3:
            raise httpx.ConnectError("connect failed", request=request)
        return _ok_response(request)

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(client.client, "post", fake_post)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
    asyncio.run(client.close())

    assert result["choices"][0]["message"]["content"] == "{}"
    assert attempts["count"] == 3
    assert sleeps == [1.0, 2.0]


def test_llm_client_limits_concurrency(monkeypatch):
    client = LlmClient(
        base_url="https://tools.ailab.gc.cuny.edu/v1",
        api_key="test",
        model="qwen3-vl-235b-a22b-instruct",
        max_retries=0,
        max_concurrency=2,
    )
    state = {"active": 0, "max_seen": 0}

    async def fake_post(path, json):
        state["active"] += 1
        state["max_seen"] = max(state["max_seen"], state["active"])
        try:
            await asyncio.sleep(0.01)
            request = client.client.build_request("POST", path, json=json)
            return _ok_response(request)
        finally:
            state["active"] -= 1

    monkeypatch.setattr(client.client, "post", fake_post)

    async def _run():
        await asyncio.gather(
            *[
                client.chat_completion([{"role": "user", "content": f"hi-{idx}"}])
                for idx in range(5)
            ]
        )
        await client.close()

    asyncio.run(_run())

    assert state["max_seen"] == 2


def test_llm_client_tracks_usage_cost(monkeypatch):
    client = LlmClient(
        base_url="https://tools.ailab.gc.cuny.edu/v1",
        api_key="test",
        model="qwen3-vl-235b-a22b-instruct",
        max_retries=0,
        max_concurrency=1,
    )

    async def fake_post(path, json):
        request = client.client.build_request("POST", path, json=json)
        return _ok_response(
            request,
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 45,
                    "total_tokens": 168,
                    "cost": 0.01234,
                },
            },
        )

    monkeypatch.setattr(client.client, "post", fake_post)

    async def _run():
        with track_llm_usage() as usage:
            await client.chat_completion([{"role": "user", "content": "hi"}])
            snapshot = usage
        await client.close()
        return snapshot

    usage = asyncio.run(_run())

    assert usage.request_count == 1
    assert usage.prompt_tokens == 123
    assert usage.completion_tokens == 45
    assert usage.total_tokens == 168
    assert usage.cost_usd == 0.01234


def test_a_read_timeout_is_not_repeated(monkeypatch):
    client = LlmClient(
        base_url="https://tools.example.test/v1",
        api_key="test",
        model="vision-model",
        max_retries=3,
        max_concurrency=1,
    )
    attempts = {"count": 0}

    async def fake_post(path, json):
        attempts["count"] += 1
        request = client.client.build_request("POST", path, json=json)
        raise httpx.ReadTimeout("no answer in time", request=request)

    monkeypatch.setattr(client.client, "post", fake_post)

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
    asyncio.run(client.close())

    # The model may have run and been charged; the request is not sent again.
    assert attempts["count"] == 1


def _refusing_client(monkeypatch, response: httpx.Response, *, max_retries: int = 2):
    client = LlmClient(
        base_url="https://gateway.test/v1",
        api_key="test",
        model="vision-model",
        max_retries=max_retries,
        max_concurrency=1,
    )
    posts: list[str] = []

    async def fake_post(path, json):
        posts.append(path)
        response.request = client.client.build_request("POST", path, json=json)
        return response

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(client.client, "post", fake_post)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return client, posts


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (401, {}, "invalid_credential"),
        (403, {}, "insufficient_scope"),
        (429, {"x-should-retry": "false"}, "quota_exceeded"),
    ],
)
def test_refused_credential_ends_the_jobs_model_calls(monkeypatch, status, headers, code):
    response = httpx.Response(
        status,
        headers=headers,
        json={"error": {"code": code, "message": "refused"}},
    )
    client, posts = _refusing_client(monkeypatch, response)

    async def run():
        with job_model_access() as access:
            with pytest.raises(ModelAccessRefused) as first:
                await client.chat_completion([{"role": "user", "content": "hi"}])
            with pytest.raises(ModelAccessRefused):
                await client.chat_completion([{"role": "user", "content": "again"}])
            return first.value, access.refusal

    refusal, recorded = asyncio.run(run())
    asyncio.run(client.close())

    assert posts == ["/chat/completions"]
    assert recorded is refusal
    assert refusal.status_code == status
    assert refusal.code == code
    assert "refused" not in str(refusal)


def test_retryable_rate_limit_is_not_a_refusal(monkeypatch):
    response = httpx.Response(
        429,
        headers={"x-should-retry": "true"},
        json={"error": {"code": "upstream_rate_limited", "message": "busy"}},
    )
    client, posts = _refusing_client(monkeypatch, response, max_retries=1)

    async def run():
        with job_model_access() as access:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat_completion([{"role": "user", "content": "hi"}])
            return access.refusal

    assert asyncio.run(run()) is None
    asyncio.run(client.close())
    assert len(posts) == 2


def test_job_endpoint_replaces_the_settings_key():
    settings = SimpleNamespace(
        llm_base_url="https://gateway.test/v1",
        llm_api_key="sk-app",
        llm_model="vision-model",
        llm_timeout=30,
        llm_max_retries=0,
        llm_retry_backoff_base=2.0,
        llm_retry_max_backoff_seconds=60.0,
        llm_max_concurrency=1,
    )
    relay = ModelEndpoint(
        base_url="https://worker.test/pdf-accessibility/runner/tasks/job-1/v1",
        headers={"x-pdf-runner-token": "host", "x-pdf-job-token": "lease"},
    )

    app_client = make_llm_client(settings)
    with job_model_access(relay):
        job_client = make_llm_client(settings)

    assert str(app_client.client.base_url) == "https://gateway.test/v1/"
    assert app_client.client.headers["authorization"] == "Bearer sk-app"
    assert str(job_client.client.base_url) == relay.base_url + "/"
    assert "authorization" not in job_client.client.headers
    assert job_client.client.headers["x-pdf-job-token"] == "lease"
    asyncio.run(app_client.close())
    asyncio.run(job_client.close())
