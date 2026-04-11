from google.genai import types

from app.config import Settings
from app.services.gemini_direct import (
    _build_prompt_text,
    _gemini_json_config,
    direct_gemini_thinking_override,
    direct_gemini_timeout_override,
)


def _settings(**overrides):
    return Settings(llm_api_key="test-key", gemini_api_key="test-key", **overrides)


def test_build_prompt_text_omits_schema_dump_when_native_schema_is_used():
    prompt = _build_prompt_text(
        prompt="Return a bookmark plan.",
        context_payload={"job_filename": "demo.pdf"},
        response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )

    assert "Context JSON:" in prompt
    assert "JSON schema to satisfy:" not in prompt
    assert "Return exactly one JSON object" not in prompt


def test_gemini_json_config_uses_response_json_schema():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    config = _gemini_json_config(
        types,
        settings=_settings(),
        system_instruction="Stay grounded.",
        response_schema=schema,
        cached_content="cachedContents/demo",
    )

    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema
    assert config.cached_content == "cachedContents/demo"
    assert config.temperature == 0
    assert config.max_output_tokens == 8192
    assert config.http_options.timeout == 120000
    assert config.thinking_config.include_thoughts is False
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config.thinking_config.thinking_budget is None


def test_gemini_json_config_uses_thinking_level_override():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    with direct_gemini_thinking_override(level="medium"):
        config = _gemini_json_config(
            types,
            settings=_settings(),
            system_instruction="Stay grounded.",
            response_schema=schema,
        )

    assert config.thinking_config.include_thoughts is False
    assert config.thinking_config.thinking_level == types.ThinkingLevel.MEDIUM
    assert config.thinking_config.thinking_budget is None


def test_gemini_json_config_uses_2_5_thinking_budget():
    config = _gemini_json_config(
        types,
        settings=_settings(gemini_model="gemini-2.5-flash", gemini_direct_thinking_budget=0),
        system_instruction=None,
        response_schema=None,
    )

    assert config.thinking_config.include_thoughts is False
    assert config.thinking_config.thinking_budget == 0
    assert config.thinking_config.thinking_level is None


def test_gemini_json_config_uses_2_5_thinking_budget_override():
    with direct_gemini_thinking_override(budget=256):
        config = _gemini_json_config(
            types,
            settings=_settings(gemini_model="gemini-2.5-flash", gemini_direct_thinking_budget=0),
            system_instruction=None,
            response_schema=None,
        )

    assert config.thinking_config.include_thoughts is False
    assert config.thinking_config.thinking_budget == 256
    assert config.thinking_config.thinking_level is None


def test_gemini_json_config_uses_configured_bounds():
    config = _gemini_json_config(
        types,
        settings=_settings(gemini_direct_timeout=12, gemini_direct_max_output_tokens=2048),
        system_instruction=None,
        response_schema=None,
    )

    assert config.http_options.timeout == 12000
    assert config.max_output_tokens == 2048


def test_gemini_json_config_uses_timeout_override():
    with direct_gemini_timeout_override(7.25):
        config = _gemini_json_config(
            types,
            settings=_settings(gemini_direct_timeout=45),
            system_instruction=None,
            response_schema=None,
        )

    assert config.http_options.timeout == 7250
