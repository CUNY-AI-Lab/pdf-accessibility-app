import asyncio

from app.services.local_semantic import request_local_semantic_content_json_with_response


class _FakeLocalClient:
    def __init__(self, response):
        self.response = response
        self.closed = False

    async def chat_completion(self, messages, **kwargs):
        return self.response

    async def close(self):
        self.closed = True


def test_request_local_semantic_uses_reasoning_content_when_content_empty(monkeypatch):
    fake_client = _FakeLocalClient(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": '{"summary":"ok","task_type":"demo"}',
                    }
                }
            ]
        }
    )

    monkeypatch.setattr(
        "app.services.local_semantic.make_local_semantic_client",
        lambda settings=None: fake_client,
    )

    parsed, response = asyncio.run(
        request_local_semantic_content_json_with_response(
            content=[{"type": "text", "text": "hello"}],
            response_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "task_type": {"type": "string"},
                },
                "required": ["summary", "task_type"],
            },
        )
    )

    assert parsed == {"summary": "ok", "task_type": "demo"}
    assert response["choices"][0]["message"]["reasoning_content"]
    assert fake_client.closed is True


def test_request_local_semantic_prefers_content_when_present(monkeypatch):
    fake_client = _FakeLocalClient(
        {
            "choices": [
                {
                    "message": {
                        "content": '{"summary":"from-content"}',
                        "reasoning_content": '{"summary":"from-reasoning"}',
                    }
                }
            ]
        }
    )

    monkeypatch.setattr(
        "app.services.local_semantic.make_local_semantic_client",
        lambda settings=None: fake_client,
    )

    parsed, _response = asyncio.run(
        request_local_semantic_content_json_with_response(
            content=[{"type": "text", "text": "hello"}],
            response_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        )
    )

    assert parsed == {"summary": "from-content"}
