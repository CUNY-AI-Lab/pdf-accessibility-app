"""Transition contracts and behavior at the live runner's I/O boundaries."""

import asyncio
import itertools
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import database
from app.pipeline import docling_io, ocr, ocr_vlm_plugin, table_model
from app.pipeline.docling_io import DoclingConversion, DoclingPhase, DoclingSubmissionUnknown
from app.services import intelligence_llm_utils as structured
from app.services import llm_client
from app.services.alt_text_cache import (
    AltTextCache,
    CacheEntryState,
    cache_entry_state,
    guard_cache_transition,
)
from app.services.llm_client import (
    LlmClient,
    ModelAccess,
    ModelAccessRefused,
    ModelAccessState,
    ModelEndpoint,
    RequestAttempt,
    RequestState,
    job_model_access,
)
from app.services.ocr_access import OcrJobAccess


def _assert_transitions(states, allowed, make, transition):
    for current, target in itertools.product(states, repeat=2):
        instance = make(current)
        if (current.value, target.value) in allowed:
            transition(instance, target)
        else:
            with pytest.raises(ValueError):
                transition(instance, target)


def test_request_transition_contract_and_attempt_bound():
    allowed = {
        ("waiting", "requesting"), ("waiting", "failed"), ("waiting", "cancelled"),
        ("requesting", "backoff"), ("requesting", "succeeded"),
        ("requesting", "failed"), ("requesting", "cancelled"),
        ("backoff", "requesting"), ("backoff", "failed"), ("backoff", "cancelled"),
    }
    _assert_transitions(RequestState, allowed, lambda s: RequestAttempt(2, state=s),
                        lambda a, t: a.transition(t))
    attempt = RequestAttempt(1)
    attempt.transition(RequestState.REQUESTING)
    attempt.transition(RequestState.BACKOFF)
    with pytest.raises(ValueError):
        attempt.transition(RequestState.REQUESTING)
    assert attempt.attempts == 1


def test_access_is_monotone_and_keeps_first_refusal():
    access = ModelAccess()
    assert access.state is ModelAccessState.AVAILABLE
    first = ModelAccessRefused(401, "invalid_credential", "")
    assert access.refuse(first) is first
    assert access.state is ModelAccessState.REFUSED
    assert access.refuse(ModelAccessRefused(403, "insufficient_scope", "")) is first
    with pytest.raises(ValueError):
        access.refusal = None
    assert access.refusal is first


@pytest.mark.asyncio
async def test_refusal_stops_a_caller_waiting_for_the_semaphore(monkeypatch):
    client = LlmClient("https://relay.test/v1", "", "model", max_concurrency=1)
    entered, release = asyncio.Event(), asyncio.Event()
    posts = []

    async def prepare(messages, **kwargs):
        return {"model": "model", "messages": messages, **kwargs}

    async def post(path, **kwargs):
        posts.append(path)
        entered.set()
        await release.wait()
        return httpx.Response(403, request=httpx.Request("POST", "https://relay.test"))

    monkeypatch.setattr(client, "prepare_request", prepare)
    monkeypatch.setattr(client.client, "post", post)
    try:
        with job_model_access():
            first = asyncio.create_task(client.chat_completion([]))
            await entered.wait()
            queued = asyncio.create_task(client.chat_completion([]))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, queued, return_exceptions=True)
            assert all(isinstance(result, ModelAccessRefused) for result in results)
            assert len(posts) == 1
    finally:
        await client.close()


def test_nested_access_scopes_restore_the_outer_refusal():
    with job_model_access(ModelEndpoint("https://outer")) as outer:
        outer.refuse(ModelAccessRefused(401, "invalid", ""))
        with job_model_access() as nested:
            assert nested is outer
        with job_model_access(ModelEndpoint("https://inner")) as inner:
            assert inner is not outer
            llm_client.raise_if_model_access_refused()
        with pytest.raises(ModelAccessRefused):
            llm_client.raise_if_model_access_refused()
    with job_model_access():
        llm_client.raise_if_model_access_refused()


@pytest.mark.asyncio
async def test_cancellation_during_llm_backoff_never_dispatches_again(monkeypatch):
    client = LlmClient("https://relay.test", "", "model")
    sleeping = asyncio.Event()
    posts = []

    async def dispatch(*args, **kwargs):
        posts.append(1)
        raise httpx.ConnectError("not sent")

    async def sleep(delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_do_chat_completion", dispatch)
    monkeypatch.setattr(llm_client.asyncio, "sleep", sleep)
    task = asyncio.create_task(client.chat_completion([]))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert posts == [1]
    await client.close()


def test_json_attempt_and_format_transition_contracts():
    allowed = {("initial", "repair"), ("initial", "succeeded"), ("initial", "failed"),
               ("repair", "succeeded"), ("repair", "failed")}
    _assert_transitions(structured.JsonAttempt, allowed, lambda s: s,
                        structured.transition_json_attempt)
    for mode, expected in [(structured.ResponseFormatMode.SCHEMA, structured.ResponseFormatMode.OBJECT),
                           (structured.ResponseFormatMode.OBJECT, structured.ResponseFormatMode.TEXT)]:
        assert structured.downgrade_response_format(mode) is expected
    with pytest.raises(ValueError):
        structured.downgrade_response_format(structured.ResponseFormatMode.TEXT)


def _format_error(status=400, mode="json_schema", **error):
    response = httpx.Response(status, request=httpx.Request("POST", "https://relay.test"),
        json={"error": {"code": "capability_unsupported", "message": f"{mode} unsupported", **error}})
    return httpx.HTTPStatusError("rejected", request=response.request, response=response)


@pytest.mark.parametrize("exc", [
    _format_error(401), _format_error(403), _format_error(429), _format_error(500),
    _format_error(message="vision unsupported"),
    _format_error(message="No single route supports image input and response_format json_schema together"),
    _format_error(message="model does not support image input and response_format json_schema"),
    _format_error(code="invalid_json_schema", message="json_schema has invalid properties"),
    httpx.ReadTimeout("uncertain"), httpx.RemoteProtocolError("lost"), ValueError("json_schema unsupported"),
    ModelAccessRefused(401, "invalid_credential", ""),
])
@pytest.mark.asyncio
async def test_format_failures_without_capability_evidence_do_not_downgrade_or_cache(monkeypatch, exc):
    cache = structured.FormatCapabilityCache()
    monkeypatch.setattr(structured, "FORMAT_CAPABILITIES", cache)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        raise exc

    client = SimpleNamespace(base_url="https://relay.test", model="model", chat_completion=completion)
    with pytest.raises(type(exc)):
        await structured.request_llm_json(llm_client=client, content=[], response_schema={"type": "object"}, schema_name="x")
    assert len(calls) == 1
    assert not cache.entries


@pytest.mark.asyncio
async def test_format_cache_isolates_endpoints_and_capabilities_and_expires(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(structured.time, "monotonic", lambda: now[0])
    cache = structured.FormatCapabilityCache(ttl=5, max_entries=8)
    monkeypatch.setattr(structured, "FORMAT_CAPABILITIES", cache)
    calls = []

    async def completion(**kwargs):
        mode = kwargs.get("response_format", {}).get("type", "text")
        calls.append(mode)
        if mode != "text":
            raise _format_error(mode=mode)
        return {"choices": [{"message": {"content": "{}"}}]}

    client = SimpleNamespace(base_url="https://a.test", model="model", chat_completion=completion)
    async def request():
        return await structured.request_llm_json(llm_client=client, content=[], response_schema={"type": "object"}, schema_name="x")

    await request()
    assert calls == ["json_schema", "json_object", "text"]
    calls.clear()
    await request()
    assert calls == ["text"]
    client.base_url = "https://b.test"
    calls.clear()
    await request()
    assert calls == ["json_schema", "json_object", "text"]
    client.model = "another-model"
    calls.clear()
    await request()
    assert calls == ["json_schema", "json_object", "text"]
    now[0] += 5
    calls.clear()
    await request()
    assert calls == ["json_schema", "json_object", "text"]
    with pytest.raises(ValueError):
        cache.record(("a", "m", structured.ResponseFormatMode.OBJECT), structured.FormatCapability.UNKNOWN)
    cache = structured.FormatCapabilityCache(max_entries=1)
    for endpoint in ("a", "b"):
        cache.record((endpoint, "m", structured.ResponseFormatMode.SCHEMA), structured.FormatCapability.UNSUPPORTED)
    assert len(cache.entries) == 1


@pytest.mark.asyncio
async def test_json_repair_is_exactly_one_additional_answer():
    calls = []
    async def completion(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "malformed"}}]}
    client = SimpleNamespace(model="m", chat_completion=completion)
    with pytest.raises(ValueError):
        await structured.request_llm_json(llm_client=client, content=[])
    assert len(calls) == 2


def test_docling_transition_contract():
    allowed = {
        ("ready", "submitting"), ("submitting", "unknown_submission"), ("submitting", "polling"),
        ("submitting", "failed"), ("submitting", "timed_out"), ("submitting", "cancelled"),
        ("unknown_submission", "resubmitting"), ("unknown_submission", "timed_out"), ("unknown_submission", "cancelled"),
        ("resubmitting", "unknown_submission"), ("resubmitting", "polling"),
        ("resubmitting", "failed"), ("resubmitting", "timed_out"), ("resubmitting", "cancelled"),
        ("polling", "fetching"), ("polling", "failed"), ("polling", "timed_out"), ("polling", "cancelled"),
        ("fetching", "succeeded"), ("fetching", "failed"), ("fetching", "timed_out"), ("fetching", "cancelled"),
    }
    _assert_transitions(DoclingPhase, allowed, lambda s: DoclingConversion(phase=s), lambda a, t: a.transition(t))


async def _no_delay(_):
    pass


@pytest.mark.parametrize("failure", [httpx.ConnectError("connect"), httpx.ConnectTimeout("connect"),
                                      httpx.PoolTimeout("pool"), httpx.ReadTimeout("lost"),
                                      httpx.RemoteProtocolError("lost"), httpx.WriteTimeout("partial upload"),
                                      502, 503, 504, {}])
@pytest.mark.asyncio
async def test_docling_submission_fails_unknown_only_after_three_attempts(monkeypatch, failure):
    calls = []
    async def handler(request):
        calls.append(request)
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure if isinstance(failure, int) else 200, json={})
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DoclingSubmissionUnknown):
            await conversion.run(client, "https://docling.test", 5)
    assert conversion.phase is DoclingPhase.UNKNOWN_SUBMISSION
    assert conversion.submission_attempts == 3
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_docling_retries_safe_connect_and_lost_poll_using_one_task_id(monkeypatch):
    calls = []
    async def handler(request):
        calls.append((request.method, request.url.path))
        if len(calls) == 1:
            raise httpx.ConnectError("not sent")
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "same-task"})
        if len(calls) == 3:
            raise httpx.ReadTimeout("lost poll")
        if "poll" in request.url.path:
            return httpx.Response(200, json={"task_status": "success"})
        return httpx.Response(200, json={"document": {"json_content": {"texts": []}}})
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await conversion.run(client, "https://docling.test", 5)
    assert result["document"]["json_content"] == {"texts": []}
    assert conversion.phase is DoclingPhase.SUCCEEDED
    assert [path for method, path in calls if method == "GET"] == [
        "/v1/status/poll/same-task", "/v1/status/poll/same-task", "/v1/result/same-task"]
    assert len([method for method, _ in calls if method == "POST"]) == 2


@pytest.mark.parametrize("status", [None, "legacy-value", "failure"])
@pytest.mark.asyncio
async def test_docling_unknown_or_failed_status_stops_without_resubmission(monkeypatch, status):
    calls = []
    async def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"task_id": "task"} if request.method == "POST" else {"task_status": status})
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await conversion.run(client, "https://docling.test", 5)
    assert conversion.phase is DoclingPhase.FAILED
    assert calls == ["POST", "GET"]


@pytest.mark.asyncio
async def test_docling_one_deadline_includes_submission_and_polling():
    calls = []
    async def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={"task_id": "task"})
        return httpx.Response(200, json={"task_status": "started"})
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await conversion.run(client, "https://docling.test", 0.04)
    assert time.monotonic() - start < 0.5
    assert conversion.phase is DoclingPhase.TIMED_OUT
    assert calls == ["POST"]


@pytest.mark.asyncio
async def test_docling_deadline_during_submission_times_out_before_budget_exhaustion():
    async def handler(request):
        await asyncio.sleep(1)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await conversion.run(client, "https://docling.test", 0.01)
    assert conversion.phase is DoclingPhase.TIMED_OUT
    assert conversion.submission_attempts == 1


def test_ocr_page_transition_contract_and_bounds():
    allowed = {
        ("waiting", "requesting"), ("waiting", "failed"),
        ("requesting", "backoff"), ("requesting", "answered"), ("requesting", "failed"),
        ("backoff", "requesting"), ("backoff", "failed"),
        ("answered", "regenerating"), ("answered", "succeeded"), ("answered", "failed"),
        ("regenerating", "requesting"), ("regenerating", "failed"),
    }
    _assert_transitions(ocr_vlm_plugin.PageState, allowed,
                       lambda s: ocr_vlm_plugin.PageAttempt(state=s), lambda a, t: a.transition(t))
    for exhausted in [ocr_vlm_plugin.PageAttempt(state=ocr_vlm_plugin.PageState.REQUESTING, retries=2),
                      ocr_vlm_plugin.PageAttempt(state=ocr_vlm_plugin.PageState.ANSWERED, generation=2)]:
        with pytest.raises(ValueError):
            exhausted.transition(ocr_vlm_plugin.PageState.BACKOFF if exhausted.retries else ocr_vlm_plugin.PageState.REGENERATING)


def test_ocr_refusal_receipt_is_shared_across_processes_and_isolated_between_jobs(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    script = "from pathlib import Path; from app.services.ocr_access import OcrJobAccess; from app.services.llm_client import ModelAccessRefused; import sys; OcrJobAccess(Path(sys.argv[1])).refuse(ModelAccessRefused(429, 'quota', ''))"
    subprocess.run([sys.executable, "-c", script, str(first)], check=True)
    with pytest.raises(ModelAccessRefused):
        OcrJobAccess(first).check()
    OcrJobAccess(second).check()
    assert sorted(p.name for p in first.iterdir()) == ["429"]


@pytest.mark.parametrize("status,headers", [(401, {}), (403, {}), (429, {"x-should-retry": "false"})])
def test_ocr_refusal_suppresses_other_pages(monkeypatch, tmp_path, status, headers):
    monkeypatch.setenv("CAIL_OCR_ACCESS_DIR", str(tmp_path))
    posts = []
    def post(*args, **kwargs):
        posts.append(1)
        return httpx.Response(status, headers=headers)
    monkeypatch.setattr(ocr_vlm_plugin.httpx, "post", post)
    for _ in range(2):
        with pytest.raises(ModelAccessRefused):
            ocr_vlm_plugin._complete("https://relay.test", {}, {}, time.monotonic() + 5)
    assert posts == [1]


@pytest.mark.asyncio
async def test_parent_ocr_refusal_latches_job_even_when_subprocess_returns_success(monkeypatch, tmp_path):
    import pikepdf
    source = tmp_path / "source.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(source)
    settings = SimpleNamespace(ocr_gateway_jobs=1, ocrmypdf_max_image_mpixels=75,
        llm_base_url="https://relay.test", llm_api_key="", llm_max_request_bytes=10000,
        ocr_model="model", ocr_gateway_page_seconds=5)
    seen = []
    async def spawn(*args, env, **kwargs):
        access = Path(env["CAIL_OCR_ACCESS_DIR"])
        seen.append(access)
        OcrJobAccess(access).refuse(ModelAccessRefused(403, "scope", ""))
        return SimpleNamespace(returncode=0)
    async def communicate(*args):
        return b"", b""
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    monkeypatch.setattr(ocr.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ocr, "communicate_with_timeout", communicate)
    with job_model_access() as access:
        with pytest.raises(ModelAccessRefused):
            await ocr.run_ocr(source, tmp_path / "out.pdf", engine="gateway")
        assert access.state is ModelAccessState.REFUSED
        with pytest.raises(ModelAccessRefused):
            llm_client.raise_if_model_access_refused()
    assert len(seen) == 1
    assert not seen[0].exists()


def test_table_circuit_transition_contract():
    _assert_transitions(table_model.TableServiceState, {("available", "unavailable_for_document")},
                        lambda s: table_model.TableServiceCircuit(state=s),
                        lambda a, t: a.transition(t, "outage"))
    with pytest.raises(ValueError):
        table_model.TableServiceCircuit().transition(table_model.TableServiceState.UNAVAILABLE_FOR_DOCUMENT)


def test_cache_transition_contract_and_lenient_storage_time():
    allowed = {("absent", "fresh"), ("fresh", "fresh"), ("fresh", "absent"),
               ("expired", "fresh"), ("expired", "absent")}
    _assert_transitions(CacheEntryState, allowed, lambda s: s, guard_cache_transition)
    assert cache_entry_state("legacy", 1) is CacheEntryState.EXPIRED
    assert cache_entry_state(None, 1, present=False) is CacheEntryState.ABSENT


def test_schema_ledger_transition_contract():
    _assert_transitions(database.MigrationState, {("pending", "applied")}, lambda s: s,
                        database.guard_migration_transition)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 400, 422])
@pytest.mark.asyncio
async def test_table_http_status_policy(monkeypatch, status):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(status))
    monkeypatch.setattr(table_model.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    reader = table_model.HttpReader("https://tables.test/v1", "tables")
    if status in {429, 500, 502, 503, 504}:
        with pytest.raises(table_model.TableModelUnavailable):
            await reader.read(b"png")
    else:
        assert await reader.read(b"png") is None


@pytest.mark.asyncio
async def test_table_outage_skips_remaining_tables_and_new_document_resets(monkeypatch, tmp_path):
    prepared = [SimpleNamespace(png=b"png", element_index=i) for i in range(3)]
    monkeypatch.setattr(table_model, "prepare_tables", lambda *args: prepared)
    monkeypatch.setattr(table_model, "rebuild_tables", lambda elements, preps, answers, **kwargs: (elements, []))
    calls = []
    async def read(png):
        calls.append(png)
        raise table_model.TableModelUnavailable("outage")
    reader = SimpleNamespace(model="tables", read=read)
    for expected in (1, 2):
        _, audit = await table_model.rewrite_tables(tmp_path / "doc.pdf", {"elements": []}, reader, min_coverage=0.7)
        assert len(calls) == expected
        assert audit["unavailable"] == "outage"


@pytest.mark.parametrize("exc", [ValueError("bad JSON"), KeyError("choices"), IndexError(), TypeError()])
@pytest.mark.asyncio
async def test_malformed_table_answer_does_not_open_document_circuit(monkeypatch, tmp_path, exc):
    prepared = [SimpleNamespace(png=b"png", element_index=i) for i in range(3)]
    monkeypatch.setattr(table_model, "prepare_tables", lambda *args: prepared)
    monkeypatch.setattr(table_model, "rebuild_tables", lambda elements, preps, answers, **kwargs: (elements, []))
    calls = []
    async def read(png):
        calls.append(png)
        raise exc
    _, audit = await table_model.rewrite_tables(tmp_path / "doc.pdf", {"elements": []},
        SimpleNamespace(model="tables", read=read), min_coverage=0.7)
    assert len(calls) == 3
    assert audit["unavailable"] is None


@pytest.mark.parametrize("fetch", [False, True])
@pytest.mark.asyncio
async def test_docling_fetch_failure_or_lost_poll_exhausts_without_resubmission(monkeypatch, fetch):
    calls = []
    async def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "task"})
        if "poll" in request.url.path:
            if not fetch:
                raise httpx.ReadTimeout("lost")
            return httpx.Response(200, json={"task_status": "success"})
        return httpx.Response(500)
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await conversion.run(client, "https://docling.test", 5)
    assert conversion.phase is DoclingPhase.FAILED
    assert calls == (["POST", "GET", "GET"] if fetch else ["POST", "GET", "GET", "GET"])


@pytest.mark.asyncio
async def test_docling_deadline_includes_fetch(monkeypatch):
    actual_sleep = asyncio.sleep
    async def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "task"})
        if "poll" in request.url.path:
            return httpx.Response(200, json={"task_status": "success"})
        await actual_sleep(1)
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await conversion.run(client, "https://docling.test", 0.02)
    assert conversion.phase is DoclingPhase.TIMED_OUT


@pytest.mark.asyncio
async def test_schema_migration_failure_is_rolled_back_and_replayed(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ledger.db'}")
    # Ensure the data table and ledger exist before the failing transaction.
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE evidence (id INTEGER PRIMARY KEY)"))
        await connection.run_sync(database._create_schema_migrations_table)
    calls = []
    def apply(connection):
        def migrate():
            calls.append(1)
            connection.execute(text("INSERT INTO evidence VALUES (1)"))
            if len(calls) == 1:
                raise RuntimeError("migration interrupted")
        database._run_migrations(connection, [("test-migration", migrate)])
    try:
        with pytest.raises(RuntimeError):
            async with engine.begin() as connection:
                await connection.run_sync(apply)
        async with engine.begin() as connection:
            assert (await connection.execute(text("SELECT COUNT(*) FROM evidence"))).scalar_one() == 0
            assert (await connection.execute(text("SELECT COUNT(*) FROM schema_migrations"))).scalar_one() == 0
            # Unknown future IDs are preserved and don't block a known migration.
            await connection.execute(text("INSERT INTO schema_migrations VALUES ('future-id', 'legacy-time')"))
            await connection.run_sync(apply)
            await connection.run_sync(apply)
            with pytest.raises(ValueError):
                await connection.run_sync(database._record_migration, "test-migration")
            assert set((await connection.execute(text("SELECT id FROM schema_migrations"))).scalars()) == {"future-id", "test-migration"}
        assert len(calls) == 2
    finally:
        await engine.dispose()


def test_malformed_cache_timestamp_is_a_miss_and_can_be_replaced(tmp_path):
    import sqlite3
    cache = AltTextCache(tmp_path / "cache.db")
    cache.put("image", "model", "v", "input", "a description")
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE descriptions SET stored_at = 'legacy-time'")
    assert cache.get("image", "model", "v", "input") is None
    cache.put("image", "model", "v", "input", "new description")
    assert cache.get("image", "model", "v", "input") == "new description"


def test_ocr_deadline_spans_backoff_and_regeneration(monkeypatch, tmp_path):
    from PIL import Image
    now = [0.0]
    monkeypatch.setattr(ocr_vlm_plugin.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ocr_vlm_plugin.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    for key, value in {"CAIL_OCR_ACCESS_DIR": str(tmp_path), "CAIL_OCR_BASE_URL": "https://relay.test",
                       "CAIL_OCR_HEADERS": "{}", "CAIL_OCR_MODEL": "m", "CAIL_OCR_PAGE_SECONDS": "6"}.items():
        monkeypatch.setenv(key, value)
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        now[0] += 1
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "bad"}, "finish_reason": "length"}]})
        return httpx.Response(503)
    monkeypatch.setattr(ocr_vlm_plugin.httpx, "post", post)
    with pytest.raises(ocr_vlm_plugin.GatewayOcrError):
        ocr_vlm_plugin._request_lines(Image.new("RGB", (5, 5)))
    assert len(calls) == 2
    assert [c["timeout"] for c in calls] == [6, 5]
    assert now[0] == 6


@pytest.mark.asyncio
async def test_success_from_inflight_llm_call_cannot_clear_another_callers_refusal(monkeypatch):
    client = LlmClient("https://relay.test", "", "model", max_concurrency=2)
    entered, release = asyncio.Event(), asyncio.Event()
    async def post(path, **kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(200, request=httpx.Request("POST", "https://relay.test"), json={"choices": []})
    monkeypatch.setattr(client.client, "post", post)
    try:
        with job_model_access() as access:
            task = asyncio.create_task(client.chat_completion([]))
            await entered.wait()
            refusal = access.refuse(ModelAccessRefused(403, "scope", ""))
            release.set()
            with pytest.raises(ModelAccessRefused) as result:
                await task
            assert result.value is refusal
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_docling_pending_started_success_are_read_from_the_same_task(monkeypatch):
    statuses = iter(["pending", "started", "success"])
    polls = []
    async def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "task"})
        if "poll" in request.url.path:
            polls.append(request.url.path)
            return httpx.Response(200, json={"task_status": next(statuses)})
        return httpx.Response(200, json={"document": {"json_content": {}}})
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await conversion.run(client, "https://docling.test", 5)
    assert conversion.phase is DoclingPhase.SUCCEEDED
    assert polls == ["/v1/status/poll/task"] * 3


@pytest.mark.asyncio
async def test_concurrent_runner_database_initialization_uses_independent_ledgers(tmp_path):
    from sqlalchemy import text

    from app.config import Settings
    from app.runner.state import open_job_database
    settings = Settings(_env_file=None, llm_strict_validation=False,
        upload_dir=tmp_path / "uploads", processing_dir=tmp_path / "processing", output_dir=tmp_path / "output")
    databases = await asyncio.gather(open_job_database(settings, "first"), open_job_database(settings, "second"))
    try:
        for engine, _ in databases:
            async with engine.begin() as connection:
                ids = set((await connection.execute(text("SELECT id FROM schema_migrations"))).scalars())
                assert len(ids) == 4
    finally:
        await asyncio.gather(*(engine.dispose() for engine, _ in databases))


def test_local_docling_transition_contract():
    allowed = {("ready", "original"), ("original", "repairing"), ("original", "succeeded"),
               ("repairing", "repaired"), ("repairing", "failed"),
               ("repaired", "succeeded"), ("repaired", "failed")}
    _assert_transitions(docling_io.LocalDoclingPhase, allowed,
        lambda s: docling_io.LocalDoclingConversion(phase=s), lambda a, t: a.transition(t))


@pytest.mark.parametrize("original_ok,repair_ok,repaired_ok", [(True, False, False), (False, True, True),
                                                              (False, False, False), (False, True, False)])
def test_local_docling_original_then_at_most_one_repaired_conversion(original_ok, repair_ok, repaired_ok, tmp_path):
    source, repaired = tmp_path / "source.pdf", tmp_path / "repaired.pdf"
    calls = []
    def convert(path):
        calls.append(path)
        if (path == source and original_ok) or (path == repaired and repaired_ok):
            return {"converted": str(path)}
        raise RuntimeError("conversion failed")
    repairs = []
    def repair(first, second):
        repairs.append((first, second))
        return repair_ok
    conversion = docling_io.LocalDoclingConversion()
    if original_ok or (repair_ok and repaired_ok):
        result, path = conversion.run(convert, repair, source, repaired)
        assert path == (source if original_ok else repaired)
        assert result == {"converted": str(path)}
        assert conversion.phase is docling_io.LocalDoclingPhase.SUCCEEDED
    else:
        with pytest.raises(RuntimeError):
            conversion.run(convert, repair, source, repaired)
        assert conversion.phase is docling_io.LocalDoclingPhase.FAILED
    assert calls == ([source, repaired] if not original_ok and repair_ok else [source])
    assert len(repairs) == (0 if original_ok else 1)
    with pytest.raises(ValueError):
        conversion.run(convert, repair, source, repaired)


@pytest.mark.asyncio
async def test_docling_concurrent_reuse_is_rejected_before_a_second_submission():
    entered = asyncio.Event()
    posts = []
    async def handler(request):
        posts.append(request.method)
        entered.set()
        await asyncio.Event().wait()
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(conversion.run(client, "https://docling.test", 5))
        await entered.wait()
        with pytest.raises(ValueError):
            await conversion.run(client, "https://docling.test", 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert posts == ["POST"]
    assert conversion.phase is DoclingPhase.CANCELLED


@pytest.mark.asyncio
async def test_docling_deadline_expires_during_poll_retry_backoff(monkeypatch):
    actual_sleep = asyncio.sleep
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) > 1:
            await actual_sleep(delay)
    monkeypatch.setattr(docling_io.asyncio, "sleep", sleep)
    calls = []
    async def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"task_id": "task"}) if request.method == "POST" else httpx.Response(503)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await conversion.run(client, "https://docling.test", 0.02)
    assert conversion.phase is DoclingPhase.TIMED_OUT
    assert calls == ["POST", "GET"]


@pytest.mark.parametrize("failure", [httpx.ConnectError("connect"), httpx.ConnectTimeout("connect"),
                                      httpx.PoolTimeout("pool"), httpx.ReadTimeout("lost"),
                                      httpx.RemoteProtocolError("lost"), httpx.WriteTimeout("upload"),
                                      502, 503, 504, {}])
@pytest.mark.asyncio
async def test_docling_resubmits_uncertain_outcomes_and_can_succeed_on_third_attempt(monkeypatch, failure):
    conversion = DoclingConversion()
    phases = []
    transitions = []
    transition = conversion.transition
    def observe(target):
        transition(target)
        transitions.append(target)
    monkeypatch.setattr(conversion, "transition", observe)
    async def handler(request):
        if request.method == "POST":
            phases.append(conversion.phase)
            if len(phases) < 3:
                if isinstance(failure, Exception):
                    raise failure
                return httpx.Response(failure if isinstance(failure, int) else 200, json={})
            return httpx.Response(200, json={"task_id": "third-task"})
        if "poll" in request.url.path:
            assert request.url.path.endswith("/third-task")
            return httpx.Response(200, json={"task_status": "success"})
        return httpx.Response(200, json={"document": {"json_content": {"texts": []}}})
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await conversion.run(client, "https://docling.test", 5)
    assert result["document"]["json_content"] == {"texts": []}
    assert conversion.phase is DoclingPhase.SUCCEEDED
    assert conversion.submission_attempts == 3
    assert phases == [DoclingPhase.SUBMITTING, DoclingPhase.RESUBMITTING, DoclingPhase.RESUBMITTING]
    assert transitions == [DoclingPhase.SUBMITTING, DoclingPhase.UNKNOWN_SUBMISSION,
        DoclingPhase.RESUBMITTING, DoclingPhase.UNKNOWN_SUBMISSION, DoclingPhase.RESUBMITTING,
        DoclingPhase.POLLING, DoclingPhase.FETCHING, DoclingPhase.SUCCEEDED]


def test_docling_submission_transition_guard_rejects_a_fourth_attempt():
    conversion = DoclingConversion()
    conversion.transition(DoclingPhase.SUBMITTING)
    for _ in range(2):
        conversion.transition(DoclingPhase.UNKNOWN_SUBMISSION)
        conversion.transition(DoclingPhase.RESUBMITTING)
    conversion.transition(DoclingPhase.UNKNOWN_SUBMISSION)
    with pytest.raises(ValueError):
        conversion.transition(DoclingPhase.RESUBMITTING)
    assert conversion.phase is DoclingPhase.UNKNOWN_SUBMISSION
    assert conversion.submission_attempts == 3


@pytest.mark.asyncio
async def test_docling_post_helper_preserves_its_configured_attempt_bound(monkeypatch):
    calls = []
    async def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("lost response")
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DoclingSubmissionUnknown):
            await docling_io._docling_serve_request(client, "POST", "https://docling.test/convert", attempts=2)
    assert calls == ["POST", "POST"]


@pytest.mark.asyncio
async def test_docling_submission_retry_budget_is_shared_across_failure_types(monkeypatch):
    calls = []
    async def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost")
        if len(calls) == 2:
            return httpx.Response(503)
        raise httpx.RemoteProtocolError("lost")
    monkeypatch.setattr(docling_io.asyncio, "sleep", _no_delay)
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DoclingSubmissionUnknown):
            await conversion.run(client, "https://docling.test", 5)
    assert conversion.phase is DoclingPhase.UNKNOWN_SUBMISSION
    assert conversion.submission_attempts == 3
    assert calls == ["POST"] * 3


@pytest.mark.asyncio
async def test_docling_deadline_stops_resubmission_backoff(monkeypatch):
    calls = []
    async def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("lost")
    conversion = DoclingConversion()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await conversion.run(client, "https://docling.test", 0.02)
    assert conversion.phase is DoclingPhase.TIMED_OUT
    assert conversion.submission_attempts == 1
    assert calls == ["POST"]
