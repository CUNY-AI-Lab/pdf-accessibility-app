"""The bench-only docling-serve cache and model-call cassette."""

import gzip
import json
from types import SimpleNamespace

import pytest

from app.pipeline import structure
from app.services import llm_client
from scripts import bench_caches


def test_docling_cache_key_follows_bytes_options_and_version():
    versions = {"docling": "2.130.0", "docling-serve": "1.35.0"}
    key = bench_caches.docling_cache_key(b"pdf", {"ocr_engine": "rapidocr"}, versions)
    assert key == bench_caches.docling_cache_key(b"pdf", {"ocr_engine": "rapidocr"}, versions)
    assert key != bench_caches.docling_cache_key(b"pdf2", {"ocr_engine": "rapidocr"}, versions)
    assert key != bench_caches.docling_cache_key(b"pdf", {"ocr_engine": "easyocr"}, versions)
    newer = {**versions, "docling": "2.131.0"}
    assert key != bench_caches.docling_cache_key(b"pdf", {"ocr_engine": "rapidocr"}, newer)


async def test_docling_cache_answers_a_repeated_conversion_from_disk(tmp_path, monkeypatch):
    calls = []

    async def convert(
        pdf_path, job_dir, base_url, timeout, ocr_engine, token, figures, table_cell_matching=True
    ):
        calls.append(pdf_path)
        return {"name": "converted"}, []

    def version(url, **_kwargs):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"docling": "2.130.0"})

    monkeypatch.setattr(structure, "_convert_via_docling_serve", convert)
    monkeypatch.setattr(bench_caches.httpx, "get", version)
    bench_caches.install_docling_cache(tmp_path / "cache")
    pdf = tmp_path / "page.pdf"
    pdf.write_bytes(b"%PDF-1.7 page")

    ledger = bench_caches.new_page()
    arguments = (pdf, tmp_path, "http://docling", 60, "rapidocr", "", False)
    assert await structure._convert_via_docling_serve(*arguments) == ({"name": "converted"}, [])
    assert await structure._convert_via_docling_serve(*arguments) == ({"name": "converted"}, [])
    assert len(calls) == 1
    assert [entry["cache"] for entry in ledger.docling] == ["miss", "hit"]
    (stored,) = (tmp_path / "cache").glob("*.json.gz")
    assert json.loads(gzip.decompress(stored.read_bytes())) == {"name": "converted"}


def chat_client() -> llm_client.LlmClient:
    return llm_client.LlmClient(base_url="http://127.0.0.1:9/v1", api_key="", model="m")


async def test_cassette_records_then_replays_and_counts_usage(tmp_path, monkeypatch):
    calls = []
    payload = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.5}}

    async def live(self, messages, **kwargs):
        calls.append(messages)
        return payload

    monkeypatch.setattr(llm_client.LlmClient, "_do_chat_completion", live)
    client = chat_client()
    messages = [{"role": "user", "content": "describe"}]

    bench_caches.install_llm_cassette(tmp_path, "record")
    ledger = bench_caches.new_page()
    assert await client._do_chat_completion(messages, temperature=0) == payload
    assert ledger.cassette["recorded"] == 1

    monkeypatch.setattr(llm_client.LlmClient, "_do_chat_completion", live)
    bench_caches.install_llm_cassette(tmp_path, "strict")
    ledger = bench_caches.new_page()
    with llm_client.track_llm_usage() as job_usage:
        assert await client._do_chat_completion(messages, temperature=0) == payload
    assert len(calls) == 1
    assert ledger.cassette["replayed"] == 1
    assert (ledger.usage.prompt_tokens, ledger.usage.completion_tokens) == (10, 5)
    assert job_usage.cost_usd == 0.5
    await client.close()


async def test_strict_cassette_fails_an_unrecorded_request_without_calling(tmp_path, monkeypatch):
    async def live(self, messages, **kwargs):
        raise AssertionError("no model call in strict mode")

    monkeypatch.setattr(llm_client.LlmClient, "_do_chat_completion", live)
    bench_caches.install_llm_cassette(tmp_path, "strict")
    ledger = bench_caches.new_page()
    client = chat_client()
    with pytest.raises(bench_caches.CassetteMiss):
        await client._do_chat_completion([{"role": "user", "content": "new"}])
    assert ledger.cassette["missing"] == 1
    await client.close()
