import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


class LlmClient:
    """Thin wrapper around any OpenAI-compatible chat completions API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
    ):
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
        self._retryer = retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=30, exp_base=retry_backoff_base),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                f"LLM request failed ({rs.outcome.exception()}), retrying attempt {rs.attempt_number}..."
            ),
        )

    async def chat_completion(self, messages: list[dict], **kwargs) -> dict:
        return await self._retryer(self._do_chat_completion)(messages, **kwargs)

    async def _do_chat_completion(self, messages: list[dict], **kwargs) -> dict:
        response = await self.client.post(
            "/chat/completions",
            json={"model": self.model, "messages": messages, **kwargs},
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.client.aclose()
