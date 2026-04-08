from google.genai import types

from app.services.gemini_direct import _build_prompt_text, _gemini_json_config


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
        system_instruction="Stay grounded.",
        response_schema=schema,
        cached_content="cachedContents/demo",
    )

    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema
    assert config.cached_content == "cachedContents/demo"
    assert config.temperature == 0
