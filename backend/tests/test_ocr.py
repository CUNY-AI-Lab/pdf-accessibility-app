from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.pipeline import ocr
from app.pipeline.ocr import _build_ocrmypdf_args
from app.services import runtime_paths


def test_build_ocrmypdf_args_enables_rotate_and_deskew():
    args = _build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="skip",
        rotate_pages=True,
        deskew=True,
    )

    assert "--rotate-pages" in args
    assert "--deskew" in args
    assert "--skip-text" in args
    assert args[args.index("--jobs") + 1] == "1"
    assert args[args.index("--max-image-mpixels") + 1] == "75"
    assert args[-2:] == ["input.pdf", "output.pdf"]


def test_build_ocrmypdf_args_respects_mode_and_optional_flags():
    args = _build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="redo",
        rotate_pages=False,
        deskew=False,
    )

    assert "--rotate-pages" not in args
    assert "--deskew" not in args
    assert "--redo-ocr" in args
    assert "--skip-text" not in args


def test_build_ocrmypdf_args_omits_deskew_in_redo_mode():
    args = _build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="redo",
        rotate_pages=True,
        deskew=True,
    )

    assert "--rotate-pages" in args
    assert "--redo-ocr" in args
    assert "--deskew" not in args


def test_build_ocrmypdf_args_accepts_resource_limits():
    args = _build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="skip",
        rotate_pages=True,
        deskew=True,
        jobs=3,
        max_image_mpixels=42,
    )

    assert args[args.index("--jobs") + 1] == "3"
    assert args[args.index("--max-image-mpixels") + 1] == "42"


def test_enriched_subprocess_env_adds_common_binary_dirs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(runtime_paths.Path, "exists", lambda self: str(self) == "/opt/homebrew/bin")
    env = runtime_paths.enriched_subprocess_env()

    assert "/opt/homebrew/bin" in env["PATH"]


def test_resolve_binary_prefers_explicit_path(tmp_path):
    binary = tmp_path / "tesseract"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    resolved = runtime_paths.resolve_binary("tesseract", explicit=str(binary))

    assert resolved == str(binary.resolve())


def test_enriched_subprocess_env_adds_configured_binary_dirs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(binary_search_dirs="/srv/bin,/opt/tools/bin"),
    )

    env = runtime_paths.enriched_subprocess_env()

    assert "/srv/bin" in env["PATH"]
    assert "/opt/tools/bin" in env["PATH"]


def test_gateway_engine_loads_the_plugin_and_runs_pages_in_parallel():
    args = _build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="skip",
        rotate_pages=True,
        deskew=True,
        engine="gateway",
    )

    assert args[args.index("--plugin") + 1] == "app.pipeline.ocr_vlm_plugin"
    assert args[args.index("--jobs") + 1] == str(Settings().ocr_gateway_jobs)
    assert args[-2:] == ["input.pdf", "output.pdf"]


@pytest.mark.asyncio
async def test_gateway_engine_gives_ocrmypdf_the_llm_connection(monkeypatch, tmp_path):
    settings = Settings(
        llm_base_url="https://gateway.test/v1",
        llm_api_key="sk-app",
        llm_model="gemini-check",
        ocr_model="vision-model",
    )
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    seen = {}

    async def create_subprocess_exec(*args, env, **kwargs):
        seen.update(args=args, env=env)
        return SimpleNamespace(returncode=0)

    async def communicate(proc, timeout):
        return b"", b""

    monkeypatch.setattr(ocr.asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(ocr, "communicate_with_timeout", communicate)

    result = await ocr.run_ocr(tmp_path / "in.pdf", tmp_path / "out.pdf", engine="gateway")

    assert result.success
    assert "--plugin" in seen["args"]
    assert seen["env"]["CAIL_OCR_BASE_URL"] == "https://gateway.test/v1"
    assert seen["env"]["CAIL_OCR_API_KEY"] == "sk-app"
    assert seen["env"]["CAIL_OCR_MODEL"] == "vision-model"
