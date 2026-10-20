import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base
from app.services import readiness


@pytest.fixture(autouse=True)
def _clear_docling_probe_cache():
    readiness._docling_probe_cache.clear()
    yield
    readiness._docling_probe_cache.clear()


def _settings(tmp_path):
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        upload_dir=tmp_path / "uploads",
        processing_dir=tmp_path / "processing",
        output_dir=tmp_path / "output",
        ghostscript_path="gs",
        tesseract_path="tesseract",
        pdftoppm_path="pdftoppm",
        verapdf_path="verapdf",
        llm_base_url="https://tools.ailab.gc.cuny.edu/v1",
        llm_api_key="real-key",
        llm_model="qwen3-vl-235b-a22b-instruct",
        docling_serve_url="",
        docling_serve_token="",
        docling_serve_timeout=300,
        docling_serve_ocr_engine="rapidocr",
    )


def _remote_docling_settings(tmp_path, *, token=""):
    settings = _settings(tmp_path)
    settings.docling_serve_url = "https://docling.example.test/service/"
    settings.docling_serve_token = token
    settings.docling_serve_timeout = 7
    return settings


def _patch_async_client(monkeypatch, *, response=None, error=None):
    calls = []

    class FakeTransport:
        def __init__(self, *, retries):
            self.retries = retries

    class FakeAsyncClient:
        def __init__(self, *, timeout, transport):
            self.timeout = timeout
            self.transport = transport

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc_value, _traceback):
            return None

        async def get(self, url, *, headers):
            calls.append(
                {
                    "timeout": self.timeout,
                    "transport": self.transport,
                    "url": url,
                    "headers": headers,
                }
            )
            if error is not None:
                raise error
            return response

    monkeypatch.setattr(readiness.httpx, "AsyncHTTPTransport", FakeTransport)
    monkeypatch.setattr(readiness.httpx, "AsyncClient", FakeAsyncClient)
    return calls


@pytest.mark.asyncio
async def test_collect_readiness_reports_ready_runtime(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        readiness,
        "resolve_binary",
        lambda binary, *, explicit=None: f"/usr/bin/{binary}",
    )
    monkeypatch.setattr(
        readiness.importlib.util,
        "find_spec",
        lambda name: object() if name == "docling" else None,
    )

    report = await readiness.collect_readiness(
        _settings(tmp_path),
        session_maker_factory=lambda: session_maker,
    )

    assert report["status"] == "ready"
    assert report["checks"]["database"]["ok"] is True
    assert report["checks"]["storage"]["ok"] is True
    assert report["checks"]["binaries"]["ok"] is True
    assert report["checks"]["llm"]["ok"] is True
    assert report["checks"]["docling"]["ok"] is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_collect_readiness_reports_missing_dependencies(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = _settings(tmp_path)
    settings.llm_api_key = "your-api-key"
    monkeypatch.setattr(readiness, "resolve_binary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(readiness.importlib.util, "find_spec", lambda _name: None)

    report = await readiness.collect_readiness(
        settings,
        session_maker_factory=lambda: session_maker,
    )

    assert report["status"] == "not_ready"
    assert report["checks"]["binaries"]["ok"] is False
    assert report["checks"]["llm"]["ok"] is False
    assert report["checks"]["docling"]["ok"] is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_remote_docling_health_check_uses_canonical_endpoint_and_timeout(
    tmp_path, monkeypatch
):
    response = httpx.Response(
        204,
        request=httpx.Request("GET", "https://docling.example.test/service/health"),
    )
    calls = _patch_async_client(monkeypatch, response=response)

    result = await readiness._docling_check(
        _remote_docling_settings(tmp_path),
        {"docling": {"configured": True, "local": False}},
    )

    assert result["ok"] is True
    assert calls[0]["url"] == "https://docling.example.test/service/health"
    assert calls[0]["headers"] == {}
    assert calls[0]["timeout"].connect == 5
    assert calls[0]["timeout"].read == 5
    assert calls[0]["transport"].retries == 2


@pytest.mark.asyncio
async def test_remote_docling_health_check_reports_connection_failure_without_details(
    tmp_path, monkeypatch
):
    calls = _patch_async_client(
        monkeypatch,
        error=httpx.ConnectError("provider-secret-connection-detail"),
    )

    result = await readiness._docling_check(
        _remote_docling_settings(tmp_path),
        {"docling": {"configured": True, "local": False}},
    )

    assert result["ok"] is False
    assert result["metadata"]["health_check"] == "connection_failed"
    assert "provider-secret-connection-detail" not in repr(result)
    assert calls[0]["headers"] == {}


@pytest.mark.asyncio
async def test_remote_docling_health_check_reports_timeout_without_details(
    tmp_path, monkeypatch
):
    _patch_async_client(
        monkeypatch,
        error=httpx.ReadTimeout("provider-secret-timeout-detail"),
    )

    result = await readiness._docling_check(
        _remote_docling_settings(tmp_path),
        {"docling": {"configured": True, "local": False}},
    )

    assert result["ok"] is False
    assert result["metadata"]["health_check"] == "timed_out"
    assert "provider-secret-timeout-detail" not in repr(result)


@pytest.mark.asyncio
async def test_remote_docling_health_check_reports_invalid_url_without_details(
    tmp_path, monkeypatch
):
    _patch_async_client(
        monkeypatch,
        error=httpx.InvalidURL("provider-secret-invalid-url-detail"),
    )

    result = await readiness._docling_check(
        _remote_docling_settings(tmp_path),
        {"docling": {"configured": True, "local": False}},
    )

    assert result["ok"] is False
    assert result["metadata"]["health_check"] == "invalid_url"
    assert "provider-secret-invalid-url-detail" not in repr(result)


@pytest.mark.asyncio
async def test_remote_docling_health_check_reports_non_2xx_without_provider_body(
    tmp_path, monkeypatch
):
    response = httpx.Response(
        503,
        content=b"provider-secret-response-body",
        request=httpx.Request("GET", "https://docling.example.test/service/health"),
    )
    _patch_async_client(monkeypatch, response=response)

    result = await readiness._docling_check(
        _remote_docling_settings(tmp_path),
        {"docling": {"configured": True, "local": False}},
    )

    assert result["ok"] is False
    assert result["metadata"]["health_check"] == "non_2xx"
    assert result["metadata"]["status_code"] == 503
    assert "provider-secret-response-body" not in repr(result)


@pytest.mark.asyncio
async def test_remote_docling_health_check_sends_bearer_only_when_configured(
    tmp_path, monkeypatch
):
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://docling.example.test/service/health"),
    )
    calls = _patch_async_client(monkeypatch, response=response)
    settings = _remote_docling_settings(tmp_path, token="health-secret")

    result = await readiness._docling_check(
        settings,
        {"docling": {"configured": True, "local": False}},
    )

    assert result["ok"] is True
    assert calls[0]["headers"] == {"Authorization": "Bearer health-secret"}
    assert "health-secret" not in repr(result)


@pytest.mark.asyncio
async def test_concurrent_readiness_requests_share_one_remote_docling_probe(
    tmp_path, monkeypatch
):
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://docling.example.test/service/health"),
    )
    calls = _patch_async_client(monkeypatch, response=response)
    settings = _remote_docling_settings(tmp_path)
    diagnostics = {"docling": {"configured": True, "local": False}}

    results = await asyncio.gather(
        *(readiness._docling_check(settings, diagnostics) for _ in range(25))
    )
    results.append(await readiness._docling_check(settings, diagnostics))

    assert all(result["ok"] is True for result in results)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_remote_docling_probe_failure_is_cached_until_window_expires(
    tmp_path, monkeypatch
):
    calls = _patch_async_client(
        monkeypatch,
        error=httpx.ConnectError("unreachable"),
    )
    clock = [1000.0]
    monkeypatch.setattr(readiness.time, "monotonic", lambda: clock[0])
    settings = _remote_docling_settings(tmp_path)
    diagnostics = {"docling": {"configured": True, "local": False}}

    first = await readiness._docling_check(settings, diagnostics)
    clock[0] += readiness._DOCLING_HEALTH_CACHE_SECONDS - 1
    second = await readiness._docling_check(settings, diagnostics)

    assert first["metadata"]["health_check"] == "connection_failed"
    assert second == first
    assert len(calls) == 1

    clock[0] += 1
    await readiness._docling_check(settings, diagnostics)

    assert len(calls) == 2
