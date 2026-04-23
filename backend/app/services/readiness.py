from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from app.config import LOCAL_LLM_HOSTS, PLACEHOLDER_LLM_KEYS, get_settings
from app.database import get_session_maker
from app.services.runtime_diagnostics import collect_runtime_diagnostics
from app.services.runtime_paths import resolve_binary


def _check_payload(
    *,
    ok: bool,
    detail: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "status": "ok" if ok else "error",
        "detail": detail,
        "metadata": metadata or {},
    }


async def _database_check(session_maker_factory: Callable | None = None) -> dict[str, Any]:
    try:
        session_maker = session_maker_factory() if session_maker_factory else get_session_maker()
        async with session_maker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        return _check_payload(ok=False, detail=str(exc))
    return _check_payload(ok=True, detail="Database connection is usable")


def _directory_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".readiness-{uuid.uuid4().hex}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        return False, str(exc)
    return True, "Writable"


def _storage_check(settings: Any) -> dict[str, Any]:
    roots = {
        "upload_dir": Path(settings.upload_dir),
        "processing_dir": Path(settings.processing_dir),
        "output_dir": Path(settings.output_dir),
    }
    results: dict[str, Any] = {}
    ok = True
    for name, path in roots.items():
        writable, detail = _directory_writable(path)
        ok = ok and writable
        results[name] = {
            "path": str(path),
            "writable": writable,
            "detail": detail,
        }

    return _check_payload(
        ok=ok,
        detail="Runtime storage is writable" if ok else "One or more storage directories is not writable",
        metadata=results,
    )


def _binary_check(settings: Any) -> dict[str, Any]:
    binaries = {
        "ghostscript": ("gs", settings.ghostscript_path),
        "tesseract": ("tesseract", settings.tesseract_path),
        "pdftoppm": ("pdftoppm", settings.pdftoppm_path),
        "verapdf": ("verapdf", settings.verapdf_path),
    }
    resolved: dict[str, Any] = {}
    ok = True
    for label, (binary, explicit) in binaries.items():
        path = resolve_binary(binary, explicit=explicit)
        resolved[label] = {
            "configured": explicit,
            "resolved": path,
            "available": bool(path),
        }
        ok = ok and bool(path)

    return _check_payload(
        ok=ok,
        detail="Required PDF command-line tools are available"
        if ok
        else "One or more required PDF command-line tools is missing",
        metadata=resolved,
    )


def _llm_check(settings: Any) -> dict[str, Any]:
    base_url = str(settings.llm_base_url or "").strip()
    model = str(settings.llm_model or "").strip()
    api_key = str(settings.llm_api_key or "").strip() or str(settings.gemini_api_key or "").strip()
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    is_local = host in LOCAL_LLM_HOSTS

    ok = bool(base_url and model and parsed.scheme in {"http", "https"})
    detail = "LLM configuration is usable"
    if not ok:
        detail = "LLM_BASE_URL and LLM_MODEL must be valid"
    elif not is_local and api_key.lower() in PLACEHOLDER_LLM_KEYS:
        ok = False
        detail = "Remote LLM endpoint requires a real API key"

    return _check_payload(
        ok=ok,
        detail=detail,
        metadata={
            "base_url": base_url,
            "model": model,
            "local_endpoint": is_local,
            "api_key_configured": bool(api_key and api_key.lower() not in PLACEHOLDER_LLM_KEYS),
        },
    )


def _docling_check(settings: Any, diagnostics: dict[str, Any]) -> dict[str, Any]:
    docling = diagnostics.get("docling", {}) if isinstance(diagnostics, dict) else {}
    if getattr(settings, "docling_serve_url", ""):
        local = bool(docling.get("local"))
        listener_found = bool(docling.get("listener_found"))
        if local and not listener_found:
            return _check_payload(
                ok=False,
                detail="Local docling-serve is configured but no listener was found",
                metadata=docling,
            )
        return _check_payload(
            ok=True,
            detail="docling-serve is configured",
            metadata=docling,
        )

    has_local_docling = importlib.util.find_spec("docling") is not None
    return _check_payload(
        ok=has_local_docling,
        detail="Local Docling is importable"
        if has_local_docling
        else "Local Docling is not installed and DOCLING_SERVE_URL is not configured",
        metadata={
            "configured": False,
            "local_docling_importable": has_local_docling,
        },
    )


async def collect_readiness(
    settings: Any | None = None,
    *,
    session_maker_factory: Callable | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    diagnostics = collect_runtime_diagnostics(settings)
    checks = {
        "database": await _database_check(session_maker_factory),
        "storage": _storage_check(settings),
        "binaries": _binary_check(settings),
        "llm": _llm_check(settings),
        "docling": _docling_check(settings, diagnostics),
    }
    ready = all(bool(check.get("ok")) for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "version": "0.1.0",
        "checks": checks,
        "diagnostics": diagnostics,
    }
