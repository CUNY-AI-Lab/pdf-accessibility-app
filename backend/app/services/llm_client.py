import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_MAX_BACKOFF_SECONDS = 60.0
_ACTIVE_USAGE_RECORDER: ContextVar["LlmUsageRecorder | None"] = ContextVar(
    "active_llm_usage_recorder",
    default=None,
)


@dataclass
class LlmUsageRecorder:
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def record(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.request_count += 1
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self.total_tokens += max(0, int(total_tokens))
        self.cost_usd += max(0.0, float(cost_usd))


@contextmanager
def track_llm_usage():
    recorder = LlmUsageRecorder()
    token = _ACTIVE_USAGE_RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _ACTIVE_USAGE_RECORDER.reset(token)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


def is_retryable_llm_exception(exc: BaseException) -> bool:
    """Return True when an LLM failure is transient and retry-oriented."""
    return _is_retryable(exc)


def _retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    retry_after = (exc.response.headers.get("Retry-After") or "").strip()
    if not retry_after:
        return None
    try:
        seconds = float(retry_after)
    except ValueError:
        try:
            retry_after_dt = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, IndexError):
            return None
        if retry_after_dt.tzinfo is None:
            retry_after_dt = retry_after_dt.replace(tzinfo=UTC)
        seconds = (retry_after_dt - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds)


def _retry_delay_seconds(
    exc: BaseException,
    *,
    attempt_number: int,
    retry_backoff_base: float,
    max_backoff_seconds: float,
) -> float:
    retry_after_seconds = _retry_after_seconds(exc)
    if retry_after_seconds is not None:
        return min(retry_after_seconds, max_backoff_seconds)
    delay = retry_backoff_base ** max(0, attempt_number - 1)
    return min(delay, max_backoff_seconds)


def _usage_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _record_response_usage(response_json: dict) -> None:
    recorder = _ACTIVE_USAGE_RECORDER.get()
    if recorder is None or not isinstance(response_json, dict):
        return
    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return
    prompt_tokens = _usage_int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0))
    )
    completion_tokens = _usage_int(
        usage.get("completion_tokens", usage.get("output_tokens", 0))
    )
    total_tokens = _usage_int(usage.get("total_tokens", prompt_tokens + completion_tokens))
    cost_usd = _usage_float(usage.get("cost", usage.get("total_cost", 0.0)))
    recorder.record(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


def record_external_llm_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    recorder = _ACTIVE_USAGE_RECORDER.get()
    if recorder is None:
        return
    recorder.record(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


class LlmClient:
    """Thin wrapper around a chat-completions API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        max_concurrency: int = 2,
    ):
        headers = {}
        token = api_key.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base = max(1.0, float(retry_backoff_base))
        self.max_backoff_seconds = max(1.0, float(max_backoff_seconds))
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=self.max_concurrency,
                max_keepalive_connections=self.max_concurrency,
            ),
        )
        self.model = model

    async def chat_completion(self, messages: list[dict], **kwargs) -> dict:
        attempt_count = self.max_retries + 1
        last_exc: BaseException | None = None
        for attempt_number in range(1, attempt_count + 1):
            try:
                return await self._do_chat_completion(messages, **kwargs)
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt_number >= attempt_count:
                    raise
                delay = _retry_delay_seconds(
                    exc,
                    attempt_number=attempt_number,
                    retry_backoff_base=self.retry_backoff_base,
                    max_backoff_seconds=self.max_backoff_seconds,
                )
                logger.warning(
                    "LLM request failed (%s), retrying in %.1fs [attempt %s/%s]",
                    exc,
                    delay,
                    attempt_number,
                    attempt_count,
                )
                await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("LLM request failed without returning a response")

    async def _do_chat_completion(self, messages: list[dict], **kwargs) -> dict:
        async with self._semaphore:
            response = await self.client.post(
                "/chat/completions",
                json={"model": self.model, "messages": messages, **kwargs},
            )
            response.raise_for_status()
            payload = response.json()
            _record_response_usage(payload)
            return payload

    async def close(self):
        await self.client.aclose()


def make_llm_client(settings: Any) -> LlmClient:
    return make_llm_client_with_overrides(settings)


def make_llm_client_with_overrides(
    settings: Any,
    *,
    timeout: int | None = None,
    max_retries: int | None = None,
    retry_backoff_base: float | None = None,
    max_backoff_seconds: float | None = None,
    max_concurrency: int | None = None,
) -> LlmClient:
    api_key = (getattr(settings, "llm_api_key", "") or "").strip() or (
        getattr(settings, "gemini_api_key", "") or ""
    ).strip()
    return LlmClient(
        base_url=settings.llm_base_url,
        api_key=api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout if timeout is None else timeout,
        max_retries=settings.llm_max_retries if max_retries is None else max_retries,
        retry_backoff_base=(
            settings.llm_retry_backoff_base if retry_backoff_base is None else retry_backoff_base
        ),
        max_backoff_seconds=(
            settings.llm_retry_max_backoff_seconds
            if max_backoff_seconds is None
            else max_backoff_seconds
        ),
        max_concurrency=settings.llm_max_concurrency if max_concurrency is None else max_concurrency,
    )
