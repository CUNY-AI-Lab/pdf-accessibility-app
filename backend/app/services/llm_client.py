import httpx


class LlmClient:
    """Thin wrapper around any OpenAI-compatible chat completions API."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120):
        headers = {}
        token = api_key.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )
        self.model = model

    async def chat_completion(self, messages: list[dict], **kwargs) -> dict:
        response = await self.client.post(
            "/chat/completions",
            json={"model": self.model, "messages": messages, **kwargs},
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.client.aclose()
