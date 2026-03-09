import asyncio

import httpx

from app.services.llm_client import LlmClient, track_llm_usage


def _ok_response(request: httpx.Request, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json=payload or {"choices": [{"message": {"content": "{}"}}]}, request=request)


def test_llm_client_honors_retry_after_header(monkeypatch):
    client = LlmClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="google/gemini-3-flash-preview",
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
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="google/gemini-3-flash-preview",
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
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="google/gemini-3-flash-preview",
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


def test_llm_client_tracks_openrouter_usage_cost(monkeypatch):
    client = LlmClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="google/gemini-3-flash-preview",
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
