"""Helpers for bounded LLM operations used by pipeline stages."""

import asyncio
from collections.abc import Awaitable
from typing import Any

from app.config import Settings
from app.services.gemini_direct import direct_gemini_pdf_enabled, direct_gemini_timeout_override
from app.services.llm_client import LlmClient, make_llm_client_with_overrides


def _llm_operation_timeout_seconds_from_values(
    *,
    per_attempt: float,
    retries: int,
    retry_backoff_cap: float,
) -> float:
    retry_slack = min(retry_backoff_cap * retries, max(30.0, per_attempt * 0.5))
    completion_slack = min(15.0, max(1.0, per_attempt * 0.25))
    return per_attempt + retry_slack + completion_slack


def llm_operation_timeout_seconds(settings: Settings) -> float:
    """Bound one logical LLM operation, including modest retry/backoff slack."""
    return _llm_operation_timeout_seconds_from_values(
        per_attempt=max(1.0, float(getattr(settings, "llm_timeout", 120) or 120)),
        retries=max(0, int(getattr(settings, "llm_max_retries", 0) or 0)),
        retry_backoff_cap=max(
            0.0,
            float(getattr(settings, "llm_retry_max_backoff_seconds", 0.0) or 0.0),
        ),
    )


def pretag_llm_operation_timeout_seconds(settings: Settings) -> float:
    """Bound non-critical pretag intelligence so flaky endpoints fail open quickly."""
    return _llm_operation_timeout_seconds_from_values(
        per_attempt=max(
            1.0,
            float(getattr(settings, "llm_pretag_timeout", getattr(settings, "llm_timeout", 120)) or 120),
        ),
        retries=max(
            0,
            int(getattr(settings, "llm_pretag_max_retries", getattr(settings, "llm_max_retries", 0)) or 0),
        ),
        retry_backoff_cap=max(
            0.0,
            float(getattr(settings, "llm_retry_max_backoff_seconds", 0.0) or 0.0),
        ),
    )


def pretag_llm_fallback_operation_timeout_seconds(settings: Settings) -> float:
    """Use a shorter bound for per-item fallbacks after page-batch evidence was tried."""
    primary_per_attempt = max(
        1.0,
        float(getattr(settings, "llm_pretag_timeout", getattr(settings, "llm_timeout", 120)) or 120),
    )
    fallback_per_attempt = max(
        1.0,
        float(getattr(settings, "llm_pretag_fallback_timeout", primary_per_attempt) or primary_per_attempt),
    )
    return _llm_operation_timeout_seconds_from_values(
        per_attempt=min(primary_per_attempt, fallback_per_attempt),
        retries=0,
        retry_backoff_cap=max(
            0.0,
            float(getattr(settings, "llm_retry_max_backoff_seconds", 0.0) or 0.0),
        ),
    )


def make_pretag_llm_client(settings: Settings) -> LlmClient:
    return make_llm_client_with_overrides(
        settings,
        timeout=int(getattr(settings, "llm_pretag_timeout", settings.llm_timeout) or settings.llm_timeout),
        max_retries=int(
            getattr(settings, "llm_pretag_max_retries", settings.llm_max_retries) or settings.llm_max_retries
        ),
    )


async def await_llm_operation[T](
    *,
    settings: Settings,
    label: str,
    awaitable: Awaitable[T],
    timeout_seconds: float | None = None,
) -> T:
    timeout = timeout_seconds if timeout_seconds is not None else llm_operation_timeout_seconds(settings)
    timeout_guard = direct_gemini_timeout_override(None)
    if direct_gemini_pdf_enabled(settings):
        # Direct Gemini requests run through the sync SDK in a worker thread.
        # Bound them with the SDK timeout instead of clipping them with pretag's
        # shorter wrapper timeout; otherwise a page-batch timeout can cascade
        # into many lower-quality individual fallback calls.
        configured_direct_timeout = max(
            1.0,
            float(getattr(settings, "gemini_direct_timeout", 45) or 45),
        )
        timeout = max(
            timeout,
            configured_direct_timeout + min(5.0, max(1.0, configured_direct_timeout * 0.1)),
        )
        timeout_guard = direct_gemini_timeout_override(configured_direct_timeout)
    try:
        with timeout_guard:
            operation = asyncio.ensure_future(awaitable)
            return await asyncio.wait_for(operation, timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError(f"{label} timed out after {timeout:.1f}s") from exc


def empty_llm_usage() -> dict[str, float | int]:
    return {
        "request_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }


def llm_usage_snapshot(recorder: Any) -> dict[str, float | int]:
    if recorder is None:
        return empty_llm_usage()
    return {
        "request_count": int(getattr(recorder, "request_count", 0) or 0),
        "prompt_tokens": int(getattr(recorder, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(recorder, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(recorder, "total_tokens", 0) or 0),
        "cost_usd": float(getattr(recorder, "cost_usd", 0.0) or 0.0),
    }
