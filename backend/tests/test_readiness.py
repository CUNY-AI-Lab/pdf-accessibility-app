from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.services import readiness


def _settings(tmp_path):
    return SimpleNamespace(
        database_url="sqlite+aiosqlite:///:memory:",
        upload_dir=tmp_path / "uploads",
        processing_dir=tmp_path / "processing",
        output_dir=tmp_path / "output",
        ghostscript_path="gs",
        tesseract_path="tesseract",
        pdftoppm_path="pdftoppm",
        verapdf_path="verapdf",
        llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        llm_api_key="real-key",
        gemini_api_key="",
        llm_model="google/gemini-3-flash-preview",
        docling_serve_url="",
        docling_serve_ocr_engine="rapidocr",
    )


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
