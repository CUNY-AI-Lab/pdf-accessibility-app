import asyncio

import pytest

from app.config import Settings
from app.pipeline.llm_operations import (
    await_llm_operation,
    empty_llm_usage,
    llm_usage_snapshot,
    make_pretag_llm_client,
    pretag_llm_fallback_operation_timeout_seconds,
    pretag_llm_operation_timeout_seconds,
)


def _settings(**overrides) -> Settings:
    values = {
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "test-model",
    }
    values.update(overrides)
    return Settings(**values)


def test_pretag_timeout_includes_retry_and_completion_slack():
    settings = _settings(
        llm_timeout=120,
        llm_pretag_timeout=20,
        llm_pretag_max_retries=2,
        llm_retry_max_backoff_seconds=5,
    )

    assert pretag_llm_operation_timeout_seconds(settings) == 35


def test_pretag_fallback_timeout_uses_shorter_single_attempt_bound():
    settings = _settings(
        llm_timeout=120,
        llm_pretag_timeout=30,
        llm_pretag_fallback_timeout=8,
        llm_retry_max_backoff_seconds=5,
    )

    assert pretag_llm_fallback_operation_timeout_seconds(settings) == 10


def test_default_fallback_bound_lets_the_longest_table_answer_finish():
    """Through the Gateway a call takes about 1 s, plus 15 ms per output token
    and 0.07 s per 1,000 prompt tokens, with 3 s to spare at the 99th
    percentile. The longest table answer seen was 807 tokens, and prompts
    reach 28,500 tokens. The per-table bound leaves that answer room and
    still cuts a runaway answer before the page-batch bound would."""
    settings = _settings()
    longest_answer = 1.0 + 0.0153 * 807 + 0.073 * 28.5 + 3.0
    fallback = pretag_llm_fallback_operation_timeout_seconds(settings)
    assert 1.5 * longest_answer <= fallback < pretag_llm_operation_timeout_seconds(settings)


def test_make_pretag_llm_client_applies_pretag_overrides():
    client = make_pretag_llm_client(
        _settings(
            llm_timeout=120,
            llm_max_retries=4,
            llm_pretag_timeout=17,
            llm_pretag_max_retries=1,
        )
    )

    try:
        assert client.client.timeout.connect == 17
        assert client.max_retries == 1
    finally:
        asyncio.run(client.close())


@pytest.mark.asyncio
async def test_await_llm_operation_returns_result():
    async def _operation():
        return "ok"

    assert await await_llm_operation(
        settings=_settings(),
        label="unit-test",
        awaitable=_operation(),
        timeout_seconds=1,
    ) == "ok"


@pytest.mark.asyncio
async def test_await_llm_operation_reports_timeout_label():
    async def _operation():
        await asyncio.sleep(0.05)

    with pytest.raises(RuntimeError, match="unit-test timed out after 0.0s"):
        await await_llm_operation(
            settings=_settings(),
            label="unit-test",
            awaitable=_operation(),
            timeout_seconds=0.001,
        )


def test_llm_usage_helpers_normalize_missing_recorder():
    assert llm_usage_snapshot(None) == empty_llm_usage()
