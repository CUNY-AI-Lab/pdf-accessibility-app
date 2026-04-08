import pytest

from app.services.gemini_direct_experiment import (
    GeminiExperimentConfigError,
    find_gemini_api_key,
    parse_page_spec,
)


def test_parse_page_spec_accepts_ranges_and_dedupes():
    assert parse_page_spec("1-3,2,5") == [1, 2, 3, 5]


def test_parse_page_spec_rejects_invalid_ranges():
    with pytest.raises(ValueError):
        parse_page_spec("3-1")


def test_find_gemini_api_key_prefers_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=file-key\n")
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key")

    assert find_gemini_api_key(env_file=env_file) == "env-key"


def test_find_gemini_api_key_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_API_KEY=file-key\n")

    assert find_gemini_api_key(env_file=env_file) == "file-key"


def test_find_gemini_api_key_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=other\n")

    with pytest.raises(GeminiExperimentConfigError):
        find_gemini_api_key(env_file=env_file)
